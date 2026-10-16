"""2026-09-29：全部班次录制、豆包深度档中等思考/等待上限、读超时不重试、
分段点评失败不外抛、官网价比对默认关闭。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.core import llm, runtime
from app.core import pipeline as pipeline_mod
from app.core.knowledge import KnowledgeBase, Product
from app.core.review import screen as screen_mod
from app.core.review import sentence as sentence_mod
from app.core.review.sentence import check_sentence


# ---------- 全部班次录制 ----------

def test_record_all_switch_follows_system_setting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runtime, "_get", lambda key: "1" if key == "signal_record_all" else None)
    assert runtime.signal_record_all() is True
    monkeypatch.setattr(runtime, "_get", lambda key: "0" if key == "signal_record_all" else None)
    assert runtime.signal_record_all() is False


def test_record_all_defaults_off_without_system_setting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runtime, "_get", lambda key: None)
    monkeypatch.setattr(runtime, "get_settings", lambda: Settings(_env_file=None))
    assert runtime.signal_record_all() is False


def test_new_switches_keep_old_behaviour_by_default():
    fields = Settings.model_fields
    assert fields["signal_record_all"].default is False
    assert fields["llm_deep_timeout_sec"].default == 180
    assert fields["doubao_reasoning_effort"].default == ""
    assert fields["official_price_check_enabled"].default is False


# ---------- 豆包深度档 ----------

def test_deep_timeout_is_configurable_with_safe_fallbacks():
    assert llm._deep_timeout(SimpleNamespace()) == 180
    assert llm._deep_timeout(SimpleNamespace(llm_deep_timeout_sec=600)) == 600
    assert llm._deep_timeout(SimpleNamespace(llm_deep_timeout_sec="bad")) == 180
    assert llm._deep_timeout(SimpleNamespace(llm_deep_timeout_sec=5)) == 30


def test_reasoning_effort_only_applies_to_deep_requests():
    s = SimpleNamespace(doubao_reasoning_effort=" Medium ")
    assert llm._doubao_extra_body(s, True) == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "medium",
    }
    assert llm._doubao_extra_body(s, False) == {"thinking": {"type": "disabled"}}
    unknown = SimpleNamespace(doubao_reasoning_effort="max")
    assert llm._doubao_extra_body(unknown, True) == {"thinking": {"type": "enabled"}}


def test_doubao_clients_pick_up_timeout_and_effort(monkeypatch: pytest.MonkeyPatch):
    s = Settings(
        _env_file=None, llm_provider="doubao", ark_api_key="secret",
        doubao_model="deep-model", doubao_fast_model="fast-model",
        llm_deep_timeout_sec=600, doubao_reasoning_effort="medium",
    )
    monkeypatch.setattr(llm, "get_settings", lambda: s)

    deep = llm.get_llm("deep")
    fast = llm.get_llm("fast")

    assert isinstance(deep, llm.OpenAICompatLLM) and isinstance(fast, llm.OpenAICompatLLM)
    assert deep.timeout == 600
    assert deep.extra_body == {"thinking": {"type": "enabled"}, "reasoning_effort": "medium"}
    assert deep.retry_transient_once is True
    assert fast.timeout == 120
    assert fast.extra_body == {"thinking": {"type": "disabled"}}


def test_read_timeout_is_not_retried_to_avoid_double_billing(monkeypatch: pytest.MonkeyPatch):
    client = llm.OpenAICompatLLM(
        "doubao", "https://provider.invalid/v1", "secret", "model",
        timeout=600, retry_transient_once=True, allow_mock_fallback=False,
    )
    client._request = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    monkeypatch.setattr(llm.asyncio, "sleep", AsyncMock())

    with pytest.raises(llm.LLMCallError) as captured:
        asyncio.run(client.complete("system", "user", json_mode=True))

    assert captured.value.code == "LLM_REQUEST_TIMEOUT"
    assert client._request.await_count == 1


def test_connect_timeout_is_still_retried():
    assert llm._is_retryable(httpx.ConnectTimeout("timeout")) is True
    assert llm._is_retryable(httpx.ReadTimeout("timeout")) is False


# ---------- 分段点评失败不外抛 ----------

def test_segment_review_failure_is_contained(monkeypatch: pytest.MonkeyPatch):
    async def boom(*_args, **_kwargs):
        raise llm.LLMCallError("LLM_REQUEST_REJECTED", "余额不足")

    monkeypatch.setattr(pipeline_mod, "review_segment", boom)
    monkeypatch.setattr(pipeline_mod, "compute_rhythm_metrics", lambda *_a, **_k: {})
    monkeypatch.setattr(pipeline_mod, "summarize_prosody", lambda *_a, **_k: {})
    fake = SimpleNamespace(
        _seg_lock=asyncio.Lock(),
        _seg_buffer=[{"text": "大家好", "start_ms": 0, "end_ms": 1000}],
        _seg_emos=[],
        _current_sid=1,
        settings=SimpleNamespace(segment_max_sentences=20),
        _count_danmaku=lambda *_a: 0,
        _danmaku_texts=lambda *_a: [],
        kb=None,
        _prosody=[],
        _metrics_by=defaultdict(list),
        session_id=1,
        _save_critique=MagicMock(),
    )

    asyncio.run(pipeline_mod.ReplayPipeline._maybe_segment(fake, force=True))

    fake._save_critique.assert_not_called()
    assert fake._seg_buffer == []


# ---------- 官网价比对默认关闭 ----------

def _kb() -> KnowledgeBase:
    return KnowledgeBase(
        products=[Product(name="Demo Air Plus", aliases=[], official_price=3999)],
        forbidden=[], script_rules=[],
    )


def test_official_price_check_is_off_by_default():
    assert sentence_mod._official_price_check() is False


def test_spoken_price_is_not_compared_with_official_price_by_default():
    flags = check_sentence("Demo Air Plus 到手价5999元", _kb(), check_forbidden=False)
    assert [f for f in flags if f.category == "价格"] == []


def test_spoken_price_comparison_can_be_turned_back_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sentence_mod, "_official_price_check", lambda: True)
    flags = check_sentence("Demo Air Plus 到手价5999元", _kb(), check_forbidden=False)
    assert [f for f in flags if f.category == "价格"]


def test_screen_price_is_not_compared_with_official_price_by_default():
    flags = screen_mod.check_screen("Demo Air Plus 到手价5999元", _kb())
    assert [f for f in flags if f.category == "画面价格"] == []


def test_screen_price_comparison_can_be_turned_back_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(screen_mod, "_official_price_check", lambda: True)
    flags = screen_mod.check_screen("Demo Air Plus 到手价5999元", _kb())
    assert [f for f in flags if f.category == "画面价格"]
