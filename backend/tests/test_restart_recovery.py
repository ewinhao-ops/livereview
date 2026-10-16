from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import sessions as sessions_api
from app.core import pipeline as pipeline_module
from app.core import recovery
from app.db import Base
from app.models import (
    AudioFile,
    AudioStatus,
    Critique,
    CritiqueLevel,
    ReplaySession,
    SessionStatus,
    Severity,
)


@pytest.fixture
def recovery_db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'recovery.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    original = recovery.SessionLocal
    recovery.SessionLocal = factory
    try:
        yield factory, tmp_path
    finally:
        recovery._recovery_tasks.clear()
        recovery.SessionLocal = original
        engine.dispose()


def test_consecutive_pipeline_start_reserves_one_capture_worker() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        started = 0

        class FakePipeline:
            def __init__(self, session_id: int) -> None:
                self.session_id = session_id

            async def run(self) -> None:
                nonlocal started
                started += 1
                await gate.wait()

        pipeline_module._running.clear()
        with patch.object(pipeline_module, "ReplayPipeline", FakePipeline):
            assert pipeline_module.start_pipeline(9001) is True
            assert pipeline_module.start_pipeline(9001) is False
            await asyncio.sleep(0)
            assert started == 1
            assert list(pipeline_module._running) == [9001]
            gate.set()
            await asyncio.sleep(0)
        pipeline_module._running.clear()

    asyncio.run(scenario())


def test_processing_restart_reuses_report_and_never_calls_paid_models(
    recovery_db,
) -> None:
    factory, audio_dir = recovery_db
    audio_file = audio_dir / "session_1.m4a"
    audio_file.write_bytes(b"non-zero-test-media")
    with factory() as db:
        row = ReplaySession(
            title="脱敏恢复场次", status=SessionStatus.processing,
            started_at=datetime.now() - timedelta(hours=1), ended_at=datetime.now(),
        )
        db.add(row)
        db.flush()
        db.add(AudioFile(
            session_id=row.id, path=str(audio_file), filename=audio_file.name,
            size_bytes=0, status=AudioStatus.recording,
        ))
        db.add(Critique(
            session_id=row.id, level=CritiqueLevel.summary, severity=Severity.info,
            category="整场总结", comment="既有脱敏报告",
        ))
        db.commit()
        session_id = row.id

    async def scenario() -> None:
        await asyncio.gather(
            recovery.recover_session(session_id), recovery.recover_session(session_id),
        )

    with patch.object(recovery, "get_settings", return_value=SimpleNamespace(
        audio_path=audio_dir,
    )), patch.object(recovery, "_probe_readable", return_value=True), patch(
        "app.core.review.summary.review_summary", new_callable=AsyncMock,
    ) as model_call:
        asyncio.run(scenario())
    model_call.assert_not_awaited()
    with factory() as db:
        restored = db.get(ReplaySession, session_id)
        assert restored.status == SessionStatus.done
        assert restored.audio.status == AudioStatus.available
        assert restored.audio.size_bytes > 0
        assert db.query(Critique).filter_by(
            session_id=session_id, level=CritiqueLevel.summary,
        ).count() == 1


def test_processing_restart_without_report_enters_explicit_failure(
    recovery_db,
) -> None:
    factory, audio_dir = recovery_db
    audio_file = audio_dir / "session_1.m4a"
    audio_file.write_bytes(b"non-zero-test-media")
    with factory() as db:
        row = ReplaySession(
            status=SessionStatus.processing,
            started_at=datetime.now() - timedelta(hours=1),
        )
        db.add(row)
        db.flush()
        db.add(AudioFile(
            session_id=row.id, path=str(audio_file), filename=audio_file.name,
            status=AudioStatus.recording,
        ))
        db.commit()
        session_id = row.id

    with patch.object(recovery, "get_settings", return_value=SimpleNamespace(
        audio_path=audio_dir,
    )), patch.object(recovery, "_probe_readable", return_value=True):
        asyncio.run(recovery.recover_session(session_id))

    with factory() as db:
        failed = db.get(ReplaySession, session_id)
        assert failed.status == SessionStatus.error
        assert failed.error_code == "RECOVERY_REPORT_INCOMPLETE"
        assert failed.ended_at is not None


def test_repeated_stop_after_worker_loss_is_idempotent(recovery_db) -> None:
    factory, _audio_dir = recovery_db
    sessions_original = sessions_api.recover_session
    with factory() as db:
        row = ReplaySession(status=SessionStatus.processing)
        db.add(row)
        db.commit()
        session_id = row.id

        recover_count = 0

        async def finish_once(sid: int) -> None:
            nonlocal recover_count
            recover_count += 1
            with factory() as worker_db:
                worker_row = worker_db.get(ReplaySession, sid)
                worker_row.status = SessionStatus.error
                worker_row.error_code = "RECOVERY_REPORT_INCOMPLETE"
                worker_row.error_msg = "未重复调用模型"
                worker_row.ended_at = datetime.now()
                worker_db.commit()

        sessions_api.recover_session = finish_once
        try:
            first = asyncio.run(sessions_api.stop_session(session_id, db))
            second = asyncio.run(sessions_api.stop_session(session_id, db))
        finally:
            sessions_api.recover_session = sessions_original

    assert recover_count == 1
    assert first.status == SessionStatus.error
    assert second.status == SessionStatus.error
