"""贴吧采集：走桌面版首页「个性推荐」流 + 移动版接口取首楼正文。

首页未登录也能渲染个性推荐流；正文走 mo/q/m 移动版接口（免登录、服务端渲染）。
流程：开独立标签页 → 首页个性推荐滚动加载取标题 → fetch 移动版接口取首楼 → 入库打分。
"""

from __future__ import annotations

import json
import time

from . import config, douyin, ocr, scoring, store
from .zhihu import _summarize

MAX_POSTS = 200

LIST_JS = """(() => {
  const map = new Map();
  for (const a of document.querySelectorAll('a[href*="/p/"]')) {
    const m = (a.getAttribute('href') || '').match(/\\/p\\/(\\d+)/);
    if (!m) continue;
    const title = (a.innerText || '').trim();
    if (!title || title.length < 2) continue;
    if (/^(分享|评论|回复|赞|踩|举报|\\d+)$/.test(title)) continue;
    const prev = map.get(m[1]);
    if (!prev || title.length > prev.length) map.set(m[1], title);
  }
  const out = [];
  for (const [tid, title] of map) out.push({tid, title, reply: 0});
  return out;
})()"""

# 首楼正文走移动版接口 mo/q/m（服务端渲染、免登录），返回 JSON：{text, imgs}
FETCH_JS = """(async () => {
  try {
    const r = await fetch('https://tieba.baidu.com/mo/q/m?kz={tid}');
    const html = await r.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const e = doc.querySelector('.list_item_wrapper div.content[lz="1"]') || doc.querySelector('.list_item_wrapper .content');
    if (!e) return '';
    const t = (e.innerText || '').replace(/\\s+/g, ' ').trim();
    const imgs = [];
    e.querySelectorAll('img').forEach(function(img) {
      const src = img.getAttribute('data-original') || img.getAttribute('src') || img.getAttribute('data-src') || '';
      if (src && src.indexOf('baidu.com') !== -1 && (src.indexOf('tiebapic') !== -1 || src.indexOf('imgsa') !== -1 || src.indexOf('hiphotos') !== -1)) imgs.push(src);
    });
    return JSON.stringify({text: t.length > 400 ? t.slice(0, 400) : t, imgs: imgs.slice(0, 4)});
  } catch (err) { return ''; }
})()"""


def _tab() -> dict:
    """单独开一个标签页，别把用户正在看的贴吧页导航走。"""
    t = douyin._open_tab("https://tieba.baidu.com/")
    if not t:
        raise RuntimeError("打不开贴吧标签页")
    time.sleep(4)
    return t


def _close_tab(target_id: str) -> None:
    try:
        import urllib.request

        urllib.request.urlopen(f"http://127.0.0.1:{douyin.DEBUG_PORT}/json/close/{target_id}", timeout=5)
    except Exception:
        pass


def _tieba_target() -> dict | None:
    """复用调试浏览器里已经开着的贴吧标签页（用户已登录、已选好吧）。"""
    return next(
        (t for t in douyin._http("/json/list")
         if t.get("type") == "page" and "tieba.baidu.com" in t.get("url", "")),
        None,
    )


def _eval(cdp, expr: str, await_promise: bool = False):
    r = cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": await_promise})
    if r.get("exceptionDetails"):
        raise RuntimeError(str(r["exceptionDetails"])[:180])
    return r.get("result", {}).get("value")


