"""2026-09-29：DeepSeek 备用密钥——现用密钥失败（如余额用完）时自动改用第二个账户。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.core import llm


def _settings(**kw) -> Settings:
    base = dict(
        _env_file=None, llm_provider="doubao", ark_api_key="ark", llm_fast_provider="deepseek",
        deepseek_api_key="primary-key", deepseek_fast_model="deepseek-flash",
    )
    base.update(kw)
    return Settings(**base)


def test_backup_key_becomes_fallback_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings(deepseek_api_key_backup="backup-key"))

    fast = llm.get_llm("fast")

    assert fast.name == "deepseek" and fast.api_key == "primary-key"
    assert fast.fallback is not None
    assert fast.fallback.api_key == "backup-key"
    assert fast.fallback.fallback is None
    assert (fast.fallback.model, fast.fallback.timeout, fast.fallback.extra_body) == (
        fast.model, fast.timeout, fast.extra_body)


def test_no_fallback_without_backup_or_with_same_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings())
    assert llm.get_llm("fast").fallback is None
    monkeypatch.setattr(llm, "get_settings", lambda: _settings(deepseek_api_key_backup="primary-key"))
    assert llm.get_llm("fast").fallback is None


def _insufficient_balance() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(402, request=request)
    return httpx.HTTPStatusError("Insufficient Balance", request=request, response=response)


def test_insufficient_balance_switches_to_backup_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings(deepseek_api_key_backup="backup-key"))
    fast = llm.get_llm("fast")
    fast._request = AsyncMock(side_effect=_insufficient_balance())
    fast.fallback._request = AsyncMock(return_value=llm.LLMResult('{"ok":true}', provider="deepseek"))

    result = asyncio.run(fast.complete("system", "user", json_mode=True))

    assert str(result) == '{"ok":true}'
    assert result.is_fallback is True
    assert fast._request.await_count == 1           # 402 不重试，直接换备用密钥
    assert fast.fallback._request.await_count == 1


def test_probe_uses_backup_when_primary_fails(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings(deepseek_api_key_backup="backup-key"))
    fast = llm.get_llm("fast")
    fast._request = AsyncMock(side_effect=_insufficient_balance())
    fast.fallback._request = AsyncMock(return_value="OK")

    assert asyncio.run(fast.probe()) == "OK"


def test_probe_still_raises_without_backup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings())
    fast = llm.get_llm("fast")
    fast._request = AsyncMock(side_effect=_insufficient_balance())

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(fast.probe())
