"""本地图片文字提取（RapidOCR），供图文帖补足正文。

只在需要时懒加载引擎；失败降级返回空串，不阻塞拉取流程。
"""

from __future__ import annotations

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_OCR = None


def _engine():
    global _OCR
    if _OCR is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR = RapidOCR()
        except Exception as e:
            _OCR = False
            print(f"[OCR] 初始化失败：{e}")
    return _OCR


def extract_from_url(url: str, referer: str = "https://tieba.baidu.com/", timeout: int = 15) -> str:
    """下载一张图并 OCR 出文字，失败返回空串。"""
    if not url:
        return ""
    eng = _engine()
    if not eng:
        return ""
    try:
        r = requests.get(url, headers={"Referer": referer, "User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        img = r.content
    except Exception:
        return ""
    try:
        result, _ = eng(img)
    except Exception:
        return ""
    if not result:
        return ""
    return " ".join(line[1] for line in result if len(line) > 1 and line[1]).strip()


def extract_many(urls: list[str], referer: str = "https://tieba.baidu.com/", timeout: int = 15) -> str:
    """多张图依次 OCR，合并文字。"""
    out = []
    for u in (urls or [])[:4]:
        t = extract_from_url(u, referer, timeout)
        if t:
            out.append(t)
    return " ".join(out)