def fetch_posts(cfg: dict, log=print, limit: int = 10) -> dict:
    limit = max(1, min(MAX_POSTS, int(limit or 10)))
    conn = store.connect(config.path(cfg, "db"))
    store.init_db(conn)
    try:
        existing = store.existing_ids(conn, "tieba")
        known = store.existing_titles(conn, "tieba")
        douyin.ensure_browser(log)   # 先确保调试浏览器在运行，否则开标签页会失败
        cdp_target = _tieba_target()
        opened = False
        if not cdp_target:
            log("调试浏览器里没有贴吧标签页，新开一个…")
            cdp_target = _tab()
            opened = True
        cdp = douyin._CDP(cdp_target["webSocketDebuggerUrl"])
        items: list[dict] = []
        now = int(time.time())
        try:
            cdp.call("Page.enable")
            cdp.call("Network.enable")
            # 抓首页「个性推荐」流：标题在帖子链接 a[href*="/p/"]，滚动加载更多。
            # 图文平台过滤规则：正文（含 OCR 补足后）不足 15 字的纯图/水帖不算数，
            # 继续滚屏直到凑够 limit 条有文字内容的帖子。
            log("贴吧首页「个性推荐」读列表…")
            stale = 0
            while len(items) < limit and stale < 6:
                threads = _eval(cdp, LIST_JS) or []
                added = 0
                for th in threads:
                    title = (th.get("title") or "").strip()
                    tid = th.get("tid") or ""
                    if not title or not tid or f"tb_{tid}" in existing:
                        continue
                    nt = store.norm_title(title)
                    if nt and nt in known:
                        continue
                    known.add(nt)
                    # 抓到候选立刻取正文并过滤，过滤掉的不算数
                    try:
                        raw = _eval(cdp, FETCH_JS.replace("{tid}", tid), await_promise=True) or ""
                        obj = json.loads(raw) if raw else {}
                        desc = (obj.get("text") or "").strip()[:400]
                        imgs = obj.get("imgs") or []
                    except Exception as e:
                        log(f"  帖子 {tid} 取正文失败：{e}")
                        continue
                    if len(desc) < 15 and imgs:
                        try:
                            ocr_text = ocr.extract_many(imgs)
                            if ocr_text:
                                log(f"  帖子 {tid} 图片 OCR 补足 {len(ocr_text)} 字")
                                desc = (desc + " " + ocr_text).strip()[:400]
                        except Exception as e:
                            log(f"  帖子 {tid} 图片 OCR 失败：{e}")
                    if len(desc) < 15:
                        log(f"  帖子 {tid} 无有效文字（纯图/水帖），跳过")
                        continue
                    items.append({
                        "id": f"tb_{tid}",
                        "source": "tieba",
                        "title": title,
                        "desc": desc,
                        "tags": "贴吧",
                        "author": "",
                        "author_id": "",
                        "duration_s": 0,
                        "pub_ts": now,
                        "play": 0,
                        "like_count": int(th.get("reply") or 0),
                        "fav": 0,
                        "coin": 0,
                        "danmaku": 0,
                        "reply": int(th.get("reply") or 0),
                        "cover_url": "",
                        "url": f"https://tieba.baidu.com/p/{tid}",
                        "transcript": "",
                        "media_url": "",
                    })
                    added += 1
                    if len(items) >= limit:
                        break
                log(f"首页已抓有效 {len(items)}/{limit} 帖，本屏新增 {added}")
                stale = 0 if added else stale + 1
                if len(items) >= limit:
                    break
                _eval(cdp, "window.scrollBy(0, 1600)")
                time.sleep(1.5)
            log(f"库里已有贴吧 {len(existing)} 条，本次取到有效正文 {len(items)} 帖")
        finally:
            try:
                cdp.ws.close()
            except Exception:
                pass
            if opened:
                _close_tab(cdp_target.get("id") or "")

        if not items:
            log("贴吧没有新帖")
            return {"new": 0, "updated": 0}
        new_n, upd_n, skip_n = store.upsert_videos(conn, items)
        store.add_daily_count(conn, new_n)
        _summarize(conn, cfg, items, log, label="贴吧")
        total = scoring.apply_all(conn, cfg)
        log(f"贴吧入库：新增 {new_n}，更新 {upd_n}，标题重复 {skip_n}，重算 {total} 条分数")
        return {"new": new_n, "updated": upd_n}
    finally:
        conn.close()
