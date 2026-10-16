from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core import llm, summary_service
from app.core.review import report_data
from app.db import Base
from app.models import Critique, CritiqueLevel, ReplaySession, SessionStatus, Transcript


def test_only_long_requests_use_180_second_timeout_and_retry(monkeypatch: pytest.MonkeyPatch):
    settings = SimpleNamespace(
        llm_provider="qwen",
        llm_mock_fallback=False,
        qwen_api_key="secret",
        aliyun_dashscope_api_key="",
        qwen_base_url="https://provider.invalid/v1",
        qwen_model="qwen-test",
        llm_model="",
    )
    monkeypatch.setattr(llm, "get_settings", lambda: settings)

    realtime = llm.get_llm("fast")
    long_request = llm.get_llm("deep")

    assert isinstance(realtime, llm.OpenAICompatLLM)
    assert isinstance(long_request, llm.OpenAICompatLLM)
    assert realtime.timeout == 120
    assert realtime.retry_transient_once is False
    assert long_request.timeout == 180
    assert long_request.retry_transient_once is True


def test_long_request_retries_timeout_once_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    client = llm.OpenAICompatLLM(
        "qwen", "https://provider.invalid/v1", "secret", "model",
        timeout=180, retry_transient_once=True, allow_mock_fallback=False,
    )
    success = llm.LLMResult('{"ok":true}', provider="qwen")
    client._request = AsyncMock(side_effect=[httpx.ConnectTimeout("timeout"), success])
    monkeypatch.setattr(llm.asyncio, "sleep", AsyncMock())

    result = asyncio.run(client.complete("system", "user", json_mode=True))

    assert json.loads(result) == {"ok": True}
    assert client._request.await_count == 2


def test_long_request_fails_after_exactly_two_attempts(monkeypatch: pytest.MonkeyPatch):
    client = llm.OpenAICompatLLM(
        "qwen", "https://provider.invalid/v1", "secret", "model",
        timeout=180, retry_transient_once=True, allow_mock_fallback=False,
    )
    client._request = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))
    monkeypatch.setattr(llm.asyncio, "sleep", AsyncMock())

    with pytest.raises(llm.LLMCallError) as captured:
        asyncio.run(client.complete("system", "user", json_mode=True))

    assert captured.value.code == "LLM_REQUEST_TIMEOUT"
    assert client._request.await_count == 2


def test_explicit_4xx_is_never_retried(monkeypatch: pytest.MonkeyPatch):
    response = httpx.Response(401, request=httpx.Request("POST", "https://provider.invalid/v1"))
    error = httpx.HTTPStatusError("unauthorized", request=response.request, response=response)
    client = llm.OpenAICompatLLM(
        "qwen", "https://provider.invalid/v1", "secret", "model",
        timeout=180, retry_transient_once=True, allow_mock_fallback=False,
    )
    client._request = AsyncMock(side_effect=error)
    monkeypatch.setattr(llm.asyncio, "sleep", AsyncMock())

    with pytest.raises(llm.LLMCallError) as captured:
        asyncio.run(client.complete("system", "user", json_mode=True))

    assert captured.value.code == "LLM_REQUEST_REJECTED"
    assert client._request.await_count == 1


@pytest.fixture
def summary_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'summary.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(summary_service, "SessionLocal", factory)
    summary_service._inflight.clear()
    with factory() as db:
        session = ReplaySession(
            title="summary", status=SessionStatus.done,
            started_at=datetime.now(), ended_at=datetime.now(),
        )
        db.add(session)
        db.flush()
        db.add(Transcript(
            session_id=session.id, streamer_id=None, seq=0,
            start_ms=0, end_ms=1000, raw_text="测试逐字稿", corrected_text="测试逐字稿",
        ))
        db.commit()
        session_id = session.id
    try:
        yield factory, session_id
    finally:
        summary_service._inflight.clear()
        engine.dispose()


def test_concurrent_summary_requests_share_one_generation_and_one_row(
    summary_db, monkeypatch: pytest.MonkeyPatch,
):
    factory, session_id = summary_db
    calls = 0

    async def fake_review(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.03)
        return {"summary": "new", "overall_score": 8, "llm": "test"}

    monkeypatch.setattr(summary_service, "review_summary", fake_review)
    async def run():
        return await asyncio.gather(
            summary_service.generate_summary(session_id, None),
            summary_service.generate_summary(session_id, None),
        )

    first, second = asyncio.run(run())

    assert calls == 1
    assert first == second
    with factory() as db:
        rows = db.execute(select(Critique).where(
            Critique.session_id == session_id,
            Critique.level == CritiqueLevel.summary,
        )).scalars().all()
        assert len(rows) == 1


