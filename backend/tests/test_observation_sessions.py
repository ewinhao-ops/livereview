from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api import stats as stats_api
from app.core import attribution, observation, pattern_mining, pipeline, rhythm_library
from app.db import Base, _install_b12_guards
from app.models import (
    AudioFile,
    AudioStatus,
    ComplianceDecision,
    Critique,
    CritiqueLevel,
    OcrRecord,
    ReplaySession,
    SessionStatus,
    SessionType,
    Severity,
    Streamer,
    Transcript,
)


@pytest.fixture
def observation_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'observation.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _install_b12_guards(conn)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(observation, "SessionLocal", factory)
    monkeypatch.setattr(pipeline, "SessionLocal", factory)
    try:
        yield factory
    finally:
        observation._locks.clear()
        engine.dispose()


def test_observation_type_is_locked_and_does_not_pollute_streamer_statistics(
    observation_db, monkeypatch: pytest.MonkeyPatch,
):
    with observation_db() as db:
        streamer = Streamer(name="自家主播")
        db.add(streamer)
        db.flush()
        session = ReplaySession(
            title="污染防护", session_type=SessionType.observation,
            status=SessionStatus.done, started_at=datetime.now(), ended_at=datetime.now(),
        )
        db.add(session)
        db.flush()
        db.add(Critique(
            session_id=session.id, streamer_id=streamer.id,
            level=CritiqueLevel.summary, severity=Severity.info,
            category="错误注入的总结", comment="不得统计", score=10,
        ))
        db.commit()
        session_id = session.id

    with observation_db() as db:
        assert stats_api.streamer_stats(db) == []
        row = db.get(ReplaySession, session_id)
        row.session_type = SessionType.normal
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    monkeypatch.setattr(attribution, "SessionLocal", observation_db)
    monkeypatch.setattr(pattern_mining, "SessionLocal", observation_db)
    assert attribution.analyze_attribution(session_id)["disabled"] is True
    assert asyncio.run(pattern_mining.mine_session(session_id))["disabled"] is True


def _seed(factory, *, danmaku: list[tuple[int, str]] = (), transcripts=(), audio=False):  # noqa: ANN001
    with factory() as db:
        session = ReplaySession(
            title="观摩学习", session_type=SessionType.observation,
            status=SessionStatus.done, started_at=datetime.now(), ended_at=datetime.now(),
        )
        db.add(session)
        db.flush()
        if audio:
            db.add(AudioFile(
                session_id=session.id, path="missing.m4a", filename="missing.m4a",
                size_bytes=100, status=AudioStatus.available,
            ))
        for index, (at_ms, text) in enumerate(danmaku):
            db.add(OcrRecord(
                session_id=session.id, streamer_id=None,
                start_ms=at_ms, end_ms=at_ms, text=text,
            ))
        for index, item in enumerate(transcripts):
            start_ms, end_ms, text = item
            db.add(Transcript(
                session_id=session.id, streamer_id=None, seq=index,
                start_ms=start_ms, end_ms=end_ms,
                raw_text=text, corrected_text=text,
            ))
        db.commit()
        return session.id


def test_heat_no_danmaku_and_too_little_are_not_reliable(observation_db):
    no_data = _seed(observation_db)
    little = _seed(observation_db, danmaku=[(0, "甲：1"), (10_000, "乙：2"), (20_000, "丙：3")])
    assert observation.calculate_heat_slices(no_data) == {
        "reliable": False, "message": "没有可靠高互动片段", "slices": [],
    }
    assert observation.calculate_heat_slices(little)["reliable"] is False


def test_short_spam_and_overlapping_windows_are_selected_once(observation_db):
    messages = [(i * 1_000, f"同一用户：刷屏{i}") for i in range(10)]
    session_id = _seed(observation_db, danmaku=messages)
    result = observation.calculate_heat_slices(session_id)
    assert result["reliable"] is True
    assert len(result["slices"]) == 1
    assert result["slices"][0]["danmaku_count"] == 10
    assert result["slices"][0]["unique_user_count"] == 1


def test_adjacent_hot_windows_merge_but_independent_peaks_do_not(observation_db):
    adjacent = [
        (at, f"用户{i % 4}：消息{i}")
        for i, at in enumerate(range(0, 90_000, 5_000))
    ]
    adjacent_id = _seed(observation_db, danmaku=adjacent)
    adjacent_result = observation.calculate_heat_slices(adjacent_id)
    assert adjacent_result["reliable"] is True
    assert len(adjacent_result["slices"]) == 1

    independent = [
        *((i * 2_000, f"前峰{i % 4}：消息{i}") for i in range(8)),
        *((180_000 + i * 2_000, f"后峰{i % 4}：消息{i}") for i in range(8)),
    ]
    independent_id = _seed(observation_db, danmaku=independent)
    independent_result = observation.calculate_heat_slices(independent_id)
    assert independent_result["reliable"] is True
    assert len(independent_result["slices"]) == 2
    assert independent_result["slices"][0]["end_ms"] <= independent_result["slices"][1]["start_ms"]


def test_heat_handles_danmaku_without_transcript_and_transcript_without_audio(observation_db):
    messages = [(i * 2_000, f"用户{i % 3}：消息{i}") for i in range(8)]
    no_transcript = _seed(observation_db, danmaku=messages)
    first = observation.calculate_heat_slices(no_transcript)["slices"][0]
    assert first["transcript_ids"] == []
    assert first["corresponding_script"] == ""

    no_audio = _seed(
        observation_db, danmaku=messages,
        transcripts=[(0, 30_000, "这是一段可提炼的观摩逐字稿内容，足够长。")],
    )
    second = observation.calculate_heat_slices(no_audio)["slices"][0]
    assert second["transcript_ids"]
    assert "可提炼" in second["corresponding_script"]
    assert second["audio_at_ms"] is None


