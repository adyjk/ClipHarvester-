"""进货流水线：拉取元数据 → 入库 → 重算分数。

只走元数据接口，不下载视频；串行 + 限速 + 每日上限 + 断点页码。
"""

from __future__ import annotations

import os
import re
import threading
import time
from queue import Queue

from . import config, scoring, stopflag, store
from .bilibili import AuthRequired, Bilibili, RateLimited
from .summarize import LocalAI, fallback

# 转写闸门：结束任务后上一批可能还有几条在跑，用它保证同时转写的数量不翻倍（防显存爆）
_ASR_GATE = {"sem": None, "n": 0}


def _asr_gate(workers: int) -> threading.Semaphore:
    sem = _ASR_GATE["sem"]
    if sem is None or workers > _ASR_GATE["n"]:
        sem = threading.Semaphore(max(1, workers))
        _ASR_GATE["sem"], _ASR_GATE["n"] = sem, max(1, workers)
    return sem


def _backoff(seconds: float, reason: str, log) -> None:
    log(f"  退避 {seconds:.0f}s：{reason}")
    time.sleep(seconds)


def run_fetch(cfg: dict, log=print, queries: list[str] | None = None) -> dict:
    ing = cfg["ingest"]
    conn = store.connect(config.path(cfg, "db"))
    store.init_db(conn)

    # 关键词模式：前端传了关键词就只搜关键词，跳过推荐流/相关/排行
    keyword_mode = queries is not None

    already_today = store.today_count(conn)
    budget = min(int(ing["max_per_run"]), max(0, int(ing["daily_limit"]) - already_today))
    if budget <= 0:
        log(f"今日已拉取 {already_today} 条，达到上限 {ing['daily_limit']}，停止进货")
        return {"new": 0, "updated": 0, "skipped": True}

    # 预加载库里已有 id 和标题，收集阶段直接跳过（避免重复 API 请求）
    existing = store.existing_ids(conn, "bilibili")
    known_titles = store.existing_titles(conn, "bilibili")
    log(f"库里已有 B 站 {len(existing)} 条，本次目标 {budget} 条新的")

    bili = Bilibili(
        cookie=ing.get("cookie", ""),
        interval=float(ing.get("request_interval", 1.5)),
        jitter=float(ing.get("jitter", 0.8)),
    )

    collected: list[dict] = []
    seen_run: set[str] = set()        # 本次运行内已收集的 id
    stop = {"flag": False}
    auth_blocked = {"search": False}

    def _fresh(items: list[dict]) -> list[dict]:
        """过滤掉库里已有的 id/标题 + 本次已收集的，只留真正新的。"""
        out = []
        for v in items:
            vid = v["id"]
            nt = store.norm_title(v.get("title", ""))
            if vid in existing or vid in seen_run:
                continue
            if nt and nt in known_titles:
                continue
            seen_run.add(vid)
            if nt:
                known_titles.add(nt)   # 本轮内同标题也只留一条
            out.append(v)
        return out

    def safe_call(fn, *args, label="", need_login=False):
        """统一异常处理：限流/未登录/失败都不炸整个流程。"""
        if stopflag.stopped():
            stop["flag"] = True
        if stop["flag"] or len(collected) >= budget:
            return []
        try:
            return fn(*args)
        except AuthRequired as e:
            if need_login and not auth_blocked["search"]:
                auth_blocked["search"] = True
                log(f"  {label} 需要登录：{e}")
                log("  → 想用关键词搜索，请把浏览器登录后的 Cookie 填进 config.json 的 ingest.cookie")
            return []
        except RateLimited as e:
            _backoff(60, f"{label} 被限流 {e}", log)
            if bili.fail_streak >= 2:
                stop["flag"] = True
                log("  连续被限流，本次进货提前结束")
            return []
        except Exception as e:
            _backoff(15, f"{label} 失败 {e}", log)
            if bili.fail_streak >= 4:
                stop["flag"] = True
                log("  连续失败过多，本次进货提前结束")
            return []

    # 1) 推荐流进货：首页推荐流（bilibili.com 首页「推荐」tab，混合各类别，持续更新）
    if not keyword_mode:
        for idx in range(1, max(1, int(ing.get("feed_pages", 2))) + 1):
            if stop["flag"] or len(collected) >= budget:
                break
            got = safe_call(bili.recommend, idx, 30, label=f"首页推荐 第{idx}页")
            collected += _fresh(got)
            log(f"[首页推荐] 第 {idx} 页 → {len(got)} 条")

    # 2) 关键词搜索：前端传了关键词就强制搜，否则按 config 里的 enable_search 走
    search_queries = queries if keyword_mode else (ing.get("queries") if ing.get("enable_search") else [])
    if search_queries and ing.get("cookie"):
        for q in search_queries:
            if stop["flag"] or len(collected) >= budget:
                break
            key = f"search:{q}"
            page = store.get_cursor(conn, key) + 1
            if page > 5:
                page = 1
            log(f"[搜索] {q} 第 {page} 页")
            got = safe_call(bili.search, q, page, label=f"搜索 {q}", need_login=True)
            if got:
                collected += _fresh(got)
                store.set_cursor(conn, key, page if page <= 5 else 1, len(got))
            log(f"  拿到 {len(got)} 条")
            if auth_blocked["search"]:
                log("  [搜索] 已跳过（缺登录 Cookie）")
                break

    # 3) 相关推荐扩散：优先用你点过「喜欢」的视频当种子，其次才是高分的
    if not keyword_mode and ing.get("enable_related", True) and len(collected) < budget:
        seeds = conn.execute(
            "SELECT id, title FROM videos WHERE source='bilibili' "
            "ORDER BY (feedback='like') DESC, score DESC LIMIT ?",
            (int(ing.get("related_seeds", 6)),),
        ).fetchall()
        for depth in range(max(1, int(ing.get("related_depth", 1)))):
            if stop["flag"] or len(collected) >= budget:
                break
            if not seeds:
                break
            next_seeds = []
            for s in seeds:
                if stop["flag"] or len(collected) >= budget:
                    break
                got = safe_call(bili.related, s["id"], label=f"相关 {s['id']}")
                if got:
                    collected += _fresh(got)
                    next_seeds += [{"id": v["id"], "title": v["title"]} for v in got[:3]]
                    log(f"[扩散] 种子 {s['id']} → {len(got)} 条")
            seeds = next_seeds

    # 3) 排行榜兜底看趋势
    if not keyword_mode and ing.get("enable_ranking", True) and len(collected) < budget:
        for rid in ing.get("ranking_rids") or [0]:
            if stop["flag"] or len(collected) >= budget:
                break
            got = safe_call(bili.ranking, int(rid), label=f"排行榜 rid={rid}")
            collected += _fresh(got)
            log(f"[排行] rid={rid} → {len(got)} 条")

    # 去重后入库（同一次拉取可能重复命中）
    uniq: dict[str, dict] = {}
    for v in collected:
        uniq[v["id"]] = v
    # 超长合集/课程（多 P 视频时长是整季总和）不进库，避免占位置也拖慢转写
    max_dur = int(ing.get("max_duration_s", 1800))
    over = [v for v in uniq.values() if int(v.get("duration_s") or 0) > max_dur]
    if over:
        for v in over:
            uniq.pop(v["id"], None)
        store.remember_seen(conn, over)  # 淘汰也记进索引，以后不再重复拉取
        log(f"[过滤] 超过 {max_dur // 60} 分钟的合集/课程 {len(over)} 条，跳过")
    # 同事件扎堆投稿去重：标题高度相似只留播放量最高的一条
    kept = _dedupe_titles(conn, list(uniq.values()))
    if len(kept) < len(uniq):
        log(f"[去重] 相似内容合并：{len(uniq)} → {len(kept)} 条")
    kept = kept[:budget]   # 严格按本次目标条数入库，别一次塞进一整页
    new_n, upd_n, skip_n = store.upsert_videos(conn, kept)
    store.add_daily_count(conn, new_n)

    total = scoring.apply_all(conn, cfg)
    log(f"入库完成：新增 {new_n} 条，更新 {upd_n} 条，标题重复跳过 {skip_n} 条，重算 {total} 条分数")

    # 顺手给新入库的补降级摘要，保证卡片立刻有简介
    _fill_fallback(conn, log)
    return {"new": new_n, "updated": upd_n, "skipped": False}


