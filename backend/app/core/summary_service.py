"""Authoritative, reusable whole-session summary generation service."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select, update

from ..db import SessionLocal
from ..models import (
    Critique, CritiqueLevel, LiveAlert, ReplaySession, SessionStatus, Severity, Streamer,
    StreamerTurn, Transcript,
)
from .review.asr_fix import fix_text
from .review.summary import review_summary

_inflight: dict[tuple[int, int | None], asyncio.Task[dict]] = {}

# The fields review_summary produces. Regeneration replaces exactly these. Everything else in
# the row's data was attached afterwards by someone else -- the v2 deep review, emotion_overview
# and rhythm_analysis (via merge_summary_data), prev_compare / missed_summary / examples on
# older sessions -- and has to survive: a v2 takes minutes and a paid model run to rebuild.
_SUMMARY_KEYS = frozenset({
    "overall_score", "dimensions", "summary", "emotion", "structure_review", "top_issues",
    "action_items", "experience_citations", "experience_sources", "personalized_experience",
    "emotion_context", "structure", "loop", "llm", "llm_requested", "is_fallback",
    "danmaku_captured", "data_notes",
})
_STORE_RETRIES = 5
# 弹幕没采集到（2026-10-03）：给模型的输入里加这一句，报告里注明，免得批评主播「没有公屏互动」
DANMAKU_MISSING_NOTE = "本场弹幕没有采集到，不要评价公屏互动"
DANMAKU_MISSING_LABEL = "本场弹幕未采集到"


def _loads(value: str | None) -> dict:
    try:
        result = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def _read_inputs(session_id: int, streamer_id: int | None) -> dict:
    with SessionLocal() as db:
        session = db.get(ReplaySession, session_id)
        if session is None:
            raise ValueError("场次不存在")
        if streamer_id is not None and db.get(Streamer, streamer_id) is None:
            raise ValueError("主播不存在")
        owner = Transcript.streamer_id == streamer_id
        transcripts = db.execute(select(Transcript).where(
            Transcript.session_id == session_id, owner,
        ).order_by(Transcript.seq)).scalars().all()
        critiques = db.execute(select(Critique).where(
            Critique.session_id == session_id,
            Critique.streamer_id == streamer_id,
            Critique.level != CritiqueLevel.summary,
        ).order_by(Critique.start_ms)).scalars().all()
        alerts = []
        if streamer_id is not None:
            alerts = db.execute(select(LiveAlert).where(
                LiveAlert.session_id == session_id,
                LiveAlert.streamer_id == streamer_id,
            ).order_by(LiveAlert.at_ms)).scalars().all()
        if not transcripts and not critiques:
            raise ValueError("本场尚无逐字稿或有效点评，不能生成整场总结")
        # pending = 抓取中一直没采到（直播中换班时出总结），missing = 整场都没采到
        danmaku_missing = (getattr(session, "danmaku_status", "") or "") in ("pending", "missing")
        # 型号昵称的识别错字（中额→中鹅、叉S→XS）先改回来，免得被当成主播口误（2026-10-04）
        digest = "\n".join(fix_text((row.corrected_text or row.raw_text).strip()) for row in transcripts if (row.corrected_text or row.raw_text).strip())
        turns = [(int(t.start_ms or 0), int(t.end_ms or 0)) for t in db.execute(select(StreamerTurn).where(
            StreamerTurn.session_id == session_id, StreamerTurn.streamer_id == streamer_id,
        ).order_by(StreamerTurn.start_ms)).scalars()] if streamer_id is not None else []
        long_gaps = _long_gaps(transcripts, turns)
        segment_cards, flags, emotions, scenes, loops, metrics = [], [], [], [], [], []
        for row in critiques:
            data = _loads(row.data)
            if row.level == CritiqueLevel.segment:
                segment_cards.append(fix_text(row.comment or ""))
                if data.get("emotion"):
                    emotions.append(str(data["emotion"]))
                if data.get("scene_label"):
                    scenes.append(str(data["scene_label"]))
                if isinstance(data.get("loop"), dict):
                    loops.append(data["loop"])
                if isinstance(data.get("metrics"), dict):
                    metrics.append(data["metrics"])
            elif row.severity in (Severity.warn, Severity.error):
                flags.append(fix_text(row.comment or row.quote or row.category))
        flags.extend(fix_text(a.message or a.quote or a.title) for a in alerts)
        return {
            "digest": digest[:24_000], "flags": flags, "segment_cards": segment_cards,
            "emotions": emotions, "scenes": scenes, "loops": loops, "metrics": metrics,
            "danmaku_missing": danmaku_missing, "long_gaps": long_gaps,
        }


LONG_GAP_STEPS = (45, 90)    # 总结里说冷场只看长空白（和改进动作的冷场秒数同一套算法），3 秒级停顿是正常说话节奏


def _long_gaps(transcripts, turns: list[tuple[int, int]]) -> dict:
    """这位主播相邻两句之间的长空白（跨班次的不算；3 分钟以上多半是放歌试听或离场，单独计）。"""
    from .review import focus_store
    from .review.focus import Sent, find_gaps
    sentences = sorted((Sent(int(t.start_ms or 0), max(int(t.start_ms or 0), int(t.end_ms or 0)),
                             (t.corrected_text or t.raw_text or "").strip())
                        for t in transcripts if (t.corrected_text or t.raw_text or "").strip()),
                       key=lambda x: x.start_ms)
    cap = focus_store.SILENCE_CAP_SEC
    gaps = find_gaps(sentences, turns)
    inside = [g.sec for g in gaps if g.sec <= cap]
    out = {f"over_{n}": sum(1 for sec in inside if sec >= n) for n in LONG_GAP_STEPS}
    out.update({"max_sec": round(max(inside, default=0)), "over_cap": sum(1 for g in gaps if g.sec > cap),
                "cap_sec": cap})
    return out


def _mark_danmaku_missing(data: dict) -> None:
    """报告里注明弹幕没采集到：结构化字段 + 总评开头一句（页面不用改就能看到）。"""
    data["danmaku_captured"] = False
    data["data_notes"] = [DANMAKU_MISSING_LABEL]
    summary = str(data.get("summary") or "")
    if DANMAKU_MISSING_LABEL not in summary:
        data["summary"] = f"（{DANMAKU_MISSING_LABEL}，公屏互动不作评价）{summary}"


def _carry_over(new: dict, old: dict) -> dict:
    """The new summary plus the non-summary content already attached to the stored report."""
    kept = {key: value for key, value in old.items()
            if key not in _SUMMARY_KEYS and key not in new}
    return {**new, **kept}


def _store_summary(session_id: int, streamer_id: int | None, data: dict) -> None:
    columns = {
        "comment": data.get("summary", ""),
        "score": data.get("overall_score"),
        "llm_provider": data.get("llm", ""),
        "llm_requested_provider": data.get("llm_requested", ""),
        "is_fallback": bool(data.get("is_fallback")),
    }
    for _ in range(_STORE_RETRIES):
        with SessionLocal() as db:
            # Only 整场总结 rows are ours; the owner's other summary-level rows (罗盘归因) are not.
            rows = db.execute(select(Critique).where(
                Critique.session_id == session_id,
                Critique.streamer_id == streamer_id,
                Critique.level == CritiqueLevel.summary,
                Critique.category == "整场总结",
            ).order_by(Critique.id)).scalars().all()
            if not rows:
                db.add(Critique(
                    session_id=session_id, streamer_id=streamer_id,
                    level=CritiqueLevel.summary, severity=Severity.info,
                    category="整场总结", start_ms=0, end_ms=0, quote="",
                    data=json.dumps(data, ensure_ascii=False), **columns,
                ))
                db.commit()
                return
            old = rows[0].data
            merged = json.dumps(_carry_over(data, _loads(old)), ensure_ascii=False)
            # Other tasks merge into this JSON text from their own threads. Swap only if it is
            # still the text we read (same optimistic lock as merge_summary_data), else re-read.
            swapped = db.execute(
                update(Critique)
                .where(Critique.id == rows[0].id, Critique.data == old)
                .values(data=merged, **columns)
            )
            if swapped.rowcount != 1:
                db.rollback()
                continue
            for duplicate in rows[1:]:
                db.delete(duplicate)
            db.commit()
            return
    raise RuntimeError("整场报告正在被其他分析任务更新，请稍后重试")


async def _generate_summary_once(session_id: int, streamer_id: int | None) -> dict:
    inputs = _read_inputs(session_id, streamer_id)
    from .review.rhythm import aggregate_metrics
    rhythm = aggregate_metrics(inputs["metrics"])
    if isinstance(rhythm, dict):
        rhythm = {**rhythm, "long_gaps": inputs["long_gaps"]}
    data = await review_summary(
        inputs["digest"], inputs["flags"], inputs["segment_cards"],
        rhythm=rhythm, emotions=inputs["emotions"],
        scenes=inputs["scenes"], loops=inputs["loops"],
        notes=[DANMAKU_MISSING_NOTE] if inputs["danmaku_missing"] else [],
    )
    if inputs["danmaku_missing"]:
        _mark_danmaku_missing(data)
    # The model call has succeeded. Only now mutate storage, so an old report survives failures.
    _store_summary(session_id, streamer_id, data)
    refresh_report_status(session_id)
    return data


_STALE_ERRORS = ("SUMMARY_GENERATION_FAILED", "REPORT_INCOMPLETE", "PIPELINE_FINALIZE_INCOMPLETE")


def refresh_report_status(session_id: int) -> bool:
    """收播时总结没出来、报告记成 partial；后来重新生成齐了要改回 ready（2026-10-04：以前一直挂着
    「报告不完整，可重试」）。只动「已结束 + partial + 是这几种失败原因」的场次。返回是否改了。"""
    with SessionLocal() as db:
        row = db.get(ReplaySession, session_id)
        if row is None or row.status != SessionStatus.done or row.report_status != "partial":
            return False
        if (row.error_code or "") not in _STALE_ERRORS:
            return False
        talked = {sid for (sid,) in db.execute(select(Transcript.streamer_id).where(
            Transcript.session_id == session_id, Transcript.streamer_id.is_not(None)).distinct())}
        summarized = {sid for (sid,) in db.execute(select(Critique.streamer_id).where(
            Critique.session_id == session_id, Critique.level == CritiqueLevel.summary,
            Critique.category == "整场总结").distinct())}
        if not talked or not talked <= summarized:
            return False
        row.report_status = "ready"
        row.error_code = ""
        row.error_msg = ""
        db.commit()
        return True


async def generate_summary(session_id: int, streamer_id: int | None) -> dict:
    """Single-flight generation followed by one atomic authoritative-row update."""
    key = (session_id, streamer_id)
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_generate_summary_once(session_id, streamer_id))
        _inflight[key] = task

        def release(done: asyncio.Task[dict]) -> None:
            if _inflight.get(key) is done:
                _inflight.pop(key, None)

        task.add_done_callback(release)
    return await asyncio.shield(task)