def test_observation_extraction_is_idempotent_pending_and_emotion_off_by_default(
    observation_db, monkeypatch: pytest.MonkeyPatch,
):
    messages = [(i * 2_000, f"用户{i % 3}：消息{i}") for i in range(8)]
    session_id = _seed(
        observation_db, danmaku=messages,
        transcripts=[(0, 30_000, "先讲用户痛点，再说明适用场景，最后给出清晰行动建议。")],
    )
    calls = 0

    async def fake_ingest(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["origin_type"] == "observation_session"
        metadata = kwargs["source_metadata"]
        assert metadata["source_session_id"] == session_id
        assert metadata["ranges"][0]["transcript_ids"]
        assert metadata["interaction_claim"] == "高互动片段；不代表高成交"
        return {"corpus": {"id": 99, "status": "ready"}, "templates": [{"status": "pending"}]}

    monkeypatch.setattr(rhythm_library, "ingest_corpus", fake_ingest)
    monkeypatch.setattr(rhythm_library, "list_templates", lambda **_kwargs: [{"status": "pending"}])
    monkeypatch.setattr(
        observation, "_emotion_for_slices",
        lambda *_args, **_kwargs: pytest.fail("emotion model must stay off by default"),
    )

    async def run_twice():
        first_result = await observation.extract_session(session_id)
        second_result = await observation.extract_session(session_id)
        return first_result, second_result

    first, second = asyncio.run(run_twice())

    assert calls == 1
    assert first["status"] == "ready"
    assert first["templates"][0]["status"] == "pending"
    assert first["emotion_generated"] is False
    assert first["emotion_status"] == "disabled"
    assert second["duplicate"] is True


def test_emotion_switch_only_runs_when_enabled_and_failure_does_not_break_templates(
    observation_db, monkeypatch: pytest.MonkeyPatch,
):
    messages = [(i * 2_000, f"用户{i % 3}：消息{i}") for i in range(8)]
    first_session = _seed(
        observation_db, danmaku=messages,
        transcripts=[(0, 30_000, "先讲用户痛点，再说明场景，最后给出清晰行动建议。")],
    )
    second_session = _seed(
        observation_db, danmaku=messages,
        transcripts=[(0, 30_000, "先建立信任，再解释产品差异，最后引导用户互动。")],
    )
    corpus_ids = iter((101, 102))

    async def fake_ingest(*_args, **_kwargs):
        return {
            "corpus": {"id": next(corpus_ids), "status": "ready"},
            "templates": [{"status": "pending"}],
        }

    monkeypatch.setattr(rhythm_library, "ingest_corpus", fake_ingest)
    monkeypatch.setattr(
        rhythm_library, "list_templates", lambda **_kwargs: [{"status": "pending"}],
    )
    emotion_calls: list[tuple[int, list[dict]]] = []

    def generated_emotion(session_id: int, slices: list[dict]):
        emotion_calls.append((session_id, slices))
        return "generated", [{
            "start_ms": slices[0]["start_ms"],
            "end_ms": slices[0]["end_ms"],
            "emotion": "积极",
            "generated_by_model": True,
        }]

    monkeypatch.setattr(observation, "_emotion_for_slices", generated_emotion)
    generated = asyncio.run(observation.extract_session(first_session, include_emotion=True))
    assert generated["status"] == "ready"
    assert generated["include_emotion"] is True
    assert generated["emotion_generated"] is True
    assert generated["emotion_status"] == "generated"
    assert generated["templates"][0]["status"] == "pending"
    assert len(emotion_calls) == 1
    assert all(item["corresponding_script"] for item in emotion_calls[0][1])

    def failed_emotion(*_args, **_kwargs):
        raise RuntimeError("emotion provider unavailable")

    monkeypatch.setattr(observation, "_emotion_for_slices", failed_emotion)
    failed = asyncio.run(observation.extract_session(second_session, include_emotion=True))
    assert failed["status"] == "ready"
    assert failed["emotion_generated"] is False
    assert failed["emotion_status"] == "failed"
    assert failed["templates"][0]["status"] == "pending"


def test_observation_sentence_path_never_calls_realtime_llm_or_creates_alert_data(
    observation_db, monkeypatch: pytest.MonkeyPatch,
):
    session_id = _seed(observation_db)
    worker = pipeline.ReplayPipeline(session_id)
    worker._observation = True
    worker.kb = SimpleNamespace(glossary=lambda: [])

    async def forbidden_corrector(*_args, **_kwargs):
        pytest.fail("observation must not call the realtime correction LLM")

    async def no_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pipeline, "correct_sentence", forbidden_corrector)
    monkeypatch.setattr(pipeline.bus, "publish", no_publish)
    asyncio.run(worker._handle_final("绝对最低价", 0, 1_000))

    with observation_db() as db:
        assert db.scalar(select(Transcript.id).where(Transcript.session_id == session_id)) is not None
        assert db.execute(select(Critique).where(Critique.session_id == session_id)).scalars().all() == []
        assert db.execute(select(ComplianceDecision).where(
            ComplianceDecision.session_id == session_id,
        )).scalars().all() == []
