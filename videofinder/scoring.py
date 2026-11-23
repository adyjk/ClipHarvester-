"""打分与反馈学习。

分数完全可解释：每一条都会生成 reasons，显示在卡片上，
你能看出「为什么这条排前面」。
"""

from __future__ import annotations

import json
import math
import re
import time

HAN_RE = re.compile(r"[\u4e00-\u9fff]+")
WORD_RE = re.compile(r"[a-z0-9_+#]{2,}")


def tokenize(text: str) -> set[str]:
    """中文按字符 bigram，英文/数字按单词。不依赖第三方分词器。"""
    if not text:
        return set()
    text = text.lower()
    toks: set[str] = set()
    for w in WORD_RE.findall(text):
        toks.add(w)
    for seg in HAN_RE.findall(text):
        if len(seg) == 1:
            toks.add(seg)
        for i in range(len(seg) - 1):
            toks.add(seg[i:i + 2])
    return toks


def build_interest(conn) -> dict[str, float]:
    """从 like / dislike 反馈反推关键词权重（log 似然比）。"""
    like: dict[str, int] = {}
    dislike: dict[str, int] = {}
    rows = conn.execute(
        "SELECT title, desc, tags, feedback FROM videos WHERE feedback IN ('like','dislike')"
    ).fetchall()
    for r in rows:
        tgt = like if r["feedback"] == "like" else dislike
        text = " ".join([r["title"] or "", r["tags"] or "", (r["desc"] or "")[:120]])
        for t in tokenize(text):
            tgt[t] = tgt.get(t, 0) + 1

    weights: dict[str, float] = {}
    for t in set(like) | set(dislike):
        w = math.log((like.get(t, 0) + 1) / (dislike.get(t, 0) + 1))
        if abs(w) > 0.3:
            weights[t] = w
    return weights


def _hit_keywords(text: str, words: list[str]) -> list[str]:
    low = (text or "").lower()
    return [w for w in words if w and w.lower() in low]


def score_video(row, interest: dict[str, float], cfg: dict, now: int | None = None) -> tuple[float, list[str]]:
    """返回 (分数, 理由列表)。理由用于在 UI 上解释排序。"""
    now = now or int(time.time())
    sc = cfg["scoring"]
    w = sc["weights"]

    title = row["title"] or ""
    tags = row["tags"] or ""
    desc = row["desc"] or ""
    reasons: list[str] = []
    score = 0.0

    # 屏蔽词直接压到底
    banned = _hit_keywords(title + " " + tags, sc.get("exclude_keywords") or [])
    if banned:
        return -999.0, ["屏蔽词：" + "、".join(banned)]

    # 1) 兴趣（从反馈学习到的关键词权重）
    tokens = tokenize(title + " " + tags + " " + desc[:120])
    raw_interest = sum(interest.get(t, 0.0) for t in tokens)
    raw_interest = max(-3.0, min(3.0, raw_interest))
    if abs(raw_interest) > 0.05:
        score += raw_interest * w["interest"]
        reasons.append(f"兴趣{'+' if raw_interest > 0 else ''}{raw_interest:.1f}")

    # 2) 时效
    pub = int(row["pub_ts"] or 0)
    if pub:
        days = max(0.0, (now - pub) / 86400)
        rec = max(0.0, 1.0 - days / max(1, sc["recency_days"]))
        if rec > 0.02:
            score += rec * w["recency"]
            if days < 2:
                reasons.append("刚发布")
            elif days < 7:
                reasons.append("本周新片")

    # 3) 互动质量（收藏/投币比播放更能说明「值不值得看」）
    play = int(row["play"] or 0)
    if play > 0:
        eng = (int(row["fav"] or 0) * 6 + int(row["coin"] or 0) * 8 + int(row["like_count"] or 0) * 1.5) / play
        eng_n = min(1.0, eng)
        score += eng_n * w["engagement"]
        if eng_n > 0.5:
            reasons.append("收藏率高")

    # 4) 时长是否落在偏好区间
    dur = int(row["duration_s"] or 0)
    lo, hi = sc["duration_prefer"]
    if dur:
        in_band = lo <= dur <= hi
        score += (w["duration"] if in_band else -w["duration"] * 0.6)
        if in_band and dur >= 480:
            reasons.append("时长合适")

    # 5) 本地 AI 评的价值分
    try:
        payload = json.loads(row["summary"]) if row["summary"] else {}
    except (ValueError, TypeError):
        payload = {}
    q = payload.get("quality")
    if isinstance(q, (int, float)):
        norm = (float(q) - 3.0) / 2.0
        score += norm * w["ai_quality"]
        if norm >= 0.4:
            reasons.append(f"AI 评 {q}/5")
    kind = payload.get("kind") or ""
    if kind and any(k in kind for k in ("教程", "实战", "深度", "开发日志", "测评")):
        score += 0.3 * w["ai_quality"]
        reasons.append(kind)

    # 6) 已处理反馈
    fb = row["feedback"] or ""
    if fb == "like":
        score += 1.5
        reasons.append("你标记了喜欢")
    elif fb == "skip":
        score -= 0.8
    if row["opened"]:
        score -= 0.6

    return round(score, 3), reasons


def apply_all(conn, cfg: dict) -> int:
    """重算全库分数（几千条几毫秒，反馈变化后调用）。"""
    from . import store

    interest = build_interest(conn)
    now = int(time.time())
    scored: list[tuple[float, str, str]] = []
    for row in store.all_rows(conn):
        s, reasons = score_video(row, interest, cfg, now)
        scored.append((s, json.dumps(reasons, ensure_ascii=False), row["id"]))
    store.update_scores(conn, scored)
    return len(scored)
