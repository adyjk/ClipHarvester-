"""本地 AI 摘要：用 Ollama 把「标题 + 简介 + 标签」压成一段可读的介绍。

模型不可用时自动降级为抽取式摘要（简介首句 + 标签），
保证卡片上永远有简介，不阻塞任何流程。
"""

from __future__ import annotations

import json
import re
import time

import requests

PROMPT = """你是视频内容摘要器。下面是视频元数据，以及从视频里抓到的「实况文本」（观众弹幕与热门评论，能反映画面里真实出现的内容）。严格输出 JSON，禁止任何额外文字。
要求：
1. one_line：一句话概括视频核心主题，40字以内；
2. details：150~250字的完整总结，按全片顺序写清「开头讲什么 → 中间做了什么/讲了哪几点 → 结尾的结果或结论」。要保留具体的人名、数字、步骤、参数、结论，让读者不看视频也知道里面讲了什么，不能只复述标题，也不要和 one_line 重复；
3. points：数组，3~5 条看点，每条一句话，写具体的知识点/步骤/结论，不写空话；
4. topics：数组，3-5 个关键词标签；
5. kind：内容类型，教程/测评/演示/实战/闲聊/纪录/新闻 中选一个；
6. quality：1~5 的整数，5 分质量最高；
7. worth_watching：一句话说明适合什么人观看，25字以内。
硬性规则：
- 实况文本是观众对画面的即时反应，可以据此推断真实内容，但不存在的细节绝不编造；
- 同作者的其他视频只用来理解前因后果和背景，不能把别条视频的内容当成这条的正文；
- 禁止"引发热议""观众讨论""值得关注""值得一看"这类没有信息量的句子，details 里必须出现标题之外的具体信息；
- 信息实在不足、写不出具体内容时，quality 给 1，details 留空，并额外输出 "skip": true（这条会被直接淘汰，不用硬凑）；
- 全部使用简体中文。

标题：{title}
简介：{desc}
标签：{tags}
{context_block}{content_block}

输出格式（键名固定）：
{{"one_line": "", "details": "", "points": [], "topics": [], "kind": "", "quality": 3, "worth_watching": ""}}"""

CONTEXT_BLOCK = """
同作者近期发布的其他视频（用于理解这条视频的前因后果，只在确实相关时参考）：
{ctx}
"""

CONTENT_BLOCK = """
视频实况文本（弹幕 + 热评，反映画面里实际出现的内容）：
{content}
"""

SENT_SPLIT = re.compile(r"[。！？!?\n]+")


def _repair_truncated(text: str) -> dict | None:
    """输出被 num_predict 截断时补救：闭合未结束的字符串与括号。"""
    m = re.search(r"\{[\s\S]*", text)
    if not m:
        return None
    s = m.group(0).rstrip().rstrip(",")
    stack, in_str, esc = [], False, False
    for ch in s:
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch in "{[":
                stack.append(ch)
            elif ch in "}]" and stack:
                stack.pop()
    if in_str:
        s += '"'
    s += "".join("}" if c == "{" else "]" for c in reversed(stack))
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    return _repair_truncated(text)


def fallback(video) -> dict:
    """无模型时的抽取式摘要：用简介原文，别只有标题。"""
    desc = (video["desc"] or "").strip()
    sents = [s.strip() for s in SENT_SPLIT.split(desc) if len(s.strip()) >= 6]
    one = sents[0][:60] if sents else (video["title"] or "")
    details = "".join(sents[:3])[:140] or (video["title"] or "")
    topics = [t for t in (video["tags"] or "").split() if t][:5]
    return {
        "one_line": one,
        "details": details,
        "topics": topics,
        "kind": "",
        "quality": None,
        "worth_watching": "",
        "engine": "fallback",
    }


class LocalAI:
    def __init__(self, cfg: dict):
        ai = cfg["ai"]
        self.base = (ai["base_url"] or "").rstrip("/")
        self.model = ai["model"]
        self.timeout = ai.get("timeout", 180)
        self.num_predict = ai.get("num_predict", 260)
        self.num_ctx = int(ai.get("num_ctx", 8192))
        self._available: bool | None = None

    def available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            r = requests.get(self.base + "/api/tags", timeout=4)
            models = [m.get("name", "") for m in r.json().get("models", [])]
            self._available = any(m == self.model or m.startswith(self.model + ":") for m in models) \
                or (bool(models) and self.model == "")
            if not self._available:
                print(f"[AI] 本地模型列表里没有 {self.model}：{models[:6]}")
        except Exception as e:
            self._available = False
            print(f"[AI] 连不上本地模型服务 {self.base}（{e}），先用降级摘要")
        return self._available

    def summarize(self, video, context: str = "") -> dict:
        def get(key: str) -> str:
            try:
                return (video[key] or "").strip()
            except (IndexError, KeyError, TypeError):
                return ""

        transcript = get("transcript")
        content = get("content")
        if transcript:
            block = ("视频语音转写全文（按时间顺序的说话内容，可能有同音错别字，按上下文理解。"
                     "请覆盖全片内容，不要只讲开头）：\n" + transcript[:6000])
        elif content:
            block = CONTENT_BLOCK.format(content=content[:2500])
        else:
            block = "（没抓到实况文本，只能依据上面的元数据）"

        prompt = PROMPT.format(
            title=video["title"] or "",
            desc=(video["desc"] or "")[:1500],
            tags=video["tags"] or "",
            context_block=CONTEXT_BLOCK.format(ctx=context[:1200]) if context else "",
            content_block=block,
        )
        t0 = time.time()
        r = requests.post(
            self.base + "/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0.2,
                    "num_predict": self.num_predict,
                    # Ollama 默认只给 2048 token 上下文，长转写会被截断，必须显式放大
                    "num_ctx": self.num_ctx,
                },
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        payload = _extract_json(r.json().get("response", ""))
        if not payload:
            raise RuntimeError("模型输出无法解析")
        # 模型判定"没内容"时会返回空 one_line + skip:true，这是合法结果，不算失败
        if not payload.get("one_line"):
            payload["one_line"] = (video["title"] or "")[:40]
        payload["engine"] = "ollama"
        payload["elapsed"] = round(time.time() - t0, 1)
        return payload
