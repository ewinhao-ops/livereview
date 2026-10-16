from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ready as ready_api
from app.core.asr_stream import REAL_ASR_MODEL
from app.core.llm import llm_configuration_error


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(ready_api.router)
    return app


def _ok():
    return {"ok": True, "reason": ""}


def test_ready_returns_200_with_all_required_dimensions() -> None:
    with patch.object(ready_api, "_check_database", side_effect=_ok), \
            patch.object(ready_api, "_check_data_directory", side_effect=_ok), \
            patch.object(ready_api, "_check_ffmpeg", side_effect=_ok), \
            patch.object(ready_api, "_check_chromium", side_effect=_ok), \
            patch.object(ready_api, "_check_asr", side_effect=_ok), \
            patch.object(ready_api, "_check_llm", side_effect=_ok), \
            TestClient(_app()) as client:
        response = client.get("/api/ready")
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert set(response.json()["checks"]) == {
        "database", "data_directory", "ffmpeg", "chromium", "asr", "llm",
    }


def test_ready_returns_503_and_sanitized_reason_without_private_paths() -> None:
    with patch.object(ready_api, "_check_database", side_effect=_ok), \
            patch.object(ready_api, "_check_data_directory", side_effect=_ok), \
            patch.object(ready_api, "_check_ffmpeg",
                         return_value={"ok": False,
                                       "reason": "ffmpeg 未安装或未配置为可执行文件"}), \
            patch.object(ready_api, "_check_chromium", side_effect=_ok), \
            patch.object(ready_api, "_check_asr", side_effect=_ok), \
            patch.object(ready_api, "_check_llm", side_effect=_ok), \
            TestClient(_app()) as client:
        response = client.get("/api/ready")
    body = response.json()
    assert response.status_code == 503
    assert body["ready"] is False
    serialized = response.text.lower()
    assert "api_key=" not in serialized
    assert "d:\\" not in serialized and "/home/" not in serialized


def test_model_readiness_is_configuration_only_and_never_calls_provider() -> None:
    settings = SimpleNamespace(
        asr_provider="dashscope", asr_model=REAL_ASR_MODEL,
        aliyun_dashscope_api_key="configured-but-not-called",
        llm_provider="qwen", qwen_api_key="configured-but-not-called",
        qwen_model="qwen-plus", llm_model="", ark_api_key="",
        doubao_model="", doubao_fast_model="", hunyuan_api_key="",
        hunyuan_model="", openai_api_key="", llm_api_key="", openai_model="",
        deepseek_api_key="",
    )
    with patch.object(ready_api, "get_settings", return_value=settings), \
            patch("app.core.llm.OpenAICompatLLM._request",
                  side_effect=AssertionError("就绪检查禁止调用模型")) as paid_call:
        assert ready_api._check_asr()["ok"] is True
        assert ready_api._check_llm()["ok"] is True
        assert llm_configuration_error(settings) == ""
    paid_call.assert_not_called()


def test_fresh_missing_model_configuration_returns_503_with_safe_reasons() -> None:
    missing = SimpleNamespace(
        asr_provider="dashscope", asr_model=REAL_ASR_MODEL,
        aliyun_dashscope_api_key="", llm_provider="qwen", qwen_api_key="",
        qwen_model="", llm_model="", ark_api_key="", doubao_model="",
        doubao_fast_model="", hunyuan_api_key="", hunyuan_model="",
        openai_api_key="", llm_api_key="", openai_model="", deepseek_api_key="",
    )
    with patch.object(ready_api, "_check_database", side_effect=_ok), \
            patch.object(ready_api, "_check_data_directory", side_effect=_ok), \
            patch.object(ready_api, "_check_ffmpeg", side_effect=_ok), \
            patch.object(ready_api, "_check_chromium", side_effect=_ok), \
            patch.object(ready_api, "get_settings", return_value=missing), \
            TestClient(_app()) as client:
        response = client.get("/api/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["asr"]["ok"] is False
    assert body["checks"]["llm"]["ok"] is False
    assert "未配置" in "；".join(body["blockers"])
    assert "d:\\" not in response.text.lower() and "/home/" not in response.text.lower()