def _norm_title(t: str) -> str:
    return re.sub(r"[^0-9a-zA-Z一-鿿]", "", (t or "").lower())


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 2 else {s}


def _dedupe_titles(conn, vids: list[dict], threshold: float = 0.72) -> list[dict]:
    """同一事件的相似投稿只留一条，避免列表被同一件事刷屏。"""
    refs = [
        _bigrams(_norm_title(r["title"]))
        for r in conn.execute("SELECT title FROM videos ORDER BY fetched_at DESC LIMIT 400")
    ]
    kept = []
    for v in sorted(vids, key=lambda x: -(x.get("play") or 0)):
        bg = _bigrams(_norm_title(v["title"]))
        dup = False
        for r in refs:
            if bg and r and len(bg & r) / max(1, min(len(bg), len(r))) >= threshold:
                dup = True
                break
        if not dup:
            refs.append(bg)
            kept.append(v)
    return kept


def _fill_fallback(conn, log) -> None:
    import json

    rows = conn.execute("SELECT * FROM videos WHERE summarized_at=0").fetchall()
    if not rows:
        return
    for r in rows:
        payload = fallback(r)
        conn.execute(
            "UPDATE videos SET summary=?, summary_model='fallback', summarized_at=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), int(time.time()), r["id"]),
        )
    conn.commit()
    log(f"  先铺了 {len(rows)} 条降级简介，稍后可用本地模型升级")


