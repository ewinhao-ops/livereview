"""Observation-session heat slicing and idempotent rhythm-template extraction."""

from __future__ import annotations

import asyncio
import json
import math
import os
import statistics
import subprocess
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select

from ..db import SessionLocal
from ..models import (
    AudioStatus,
    ExperienceCorpus,
    ObservationAnalysis,
    ObservationHeatSlice,
    OcrRecord,
    ReplaySession,
    SessionStatus,
    SessionType,
    Transcript,
)
from .safe_error import safe_error_text, safe_exception_code


WINDOW_MS = 60_000
STEP_MS = 30_000
_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class ObservationStateError(RuntimeError):
    pass


def _session_is_observation(row: ReplaySession) -> bool:
    return row.session_type == SessionType.observation or str(row.session_type) == "observation"


def _danmaku_user(text: str) -> str:
    value = str(text or "").strip()
    for separator in ("：", ":"):
        if separator in value:
            name = value.split(separator, 1)[0].strip()
            return name[:80] or "unknown"
    return "unknown"


def calculate_heat_slices(session_id: int) -> dict:
    """Pure deterministic 60s/30s heat calculation with overlap merging."""
    with SessionLocal() as db:
        session = db.get(ReplaySession, session_id)
        if session is None:
            raise LookupError("场次不存在")
        if not _session_is_observation(session):
            raise ObservationStateError("仅观摩学习场次支持弹幕热度分析")
        danmaku = db.execute(select(OcrRecord).where(
            OcrRecord.session_id == session_id,
        ).order_by(OcrRecord.start_ms, OcrRecord.id)).scalars().all()
        transcripts = db.execute(select(Transcript).where(
            Transcript.session_id == session_id,
        ).order_by(Transcript.start_ms, Transcript.seq, Transcript.id)).scalars().all()
        audio = session.audio
        audio_available = bool(
            audio and audio.status in (AudioStatus.available, AudioStatus.recording)
            and (audio.size_bytes > 0 or audio.path or audio.cos_key)
        )

    if not danmaku:
        return {"reliable": False, "message": "没有可靠高互动片段", "slices": []}
    events = [
        {"at": max(0, int(row.start_ms or 0)), "user": _danmaku_user(row.text)}
        for row in danmaku
    ]
    total_users = len({event["user"] for event in events})
    if len(events) < 4:
        return {"reliable": False, "message": "没有可靠高互动片段", "slices": []}

    first = (min(event["at"] for event in events) // STEP_MS) * STEP_MS
    last = max(event["at"] for event in events)
    windows: list[dict] = []
    previous_count = 0
    start = first
    while start <= last:
        end = start + WINDOW_MS
        selected = [event for event in events if start <= event["at"] < end]
        count = len(selected)
        users = len({event["user"] for event in selected})
        growth = (count - previous_count) / max(1, previous_count)
        score = count + users * 1.5 + min(4.0, max(0.0, growth)) * 2.0
        windows.append({
            "start_ms": start,
            "end_ms": end,
            "danmaku_count": count,
            "unique_user_count": users,
            "relative_growth": round(growth, 4),
            "heat_score": round(score, 4),
        })
        previous_count = count
        start += STEP_MS

    baseline = statistics.median(window["danmaku_count"] for window in windows)
    threshold = max(4, math.ceil(baseline))
    hot = [
        window for window in windows
        if window["danmaku_count"] >= threshold
        and (window["unique_user_count"] >= 2 or window["danmaku_count"] >= 8)
    ]
    # A small room can still have a real peak when all four messages are from distinct users.
    if not hot and total_users >= 3:
        peak = max(windows, key=lambda item: (item["heat_score"], -item["start_ms"]))
        if peak["danmaku_count"] >= 4:
            hot = [peak]
    if not hot:
        return {"reliable": False, "message": "没有可靠高互动片段", "slices": []}

    merged: list[dict] = []
    for window in hot:
        if merged and window["start_ms"] < merged[-1]["end_ms"]:
            merged[-1]["end_ms"] = max(merged[-1]["end_ms"], window["end_ms"])
            merged[-1]["relative_growth"] = max(
                merged[-1]["relative_growth"], window["relative_growth"],
            )
            merged[-1]["heat_score"] = max(merged[-1]["heat_score"], window["heat_score"])
        else:
            merged.append(dict(window))

    result: list[dict] = []
    for segment in merged:
        selected_events = [
            event for event in events
            if segment["start_ms"] <= event["at"] < segment["end_ms"]
        ]
        selected_transcripts = [
            row for row in transcripts
            if int(row.end_ms or row.start_ms or 0) >= segment["start_ms"]
            and int(row.start_ms or 0) < segment["end_ms"]
        ]
        script = "\n".join(
            (row.corrected_text or row.raw_text or "").strip()
            for row in selected_transcripts
            if (row.corrected_text or row.raw_text or "").strip()
        )[:12_000]
        result.append({
            "start_ms": segment["start_ms"],
            "end_ms": segment["end_ms"],
            "danmaku_count": len(selected_events),
            "unique_user_count": len({event["user"] for event in selected_events}),
            "relative_growth": round(segment["relative_growth"], 4),
            "heat_score": round(segment["heat_score"], 4),
            "transcript_ids": [row.id for row in selected_transcripts],
            "corresponding_script": script,
            "audio_at_ms": segment["start_ms"] if audio_available else None,
        })
    return {"reliable": True, "message": "", "slices": result}


def _slice_dict(row: ObservationHeatSlice) -> dict:
    try:
        transcript_ids = json.loads(row.transcript_ids or "[]")
    except (json.JSONDecodeError, TypeError):
        transcript_ids = []
    return {
        "id": row.id,
        "start_ms": row.start_ms,
        "end_ms": row.end_ms,
        "danmaku_count": row.danmaku_count,
        "unique_user_count": row.unique_user_count,
        "relative_growth": row.relative_growth,
        "heat_score": row.heat_score,
        "transcript_ids": transcript_ids,
        "corresponding_script": row.corresponding_script,
        "audio_at_ms": row.audio_at_ms,
    }


def compute_and_store_heat(session_id: int, *, update_status: bool = True) -> dict:
    with SessionLocal() as db:
        analysis = db.execute(select(ObservationAnalysis).where(
            ObservationAnalysis.session_id == session_id,
        )).scalars().first()
        if analysis is not None and analysis.status in {"ready", "processing"}:
            rows = db.execute(select(ObservationHeatSlice).where(
                ObservationHeatSlice.analysis_id == analysis.id,
            ).order_by(
                ObservationHeatSlice.start_ms, ObservationHeatSlice.id,
            )).scalars().all()
            return {
                "analysis_id": analysis.id,
                "reliable": bool(rows),
                "message": "" if rows else analysis.error_msg,
                "slices": [_slice_dict(row) for row in rows],
            }

    calculated = calculate_heat_slices(session_id)
    with SessionLocal() as db:
        analysis = db.execute(select(ObservationAnalysis).where(
            ObservationAnalysis.session_id == session_id,
        )).scalars().first()
        if analysis is None:
            analysis = ObservationAnalysis(session_id=session_id, status="pending")
            db.add(analysis)
            db.flush()
        db.execute(delete(ObservationHeatSlice).where(
            ObservationHeatSlice.analysis_id == analysis.id,
        ))
        for item in calculated["slices"]:
            db.add(ObservationHeatSlice(
                analysis_id=analysis.id,
                session_id=session_id,
                start_ms=item["start_ms"],
                end_ms=item["end_ms"],
                danmaku_count=item["danmaku_count"],
                unique_user_count=item["unique_user_count"],
                relative_growth=item["relative_growth"],
                heat_score=item["heat_score"],
                transcript_ids=json.dumps(item["transcript_ids"]),
                corresponding_script=item["corresponding_script"],
                audio_at_ms=item["audio_at_ms"],
            ))
        if update_status and analysis.status not in {"ready", "processing"}:
            analysis.status = "pending" if calculated["reliable"] else "no_data"
            analysis.error_code = "" if calculated["reliable"] else "OBSERVATION_NO_RELIABLE_SLICE"
            analysis.error_msg = calculated["message"]
        analysis.updated_at = datetime.now()
        db.commit()
        db.refresh(analysis)
        rows = db.execute(select(ObservationHeatSlice).where(
            ObservationHeatSlice.analysis_id == analysis.id,
        ).order_by(ObservationHeatSlice.start_ms, ObservationHeatSlice.id)).scalars().all()
        return {
            "analysis_id": analysis.id,
            "reliable": calculated["reliable"],
            "message": calculated["message"],
            "slices": [_slice_dict(row) for row in rows],
        }


def _analysis_dict(session_id: int) -> dict:
    with SessionLocal() as db:
        session = db.get(ReplaySession, session_id)
        if session is None:
            raise LookupError("场次不存在")
        if not _session_is_observation(session):
            raise ObservationStateError("仅观摩学习场次支持提炼")
        row = db.execute(select(ObservationAnalysis).where(
            ObservationAnalysis.session_id == session_id,
        )).scalars().first()
        if row is None:
            return {
                "session_id": session_id, "status": "pending", "corpus_id": None,
                "heat_slices": [], "templates": [], "include_emotion": False,
                "emotion_generated": False, "emotion_status": "disabled",
                "emotion": [], "error_code": "", "error_message": "",
            }
        slices = db.execute(select(ObservationHeatSlice).where(
            ObservationHeatSlice.analysis_id == row.id,
        ).order_by(ObservationHeatSlice.start_ms, ObservationHeatSlice.id)).scalars().all()
        try:
            emotion = json.loads(row.emotion_data or "[]")
        except (json.JSONDecodeError, TypeError):
            emotion = []
        corpus_id = row.corpus_id
        result = {
            "session_id": session_id,
            "analysis_id": row.id,
            "status": row.status,
            "corpus_id": corpus_id,
            "heat_slices": [_slice_dict(item) for item in slices],
            "include_emotion": bool(row.include_emotion),
            "emotion_generated": bool(row.emotion_generated),
            "emotion_status": row.emotion_status,
            "emotion": emotion if isinstance(emotion, list) else [],
            "error_code": row.error_code,
            "error_message": row.error_msg,
            "created_at": row.created_at.isoformat(timespec="seconds"),
            "updated_at": row.updated_at.isoformat(timespec="seconds"),
        }
    if corpus_id is not None:
        from .rhythm_library import list_templates
        result["templates"] = list_templates(corpus_id=corpus_id)
    else:
        result["templates"] = []
    return result


def get_analysis(session_id: int) -> dict:
    return _analysis_dict(session_id)


def _emotion_for_slices(session_id: int, slices: list[dict]) -> tuple[str, list[dict]]:
    from . import qwen_emotion
    from .emotion_recompute import _audio_source

    if not qwen_emotion.enabled():
        return "unavailable", []
    with SessionLocal() as db:
        session = db.get(ReplaySession, session_id)
        source = _audio_source(session.audio) if session and session.audio else None
    if not source:
        return "no_audio", []
    generated: list[dict] = []
    with tempfile.TemporaryDirectory(prefix=f"obs_emo_{session_id}_") as tmpdir:
        for index, item in enumerate(slices):
            duration = min(300.0, max(1.0, (item["end_ms"] - item["start_ms"]) / 1000.0))
            output = os.path.join(tmpdir, f"slice_{index}.wav")
            command = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{item['start_ms'] / 1000.0:.3f}", "-i", source,
                "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", "16000", output,
            ]
            try:
                completed = subprocess.run(command, capture_output=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if completed.returncode != 0 or not Path(output).exists() or Path(output).stat().st_size <= 0:
                continue
            value = qwen_emotion.recognize_emotion(output)
            if value:
                generated.append({
                    "start_ms": item["start_ms"],
                    "end_ms": item["end_ms"],
                    "emotion": value.get("emotion", ""),
                    "emotion_label": value.get("emotion_label", ""),
                    "generated_by_model": True,
                })
    return ("generated" if generated else "failed"), generated


async def extract_session(
    session_id: int, *, include_emotion: bool = False, retry: bool = False,
) -> dict:
    async with _locks[session_id]:
        current = _analysis_dict(session_id)
        with SessionLocal() as db:
            session = db.get(ReplaySession, session_id)
            if session.status in {
                SessionStatus.created, SessionStatus.parsing,
                SessionStatus.live, SessionStatus.processing,
            } or session.ended_at is None:
                raise ObservationStateError("观摩场次停止后才可提炼")
        if current["status"] == "ready":
            return {**current, "duplicate": True}
        if current["status"] == "processing":
            raise ObservationStateError("观摩提炼正在执行，请勿重复提交")

        heat = compute_and_store_heat(session_id, update_status=False)
        selected = [item for item in heat["slices"] if item["corresponding_script"].strip()]
        transcript_text = "\n\n".join(
            f"[{item['start_ms'] / 1000:.0f}-{item['end_ms'] / 1000:.0f}秒]\n"
            f"{item['corresponding_script']}"
            for item in selected
        ).strip()
        if not heat["reliable"] or len(transcript_text) < 20:
            with SessionLocal() as db:
                row = db.execute(select(ObservationAnalysis).where(
                    ObservationAnalysis.session_id == session_id,
                )).scalars().one()
                row.status = "no_data"
                row.run_id = ""
                row.error_code = (
                    "OBSERVATION_NO_RELIABLE_SLICE" if not heat["reliable"]
                    else "OBSERVATION_TRANSCRIPT_MISSING"
                )
                row.error_msg = (
                    heat["message"] if not heat["reliable"]
                    else "高互动片段缺少可提炼的逐字稿"
                )
                row.include_emotion = include_emotion
                row.emotion_status = "disabled" if not include_emotion else "not_run"
                row.updated_at = datetime.now()
                db.commit()
            return _analysis_dict(session_id)

        run_id = uuid.uuid4().hex
        with SessionLocal() as db:
            row = db.execute(select(ObservationAnalysis).where(
                ObservationAnalysis.session_id == session_id,
            )).scalars().one()
            row.status = "processing"
            row.run_id = run_id
            row.error_code = ""
            row.error_msg = ""
            row.include_emotion = include_emotion
            row.emotion_generated = False
            row.emotion_status = "pending" if include_emotion else "disabled"
            row.emotion_data = "[]"
            row.updated_at = datetime.now()
            existing_corpus_id = row.corpus_id
            if retry and existing_corpus_id is None:
                existing_corpus_id = db.execute(select(ExperienceCorpus.id).where(
                    ExperienceCorpus.origin_type == "observation_session",
                    ExperienceCorpus.origin_id == session_id,
                )).scalar_one_or_none()
                if existing_corpus_id is not None:
                    row.corpus_id = existing_corpus_id
            db.commit()

        metadata = {
            "source_session_id": session_id,
            "ranges": [
                {
                    "start_ms": item["start_ms"], "end_ms": item["end_ms"],
                    "transcript_ids": item["transcript_ids"],
                    "danmaku_count": item["danmaku_count"],
                    "unique_user_count": item["unique_user_count"],
                    "heat_score": item["heat_score"],
                }
                for item in selected
            ],
            "interaction_claim": "高互动片段；不代表高成交",
        }
        try:
            from . import rhythm_library
            if retry and existing_corpus_id is not None:
                result = await rhythm_library.retry_corpus(existing_corpus_id)
            else:
                result = await rhythm_library.ingest_corpus(
                    f"[观摩学习] 场次 {session_id}", "live_skill", transcript_text,
                    origin_type="observation_session", origin_id=session_id,
                    source_metadata=metadata,
                    content_hash_salt=f"observation:{session_id}",
                )
            corpus = result.get("corpus") or {}
            corpus_id = corpus.get("id")
            if corpus_id is not None:
                with SessionLocal() as db:
                    claimed = db.execute(select(ObservationAnalysis).where(
                        ObservationAnalysis.session_id == session_id,
                        ObservationAnalysis.run_id == run_id,
                    )).scalars().first()
                    if claimed is not None:
                        claimed.corpus_id = corpus_id
                        db.commit()
            if corpus.get("status") != "ready":
                raise ObservationStateError(corpus.get("error_msg") or "节奏提炼未产出完整模板")
            emotion_status, emotion_data = ("disabled", [])
            if include_emotion:
                try:
                    emotion_status, emotion_data = await asyncio.to_thread(
                        _emotion_for_slices, session_id, selected,
                    )
                except Exception:  # noqa: BLE001
                    emotion_status, emotion_data = "failed", []
            with SessionLocal() as db:
                row = db.execute(select(ObservationAnalysis).where(
                    ObservationAnalysis.session_id == session_id,
                    ObservationAnalysis.run_id == run_id,
                )).scalars().first()
                if row is None:
                    raise ObservationStateError("观摩提炼任务已被新的执行替代")
                row.status = "ready"
                row.run_id = ""
                row.corpus_id = corpus_id
                row.emotion_status = emotion_status
                row.emotion_generated = bool(emotion_data)
                row.emotion_data = json.dumps(emotion_data, ensure_ascii=False)
                row.updated_at = datetime.now()
                db.commit()
        except Exception as exc:
            code = safe_exception_code(exc, prefix="OBSERVATION_EXTRACT")
            with SessionLocal() as db:
                row = db.execute(select(ObservationAnalysis).where(
                    ObservationAnalysis.session_id == session_id,
                    ObservationAnalysis.run_id == run_id,
                )).scalars().first()
                if row is not None:
                    row.status = "failed"
                    row.run_id = ""
                    row.error_code = code
                    row.error_msg = safe_error_text(exc, fallback="观摩提炼失败，请重试")
                    row.updated_at = datetime.now()
                    db.commit()
            raise
        return _analysis_dict(session_id)
