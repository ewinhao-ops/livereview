"""R7 semantic real-time review, reusing the R2/R3 LLM and RAG stack."""
from __future__ import annotations

import json

from .embed import retrieve_detailed
from .llm import get_llm

_SYSTEM = """你是直播实时质检员。只识别必须立刻提醒主播的重大问题：
1. 违禁、绝对化、无法证实的承诺；2. 与知识库冲突的产品型号、参数、适配信息（价格随直播活动变化，不做核对）。
普通表达、节奏、语气等播后问题不要提醒。知识库没有依据时不得猜测参数错误。
只输出 JSON：{"alert":null}，或
{"alert":{"kind":"compliance|product_error","severity":"warn|error",
"title":"短标题","message":"问题说明","quote":"问题原话","suggestion":"可直接照说的修正话术"}}。"""


async def analyze_realtime_sentence(text: str, recent_context: list[str]) -> dict | None:
    if not text.strip():
        return None
    refs = await retrieve_detailed(
        text, top_k=6, exclude_categories=("优秀节奏", "本店经验")
    )
    facts = "\n".join(f"- {item['text']}" for item in refs) or "（无相关产品事实）"
    context = "\n".join(recent_context[-3:]) or "（无）"
    user = f"【最近话术】\n{context}\n\n【当前句】\n{text}\n\n【知识库事实】\n{facts}"
    raw = await get_llm("fast").complete(_SYSTEM, user, json_mode=True)
    try:
        alert = (json.loads(raw) or {}).get("alert")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(alert, dict) or alert.get("kind") not in {"compliance", "product_error"}:
        return None
    alert["severity"] = alert.get("severity") if alert.get("severity") in {"warn", "error"} else "warn"
    for key in ("title", "message", "quote", "suggestion"):
        alert[key] = str(alert.get(key) or "")[:500]
    return alert if alert["message"] else None
