"""LLM 文字校正。

ASR 定句后调用：修正同音错字、把口语型号规范成知识库标准名、补标点。
传入"术语表"作为约束，避免把本来正确的专有名词改错。
mock LLM 直接回传原文；接入真实模型后才真正校正。
"""

from __future__ import annotations

from .llm import get_llm

_SYSTEM = (
    "你是直播话术转写校正助手。任务：把语音识别(ASR)的口语化句子做轻量校正——"
    "纠正明显的同音错别字、补全标点、并把产品型号规范为给定术语表中的标准写法。"
    "严格保持原意，不要增删信息、不要润色语气。只输出校正后的句子本身。"
)


async def correct_sentence(raw_text: str, glossary: list[str] | None = None) -> str:
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return ""
    terms = "、".join(glossary or []) or "（无）"
    user = (
        f"术语表(标准型号/专有名词，遇到同音或口语写法请对齐到这些)：{terms}\n"
        f"【待校正】{raw_text}"
    )
    llm = get_llm("fast")
    out = await llm.complete(_SYSTEM, user, json_mode=False)
    return (out or raw_text).strip()