def _grab_content(bili, conn, row, log) -> str:
    """抓视频里真实的文字：弹幕 + 热评。已抓过就直接用缓存。"""
    try:
        cached = row["content"] or ""
    except (IndexError, KeyError):
        cached = ""
    if cached:
        return cached
    if row["source"] != "bilibili":
        return ""
    try:
        v = bili.view(row["id"])
        dms = bili.danmaku(v.get("cid"), 300)
        # 弹幕够多就不必再抓热评，省一次请求（约 1/3 的网络耗时）
        reps = bili.replies(v.get("aid"), 15) if len(dms) < 80 else []
    except Exception as e:
        log(f"  抓实况失败 {row['id']}：{e}")
        return ""
    parts = []
    if dms:
        parts.append("【弹幕】" + " / ".join(dms[:200]))
    if reps:
        parts.append("【热评】" + " / ".join(reps[:12]))
    text = "\n".join(parts)[:3000]
    if text:
        store.save_content(conn, row["id"], text, "danmaku")
    return text


def _vacuous(payload: dict, title: str, has_content: bool) -> bool:
    """总结不出实质内容的直接淘汰，不端给用户。"""
    if payload.get("skip") is True:
        return True
    details = re.sub(r"\s+", "", str(payload.get("details") or ""))
    if not details:
        return True
    try:
        if int(payload.get("quality") or 0) <= 1:
            return True
    except (TypeError, ValueError):
        pass
    t = re.sub(r"\s+", "", title or "")
    if not has_content and t and (details in t or t in details):
        return True  # 没抓到内容、又只是复读标题
    return False


def _author_context(conn, row, days: int = 3, limit: int = 6) -> str:
    """同作者近期视频的标题/简介，帮模型串起前后背景（抖音这种连续更新才用得上）。"""
    try:
        if row["source"] != "douyin" or int(row["pub_ts"] or 0) <= 0:
            return ""
        aid = row["author_id"] or ""
        name = row["author"] or ""
    except (IndexError, KeyError, TypeError):
        return ""
    if not (aid or name):
        return ""
    pub = int(row["pub_ts"])
    span = days * 86400
    if aid:
        sql = ("SELECT title, desc, pub_ts FROM videos WHERE id<>? AND source='douyin' "
               "AND author_id=? AND pub_ts BETWEEN ? AND ? ORDER BY ABS(pub_ts-?) LIMIT ?")
        args = (row["id"], aid, pub - span, pub + span, pub, limit)
    else:
        # 老数据没有 author_id，只能按昵称匹配
        sql = ("SELECT title, desc, pub_ts FROM videos WHERE id<>? AND source='douyin' "
               "AND author_id='' AND author=? AND pub_ts BETWEEN ? AND ? "
               "ORDER BY ABS(pub_ts-?) LIMIT ?")
        args = (row["id"], name, pub - span, pub + span, pub, limit)
    lines = []
    for r in conn.execute(sql, args):
        t = (r["title"] or "").strip()
        if not t:
            continue
        when = time.strftime("%m-%d", time.localtime(r["pub_ts"])) if r["pub_ts"] else "?"
        line = f"· {when} {t[:40]}"
        d = re.sub(r"\s+", " ", (r["desc"] or "")).strip()
        if d and d != t:
            line += f" —— {d[:60]}"
        lines.append(line)
    return "\n".join(lines)


