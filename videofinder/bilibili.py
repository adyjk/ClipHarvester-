"""B 站进货：只取元数据 JSON，不下载视频画面。

三个入口：
  1. 关键词搜索（wbi 签名，无需登录）
  2. 分区排行榜 / 热门
  3. 相关推荐（用本地高分视频作种子向外扩散，平台自己的协同过滤）

所有请求串行 + 间隔抖动，失败退避，绝不并发。
"""

from __future__ import annotations

import hashlib
import random
import re
import time
import urllib.parse

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# wbi 签名的置换表（固定常量）
MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

TAG_RE = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return TAG_RE.sub("", s or "").strip()


def _duration(v) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    parts = [p for p in str(v or "").split(":") if p.strip().isdigit()]
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


def _cover(u: str) -> str:
    u = (u or "").strip()
    if u.startswith("//"):
        return "https:" + u
    return u


class RateLimited(RuntimeError):
    pass


class AuthRequired(RuntimeError):
    """接口要求登录（缺 Cookie），属于不可重试错误。"""


class Bilibili:
    def __init__(self, cookie: str = "", interval: float = 1.5, jitter: float = 0.8):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        })
        if cookie:
            self.s.headers["Cookie"] = cookie
        self.interval = max(0.5, interval)
        self.jitter = max(0.0, jitter)
        self._last = 0.0
        self._wbi: tuple[str, str, float] | None = None
        self._buvid_done = False
        self.fail_streak = 0

    # ---------- 底层 ----------
    def _wait(self) -> None:
        gap = self.interval + random.uniform(0, self.jitter)
        delta = time.time() - self._last
        if delta < gap:
            time.sleep(gap - delta)
        self._last = time.time()

    def _get(self, url: str, params: dict | None = None) -> dict:
        self._wait()
        try:
            r = self.s.get(url, params=params, timeout=20)
            data = r.json()
        except Exception as e:  # 网络/解析失败都算一次失败
            self.fail_streak += 1
            raise RuntimeError(f"请求失败: {e}") from e

        code = data.get("code")
        if code == 0:
            self.fail_streak = 0
            return data
        if code in (-101, -400):
            raise AuthRequired(f"code={code} {data.get('message')}（需要登录 Cookie）")
        if code in (-412, -352, -799):
            self.fail_streak += 1
            raise RateLimited(f"被限流 code={code} {data.get('message')}")
        self.fail_streak += 1
        raise RuntimeError(f"接口返回 code={code} {data.get('message')}")

    def _ensure_buvid(self) -> None:
        if self._buvid_done or self.s.cookies.get("buvid3"):
            self._buvid_done = True
            return
        try:
            d = self._get("https://api.bilibili.com/x/frontend/finger/spi").get("data") or {}
            if d.get("b_3"):
                self.s.cookies.set("buvid3", d["b_3"], domain=".bilibili.com")
            if d.get("b_4"):
                self.s.cookies.set("buvid4", d["b_4"], domain=".bilibili.com")
        except Exception:
            pass
        self._buvid_done = True

    def _wbi_keys(self) -> tuple[str, str]:
        if self._wbi and time.time() - self._wbi[2] < 3600:
            return self._wbi[0], self._wbi[1]
        nav = self._get("https://api.bilibili.com/x/web-interface/nav").get("data") or {}
        img = nav.get("wbi_img") or {}
        img_key = (img.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub_key = (img.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img_key or not sub_key:
            raise RuntimeError("拿不到 wbi 密钥")
        self._wbi = (img_key, sub_key, time.time())
        return img_key, sub_key

    def _sign(self, params: dict) -> dict:
        img_key, sub_key = self._wbi_keys()
        mixin = "".join((img_key + sub_key)[i] for i in MIXIN_TAB)[:32]
        p = dict(params)
        p["wts"] = int(time.time())
        p = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in sorted(p.items())}
        query = urllib.parse.urlencode(p)
        p["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
        return p

    # ---------- 元数据归一化 ----------
    def _norm(self, it: dict, kind: str = "") -> dict | None:
        bvid = it.get("bvid")
        if not bvid:
            return None
        stat = it.get("stat") or {}
        owner = it.get("owner") or {}
        tags = it.get("tag") or ""
        if isinstance(tags, list):
            tags = ",".join(str(t) for t in tags)
        if not tags:
            tags = it.get("tname") or ""
        return {
            "id": bvid,
            "source": "bilibili",
            "title": _clean(it.get("title")),
            "desc": _clean(it.get("description") or it.get("desc")),
            "tags": _clean(tags).replace(",", " "),
            "author": _clean(owner.get("name") or it.get("author")),
            "duration_s": _duration(it.get("duration")),
            "pub_ts": int(it.get("pubdate") or it.get("senddate") or 0),
            "play": int(stat.get("view") or it.get("play") or 0),
            "like_count": int(stat.get("like") or it.get("like") or 0),
            "fav": int(stat.get("favorite") or it.get("favorites") or 0),
            "coin": int(stat.get("coin") or 0),
            "danmaku": int(stat.get("danmaku") or it.get("video_review") or 0),
            "reply": int(stat.get("reply") or 0),
            "cover_url": _cover(it.get("pic")),
            "url": f"https://www.bilibili.com/video/{bvid}",
        }

    # ---------- 对外 ----------
    def search(self, keyword: str, page: int = 1, order: str = "pubdate") -> list[dict]:
        self._ensure_buvid()
        params = self._sign({
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "order": order,
        })
        data = self._get("https://api.bilibili.com/x/web-interface/wbi/search/type", params)
        items = (data.get("data") or {}).get("result") or []
        return [v for v in (self._norm(i, "search") for i in items) if v]

    def ranking(self, rid: int = 0) -> list[dict]:
        data = self._get("https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": rid, "type": "all"})
        items = (data.get("data") or {}).get("list") or []
        return [v for v in (self._norm(i, "ranking") for i in items) if v]

    def popular(self, ps: int = 30, pn: int = 1) -> list[dict]:
        data = self._get("https://api.bilibili.com/x/web-interface/popular", {"ps": ps, "pn": pn})
        items = (data.get("data") or {}).get("list") or []
        return [v for v in (self._norm(i, "popular") for i in items) if v]

    def recommend(self, fresh_idx: int = 1, ps: int = 30) -> list[dict]:
        """首页推荐流（bilibili.com 首页「推荐」tab），混合各类别，持续更新。"""
        self._ensure_buvid()
        data = self._get("https://api.bilibili.com/x/web-interface/index/top/feed/rcmd",
                         {"fresh_type": 3, "ps": ps, "fresh_idx": fresh_idx, "feed_version": "V8"})
        items = (data.get("data") or {}).get("item") or []
        return [v for v in (self._norm(i, "recommend") for i in items) if v]

    def region_latest(self, rid: int, page: int = 1, ps: int = 30) -> list[dict]:
        """分区最新投稿流，匿名可用，相当于「刷分区」而不是「搜关键词」。"""
        data = self._get("https://api.bilibili.com/x/web-interface/newlist",
                         {"rid": rid, "ps": ps, "pn": page})
        items = (data.get("data") or {}).get("archives") or []
        return [v for v in (self._norm(i, "region") for i in items) if v]

    def related(self, bvid: str) -> list[dict]:
        data = self._get("https://api.bilibili.com/x/web-interface/archive/related", {"bvid": bvid})
        items = data.get("data") or []
        return [v for v in (self._norm(i, "related") for i in items) if v]

    # ---------- 视频内容素材（不是元数据，是视频里真实出现的东西） ----------
    def view(self, bvid: str) -> dict:
        data = self._get("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid}).get("data") or {}
        return {"cid": data.get("cid"), "aid": data.get("aid"), "pages": len(data.get("pages") or [])}

    def subtitles(self, bvid: str, limit_chars: int = 8000, max_seconds: int = 0,
                  sample_total: float = 0, window: float = 300.0) -> str:
        """官方/AI 字幕的纯文本。需要登录 Cookie；没有字幕就返回空串。"""
        try:
            info = self.view(bvid)
        except Exception:
            return ""
        aid, cid = info.get("aid"), info.get("cid")
        if not aid or not cid:
            return ""
        dur = float(info.get("duration") or 0)

        def _once() -> str:
            subs = []
            # 只传 aid+cid：带上 bvid 时接口会返回随机视频的字幕（实测连续 5 次各不相同）
            try:
                signed = self._sign({"aid": aid, "cid": cid})
                data = self._get("https://api.bilibili.com/x/player/wbi/v2", signed)
                subs = ((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            except Exception:
                subs = []
            if not subs:
                try:
                    data = self._get("https://api.bilibili.com/x/player/v2", {"aid": aid, "cid": cid})
                    subs = ((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
                except Exception:
                    subs = []
            if not subs:
                return ""
            pick = next((s for s in subs if str(s.get("lan", "")).lower().startswith(("zh", "ai-zh"))), subs[0])
            link = pick.get("subtitle_url") or ""
            if link.startswith("//"):
                link = "https:" + link
            if not link:
                return ""
            try:
                self._wait()
                body = (self.s.get(link, timeout=20).json() or {}).get("body") or []
            except Exception:
                return ""
            # 时间轴本该落在视频时长内，超出太多说明这条字幕属于别的视频
            last_to = max((float(ln.get("to") or 0) for ln in body), default=0.0)
            if dur and last_to > dur * 2 + 120:
                return ""
            if max_seconds and sample_total and sample_total > max_seconds:
                spans = [(0.0, window),
                         (max(0.0, sample_total / 2 - window / 2), sample_total / 2 + window / 2),
                         (max(0.0, sample_total - window), sample_total)]

                def _keep(t: float) -> bool:
                    return any(s <= t < e for s, e in spans)

                body = [ln for ln in body if _keep(float(ln.get("from") or 0))]
            elif max_seconds:
                body = [ln for ln in body if float(ln.get("from") or 0) < max_seconds]
            text = " ".join(" ".join((line.get("content") or "").split()) for line in body)
            return " ".join(text.split())

        text = _once()
        # B站偶发把别的视频的缓存字幕塞过来，复取一次对开头，不一致就放弃走 ASR
        if len(text) >= 120 and (_once() or "")[:30] != text[:30]:
            return ""
        return text[:limit_chars]

    def danmaku(self, cid: int, limit: int = 400) -> list[str]:
        """弹幕是对画面的即时反应，能反推出视频里真实发生了什么。"""
        if not cid:
            return []
        self._wait()
        r = self.s.get("https://api.bilibili.com/x/v1/dm/list.so", params={"oid": cid}, timeout=20)
        text = r.content.decode("utf-8", "ignore")
        raw = re.findall(r"<d [^>]*>([^<]*)</d>", text)
        seen, out = set(), []
        for t in raw:
            t = t.strip()
            if len(t) < 3 or len(t) > 40:
                continue
            if not re.search(r"[\u4e00-\u9fffA-Za-z]", t):
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
        # 均匀采样，避免全是开头的"前排""1"
        if len(out) > limit:
            step = len(out) / limit
            out = [out[int(i * step)] for i in range(limit)]
        return out

    def replies(self, aid: int, limit: int = 20) -> list[str]:
        if not aid:
            return []
        try:
            data = self._get("https://api.bilibili.com/x/v2/reply",
                             {"type": 1, "oid": aid, "sort": 1, "ps": limit})
        except Exception:
            return []
        out = []
        for r in ((data.get("data") or {}).get("replies") or [])[:limit]:
            try:
                msg = (r.get("content") or {}).get("message") or ""
            except AttributeError:
                continue
            msg = " ".join(msg.split())
            if len(msg) >= 6:
                out.append(msg[:120])
        return out
