"""R8 emotion-context alignment using Qwen emotion labels + transcript + danmaku."""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Critique, CritiqueLevel, OcrRecord, Transcript
from .llm import get_llm

_SYSTEM = """你是直播情绪教练。判断主播实际语气是否匹配当时的话术内容和观众弹幕语境。
必须以提供的实际情绪标签为准，不得臆测实际情绪。先判断此刻理应采用什么情绪，再作对比。
注意：讲参数可以沉稳；回答投诉应冷静共情；讲福利/成交应热情有推动力；观众夸赞时应开心认同；
普通信息段允许中性。只有明显影响信任、互动或转化时才判不匹配，避免过度纠错。
只输出 JSON：{"expected_emotion":"应有情绪≤20字","matched":true,
"confidence":0.0,"reason":"结合话术和弹幕的理由≤80字","suggestion":"具体可执行建议≤60字"}。"""


def _loads(raw: str) -> dict:
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


async def analyze_emotion_context(*, actual_emotion: str, transcript: list[str],
                                  danmaku: list[str], scene: str = "") -> dict | None:
    actual = (actual_emotion or "").strip()
    if not actual or not transcript:
        return None
    user = (f"【场景】{scene or '通用'}\n【实际情绪】{actual}\n"
            f"【主播话术】\n" + "\n".join(transcript[-8:])[:1800] +
            "\n【同时段弹幕】\n" + ("\n".join(danmaku[-12:])[:1000] or "（无）"))
    raw = await get_llm("fast").complete(_SYSTEM, user, json_mode=True)
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(result, dict) or not isinstance(result.get("matched"), bool):
        return None
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "actual_emotion": actual,
        "expected_emotion": str(result.get("expected_emotion") or "")[:40],
        "matched": result["matched"],
        "confidence": confidence,
        "reason": str(result.get("reason") or "")[:200],
        "suggestion": str(result.get("suggestion") or "")[:160],
        "transcript_context": " ".join(transcript[-3:])[:300],
        "danmaku_context": "；".join(danmaku[-5:])[:300],
    }


def recompute_session_contexts(session_id: int, streamer_id: int | None = None) -> dict:
    """Rebuild R8 results after offline Qwen emotion recomputation."""
    with SessionLocal() as db:
        q = select(Critique).where(
            Critique.session_id == session_id, Critique.level == CritiqueLevel.segment)
        if streamer_id is not None:
            q = q.where(Critique.streamer_id == streamer_id)
        segments = db.execute(q.order_by(Critique.start_ms)).scalars().all()
        updated = mismatches = 0
        for segment in segments:
            data = _loads(segment.data)
            actual = data.get("asr_emotion_label") or data.get("emotion") or ""
            transcripts = db.execute(select(Transcript).where(
                Transcript.session_id == session_id,
                Transcript.start_ms <= segment.end_ms,
                Transcript.end_ms >= segment.start_ms,
            ).order_by(Transcript.start_ms)).scalars().all()
            danmaku = db.execute(select(OcrRecord).where(
                OcrRecord.session_id == session_id,
                OcrRecord.start_ms <= segment.end_ms,
                OcrRecord.end_ms >= segment.start_ms,
            ).order_by(OcrRecord.start_ms)).scalars().all()
            result = asyncio.run(analyze_emotion_context(
                actual_emotion=actual,
                transcript=[x.corrected_text for x in transcripts],
                danmaku=[x.text for x in danmaku],
                scene=data.get("scene_label") or data.get("scene") or "",
            ))
            if result is None:
                continue
            data["emotion_match"] = result
            segment.data = json.dumps(data, ensure_ascii=False)
            updated += 1
            if not result["matched"]:
                mismatches += 1
        db.commit()
    return {"updated": updated, "mismatches": mismatches}