def run_digest(cfg: dict, log=print, limit: int = 20, workers: int = 3,
               model_name: str = "small", force: bool = False, source: str = "",
               ids: list[str] | None = None) -> dict:
    """内容级总结：下音频 → 裁静音 → whisper 转写 → 本地模型总结。

    转写在后台线程并行，主线程同时做模型总结，两边不互相等。
    全程不抓弹幕，总结依据就是视频里真实说的话。
    """
    from concurrent.futures import ThreadPoolExecutor

    from . import asr

    conn = store.connect(config.path(cfg, "db"))
    store.init_db(conn)
    if ids:
        ids = [str(i) for i in ids]
        ph = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT * FROM videos WHERE id IN ({ph}) ORDER BY score DESC LIMIT ?",
            ids + [max(limit, len(ids))],
        ).fetchall()
    elif source:
        rows = conn.execute(
            "SELECT * FROM videos WHERE summary_src NOT IN ('danmaku','meta','asr') AND source=? "
            "ORDER BY score DESC LIMIT ?", (source, limit),
        ).fetchall()
    else:
        rows = store.pending_summary(conn, limit, force)
    if not rows:
        log("没有待总结的视频")
        return {"done": 0, "failed": 0, "skipped": 0}

    # 无字幕最多转写 15 分钟；有官方字幕的最多 30 分钟。超长直接淘汰，不再抽样硬啃。
    ing = cfg["ingest"]
    asr_max = int(ing.get("asr_max_duration_s", 900))
    subtitle_max = int(ing.get("subtitle_max_duration_s", 1800))
    beam = int(ing.get("asr_beam_size", 1))
    log(f"时长门槛：无字幕 ≤{asr_max // 60} 分钟，有字幕 ≤{subtitle_max // 60} 分钟，超出直接淘汰")

    ai = LocalAI(cfg)
    if not ai.available():
        log("本地模型不可用，无法总结")
        return {"done": 0, "failed": 0, "skipped": 0, "model_offline": True}

    log(f"加载 whisper {model_name}…")
    model = asr.get_model(model_name)
    audio_dir = os.path.join(config.ROOT, "data", "audio")
    os.makedirs(audio_dir, exist_ok=True)
    # 清掉上次中断留下的临时音频，避免越积越多
    now = time.time()
    for f in os.listdir(audio_dir):
        fp = os.path.join(audio_dir, f)
        try:
            if now - os.path.getmtime(fp) > 3600:
                os.remove(fp)
        except OSError:
            pass
    download_sem = threading.Semaphore(2)
    q: Queue = Queue()
    tls = threading.local()

    def _bili() -> Bilibili:
        if not getattr(tls, "b", None):
            tls.b = Bilibili(cookie=ing.get("cookie", ""), interval=0.6, jitter=0.3)
        return tls.b

    log(f"开始内容级总结 {len(rows)} 条：转写 {workers} 路并行，模型同步总结")
    gate = _asr_gate(workers)

    def transcribe_one(i: int, r) -> None:
        temps: list[str] = []
        if stopflag.stopped():
            q.put((i, r, "", 0.0, 0.0, "cancelled"))
            return
        try:
            dur = int(r["duration_s"] or 0)
            if dur > subtitle_max:
                q.put((i, r, "", 0.0, 0.0, f"skip:超过 {subtitle_max // 60} 分钟"))
                return
            # 官方字幕一次请求拿全片文字，比 ASR 快几十倍，有就优先用
            if ing.get("cookie") and str(r["id"]).startswith("BV"):
                try:
                    sub = _bili().subtitles(r["id"], max_seconds=subtitle_max, sample_total=dur)
                except Exception:
                    sub = ""
                if len(sub) >= 200:
                    log(f"  字幕直取：{r['title'][:20]}")
                    q.put((i, r, sub, 0.0, 0.0, ""))
                    return
            # 没有字幕只能靠 whisper；长视频转写慢且总结质量差，直接淘汰
            if dur > asr_max:
                q.put((i, r, "", 0.0, 0.0, f"skip:无字幕且超过 {asr_max // 60} 分钟"))
                return
            with download_sem:
                time.sleep(0.8)
                path = asr.download_audio(r["url"], audio_dir, r["id"], r["media_url"] or "")
            temps.append(path)
            src, speech, total = asr.speech_only(path, audio_dir, r["id"])
            if src != path:
                temps.append(src)
            if speech < 1.0:      # 没有可识别的语音段，别喂给 whisper 白跑
                q.put((i, r, "", speech, total, ""))
                return
            with gate:
                text = asr.transcribe_file(model, src, beam_size=beam) if not stopflag.stopped() else ""
            q.put((i, r, text, speech, total, ""))
        except Exception as e:
            msg = str(e)
            if "tuple index out of range" in msg:
                msg = "无可用音轨"
            q.put((i, r, "", 0.0, 0.0, msg))
        finally:
            for f in temps:
                if f and os.path.exists(f):
                    try:
                        os.remove(f)
                    except OSError:
                        pass

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    done = failed = skipped = 0
    cancelled = False
    t0 = time.time()
    try:
        for i, r in enumerate(rows):
            pool.submit(transcribe_one, i, r)

        for _ in range(len(rows)):
            if stopflag.stopped():
                cancelled = True
                log("已结束任务，剩余视频不再处理")
                break
            i, r, text, speech, total, err = q.get()
            if err == "cancelled":
                cancelled = True
                continue
            if err.startswith("skip:"):
                reason = err[5:]
                store.set_feedback(conn, r["id"], "skip")
                store.remember_seen(conn, [{"id": r["id"], "source": r["source"], "title": r["title"]}])
                skipped += 1
                log(f"  [{i + 1}/{len(rows)}] 淘汰（{reason}）：{r['title'][:24]}")
                continue
            if err or not text:
                if not err:
                    # 有音频但识别不出语音，基本是音乐/无声视频，别再反复转写
                    store.set_feedback(conn, r["id"], "skip")
                    store.remember_seen(conn, [{"id": r["id"], "source": r["source"], "title": r["title"]}])
                    skipped += 1
                    log(f"  [{i + 1}/{len(rows)}] 淘汰（无语音）：{r['title'][:24]}")
                    continue
                failed += 1
                log(f"  [{i + 1}/{len(rows)}] 转写失败：{err or '空'} {r['title'][:20]}")
                # 稿件已删/不可见的别反复重试，直接淘汰
                if any(k in err for k in ("不可见", "extractor error", "404", "KeyError")):
                    store.set_feedback(conn, r["id"], "skip")
                continue
            store.save_transcript(conn, r["id"], text)
            try:
                row = dict(r)
                row["transcript"] = text
                row["content"] = ""
                ctx = _author_context(conn, row)
                if ctx:
                    log(f"  [{i + 1}/{len(rows)}] 参考同作者 {len(ctx.splitlines())} 条：{r['title'][:16]}")
                payload = ai.summarize(row, ctx)
                store.save_summary(conn, r["id"], payload, ai.model, "asr")
                if _vacuous(payload, r["title"], True):
                    store.set_feedback(conn, r["id"], "skip")
                    skipped += 1
                    log(f"  [{i + 1}/{len(rows)}] 淘汰：{r['title'][:24]}")
                    continue
                done += 1
                ratio = f"有声{speech:.0f}/{total:.0f}s" if total else ""
                log(f"  [{i + 1}/{len(rows)}] {ratio} {r['title'][:20]} → "
                    f"{(payload.get('details') or payload.get('one_line') or '')[:60]}")
            except Exception as e:
                failed += 1
                log(f"  [{i + 1}/{len(rows)}] 总结失败：{e}")

            # 每 5 条报一次进度和预计剩余时间，避免干等
            finished = done + failed + skipped
            if finished % 5 == 0 and finished < len(rows):
                left = (time.time() - t0) / finished * (len(rows) - finished)
                log(f"  进度 {finished}/{len(rows)}，预计还需约 {left / 60:.0f} 分钟")
    finally:
        # 主动结束时不等在跑的几条转写，立刻把控制权交还给界面
        pool.shutdown(wait=not cancelled, cancel_futures=cancelled)

    if done:
        scoring.apply_all(conn, cfg)
    if cancelled:
        log(f"已结束任务：完成 {done} 条，淘汰 {skipped}，失败 {failed}，其余不再处理")
    else:
        log(f"内容级总结完成：成功 {done}，淘汰 {skipped}，失败 {failed}，耗时 {time.time() - t0:.0f}s")
    return {"done": done, "failed": failed, "skipped": skipped}


