"""Crash/restart recovery for orphaned live sessions.

Recovery is deliberately local and idempotent: it may finalize existing media and
reuse already persisted reports, but it never invokes ASR, an LLM, or report
generation.  Incomplete data is kept and the session enters an explicit error
state instead of being stuck in ``live``/``processing`` or being marked done.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from ..config import get_settings
from ..db import SessionLocal
from ..models import AudioStatus, Critique, CritiqueLevel, ReplaySession, SessionStatus
from . import media_store
from .safe_error import safe_error_text, safe_exception_code


_recovery_tasks: dict[int, asyncio.Task[None]] = {}


def _probe_readable(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, timeout=5, check=False,
        )
        return probe.returncode == 0 and bool(probe.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _mark_recovery_error(sid: int, code: str, message: str) -> None:
    with SessionLocal() as db:
        row = db.get(ReplaySession, sid)
        if not row or row.status in (SessionStatus.done, SessionStatus.error):
            return
        row.status = SessionStatus.error
        row.error_code = code[:64]
        row.error_msg = message
        row.ended_at = row.ended_at or datetime.now()
        if row.audio and row.audio.status == AudioStatus.recording:
            row.audio.status = AudioStatus.error
        db.commit()


async def _finalize_orphan(sid: int) -> None:
    """Finalize one orphan once, without any paid/model call."""
    with SessionLocal() as db:
        session = db.get(ReplaySession, sid)
        if not session or session.status in (SessionStatus.done, SessionStatus.error):
            return

        audio_dir = get_settings().audio_path
        audio = session.audio
        hls_dir = audio_dir / f"session_{sid}_hls"
        hls_key = ""
        if (audio and (audio.cos_key or "").endswith(".m3u8")) or hls_dir.exists():
            from .hls_uploader import HlsUploader

            uploader = HlsUploader(
                sid, hls_dir if hls_dir.exists() else audio_dir,
                f"replay/session_{sid}",
            )
            uploader.resume_from_cos()
            hls_ok, segment_count = await uploader.finalize()
            if hls_ok:
                hls_key = uploader.m3u8_key
                print(
                    f"[recovery] session={sid}; media=hls; segments={segment_count}; "
                    "status=finalized"
                )

        # Existing final audio is preferred.  Parts are only removed after a
        # readable merged output exists, so recovery failure preserves evidence.
        ext = (
            audio.filename.rsplit(".", 1)[-1]
            if audio and "." in (audio.filename or "") else "m4a"
        )
        configured_final = Path(audio.path) if audio and audio.path else None
        final = (
            configured_final if configured_final and configured_final.is_file()
            else audio_dir / f"session_{sid}.{ext}"
        )
        all_parts = sorted(audio_dir.glob(f"session_{sid}.part*"))
        parts = [part for part in all_parts if part.is_file() and part.stat().st_size > 0]

        readable = _probe_readable(final)
        if parts and not readable:
            concat_file = audio_dir / f"session_{sid}.concat.txt"
            concat_file.write_text(
                "\n".join(f"file '{part.as_posix()}'" for part in parts),
                encoding="utf-8",
            )
            try:
                merged = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "concat",
                     "-safe", "0", "-i", str(concat_file), "-c", "copy",
                     "-movflags", "+faststart", "-y", str(final)],
                    capture_output=True, check=False, timeout=30,
                )
                readable = merged.returncode == 0 and _probe_readable(final)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
                print(
                    f"[recovery] session={sid}; code="
                    f"{safe_exception_code(exc, prefix='MEDIA_MERGE')}; "
                    f"error={safe_error_text(exc)}"
                )
            finally:
                concat_file.unlink(missing_ok=True)

        size = final.stat().st_size if readable and final.is_file() else 0
        remote_key = ""
        if readable and media_store.enabled():
            key = f"replay/session_{sid}.{ext}" if not hls_key else f"replay/session_{sid}.m4a"
            if await asyncio.to_thread(media_store.upload_file, str(final), key):
                remote_key = key if not hls_key else ""
                final.unlink(missing_ok=True)

        if readable:
            for part in parts:
                part.unlink(missing_ok=True)

        intentionally_deleted = bool(audio and audio.status == AudioStatus.deleted)
        media_ready = intentionally_deleted or readable or bool(hls_key) or bool(
            audio and audio.cos_key and audio.status == AudioStatus.available
        )
        if audio and not intentionally_deleted:
            audio.status = AudioStatus.available if media_ready else AudioStatus.error
            audio.size_bytes = size or (1 if hls_key else audio.size_bytes or 0)
            if hls_key:
                audio.cos_key = hls_key
            elif remote_key:
                audio.cos_key = remote_key

        # A restart must never duplicate a real LLM/report call.  A persisted
        # summary can be reused; a missing one is an explicit recoverable failure.
        report_ready = bool(
            db.query(Critique).filter_by(
                session_id=sid, level=CritiqueLevel.summary,
            ).first()
        )
        session.ended_at = session.ended_at or datetime.now()
        if media_ready and report_ready:
            session.status = SessionStatus.done
            session.error_code = ""
            session.error_msg = ""
        else:
            session.status = SessionStatus.error
            if not media_ready and not report_ready:
                session.error_code = "RECOVERY_MEDIA_AND_REPORT_INCOMPLETE"
                session.error_msg = "异常恢复未找到可读音频和既有整场报告，已保留现场数据"
            elif not media_ready:
                session.error_code = "RECOVERY_MEDIA_INCOMPLETE"
                session.error_msg = "异常恢复未找到可读音频，已保留原始分片"
            else:
                session.error_code = "RECOVERY_REPORT_INCOMPLETE"
                session.error_msg = "音频已恢复，但既有整场报告不完整；未重复调用模型"
        db.commit()
        print(
            f"[recovery] session={sid}; status={session.status.value}; "
            f"media_ready={media_ready}; report_ready={report_ready}"
        )


async def recover_session(sid: int) -> None:
    """Recover one session with a per-process single-flight guard."""
    current = _recovery_tasks.get(sid)
    if current is not None:
        await asyncio.shield(current)
        return

    async def run_once() -> None:
        try:
            await _finalize_orphan(sid)
        except Exception as exc:  # noqa: BLE001
            code = safe_exception_code(exc, prefix="RECOVERY")
            _mark_recovery_error(
                sid, code, "异常恢复失败，原数据库和媒体文件已保留，请检查安全日志",
            )
            print(
                f"[recovery] session={sid}; code={code}; "
                f"error={safe_error_text(exc)}"
            )

    task = asyncio.create_task(run_once())
    _recovery_tasks[sid] = task
    try:
        await asyncio.shield(task)
    finally:
        if _recovery_tasks.get(sid) is task and task.done():
            _recovery_tasks.pop(sid, None)


async def recover_orphans(*, initial_delay: float = 10.0) -> None:
    """Finalize stale live/processing sessions left by a previous process."""
    from .pipeline import _running

    if initial_delay:
        await asyncio.sleep(initial_delay)
    now = datetime.now()
    with SessionLocal() as db:
        rows = db.query(ReplaySession).filter(
            ReplaySession.status.in_([SessionStatus.live, SessionStatus.processing]),
        ).all()
        ids = [
            row.id for row in rows
            if row.id not in _running
            and (row.started_at is None or now - row.started_at > timedelta(minutes=10))
        ]
    if ids:
        print(f"[recovery] orphan_count={len(ids)}; sessions={ids}")
    for sid in ids:
        await recover_session(sid)
