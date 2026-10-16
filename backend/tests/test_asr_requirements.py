from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import sessions as sessions_api
from app.core.asr_stream import (
    ASRConfigurationError,
    DashScopeASR,
    REAL_ASR_MODEL,
    ensure_real_asr_configured,
    get_asr_session,
)
from app.db import Base
from app.models import ReplaySession, SessionStatus


def _settings(provider="dashscope", model=REAL_ASR_MODEL, key="test-key"):
    return SimpleNamespace(
        asr_provider=provider,
        asr_model=model,
        aliyun_dashscope_api_key=key,
    )


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (_settings(provider="mock"), "ASR_PROVIDER"),
        (_settings(model="qwen3-asr-flash-realtime"), "ASR_MODEL"),
        (_settings(key=""), "ALIYUN_DASHSCOPE_API_KEY"),
    ],
)
def test_invalid_real_asr_configuration_fails_without_mock_fallback(settings, message) -> None:
    with pytest.raises(ASRConfigurationError, match=message):
        ensure_real_asr_configured(settings)
    with patch("app.core.asr_stream.get_settings", return_value=settings):
        with pytest.raises(ASRConfigurationError):
            get_asr_session([])


def test_valid_configuration_always_builds_stable_dashscope_model() -> None:
    with patch("app.core.asr_stream.get_settings", return_value=_settings()):
        session = get_asr_session(["脱敏热词"])
    assert isinstance(session, DashScopeASR)
    assert session.model == REAL_ASR_MODEL


def test_real_session_start_returns_503_before_pipeline_when_asr_is_missing() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as db:
        row = ReplaySession(
            title="真实直播测试", stream_url="https://media.example.test/live.flv",
            status=SessionStatus.created,
        )
        db.add(row)
        db.commit()
        with patch.object(
            sessions_api, "ensure_real_asr_configured",
            side_effect=ASRConfigurationError("ALIYUN_DASHSCOPE_API_KEY 未配置"),
        ), patch.object(sessions_api, "start_pipeline") as start:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(sessions_api.start_session(row.id, None, db))
        assert exc.value.status_code == 503
        assert exc.value.detail["code"] == "ASR_NOT_READY"
        start.assert_not_called()
    engine.dispose()
