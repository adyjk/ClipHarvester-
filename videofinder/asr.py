"""本地语音转写：下载音频 → faster-whisper 转文字。

ASR 走 CPU int8（Ollama 已占满显卡），多线程并行；
下载用信号量限制 2 路，避免触发 B 站风控。
音频临时文件用完即删，模型缓存在 data/models 内，不散落硬盘。
"""

from __future__ import annotations

import os
import random
import threading
import time

from . import config

_MODEL_LOCK = threading.Lock()
_MODELS: dict[str, object] = {}

# 国内直连 huggingface 很慢，默认走镜像（用户可自行覆盖）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def models_dir() -> str:
    p = os.path.join(config.ROOT, "data", "models")
    os.makedirs(p, exist_ok=True)
    return p


def _register_cuda_dlls() -> None:
    """pip 版 CUDA 运行库不在 PATH 里，得显式注册 DLL 目录才能被 ctranslate2 找到。"""
    import sysconfig

    nvidia = os.path.join(sysconfig.get_paths()["purelib"], "nvidia")
    if not os.path.isdir(nvidia):
        return
    for pkg in os.listdir(nvidia):
        b = os.path.join(nvidia, pkg, "bin")
        if os.path.isdir(b):
            try:
                os.add_dll_directory(b)
            except OSError:
                pass
            os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")


def _local_snapshot(name: str) -> str:
    """优先用已下载的本地快照目录，避免联网查 hub（离线时也不会报错）。"""
    d = os.path.join(models_dir(), f"models--Systran--faster-whisper-{name}", "snapshots")
    if os.path.isdir(d):
        for s in os.listdir(d):
            p = os.path.join(d, s)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "model.bin")):
                return p
    return name


def get_model(name: str = "small", device: str = "cuda", compute_type: str = "float16"):
    """加载或复用模型（多线程共享同一实例）。默认 GPU，CUDA 不可用时回退 CPU。"""
    key = f"{name}|{device}|{compute_type}"
    with _MODEL_LOCK:
        if key not in _MODELS:
            import av

            # 新版 PyAV 去掉了 metadata_errors 参数，而 faster-whisper 还在传它
            if not getattr(av.open, "_aicore_patched", False):
                _orig_open = av.open

                def _open(*args, **kwargs):
                    kwargs.pop("metadata_errors", None)
                    return _orig_open(*args, **kwargs)

                _open._aicore_patched = True
                av.open = _open

            from faster_whisper import WhisperModel

            if device != "cpu":
                _register_cuda_dlls()
            src = _local_snapshot(name)
            try:
                _MODELS[key] = WhisperModel(
                    src,
                    device=device,
                    compute_type=compute_type,
                    download_root=models_dir(),
                    cpu_threads=4,
                )
            except Exception:
                key = f"{name}|cpu|int8"
                if key not in _MODELS:
                    _MODELS[key] = WhisperModel(
                        src, device="cpu", compute_type="int8",
                        download_root=models_dir(), cpu_threads=4,
                    )
        return _MODELS[key]


def _has_audio(path: str) -> bool:
    """检查文件里有没有音频流：抖音 DASH 视频流常常是纯画面。"""
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20,
        )
        return bool(out.stdout.strip())
    except Exception:
        return True  # 探测不了就不拦，交给后面报错


def download_audio(url: str, out_dir: str, tag: str, media_url: str = "") -> str:
    """只下音频轨道（不下载视频画面）。有直链时直接拉直链（抖音）。"""
    os.makedirs(out_dir, exist_ok=True)
    if media_url:
        import requests

        path = os.path.join(out_dir, f"{tag}.mp4")
        with requests.get(
            media_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": url},
            stream=True, timeout=90,
        ) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        if not os.path.exists(path) or os.path.getsize(path) < 1024:
            raise RuntimeError("直链下载失败")
        if not _has_audio(path):
            try:
                os.remove(path)
            except OSError:
                pass
            raise RuntimeError("直链没有音轨（纯画面流）")
        return path

    import yt_dlp
    tmpl = os.path.join(out_dir, f"{tag}.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": tmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "ratelimit": 8_000_000,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://www.bilibili.com/",
        },
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    if not os.path.exists(path):
        raise RuntimeError("音频下载失败")
    return path


def clip_audio(path: str, max_seconds: int, work_dir: str, tag: str) -> str:
    """超过上限的音频只保留前 N 秒，长视频后面的部分不再转写。"""
    import subprocess

    out = os.path.join(work_dir, f"{tag}_clip.m4a")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path, "-t", str(int(max_seconds)),
             "-c", "copy", out],
            capture_output=True, timeout=300, check=True,
        )
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            return out
    except Exception:
        pass
    return path


def sample_audio(path: str, total_seconds: float, work_dir: str, tag: str,
                 window: float = 300.0) -> str:
    """超长视频跳着取：片头 / 中间 / 结尾各 window 秒，拼成一段送给 whisper。"""
    import subprocess

    total = max(0.0, float(total_seconds))
    if total <= window * 3:
        return clip_audio(path, int(window * 3), work_dir, tag)
    spans = [(0.0, window),
             (max(0.0, total / 2 - window / 2), total / 2 + window / 2),
             (max(0.0, total - window), total)]
    out = os.path.join(work_dir, f"{tag}_sample.wav")
    chains = "".join(
        f"[0:a]atrim=start={s:.2f}:end={e:.2f},asetpts=N/SR/TB[a{i}];"
        for i, (s, e) in enumerate(spans)
    )
    labels = "".join(f"[a{i}]" for i in range(len(spans)))
    fc = f"{chains}{labels}concat=n={len(spans)}:v=0:a=1[out]"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path, "-filter_complex", fc,
             "-map", "[out]", "-ar", "16000", "-ac", "1", out],
            capture_output=True, timeout=600, check=True,
        )
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            return out
    except Exception:
        pass
    return clip_audio(path, int(window * 3), work_dir, tag)


