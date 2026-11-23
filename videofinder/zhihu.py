"""知乎热榜采集：匿名接口，不需要登录。

热榜接口 /api/v3/feed/topstory/hot-list-web 免登录直接返回 50 条，
标题、摘要、热度、问题链接都在里面，入库后走现有的打分与摘要流程。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request

from . import config, douyin, scoring, stopflag, store
from .summarize import LocalAI, fallback

API = "https://www.zhihu.com/api/v3/feed/topstory/hot-list-web?limit={limit}&desktop=true"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_HOT_RE = re.compile(r"([\d.]+)\s*(亿|万)?")
_QID_RE = re.compile(r"/question/(\d+)")


def _get(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def hot_value(text: str) -> int:
    """「1189 万热度」→ 11890000，用于互动排序。"""
    m = _HOT_RE.search(text or "")
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2)
    if unit == "亿":
        num *= 100_000_000
    elif unit == "万":
        num *= 10_000
    return int(num)


def normalize(item: dict) -> dict | None:
    """热榜条目 → 入库结构；拿问题 id 当主键。"""
    target = item.get("target") or {}
    title = ((target.get("title_area") or {}).get("text") or "").strip()
    url = ((target.get("link") or {}).get("url") or "").strip()
    if not title:
        return None
    m = _QID_RE.search(url)
    rid = m.group(1) if m else ""
    if not rid:
        return None
    answers = int((item.get("feed_specific") or {}).get("answer_count") or 0)
    return {
        "id": f"zh_{rid}",
        "source": "zhihu",
        "title": title,
        "desc": ((target.get("excerpt_area") or {}).get("text") or "").strip(),
        "tags": "",
        "author": "",
        "author_id": "",
        "duration_s": 0,
        # 热榜本身就是「此刻在热」，用当前时间参与时效打分
        "pub_ts": int(time.time()),
        "play": hot_value((target.get("metrics_area") or {}).get("text") or ""),
        "like_count": answers,
        "fav": 0,
        "coin": 0,
        "danmaku": 0,
        "reply": answers,
        "cover_url": ((target.get("image_area") or {}).get("url") or ""),
        "url": url or f"https://www.zhihu.com/question/{rid}",
        "transcript": "",
        "media_url": "",
    }


def _summarize(conn, cfg: dict, rows: list[dict], log, label: str = "知乎") -> None:
    """图文帖没有语音可转写，直接压一段简介。"""
    if not rows:
        return
    ai = LocalAI(cfg)
    usable = ai.available()
    for r in rows:
        payload = None
        model = "fallback"
        if usable:
            try:
                payload = ai.summarize(r)
                model = payload.get("engine") or "ollama"
            except Exception:
                payload = None
        if payload is None:
            payload = fallback(r)
        conn.execute(
            "UPDATE videos SET summary=?, summary_model=?, summary_src='meta', summarized_at=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), model, int(time.time()), r["id"]),
        )
    conn.commit()
    log(f"{label}简介：{len(rows)} 条（{'本地模型' if usable else '降级抽取'}）")


def fetch_hot(cfg: dict, log=print, limit: int = 50) -> dict:
    # want 是用户想新增的条数；接口固定取热榜前 50 条，过滤掉库里已有的后再截取 want 条，
    # 这样不会因为热榜头部不变而每次只拉到 1 条新内容。
    want = max(1, min(50, int(limit or 50)))
    log(f"抓取知乎热榜（前 50 条，目标新增 {want} 条）…")
    try:
        data = _get(API.format(limit=50))
    except Exception as e:
        log(f"知乎热榜请求失败：{e}")
        return {"new": 0, "updated": 0}

    items = [v for v in (normalize(x) for x in (data.get("data") or [])) if v]
    if not items:
        log("知乎热榜返回空，可能被风控")
        return {"new": 0, "updated": 0}

    conn = store.connect(config.path(cfg, "db"))
    store.init_db(conn)
    try:
        existing = store.existing_ids(conn, "zhihu")
        known = store.existing_titles(conn, "zhihu")
        fresh, dup = [], 0
        for v in items:
            nt = store.norm_title(v["title"])
            if v["id"] in existing or (nt and nt in known):
                dup += 1
                continue
            if nt:
                known.add(nt)
            fresh.append(v)
        log(f"库里已有知乎 {len(existing)} 条，跳过重复 {dup} 条，新候选 {len(fresh)} 条")
        if not fresh:
            log("知乎热榜没有新内容")
            return {"new": 0, "updated": 0}

        fresh = fresh[:want]
        new_n, upd_n, skip_n = store.upsert_videos(conn, fresh)
        store.add_daily_count(conn, new_n)
        _summarize(conn, cfg, fresh, log)
        total = scoring.apply_all(conn, cfg)
        log(f"知乎入库：新增 {new_n}，更新 {upd_n}，标题重复 {skip_n}，重算 {total} 条分数")
        return {"new": new_n, "updated": upd_n}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 推荐流采集：登录后首页的个性化推荐（复用抖音的 CDP 基础设施，不逆签名）
# ---------------------------------------------------------------------------
ZH_HOME = "https://www.zhihu.com/"

_ARTICLE_RE = re.compile(r"/p/(\d+)")


def _zhihu_target() -> dict | None:
    return next(
        (t for t in douyin._http("/json/list")
         if t.get("type") == "page" and "zhihu.com" in t.get("url", "")),
        None,
    )


def _eval(cdp, expr: str, await_promise: bool = False):
    r = cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": await_promise})
    if r.get("exceptionDetails"):
        raise RuntimeError(str(r["exceptionDetails"])[:180])
    return r.get("result", {}).get("value")


# 首页推荐流改版后数据 SSR 内嵌进 DOM（不再走可监听的 XHR 接口），
# 直接扒 .TopstoryItem 卡片：标题 / 链接 / 摘要 / 赞同·回复数。
ZH_LIST_JS = """(() => {
  const cards = document.querySelectorAll('[class*="TopstoryItem"]');
  const out = [];
  for (const c of cards) {
    const titleEl = c.querySelector('.ContentItem-title');
    const title = (titleEl ? titleEl.innerText : '').trim();
    if (!title) continue;
    const a = c.querySelector('h2 a[href]') || c.querySelector('a[href]');
    const link = a ? (a.getAttribute('href') || '') : '';
    const rich = c.querySelector('.RichText');
    const desc = (rich ? rich.innerText : '').trim();
    const actions = c.querySelector('[class*="ContentItem-actions"]');
    const actText = (actions ? actions.innerText : '').replace(/\\s+/g, ' ');
    out.push({title, link, desc, actText});
  }
  return out;
})()"""

def _count(act_text: str, label: str) -> int:
    """从「赞同 7794 · 回复 34」里抠出数字，万/亿换算。"""
    m = re.search(label + r"\s*([\d.]+)\s*(亿|万)?", act_text or "")
    if not m:
        return 0
    n = float(m.group(1))
    unit = m.group(2)
    if unit == "亿":
        n *= 100_000_000
    elif unit == "万":
        n *= 10_000
    return int(n)


def normalize_dom_card(c: dict) -> dict | None:
    title = (c.get("title") or "").strip()
    link = (c.get("link") or "").strip()
    if not title or not link:
        return None
    url = link
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://www.zhihu.com" + url
    m = _QID_RE.search(url)
    if m:
        rid = f"zh_{m.group(1)}"
    else:
        m = _ARTICLE_RE.search(url)
        if not m:
            return None
        rid = f"zh_a_{m.group(1)}"
    act = (c.get("actText") or "").strip()
    like = _count(act, "赞同")
    reply = _count(act, "回复")
    return {
        "id": rid,
        "source": "zhihu",
        "title": title,
        "desc": (c.get("desc") or "").strip(),
        "tags": "",
        "author": "",
        "author_id": "",
        "duration_s": 0,
        "pub_ts": int(time.time()),
        "play": like,
        "like_count": like,
        "fav": 0,
        "coin": 0,
        "danmaku": reply,
        "reply": reply,
        "cover_url": "",
        "url": url,
        "transcript": "",
        "media_url": "",
    }


def fetch_recommend(cfg: dict, log=print, limit: int = 50, max_seconds: int = 180) -> dict:
    douyin.ensure_browser(log)
    target = _zhihu_target()
    if not target:
        # 浏览器可能刚启动还没登记标签页，先等 2 秒再查，避免误判「没有标签页」去开新的
        for _ in range(6):
            time.sleep(0.5)
            target = _zhihu_target()
            if target:
                break
    if not target:
        log("调试浏览器里没有知乎标签页，正在新开一个…")
        douyin._open_tab(ZH_HOME)
        for _ in range(20):
            time.sleep(0.6)
            target = _zhihu_target()
            if target:
                break
    if not target:
        log("打不开知乎标签页（可能需要登录）")
        return {"new": 0, "updated": 0}

    cdp = douyin._CDP(target["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Network.enable")
    cdp.call("Page.navigate", {"url": ZH_HOME})
    # 知乎标签页往往在后台（多次采集后尤其如此），后台的无限滚动懒加载会被 Chromium
    # 节流而不渲染，导致一直读不到新卡片。主动把它带到前台，免去手动点标签页。
    try:
        cdp.call("Page.bringToFront")
    except Exception:
        pass
    # 知乎首页是客户端渲染，等推荐卡片真正出现：隔 6 秒查一次，共 2 次（给足加载时间）
    for _ in range(2):
        time.sleep(6)
        try:
            if _eval(cdp, ZH_LIST_JS):
                break
        except Exception:
            pass

    conn = store.connect(config.path(cfg, "db"))
    store.init_db(conn)
    try:
        existing = store.existing_ids(conn, "zhihu")
        known = store.existing_titles(conn, "zhihu")
        log(f"库里已有知乎 {len(existing)} 条，本次目标 {limit} 条新的")
        seen: dict[str, dict] = {}
        dup = 0
        t0 = time.time()
        stale = 0   # 连续无新增的滚动屏数，攒够了就刷新首页重拉
        while len(seen) < limit and time.time() - t0 < max_seconds:
            if stopflag.stopped():
                log("已结束知乎采集")
                break
            try:
                cards = _eval(cdp, ZH_LIST_JS) or []
            except Exception as e:
                log(f"读知乎首页卡片失败：{e}")
                break
            added = 0
            for c in cards:
                v = normalize_dom_card(c)
                if not v:
                    continue
                nt = store.norm_title(v["title"])
                if v["id"] in existing or (nt and nt in known):
                    dup += 1
                    continue
                if v["id"] in seen:
                    continue
                seen[v["id"]] = v
                if nt:
                    known.add(nt)
                added += 1
            log(f"知乎首页已抓 {len(seen)}/{limit}，本屏新增 {added}（跳过重复 {dup}）")
            if len(seen) >= limit:
                break
            stale = 0 if added else stale + 1
            if stale >= 3:
                stale = 0
                log("连续 3 屏无新内容，刷新知乎首页重拉")
                try:
                    cdp.call("Page.navigate", {"url": ZH_HOME})
                    # 刷新后同样等首页渲染完再继续，避免又抓到空
                    for _ in range(2):
                        time.sleep(6)
                        try:
                            if _eval(cdp, ZH_LIST_JS):
                                break
                        except Exception:
                            pass
                except Exception:
                    break
                continue
            try:
                _eval(cdp, "window.scrollBy(0, 1600)")
            except Exception:
                break
            time.sleep(1.5)

        if not seen:
            log("知乎推荐流没有抓到内容")
            return {"new": 0, "updated": 0}

        videos = list(seen.values())[:limit]
        new_n, upd_n, skip_n = store.upsert_videos(conn, videos)
        _summarize(conn, cfg, videos, log)
        total = scoring.apply_all(conn, cfg)
        log(f"知乎入库：新增 {new_n}，更新 {upd_n}，标题重复 {skip_n}，重算 {total} 条分数")
        return {"new": new_n, "updated": upd_n}
    finally:
        conn.close()
