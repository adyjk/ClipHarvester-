"""TapTap 论坛采集：借已登录的调试浏览器渲染推荐流，解析帖子卡片。

TapTap 论坛（https://www.taptap.cn/forum）首页是客户端无限滚动的推荐流：
  SSR 只有固定 9 条热门帖，?page= 参数无效，免登录拿不到完整列表；
  但登录后的调试浏览器能渲染出几十条卡片，且视频帖的 <video>.duration 可读。

做法：
  1. 复用 douyin 的调试浏览器（独立 Edge profile，登录态共享）
  2. 滚动论坛首页，把每张 [data-scroll-restore-anchor="moment:{id}"] 卡片的
     outerHTML 收回来
  3. 复用正则解析标题 / 正文 / 作者 / 时间 / 评论数
  4. 视频帖补 video.duration 进 duration_s，套视频平台时长评分规则；
     图文帖 duration_s=0，走 meta 摘要降级流程
"""

from __future__ import annotations

import html as _html
import re
import time
from datetime import datetime

from . import config, scoring, stopflag, store
from .zhihu import _summarize

FORUM = "https://www.taptap.cn/forum"
MAX_POSTS = 200

# 论坛卡片有两种形态：视频/动态帖用 moment-video__title-text，文章帖用 moment-article__summary--title
_TITLE_RE = re.compile(r'moment-video__title-text[^>]*>(.*?)</span>', re.S)
_TITLE2_RE = re.compile(r'moment-article__summary--title[^>]*>(.*?)</span>', re.S)
# 正文统一在 moment-video__summary-content（视频/图文/纯文字帖）或 moment-summary-text（文章帖）
_DESC_RE = re.compile(r'moment-video__summary-content[^>]*>(.*?)</div>', re.S)
_DESC2_RE = re.compile(r'moment-summary-text[^>]*>(.*?)</div>', re.S)
# 作者名在 user-name__text 里；itemprop="name" 指向的是标题，不能用
_AUTHOR_RE = re.compile(r'user-name__text[^>]*>([^<]+)<', re.S)
_COMMENT_RE = re.compile(r'itemprop="commentCount"[^>]*>\s*([\d.]+)\s*(万)?')
_TIME_RE = re.compile(r'tap-time[^>]*title="([^"]+)"')
_TAG_RE = re.compile(r'#([^\s#]{2,20})')
_TS_RE = re.compile(r'(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{1,2}):(\d{1,2})')


def _parse_num(s: str, unit: str | None) -> int:
    try:
        n = float(s)
    except (TypeError, ValueError):
        return 0
    return int(n * 10000) if unit == "万" else int(n)


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_ts(s: str) -> int:
    m = _TS_RE.search(s or "")
    if not m:
        return int(time.time())
    try:
        dt = datetime(*map(int, m.groups()))
        return int(dt.timestamp())
    except (ValueError, OverflowError):
        return int(time.time())


def _parse_block(mid: str, block: str) -> dict | None:
    """把一个 moment 帖子的 HTML 块解析成入库结构。"""
    title = ""
    for re_ in (_TITLE_RE, _TITLE2_RE):
        m = re_.search(block)
        if m:
            title = _clean(m.group(1))
            break
    desc = ""
    for re_ in (_DESC_RE, _DESC2_RE):
        m = re_.search(block)
        if m:
            desc = _clean(m.group(1))
            break
    am = _AUTHOR_RE.search(block)
    author = _html.unescape(am.group(1)).strip() if am else ""
    cm = _COMMENT_RE.search(block)
    reply = _parse_num(cm.group(1), cm.group(2)) if cm else 0
    tm = _TIME_RE.search(block)
    pub = _parse_ts(tm.group(1)) if tm else int(time.time())
    tags = " ".join(sorted(set(_TAG_RE.findall(title + " " + desc))))[:200]
    return {
        "id": f"tap_{mid}",
        "source": "taptap",
        "title": title or f"TapTap 帖子 {mid}",
        "desc": desc[:400],
        "tags": tags,
        "author": author,
        "author_id": "",
        "duration_s": 0,
        "pub_ts": pub,
        "play": reply,
        "like_count": 0,
        "fav": 0,
        "coin": 0,
        "danmaku": 0,
        "reply": reply,
        "cover_url": "",
        "url": f"https://www.taptap.cn/moment/{mid}",
        "transcript": "",
        "media_url": "",
    }