def run_summarize(cfg: dict, log=print, limit: int | None = None, force: bool = False,
                  use_content: bool = False) -> dict:
    """只用已有素材（转写/缓存文本）总结，默认不再抓弹幕。"""
    conn = store.connect(config.path(cfg, "db"))
    store.init_db(conn)

    ai = LocalAI(cfg)
    if not ai.available():
        log("本地模型不可用，跳过 AI 摘要（卡片仍显示降级简介）")
        return {"done": 0, "failed": 0, "model_offline": True}

    n = int(limit or cfg["ai"].get("max_per_run", 40))
    rows = store.pending_summary(conn, n, force)
    if not rows:
        log("没有待摘要的视频")
        return {"done": 0, "failed": 0, "model_offline": False}

    log(f"开始用 {ai.model} 总结 {len(rows)} 条（抓取与模型并行）…")
    done = failed = skipped = 0
    t0 = time.time()

    # 预取流水线：网络线程提前抓弹幕，主线程专心跑模型，两边时间重叠
    q: Queue = Queue(maxsize=6)
    if use_content:
        ing = cfg["ingest"]
        db_path = config.path(cfg, "db")
        cursor = {"i": 0}
        clock = threading.Lock()

        def worker() -> None:
            bili = Bilibili(
                cookie=ing.get("cookie", ""),
                interval=float(ing.get("content_interval", 0.6)),
                jitter=0.3,
            )
            c = store.connect(db_path)
            try:
                while True:
                    with clock:
                        i = cursor["i"]
                        cursor["i"] += 1
                    if i >= len(rows):
                        return
                    r = rows[i]
                    q.put((i, r, _grab_content(bili, c, r, log)))
            finally:
                c.close()

        for _ in range(max(1, int(ing.get("content_workers", 2)))):
            threading.Thread(target=worker, daemon=True).start()
    else:
        for i, r in enumerate(rows):
            q.put((i, r, ""))

    buffer: dict[int, tuple] = {}
    for i in range(len(rows)):
        if stopflag.stopped():
            log("已结束任务，剩余视频不再处理")
            break
        while i not in buffer:
            j, r, content = q.get()
            buffer[j] = (r, content)
        r, content = buffer.pop(i)
        try:
            row = dict(r)
            transcript = (row.get("transcript") or "").strip()
            if transcript:
                content = ""
                src = "asr"
            else:
                src = "danmaku" if content else "meta"
            row["content"] = content
            row["transcript"] = transcript
            payload = ai.summarize(row, _author_context(conn, row))
            store.save_summary(conn, r["id"], payload, ai.model, src)
            if _vacuous(payload, r["title"], bool(content or transcript)):
                store.set_feedback(conn, r["id"], "skip")  # 总结不出东西，直接淘汰不上架
                skipped += 1
                log(f"  [{i + 1}/{len(rows)}] 淘汰（无实质内容）：{r['title'][:26]}")
                continue
            done += 1
            mark = {"asr": "转写", "danmaku": "弹幕"}.get(src, "仅元数据")
            log(f"  [{i + 1}/{len(rows)}] ({mark}) {r['title'][:24]} → {payload.get('details') or payload.get('one_line','')}"[:150])
        except Exception as e:
            failed += 1
            log(f"  [{i + 1}/{len(rows)}] 失败：{e}")
            if failed >= 6 and done == 0:
                log("连续失败过多，停止摘要")
                break
    if done:
        scoring.apply_all(conn, cfg)
    log(f"摘要完成：成功 {done}，淘汰 {skipped}，失败 {failed}，耗时 {time.time() - t0:.0f}s")
    return {"done": done, "failed": failed, "skipped": skipped, "model_offline": False}