def _hhmmss(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def speech_only(path: str, work_dir: str, tag: str) -> tuple[str, float, float]:
    """用 ffmpeg 找出有声段并拼接成 wav，无声段直接不送 whisper。

    返回 (文件路径, 有声秒数, 总秒数)。有声占比过高或失败时返回原文件。
    """
    import re
    import subprocess

    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
             "-af", "silencedetect=noise=-35dB:d=0.5", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
        log = p.stderr or ""
        m = re.search(r"Duration: (\d+:\d+:[\d.]+)", log)
        total = _hhmmss(m.group(1)) if m else 0.0
        if total <= 0:
            return path, 0.0, 0.0

        # 静音区间 → 取补集得到有声区间
        marks = re.findall(r"silence_(start|end): ([\d.]+)", log)
        silences, cur = [], None
        for kind, val in marks:
            v = float(val)
            if kind == "start":
                cur = v
            elif cur is not None:
                silences.append((cur, min(v, total)))
                cur = None
        if cur is not None:
            silences.append((cur, total))

        keeps, pos = [], 0.0
        for s, e in silences:
            if s - pos > 0.25:
                keeps.append((pos, s))
            pos = max(pos, e)
        if total - pos > 0.25:
            keeps.append((pos, total))
        if not keeps:
            return path, 0.0, total  # 全程无声，交给 whisper 也只会得到空

        # 间隔小于 0.6s 的相邻段合并，减少切段数量
        merged = [list(keeps[0])]
        for s, e in keeps[1:]:
            if s - merged[-1][1] < 0.6:
                merged[-1][1] = e
            else:
                merged.append([s, e])
        keeps = merged[:80]
        speech = sum(e - s for s, e in keeps)
        if speech >= total * 0.85:
            return path, speech, total  # 几乎全程有声，没必要切

        out = os.path.join(work_dir, f"{tag}_speech.wav")
        chains = "".join(
            f"[0:a]atrim=start={s:.2f}:end={e:.2f},asetpts=PTS-STARTPTS[a{i}];"
            for i, (s, e) in enumerate(keeps)
        )
        labels = "".join(f"[a{i}]" for i in range(len(keeps)))
        fc = f"{chains}{labels}concat=n={len(keeps)}:v=0:a=1[out]"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path, "-filter_complex", fc,
             "-map", "[out]", "-ar", "16000", "-ac", "1", out],
            capture_output=True, timeout=300, check=True,
        )
        return out, speech, total
    except Exception:
        return path, 0.0, 0.0


def transcribe_file(model, path: str, max_chars: int = 20000, beam_size: int = 1) -> str:
    segments, _info = model.transcribe(
        path,
        language="zh",
        beam_size=beam_size,
        temperature=0,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    parts = []
    total = 0
    for seg in segments:
        t = (seg.text or "").strip()
        if not t:
            continue
        parts.append(t)
        total += len(t)
        if total >= max_chars:
            break
    return "".join(parts)[:max_chars]


def run_transcribe(cfg: dict, log=print, limit: int = 20, workers: int = 3,
                   model_name: str = "medium") -> dict:
    """并行转写：每路自己做「限速下载 → 转写 → 落库 → 删音频」。"""
    from . import store

    db_path = config.path(cfg, "db")
    conn = store.connect(db_path)
    store.init_db(conn)
    rows = conn.execute(
        "SELECT id, url, title, duration_s FROM videos "
        "WHERE transcript='' AND source='bilibili' ORDER BY score DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    if not rows:
        log("没有需要转写的视频")
        return {"done": 0, "failed": 0}

    log(f"加载 whisper {model_name}（首次会自动下载模型）…")
    model = get_model(model_name)
    log(f"开始并行转写 {len(rows)} 条，并发 {workers}")

    audio_dir = os.path.join(config.ROOT, "data", "audio")
    download_sem = threading.Semaphore(2)
    done = {"ok": 0, "fail": 0}
    lock = threading.Lock()
    t0 = time.time()

    def job(idx: int, row) -> None:
        path = None
        cut = None
        try:
            with download_sem:
                time.sleep(random.uniform(1.0, 2.2))  # 限速，别把接口打爆
                path = download_audio(row["url"], audio_dir, row["id"])
            # 先砍掉无声段，只转写有声部分（纯音乐/长镜头不浪费算力）
            src, speech, total = speech_only(path, audio_dir, row["id"])
            cut = src if src != path else None
            text = transcribe_file(model, src)
            if not text:
                raise RuntimeError("转写为空")
            c = store.connect(db_path)
            try:
                store.save_transcript(c, row["id"], text)
            finally:
                c.close()
            with lock:
                done["ok"] += 1
                ratio = f" 有声{speech:.0f}/{total:.0f}s" if total else ""
                log(f"  [{done['ok']}/{len(rows)}]{ratio} {row['title'][:24]} → {text[:36]}…")
        except Exception as e:
            with lock:
                done["fail"] += 1
                log(f"  失败 {row['title'][:20]}：{e}")
        finally:
            for f in (path, cut):
                if f and os.path.exists(f):
                    try:
                        os.remove(f)
                    except OSError:
                        pass

    batch: list[threading.Thread] = []
    for i, row in enumerate(rows):
        t = threading.Thread(target=job, args=(i, row), daemon=True)
        t.start()
        batch.append(t)
        while sum(1 for x in batch if x.is_alive()) >= max(1, workers):
            time.sleep(0.3)
    for t in batch:
        t.join()

    log(f"转写完成：成功 {done['ok']}，失败 {done['fail']}，耗时 {time.time() - t0:.0f}s")
    return done
