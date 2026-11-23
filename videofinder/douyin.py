"""抖音推荐 feed 采集（不逆签名）。

思路：启动一个带远程调试端口的 Edge，让抖音页面自己去请求推荐接口、自己算
a_bogus 签名，程序只通过 CDP 监听 Network 响应，把 aweme JSON 收下来。
只收元数据，不下载视频画面；滚动节奏慢，避免触发风控。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request

from . import config, stopflag, store

DEBUG_PORT = 9222
HOME = "https://www.douyin.com/jingxuan"   # 精选页：内容直接铺开（接口 /aweme/v2/web/module/feed/）
# 只关心这几类接口，其它静态资源一律不碰
API_HINTS = (
    "/aweme/v1/web/tab/feed/",
    "/aweme/v2/web/module/feed/",   # 精选页实际走的接口
    "/aweme/v1/web/feed/",
    "/aweme/v1/web/recommend/",
)


def _http(path: str):
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}{path}", timeout=5))


def ensure_browser(log=print) -> None:
    """确保有一个开了调试端口的 Edge（独立 profile，不影响日常浏览器）。"""
    try:
        _http("/json/version")
        return
    except Exception:
        pass
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    edge = next((p for p in candidates if os.path.exists(p)), None)
    if not edge:
        raise RuntimeError("没找到 Edge，无法采集抖音")
    profile = os.path.join(config.ROOT, "data", "edge-profile")
    os.makedirs(profile, exist_ok=True)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [edge, f"--remote-debugging-port={DEBUG_PORT}", f"--user-data-dir={profile}", HOME,
         # 禁用后台标签页节流/挂起：知乎、TapTap 等的无限滚动懒加载
         # 只有在标签页处于前台时才渲染，后台会被 Chromium 节流导致抓不到新卡片。
         "--disable-background-timer-throttling",
         "--disable-backgrounding-occluded-windows",
         "--disable-renderer-backgrounding",
         "--disable-features=CalculateNativeWinOcclusion"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, close_fds=True,
    )
    log("启动带调试端口的浏览器，等待抖音加载…")
    for _ in range(60):
        time.sleep(0.5)
        try:
            _http("/json/version")
            return
        except Exception:
            continue
    raise RuntimeError("浏览器调试端口一直未就绪")


def _open_tab(url: str) -> dict | None:
    """调试浏览器里没有抖音页时，新开一个标签页。"""
    import urllib.parse

    target = f"http://127.0.0.1:{DEBUG_PORT}/json/new?{urllib.parse.quote(url, safe='')}"
    try:
        req = urllib.request.Request(target, method="PUT")
        return json.load(urllib.request.urlopen(req, timeout=10))
    except Exception:
        pass
    try:
        return json.load(urllib.request.urlopen(target, timeout=10))
    except Exception:
        return None


def _douyin_target() -> dict | None:
    return next(
        (t for t in _http("/json/list")
         if t.get("type") == "page" and "douyin.com" in t.get("url", "")),
        None,
    )


class _CDP:
    def __init__(self, ws_url: str, hints=API_HINTS):
        import websocket

        self.ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
        self.ws.settimeout(2)
        self.mid = 0
        self.hints = hints
        self.pending: dict[str, str] = {}   # requestId -> api url
        self.finished: list[str] = []       # 已加载完、可以取 body 的 requestId

    def call(self, method: str, params: dict | None = None) -> dict:
        self.mid += 1
        mid = self.mid
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result", {})
            self._event(msg)

    def _event(self, msg: dict) -> None:
        m = msg.get("method")
        p = msg.get("params", {})
        if m == "Network.responseReceived":
            url = p.get("response", {}).get("url", "")
            if any(h in url for h in self.hints):
                self.pending[p["requestId"]] = url
        elif m == "Network.loadingFinished" and p.get("requestId") in self.pending:
            self.finished.append(p["requestId"])

    def poll(self) -> None:
        """非阻塞读事件；超时说明这一拍没有新数据。"""
        import websocket

        try:
            self._event(json.loads(self.ws.recv()))
        except websocket.WebSocketTimeoutException:
            pass

    def take_bodies(self) -> list[dict]:
        out = []
        while self.finished:
            rid = self.finished.pop(0)
            self.pending.pop(rid, None)
            try:
                body = self.call("Network.getResponseBody", {"requestId": rid}).get("body", "")
                out.append(json.loads(body))
            except Exception:
                continue
        return out


def _find_awemes(obj, out: list) -> None:
    if isinstance(obj, dict):
        if obj.get("aweme_id") and ("desc" in obj or "video" in obj):
            out.append(obj)
            return
        for v in obj.values():
            _find_awemes(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_awemes(v, out)


def _first_url(node) -> str:
    urls = (node or {}).get("url_list") or []
    return urls[0] if urls else ""


def _audio_url(v: dict) -> str:
    """抖音长视频是 DASH，音视频分开传，视频流经常没有音轨。

    优先返回独立音频流的直链：体积只有视频的几十分之一，且必然有声音。
    """
    for a in v.get("bit_rate_audio") or []:
        urls = ((a.get("audio_meta") or {}).get("url_list") or {})
        main = urls.get("main_url") or ""
        if main:
            return main
        backup = urls.get("backup_url")
        if isinstance(backup, list) and backup:
            return backup[0]
        if isinstance(backup, str) and backup:
            return backup
    return ""


def _media_url(v: dict) -> str:
    """转写只要声音：优先独立音轨，其次最低码率的视频流。"""
    audio = _audio_url(v)
    if audio:
        return audio
    cands = [b for b in (v.get("bit_rate") or []) if (b.get("play_addr") or {}).get("url_list")]
    if cands:
        cands.sort(key=lambda b: b.get("bit_rate") or 1 << 30)
        return _first_url(cands[0]["play_addr"])
    return _first_url(v.get("play_addr"))


def normalize(a: dict) -> dict | None:
    vid = str(a.get("aweme_id") or "")
    if not vid:
        return None
    v = a.get("video") or {}
    st = a.get("statistics") or {}
    text_extra = a.get("text_extra") or []
    tags = " ".join(t.get("hashtag_name", "") for t in text_extra if t.get("hashtag_name"))
    desc = a.get("desc") or ""
    au = a.get("author") or {}
    return {
        "id": f"dy_{vid}",
        "source": "douyin",
        "title": desc[:80] or "（无标题）",
        "desc": desc,
        "tags": tags,
        "author": au.get("nickname", ""),
        "author_id": str(au.get("sec_uid") or au.get("uid") or ""),
        "duration_s": int((v.get("duration") or 0) / 1000),
        "pub_ts": int(a.get("create_time") or 0),
        "play": int(st.get("play_count") or 0),
        "like_count": int(st.get("digg_count") or 0),
        "fav": int(st.get("collect_count") or 0),
        "coin": 0,
        "danmaku": int(st.get("comment_count") or 0),
        "cover_url": _first_url(v.get("cover") or v.get("origin_cover")),
        "url": f"https://www.douyin.com/video/{vid}",
        "transcript": "",
        "media_url": _media_url(v),  # 转写时直接拉这条，无需再解签名
    }


def fetch_feed(cfg: dict, log=print, limit: int = 40, max_seconds: int = 240) -> dict:
    ensure_browser(log)
    target = _douyin_target()
    if not target:
        # 浏览器可能刚启动还没登记标签页，先等 2 秒再查，避免误判「没有标签页」去开新的
        for _ in range(6):
            time.sleep(0.5)
            target = _douyin_target()
            if target:
                break
    if not target:
        log("调试浏览器里没有抖音标签页，正在新开一个…")
        _open_tab(HOME)
        for _ in range(20):
            time.sleep(0.6)
            target = _douyin_target()
            if target:
                break
    if not target:
        raise RuntimeError("打不开抖音标签页")
    cdp = _CDP(target["webSocketDebuggerUrl"])
    cdp.call("Network.enable")
    cdp.call("Page.enable")
    cdp.call("Page.navigate", {"url": HOME})
    # 精选页比知乎更重，等页面加载完再开始监听：隔 6 秒查一次，共 3 次（18 秒）
    for _ in range(3):
        time.sleep(6)
        try:
            r = cdp.call("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True})
            if r.get("result", {}).get("value") == "complete":
                break
        except Exception:
            pass

    conn = store.connect(config.path(cfg, "db"))
    store.init_db(conn)
    # 预加载库里已有的抖音 id + 标题，采集时直接跳过
    existing = store.existing_ids(conn, "douyin")
    known_titles = store.existing_titles(conn, "douyin")
    log(f"库里已有抖音 {len(existing)} 条，本次目标 {limit} 条新的")
    seen: dict[str, dict] = {}       # 本次新抓到的（待入库）
    dup_skipped = 0                  # 本次跳过的重复数
    author_n: dict[str, int] = {}    # 本批每个作者已经收了几条
    author_skipped = 0               # 因同一作者太多而跳过的数量
    per_author = max(1, int(cfg.get("ingest", {}).get("douyin_per_author", 2)))
    max_dur = int(cfg.get("ingest", {}).get("asr_max_duration_s", 900))
    rejected: list[dict] = []        # 超长淘汰，最后统一写入去重索引
    long_skipped = 0
    t0 = time.time()
    last_scroll = 0.0
    stale = 0   # 连续无新增的滚动屏数，攒够了就刷新精选页重拉
    while len(seen) < limit and time.time() - t0 < max_seconds:
        if stopflag.stopped():
            log("已结束抖音采集")
            break
        cdp.poll()
        got_new = 0
        for body in cdp.take_bodies():
            found: list = []
            _find_awemes(body, found)
            new = 0
            for a in found:
                v = normalize(a)
                if not v:
                    continue
                nt = store.norm_title(v.get("title", ""))
                if v["id"] in existing:
                    dup_skipped += 1
                    continue
                if nt and nt in known_titles:
                    dup_skipped += 1
                    continue
                if v["id"] in seen:
                    continue
                if int(v.get("duration_s") or 0) > max_dur:
                    long_skipped += 1
                    rejected.append(v)
                    existing.add(v["id"])
                    if nt:
                        known_titles.add(nt)
                    continue
                key = v.get("author_id") or v.get("author") or ""
                if key and author_n.get(key, 0) >= per_author:
                    author_skipped += 1
                    continue
                seen[v["id"]] = v
                if key:
                    author_n[key] = author_n.get(key, 0) + 1
                if nt:
                    known_titles.add(nt)
                new += 1
            if new:
                got_new += new
                log(f"新收到 {new} 条，累计 {len(seen)}/{limit}"
                    f"（跳过重复 {dup_skipped}，超长 {long_skipped}，同作者超额 {author_skipped}）")
        # 每 6 秒滚一屏，模拟正常浏览速度
        if time.time() - last_scroll > 6:
            last_scroll = time.time()
            stale = 0 if got_new else stale + 1
            # 连续 3 屏都没新内容，说明滚到重复区了，刷新精选页重拉
            if stale >= 3:
                stale = 0
                log("连续 3 屏无新内容，刷新抖音精选页重拉")
                try:
                    cdp.call("Page.navigate", {"url": HOME})
                    cdp.pending.clear()
                    cdp.finished.clear()
                except Exception:
                    break
                continue
            try:
                cdp.call("Runtime.evaluate", {"expression": "window.scrollBy(0, 1600)"})
            except Exception:
                break

    if rejected:
        store.remember_seen(conn, rejected)
        log(f"超过 {max_dur // 60} 分钟的无字幕视频 {len(rejected)} 条，已淘汰并记入去重索引")
    if not seen:
        if rejected:
            log("本次没有收到符合条件的短视频")
        else:
            log("一条都没收到：可能页面要求登录/验证，请在弹出的浏览器里完成后再试")
        return {"new": 0, "updated": 0}

    videos = list(seen.values())[:limit]
    new_n, upd_n, skip_n = store.upsert_videos(conn, videos)
    log(f"抖音入库：新增 {new_n}，更新 {upd_n}，标题重复跳过 {skip_n}")
    return {"new": new_n, "updated": upd_n}