def test_repeated_generation_updates_in_place_and_failure_preserves_old_summary(
    summary_db, monkeypatch: pytest.MonkeyPatch,
):
    factory, session_id = summary_db
    calls = 0

    async def successful(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"summary": f"version-{calls}", "overall_score": 7, "llm": "test"}

    monkeypatch.setattr(summary_service, "review_summary", successful)
    asyncio.run(summary_service.generate_summary(session_id, None))
    asyncio.run(summary_service.generate_summary(session_id, None))
    with factory() as db:
        rows = db.execute(select(Critique).where(
            Critique.session_id == session_id,
            Critique.level == CritiqueLevel.summary,
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].comment == "version-2"

    async def failed(*_args, **_kwargs):
        raise llm.LLMCallError("LLM_REQUEST_TIMEOUT", "sanitized")

    monkeypatch.setattr(summary_service, "review_summary", failed)
    with pytest.raises(llm.LLMCallError):
        asyncio.run(summary_service.generate_summary(session_id, None))
    with factory() as db:
        row = db.execute(select(Critique).where(
            Critique.session_id == session_id,
            Critique.level == CritiqueLevel.summary,
        )).scalars().one()
        assert row.comment == "version-2"


def test_regeneration_keeps_deep_review_and_other_attached_report_data(
    summary_db, monkeypatch: pytest.MonkeyPatch,
):
    factory, session_id = summary_db
    monkeypatch.setattr(report_data, "SessionLocal", factory)
    results = iter([
        {"summary": "version-1", "overall_score": 7, "llm": "test",
         "emotion_context": "old", "unlisted": 1},
        {"summary": "version-2", "overall_score": 8, "llm": "test", "unlisted": 2},
    ])

    async def fake_review(*_args, **_kwargs):
        return next(results)

    monkeypatch.setattr(summary_service, "review_summary", fake_review)
    asyncio.run(summary_service.generate_summary(session_id, None))
    attached = {
        "v2": {"generated_at": "2026-09-30 20:58", "tone": {"verdict": "稳"}},
        "emotion_overview": {"segments": 3, "dominant": "平静"},
        "examples": {"worst": [], "best": [{"start_ms": 0, "score": 9}]},
    }
    report_data.merge_summary_data(session_id, None, lambda data: data.update(attached))

    asyncio.run(summary_service.generate_summary(session_id, None))

    with factory() as db:
        row = db.execute(select(Critique).where(
            Critique.session_id == session_id,
            Critique.level == CritiqueLevel.summary,
        )).scalars().one()
    data = json.loads(row.data)
    assert (row.comment, row.score, data["summary"]) == ("version-2", 8, "version-2")
    assert {key: data[key] for key in attached} == attached
    assert "emotion_context" not in data
    assert data["unlisted"] == 2


@pytest.mark.parametrize("compass_saved_first", [True, False])
def test_generation_leaves_other_summary_level_rows_untouched(
    summary_db, monkeypatch: pytest.MonkeyPatch, compass_saved_first: bool,
):
    factory, session_id = summary_db
    calls = 0

    async def fake_review(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"summary": f"version-{calls}", "overall_score": 7, "llm": "test"}

    monkeypatch.setattr(summary_service, "review_summary", fake_review)
    compass_data = json.dumps({"changes": [{"metric": "成交"}], "summary": "归因"}, ensure_ascii=False)

    def save_compass_review() -> int:
        with factory() as db:
            row = Critique(
                session_id=session_id, streamer_id=None, level=CritiqueLevel.summary,
                category="罗盘归因", comment="归因", data=compass_data,
            )
            db.add(row)
            db.commit()
            return row.id

    if compass_saved_first:
        compass_id = save_compass_review()
        asyncio.run(summary_service.generate_summary(session_id, None))
    else:
        asyncio.run(summary_service.generate_summary(session_id, None))
        compass_id = save_compass_review()
    asyncio.run(summary_service.generate_summary(session_id, None))

    with factory() as db:
        rows = db.execute(select(Critique).where(
            Critique.session_id == session_id,
            Critique.level == CritiqueLevel.summary,
        )).scalars().all()
    by_category = {row.category: row for row in rows}
    assert len(rows) == 2
    compass = by_category["罗盘归因"]
    assert (compass.id, compass.comment, compass.data) == (compass_id, "归因", compass_data)
    assert by_category["整场总结"].comment == "version-2"


def test_report_data_attached_while_the_summary_is_being_stored_is_not_lost(
    summary_db, monkeypatch: pytest.MonkeyPatch,
):
    factory, session_id = summary_db
    monkeypatch.setattr(report_data, "SessionLocal", factory)
    calls = 0

    async def fake_review(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"summary": f"version-{calls}", "overall_score": 7, "llm": "test"}

    monkeypatch.setattr(summary_service, "review_summary", fake_review)
    asyncio.run(summary_service.generate_summary(session_id, None))

    overview = {"segments": 3, "dominant": "平静"}
    carry_over = summary_service._carry_over
    raced = []

    def carry_over_after_a_concurrent_merge(new: dict, old: dict) -> dict:
        if not raced:
            raced.append(True)
            report_data.merge_summary_data(
                session_id, None, lambda data: data.__setitem__("emotion_overview", overview),
            )
        return carry_over(new, old)

    monkeypatch.setattr(summary_service, "_carry_over", carry_over_after_a_concurrent_merge)
    asyncio.run(summary_service.generate_summary(session_id, None))

    with factory() as db:
        row = db.execute(select(Critique).where(
            Critique.session_id == session_id,
            Critique.level == CritiqueLevel.summary,
        )).scalars().one()
    data = json.loads(row.data)
    assert raced
    assert (row.comment, data["summary"]) == ("version-2", "version-2")
    assert data["emotion_overview"] == overview