def _browse_cards(log=print, want: int = 40, max_scroll: int = 25) -> list[dict]:
    """借调试浏览器滚动论坛首页，返回渲染出的帖子卡片（含视频时长）。

    返回 [{"id": "tap_xxx", "html": outerHTML, "duration_s": int}, ...]，
    按出现顺序、去重，凑够 want 条或滚到上限为止。
    """
    from . import douyin

    try:
        douyin.ensure_browser(log)
    except Exception as e:
        log(f"TapTap 论坛：调试浏览器不可用（{e}）")
        return []

    def _target():
        return next(
            (t for t in douyin._http("/json/list")
             if t.get("type") == "page" and "taptap.cn" in t.get("url", "")),
            None,
        )

    target = _target()
    if not target:
        try:
            douyin._open_tab(FORUM)
            for _ in range(20):
                time.sleep(0.6)
                target = _target()
                if target:
                    break
        except Exception:
            pass
    if not target:
        log("TapTap 论坛：找不到 TapTap 标签页，跳过采集")
        return []

    cdp = douyin._CDP(target["webSocketDebuggerUrl"], hints=())
    cards: list[dict] = []
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Page.navigate", {"url": FORUM})
        time.sleep(4)
        seen: set[str] = set()
        js = (
            "(() => { const out = {};"
            "document.querySelectorAll('[data-scroll-restore-anchor^=\"moment:\"]')"
            ".forEach(c => {"
            "const m = c.getAttribute('data-scroll-restore-anchor').match(/moment:(\\d+)/);"
            "if (!m) return;"
            "const v = c.querySelector('video');"
            "out[m[1]] = {html: c.outerHTML,"
            " dur: (v && v.duration > 0) ? Math.round(v.duration) : 0};"
            "}); return out; })()"
        )
        for _ in range(max_scroll):
            if stopflag.stopped():
                break
            r = cdp.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
            val = (r.get("result") or {}).get("value") or {}
            if isinstance(val, dict):
                for mid, d in val.items():
                    if mid in seen:
                        continue
                    seen.add(mid)
                    cards.append({
                        "id": f"tap_{mid}",
                        "html": d.get("html", "") if isinstance(d, dict) else "",
                        "duration_s": int((d.get("dur") or 0) if isinstance(d, dict) else 0),
                    })
            if len(cards) >= want:
                break
            cdp.call("Runtime.evaluate", {
                "expression": "window.scrollBy(0, 1500)",
                "returnByValue": True,
            })
            time.sleep(1.2)
    except Exception as e:
        log(f"TapTap 论坛浏览器采集失败：{e}")
    finally:
        try:
            cdp.ws.close()
        except Exception:
            pass
    return cards


def fetch_forum(cfg: dict, log=print, limit: int = 40) -> dict:
    """借调试浏览器抓论坛推荐流，直到凑够 limit 条新帖。"""
    want = max(1, min(MAX_POSTS, int(limit or 40)))
    conn = store.connect(config.path(cfg, "db"))
    store.init_db(conn)
    try:
        existing = store.existing_ids(conn, "taptap")
        known = store.existing_titles(conn, "taptap")
        log(f"抓取 TapTap 论坛（目标新增 {want} 条，借调试浏览器渲染）…")

        items: list[dict] = []
        # 至少滚到 30 条卡片：视频帖在推荐流里稀疏分布，want 较小时可能碰不到
        for c in _browse_cards(log, want=max(want, 30)):
            if stopflag.stopped():
                break
            v = _parse_block(c["id"][4:], c["html"])
            if not v:
                continue
            nt = store.norm_title(v["title"])
            if v["id"] in existing or (nt and nt in known):
                continue
            if nt:
                known.add(nt)
            v["duration_s"] = c["duration_s"]
            if v["duration_s"] > 0:
                v["tags"] = (v["tags"] + " 视频").strip()
            items.append(v)
            if len(items) >= want:
                break

        log(f"库里已有 TapTap {len(existing)} 条，本次取到新帖 {len(items)} 条")
        if not items:
            log("TapTap 论坛没有新帖")
            return {"new": 0, "updated": 0}

        new_n, upd_n, skip_n = store.upsert_videos(conn, items)
        store.add_daily_count(conn, new_n)
        _summarize(conn, cfg, items, log, label="TapTap")
        total = scoring.apply_all(conn, cfg)
        log(f"TapTap 入库：新增 {new_n}，更新 {upd_n}，标题重复 {skip_n}，重算 {total} 条分数")
        return {"new": new_n, "updated": upd_n}
    finally:
        conn.close()
