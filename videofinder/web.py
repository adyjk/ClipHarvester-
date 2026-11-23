"""本地网页服务（标准库实现，无需安装任何 Web 框架）。

默认 http://127.0.0.1:8765 ，只监听本机。
浏览、检索、打分全部在本地完成，页面上的操作不会产生平台请求。
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import requests

from . import config, ingest, scoring, stopflag, store

HERE = os.path.dirname(os.path.abspath(__file__))
UI_PATH = os.path.join(HERE, "webui.html")

STATE = {"running": "", "log": [], "ts": 0, "phase": ""}
THUMB_CACHE: dict[str, tuple[bytes, str]] = {}
LOCK = threading.Lock()


def _log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    with LOCK:
        STATE["log"].append(line)
        del STATE["log"][:-200]


def _id_snapshot(db_path: str) -> set[str]:
    conn = store.connect(db_path)
    try:
        return {r[0] for r in conn.execute("SELECT id FROM videos")}
    finally:
        conn.close()


def _workers(cfg: dict) -> int:
    """按「最多占用 70% 性能」折算并行路数。"""
    perf = cfg.get("performance", {})
    pct = int(perf.get("max_load_pct", 70))
    cap = int(perf.get("max_workers", 5))
    return max(1, min(cap, round(os.cpu_count() * pct / 100 / 3)))


def _summarize_new(cfg: dict, ids: list[str]) -> None:
    """拉完立刻总结这一批新的；总结期间前端会持续刷新卡片。"""
    if not ids:
        _log("这批没有新视频，跳过 AI 总结")
        return
    workers = _workers(cfg)
    _log(f"本批 {len(ids)} 条，转写 {workers} 路并行（限 {cfg.get('performance', {}).get('max_load_pct', 70)}% 性能）")
    with LOCK:
        STATE["phase"] = "digest"
    try:
        ingest.run_digest(cfg, _log, limit=len(ids), workers=workers, ids=ids)
    finally:
        with LOCK:
            STATE["phase"] = ""


def _busy() -> str:
    with LOCK:
        return STATE["running"]


def _run_job(name: str, fn) -> None:
    with LOCK:
        if STATE["running"]:
            return
        STATE["running"] = name
        STATE["log"] = []
        STATE["ts"] = time.time()
        stopflag.reset()
    try:
        fn()
    except Exception as e:
        _log(f"{name} 出错：{e}")
    finally:
        with LOCK:
            STATE["running"] = ""
            STATE["phase"] = ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VideoFinder/0.1"

    def log_message(self, *args):  # 静音默认访问日志
        pass

    # ---------- 工具 ----------
    def _db(self):
        conn = store.connect(self.server.db_path)
        return conn

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except ConnectionError:
            # 客户端（浏览器）在响应写完前断开/刷新，属正常，不刷错误日志
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._serve_ui()
        if u.path == "/api/videos":
            return self._api_videos(q)
        if u.path == "/api/stats":
            conn = self._db()
            try:
                return self._json(store.stats(conn))
            finally:
                conn.close()
        if u.path == "/api/job":
            with LOCK:
                return self._json({
                    "running": STATE["running"],
                    "phase": STATE["phase"],
                    "log": STATE["log"][-40:],
                })
        if u.path == "/api/history":
            conn = self._db()
            try:
                limit = int(q.get("limit", ["200"])[0] or 200)
                offset = int(q.get("offset", ["0"])[0] or 0)
                return self._json(store.history(conn, limit, offset))
            finally:
                conn.close()
        if u.path == "/api/thumb":
            return self._api_thumb(q)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        data = self._body()
        if u.path == "/api/feedback":
            conn = self._db()
            try:
                vid = str(data.get("id") or "")
                action = str(data.get("action") or "")
                if not vid or action not in ("like", "dislike", "skip", "clear", "opened"):
                    return self._json({"error": "bad request"}, 400)
                store.set_feedback(conn, vid, action)
                scoring.apply_all(conn, self.server.cfg)
                return self._json({"ok": True, "stats": store.stats(conn)})
            finally:
                conn.close()

        if u.path == "/api/fetch":
            busy = _busy()
            if busy:
                return self._json({"started": False, "busy": busy})

            def job():
                before = _id_snapshot(self.server.db_path)
                ingest.run_fetch(self.server.cfg, _log)
                # 拉完立刻把这批新视频做内容级总结（转写语音）
                _summarize_new(self.server.cfg, sorted(_id_snapshot(self.server.db_path) - before))

            threading.Thread(target=_run_job, args=("fetch+summarize", job), daemon=True).start()
            return self._json({"started": True})

        if u.path == "/api/fetch_douyin":
            from . import douyin

            busy = _busy()
            if busy:
                return self._json({"started": False, "busy": busy})
            n = int(data.get("n") or 0) or 30

            def job():
                before = _id_snapshot(self.server.db_path)
                douyin.fetch_feed(self.server.cfg, _log, limit=n)
                _summarize_new(self.server.cfg, sorted(_id_snapshot(self.server.db_path) - before))

            threading.Thread(target=_run_job, args=("douyin+digest", job), daemon=True).start()
            return self._json({"started": True})

        if u.path == "/api/refresh":
            busy = _busy()
            if busy:
                return self._json({"started": False, "busy": busy})
            # 1) 把页面上已刷过的视频标记为 served，之后不再占位
            ids = [str(x) for x in (data.get("ids") or []) if x]
            n = max(5, int(data.get("n") or 0) or 30)
            # 各平台拉取上限分开：图文（知乎/贴吧）能多拉，视频（B站/抖音）受下载转写限制少拉
            limits = self.server.cfg.get("limits", {})
            source = str(data.get("source") or "")
            targets = data.get("targets") or {}
            zh_n = 0
            tb_n = 0
            tap_n = 0
            if targets:
                # 多平台同时拉：{douyin:5, tieba:10, ...}，各平台用各自数量，受 limits 上限约束
                bili_n = min(int(targets.get("bilibili") or 0), limits.get("bilibili", 60))
                dy_n = min(int(targets.get("douyin") or 0), limits.get("douyin", 60))
                zh_n = min(int(targets.get("zhihu") or 0), limits.get("zhihu", 50))
                tb_n = min(int(targets.get("tieba") or 0), limits.get("tieba", 100))
                tap_n = min(int(targets.get("taptap") or 0), limits.get("taptap", 100))
                n = bili_n + dy_n + zh_n + tb_n + tap_n
            elif source == "bilibili":
                bili_n, dy_n = min(n, limits.get("bilibili", 60)), 0
            elif source == "douyin":
                bili_n, dy_n = 0, min(n, limits.get("douyin", 60))
            elif source == "zhihu":
                bili_n, dy_n, zh_n = 0, 0, min(n, limits.get("zhihu", 50))
            elif source == "tieba":
                bili_n, dy_n, tb_n = 0, 0, min(n, limits.get("tieba", 100))
            elif source == "taptap":
                bili_n, dy_n, tap_n = 0, 0, min(n, limits.get("taptap", 100))
            else:
                dy_n = min(limits.get("douyin", 60), max(5, n // 2))
                bili_n = min(n - dy_n, limits.get("bilibili", 60))
            conn = self._db()
            try:
                marked = store.mark_served(conn, ids)
            finally:
                conn.close()

            # 2) 后台拉一批新的（B站 + 抖音），拉完自动总结这一批
            def job():
                cfg = json.loads(json.dumps(self.server.cfg))
                c = store.connect(self.server.db_path)
                try:
                    today = store.today_count(c)
                finally:
                    c.close()
                cfg["ingest"]["max_per_run"] = bili_n
                cfg["ingest"]["daily_limit"] = max(
                    int(cfg["ingest"].get("daily_limit", 900)), today + n * 3
                )
                keyword = (data.get("keyword") or "").strip()
                parts = []
                # 图文（知乎/贴吧）采集即出简介、快，放前面先出结果；视频（B站/抖音）要转写、慢，放后面
                for name, cnt in (("知乎", zh_n), ("贴吧", tb_n), ("TapTap", tap_n), ("B站", bili_n), ("抖音", dy_n)):
                    if cnt:
                        parts.append(f"{name} {cnt} 条")
                _log(f"本批目标 {' + '.join(parts)}" if parts else "本批没有目标")
                if keyword:
                    _log(f"关键词「{keyword}」→ 仅 B站按关键词搜索（抖音/知乎/贴吧不支持关键词，照常拉）")
                # 上一轮没总结完的旧视频本轮不再处理，标记跳过，避免"待AI"堆积拖慢本轮
                c = store.connect(self.server.db_path)
                try:
                    c.execute(
                        "UPDATE videos SET summary_src='skip', summarized_at=? WHERE summarized_at=0",
                        (int(time.time()),),
                    )
                    c.commit()
                finally:
                    c.close()
                before = _id_snapshot(self.server.db_path)
                # 四个平台并行拉取：图文（知乎/贴吧）快、视频（B站/抖音）慢，互不等待
                # 只要有一个平台要跑 CDP，就先备好共享的调试浏览器，避免并发启动互相踩
                if zh_n or tb_n or dy_n:
                    try:
                        from . import douyin

                        douyin.ensure_browser(_log)
                    except Exception as e:
                        _log(f"调试浏览器就绪失败：{e}")

                def _safe(name, fn):
                    if stopflag.stopped():
                        return
                    try:
                        fn()
                    except Exception as e:
                        _log(f"{name}采集跳过：{e}")

                threads = []
                if zh_n:
                    from . import zhihu

                    threads.append(threading.Thread(target=_safe, args=(
                        "知乎", lambda: zhihu.fetch_recommend(cfg, _log, limit=zh_n)), daemon=True))
                if tb_n:
                    from . import tieba

                    threads.append(threading.Thread(target=_safe, args=(
                        "贴吧", lambda: tieba.fetch_posts(cfg, _log, limit=tb_n)), daemon=True))
                if tap_n:
                    from . import tap

                    threads.append(threading.Thread(target=_safe, args=(
                        "TapTap", lambda: tap.fetch_forum(cfg, _log, limit=tap_n)), daemon=True))
                if bili_n:
                    def _bili():
                        if keyword:
                            ingest.run_fetch(cfg, _log, queries=[keyword])
                        else:
                            ingest.run_fetch(cfg, _log)

                    threads.append(threading.Thread(target=_safe, args=("B站", _bili), daemon=True))
                if dy_n:
                    from . import douyin

                    threads.append(threading.Thread(target=_safe, args=(
                        "抖音", lambda: douyin.fetch_feed(cfg, _log, limit=dy_n)), daemon=True))
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                new_ids = sorted(_id_snapshot(self.server.db_path) - before)[:n]
                # 知乎/贴吧/TapTap 是图文，简介在采集时就生成好了，不用再走转写
                new_ids = [i for i in new_ids if not i.startswith(("zh_", "tb_", "tap_"))]
                if stopflag.stopped():
                    _log(f"已结束任务，本批入库 {len(new_ids)} 条不做总结")
                    return
                _summarize_new(cfg, new_ids)

            threading.Thread(target=_run_job, args=("refresh", job), daemon=True).start()
            return self._json({"started": True, "marked": marked})

        if u.path == "/api/summarize":
            busy = _busy()
            if busy:
                return self._json({"started": False, "busy": busy})
            n = int(data.get("n") or 0) or 20
            threading.Thread(
                target=_run_job,
                args=("digest", lambda: ingest.run_digest(
                    self.server.cfg, _log, limit=n, workers=_workers(self.server.cfg))),
                daemon=True,
            ).start()
            return self._json({"started": True})

        if u.path == "/api/cancel":
            running = _busy()
            stopflag.request()
            if running:
                _log("收到结束指令，正在收尾…")
            return self._json({"cancelled": True, "running": running})

        if u.path == "/api/rescore":
            conn = self._db()
            try:
                n = scoring.apply_all(conn, self.server.cfg)
                return self._json({"ok": True, "rescored": n})
            finally:
                conn.close()

        return self._json({"error": "not found"}, 404)

    # ---------- 具体实现 ----------
    def _serve_ui(self):
        with open(UI_PATH, "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")

    def _api_videos(self, q: dict):
        conn = self._db()
        try:
            rows = store.search(
                conn,
                q=" ".join(q.get("q") or []),
                source=(q.get("source") or [""])[0],
                sort=(q.get("sort") or ["score"])[0],
                limit=int((q.get("limit") or ["60"])[0]),
                offset=int((q.get("offset") or ["0"])[0]),
                hide_disliked=(q.get("hide_disliked") or ["1"])[0] != "0",
                hide_opened=(q.get("opened") or ["0"])[0] != "1",
                hide_served=(q.get("served") or ["0"])[0] != "1",
            )
            out = []
            for r in rows:
                try:
                    summary = json.loads(r["summary"]) if r["summary"] else {}
                except ValueError:
                    summary = {}
                try:
                    reasons = json.loads(r["reasons"]) if r["reasons"] else []
                except ValueError:
                    reasons = []
                out.append({
                    "id": r["id"],
                    "source": r["source"],
                    "title": r["title"],
                    "desc": r["desc"],
                    "tags": r["tags"],
                    "author": r["author"],
                    "duration_s": r["duration_s"],
                    "pub_ts": r["pub_ts"],
                    "play": r["play"],
                    "like": r["like_count"],
                    "fav": r["fav"],
                    "cover_url": r["cover_url"],
                    "url": r["url"],
                    "summary": summary,
                    "score": r["score"],
                    "reasons": reasons,
                    "summary_src": r["summary_src"],
                    "feedback": r["feedback"],
                    "opened": r["opened"],
                })
            return self._json({"items": out, "stats": store.stats(conn)})
        finally:
            conn.close()

    def _api_thumb(self, q: dict):
        url = unquote((q.get("u") or [""])[0])
        if not url.startswith("http"):
            return self._json({"error": "bad url"}, 400)
        # B 站封面支持在 URL 后加处理参数，取小图省流量
        if "hdslb.com" in url:
            url = url.split("@", 1)[0] + "@480w_270h_1c.webp"
        cached = THUMB_CACHE.get(url)
        if cached:
            return self._send(200, cached[0], cached[1])
        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://www.bilibili.com/",
                },
                timeout=12,
            )
            ctype = r.headers.get("Content-Type", "image/jpeg")
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            if len(THUMB_CACHE) > 500:
                THUMB_CACHE.clear()
            THUMB_CACHE[url] = (r.content, ctype)
            return self._send(200, r.content, ctype)
        except Exception:
            # 失败返回 1x1 透明占位
            empty = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")
            return self._send(200, empty, "image/jpeg")


def _lan_ips() -> list[str]:
    """取本机局域网 IPv4，用来告诉手机该访问哪个地址。"""
    import socket

    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))          # 不真的发包，只为拿到出口网卡 IP
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


def serve(cfg: dict) -> None:
    db_path = config.path(cfg, "db")
    conn = store.connect(db_path)
    store.init_db(conn)
    conn.close()

    # 保留策略：拉取超过 N 小时的视频自动清理
    retention = int(cfg.get("retention_hours", 24))

    def _purge_loop():
        while True:
            conn = store.connect(db_path)
            try:
                n = store.purge_old(conn, retention)
                if n:
                    print(f"{time.strftime('%H:%M:%S')} 清理旧视频 {n} 条（超过 {retention} 小时）")
                keep_days = int(cfg.get("seen_retention_days", 30))
                m = store.purge_seen(conn, keep_days)
                if m:
                    print(f"{time.strftime('%H:%M:%S')} 清理去重索引 {m} 条（超过 {keep_days} 天）")
            except Exception as e:
                print(f"清理失败：{e}")
            finally:
                conn.close()
            time.sleep(1800)

    threading.Thread(target=_purge_loop, daemon=True).start()

    host = cfg["server"]["host"]
    port = int(cfg["server"]["port"])
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.db_path = db_path
    httpd.cfg = cfg
    print(f"本地界面已启动： http://127.0.0.1:{port}")
    if host in ("0.0.0.0", ""):
        for ip in _lan_ips():
            print(f"手机（同一 WiFi）访问： http://{ip}:{port}")
    print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
