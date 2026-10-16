"""统一的 LLM provider 适配层。

豆包、腾讯混元、通义千问和 ChatGPT(OpenAI API) 都通过 OpenAI 兼容的
``/chat/completions`` 协议调用。四组密钥可以同时保留，只切换 ``LLM_PROVIDER``。
"""

from __future__ import annotations

import json
import hashlib
import logging
import re
import asyncio
import time
import uuid
from typing import Protocol

import httpx

from ..config import get_settings
from .safe_error import safe_error_text, safe_exception_code
from .usage import current_llm_context


logger = logging.getLogger(__name__)


class LLMIncompleteResponse(ValueError):
    """Provider explicitly truncated output or returned an incomplete structured body."""


class LLMCallError(RuntimeError):
    """Sanitized stable provider failure exposed to long-request API boundaries."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LLMResult(str):
    """String-compatible model output with per-call execution metadata."""

    provider: str
    requested_provider: str
    is_fallback: bool

    def __new__(
        cls,
        content: str,
        *,
        provider: str,
        requested_provider: str | None = None,
        is_fallback: bool = False,
    ) -> "LLMResult":
        value = super().__new__(cls, content)
        value.provider = provider
        value.requested_provider = requested_provider or provider
        value.is_fallback = is_fallback
        return value


def result_metadata(result: str, default_provider: str = "") -> dict:
    """Return safe, serializable metadata for one concrete model call."""
    provider = str(getattr(result, "provider", "") or default_provider or "mock")
    requested = str(getattr(result, "requested_provider", "") or provider)
    return {
        "llm": provider,
        "llm_requested": requested,
        "is_fallback": bool(getattr(result, "is_fallback", False)),
    }


def _error_stage(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "request_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_status"
    if isinstance(exc, httpx.RequestError):
        return "request"
    if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, TypeError)):
        return "response_parse"
    if isinstance(exc, ValueError):
        return "response_validation"
    return "unknown"


def _is_retryable(exc: Exception) -> bool:
    """Only retry transient transport or clearly incomplete structured output."""
    # 读超时：请求已送达，服务端仍会把这次生成跑完并照常计费，原样重发只会双倍计费
    # （2026-09-27 七月真实场次对比：下播总结连续两次读超时、两次都付费）。
    # 连接/排队超时时请求没发出去，照旧可以重试。
    if isinstance(exc, httpx.ReadTimeout):
        return False
    if isinstance(exc, (
        httpx.TimeoutException, httpx.TransportError, json.JSONDecodeError,
        LLMIncompleteResponse,
    )):
        return True
    if isinstance(exc, (KeyError, IndexError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def _stable_failure_code(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "LLM_REQUEST_TIMEOUT"
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 429:
            # 限流单独一类：请求没被处理、不计费，调用方可以等一等再发（深度复盘并行时用到）
            return "LLM_RATE_LIMITED"
        return "LLM_PROVIDER_UNAVAILABLE" if exc.response.status_code >= 500 else "LLM_REQUEST_REJECTED"
    if isinstance(exc, httpx.RequestError):
        return "LLM_NETWORK_ERROR"
    if isinstance(exc, (json.JSONDecodeError, LLMIncompleteResponse, KeyError, IndexError)):
        return "LLM_RESPONSE_INCOMPLETE"
    if isinstance(exc, (TypeError, ValueError)):
        return "LLM_RESPONSE_INVALID"
    return "LLM_REQUEST_FAILED"


class LLMProvider(Protocol):
    name: str

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> LLMResult:
        ...


class MockLLM:
    name = "mock"

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> LLMResult:
        if not json_mode:
            return LLMResult(_extract_payload(user), provider=self.name)
        if "段级" in system or "segment" in system.lower():
            return LLMResult(json.dumps(
                {"score": 7.5,
                 "comment": "[mock] 本段讲解较完整，但中段语速偏快、缺少停顿引导下单；"
                            "建议在报参数后补一句促单话术。",
                 "emotion": "热情饱满",
                 "issues": ["语速偏快", "缺少转化引导"]},
                ensure_ascii=False,
            ), provider=self.name)
        if "整场" in system or "summary" in system.lower():
            return LLMResult(json.dumps(
                {"overall_score": 7.0,
                 "dimensions": {"讲解准确性": 6.5, "节奏": 7.0, "转化引导": 7.5, "合规": 8.0},
                  "summary": "[mock] 整场表现中上：产品卖点覆盖到位，主要失分在个别型号/价格"
                             "口播与知识库不一致，以及部分时段节奏偏快。",
                  "emotion": "全程较热情，偶有浮夸",
                  "top_issues": ["核对型号/价格口播", "控制语速并增加促单节点"],
                  "action_items": [
                      {"action": "开播前过一遍当日机型价格表", "source": "[知识库]"},
                      {"action": "每讲完一款补固定促单话术", "source": "[通用经验]"},
                  ],
                  "experience_citations": []},
                ensure_ascii=False,
            ), provider=self.name)
        return LLMResult("{}", provider=self.name)


def _extract_payload(user: str) -> str:
    marker = "【待校正】"
    if marker in user:
        return user.split(marker, 1)[1].strip()
    return user.strip()


# 防翻车铁律（2026-07-14 豆包切换）：盲评实锤豆包三类毛病——编造参数/伪精确数字、
# 破价冲动、违规操作建议。统一注入 system 尾部，对所有真实 provider 生效，一处维护。
_GUARD = (
    "\n\n【硬约束】①产品型号/参数以提供的知识库为准，知识库没有的参数不得出现；"
    "价格随直播活动变化，不要拿知识库价格去核对、质疑主播口播的价格（低于底价的破价除外）；"
    "②禁止编造任何未验证的数字承诺（如\u0027转化翻3倍\u0027\u0027流失砍80%\u0027），拿不准写\u0027需实测\u0027；"
    "③不得建议低于官方价或知识库价格销售；不得虚构折扣、促销、库存、赠品或权益，"
    "只有上下文明确提供的真实信息才可用于话术，不确定时写\u0027需核实\u0027；"
    "④不得建议任何违反平台规则的操作（虚假库存、自刷互动、站外导流、公屏暴露订单号）。"
)


class OpenAICompatLLM:
    """通用 OpenAI 兼容客户端；兼容各家在 JSON mode 上的细微差异。"""

    def __init__(self, name: str, base_url: str, api_key: str, model: str,
                 *, extra_body: dict | None = None, timeout: float = 60,
                 fallback: "OpenAICompatLLM | None" = None,
                 allow_mock_fallback: bool = True,
                 temperature: float | None = 0.3,
                 token_parameter: str = "max_tokens", max_tokens: int = 4096,
                 retry_transient_once: bool = False) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.extra_body = extra_body or {}
        self.timeout = timeout
        self.fallback = fallback
        self.allow_mock_fallback = allow_mock_fallback
        self.temperature = temperature
        self.token_parameter = token_parameter
        self.max_tokens = max_tokens
        self.retry_transient_once = retry_transient_once

    def _payload(self, system: str, user: str, json_mode: bool) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system + _GUARD},
                {"role": "user", "content": user},
            ],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        payload[self.token_parameter] = self.max_tokens
        payload.update(self.extra_body)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _request(self, system: str, user: str, json_mode: bool,
                       *, request_id: str = "") -> LLMResult:
        started_at = time.monotonic()
        payload = self._payload(system, user, json_mode)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers,
            )
            response.raise_for_status()
            body = response.json()
            choice = body["choices"][0]
            content = choice["message"]["content"]
            if str(choice.get("finish_reason") or "").lower() in {
                "length", "max_tokens", "max_output_tokens",
            }:
                raise LLMIncompleteResponse("模型输出被截断")
            if json_mode:
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise ValueError("结构化输出必须是 JSON object")
            usage = body.get("usage") or {}
            self._record_usage(
                request_id=request_id,
                usage=usage,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
                success=True,
            )
            return LLMResult(content, provider=self.name)

    def _record_usage(self, *, request_id: str, usage: dict,
                      elapsed_ms: int, success: bool,
                      error_code: str = "") -> None:
        """Persist provider usage without making telemetry failure affect a call."""
        try:
            from ..db import SessionLocal
            from ..models import LLMUsage

            prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion_tokens = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
            total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)
            settings = get_settings()
            estimated_cost = (
                prompt_tokens * float(getattr(settings, "llm_input_price_per_million", 0) or 0)
                + completion_tokens * float(getattr(settings, "llm_output_price_per_million", 0) or 0)
            ) / 1_000_000
            ctx = current_llm_context()
            with SessionLocal() as db:
                db.add(LLMUsage(
                    session_id=ctx.session_id,
                    stage=ctx.stage,
                    provider=self.name,
                    requested_provider=self.name,
                    model=self.model,
                    request_id=request_id,
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    estimated_cost=estimated_cost,
                    elapsed_ms=elapsed_ms,
                    success=success,
                    error_code=error_code,
                ))
                db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_usage_record_failed code=%s", safe_exception_code(exc))

    async def probe(self) -> str:
        """真实探测当前端点；失败直接抛错，不允许 mock 把错误伪装成成功。
        配了真实备用端点（如 DeepSeek 备用密钥）时，主端点不通就探备用端点。"""
        try:
            return await self._request(
                "你是 API 连通性检查助手。", "只回复 OK。", False,
            )
        except Exception:  # noqa: BLE001
            if self.fallback is None:
                raise
            return await self.fallback.probe()

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> LLMResult:
        request_id = hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:12]
        started_at = time.monotonic()
        try:
            attempt = 1
            try:
                result = await self._request(system, user, json_mode, request_id=request_id)
            except Exception as first_error:  # noqa: BLE001
                if not self.retry_transient_once or not _is_retryable(first_error):
                    raise
                logger.warning(
                    "llm_retry request_id=%s model=%s attempt=1 code=%s reason=%s",
                    request_id, self.model, safe_exception_code(first_error),
                    safe_error_text(first_error),
                )
                await asyncio.sleep(0.35)
                attempt = 2
                result = await self._request(system, user, json_mode, request_id=request_id)
            logger.info(
                "llm_request request_id=%s model=%s elapsed_ms=%d status=success code=OK attempt=%d",
                request_id, self.model, int((time.monotonic() - started_at) * 1000), attempt,
            )
            return result
        except Exception as e:  # noqa: BLE001  真实调用失败不应拖垮流水线
            stage = _error_stage(e)
            code = "LLM_" + re.sub(r"[^A-Z0-9]+", "_", stage.upper()).strip("_")
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            self._record_usage(
                request_id=request_id, usage={}, elapsed_ms=elapsed_ms,
                success=False, error_code=code,
            )
            if self.fallback is not None:
                logger.warning(
                    "llm_request request_id=%s model=%s elapsed_ms=%d "
                    "status=failed stage=%s code=%s reason=%s allow_fallback=true fallback=%s",
                    request_id, self.model, elapsed_ms, stage, code, safe_error_text(e), self.fallback.name,
                )
                fallback_result = await self.fallback.complete(
                    system, user, json_mode=json_mode,
                )
            else:
                if not self.allow_mock_fallback:
                    logger.warning(
                        "llm_request request_id=%s model=%s elapsed_ms=%d "
                        "status=failed stage=%s code=%s reason=%s allow_fallback=false fallback=disabled",
                        request_id, self.model, elapsed_ms, stage, code, safe_error_text(e),
                    )
                    raise LLMCallError(
                        _stable_failure_code(e),
                        f"真实 LLM 调用失败（provider={self.name}, stage={stage}）",
                    ) from None
                logger.warning(
                    "llm_request request_id=%s model=%s elapsed_ms=%d "
                    "status=failed stage=%s code=%s reason=%s allow_fallback=true fallback=mock",
                    request_id, self.model, elapsed_ms, stage, code, safe_error_text(e),
                )
                fallback_result = await MockLLM().complete(
                    system, user, json_mode=json_mode,
                )
            return LLMResult(
                str(fallback_result),
                provider=getattr(fallback_result, "provider", "mock"),
                requested_provider=self.name,
                is_fallback=True,
            )


_ALIASES = {
    "豆包": "doubao", "volcano": "doubao", "ark": "doubao",
    "混元": "hunyuan", "腾讯混元": "hunyuan", "tencent": "hunyuan",
    "千问": "qwen", "通义千问": "qwen", "dashscope": "qwen",
    "openai": "chatgpt", "gpt": "chatgpt", "chat_gpt": "chatgpt",
}


def _first(*values: str) -> str:
    return next((value.strip() for value in values if value and value.strip()), "")


def canonical_provider(value: str) -> str:
    provider = (value or "mock").strip().lower()
    return _ALIASES.get(provider, provider)


def _fast_provider(s) -> str:
    """快速档单独指定的 provider（LLM_FAST_PROVIDER）；留空返回 ""，表示与主力档相同。"""
    value = (getattr(s, "llm_fast_provider", "") or "").strip()
    return canonical_provider(value) if value else ""


def llm_configuration_error(settings=None) -> str:
    """纯配置检查；不实例化客户端、不访问网络、不触发付费调用。"""
    s = settings or get_settings()
    provider = canonical_provider(s.llm_provider)
    error = _provider_configuration_error(s, provider, "LLM_PROVIDER")
    if error:
        return error
    fast = _fast_provider(s)
    if fast and fast != provider:
        return _provider_configuration_error(s, fast, "LLM_FAST_PROVIDER", fast=True)
    return ""


def _provider_configuration_error(s, provider: str, label: str, *, fast: bool = False) -> str:
    if provider == "doubao":
        key, model = s.ark_api_key, _first(s.doubao_model, s.doubao_fast_model, s.llm_model)
    elif provider == "hunyuan":
        key, model = s.hunyuan_api_key, _first(s.hunyuan_model, s.llm_model)
    elif provider == "qwen":
        key = _first(s.qwen_api_key, s.aliyun_dashscope_api_key)
        model = _first(s.qwen_model, s.llm_model)
    elif provider in ("chatgpt", "compat"):
        key = _first(s.openai_api_key, s.llm_api_key)
        model = _first(s.openai_model, s.llm_model)
    elif provider == "deepseek":
        ds_deep = getattr(s, "deepseek_model", "")
        ds_fast = getattr(s, "deepseek_fast_model", "")
        key = s.deepseek_api_key
        model = (_first(ds_fast, ds_deep, s.llm_model) if fast
                 else _first(ds_deep, ds_fast, s.llm_model))
    else:
        return f"{label} 未选择受支持的真实模型"
    if not (key or "").strip():
        return f"{label}={provider} 缺少 API Key"
    return ""


_REASONING_EFFORTS = {"minimal", "low", "medium", "high"}


def _deep_timeout(s) -> float:
    """深度档单次请求等待上限（秒）；默认 180，与改造前一致。"""
    try:
        return max(30.0, float(getattr(s, "llm_deep_timeout_sec", 180) or 180))
    except (TypeError, ValueError):
        return 180.0


def _doubao_extra_body(s, deep: bool) -> dict:
    """豆包请求附加参数：深度档开思考，可按配置调低思考强度；快速档关思考。"""
    body: dict = {"thinking": {"type": "enabled" if deep else "disabled"}}
    effort = (getattr(s, "doubao_reasoning_effort", "") or "").strip().lower()
    if deep and effort in _REASONING_EFFORTS:
        body["reasoning_effort"] = effort
    return body


def get_llm(purpose: str = "fast") -> LLMProvider:
    """根据 ``LLM_PROVIDER`` 返回客户端；purpose 影响豆包/DeepSeek 的快/深模型。

    快速档（purpose != "deep"）可用 ``LLM_FAST_PROVIDER`` 单独指定 provider，留空与主力档相同。
    """
    s = get_settings()
    p = canonical_provider(s.llm_provider)
    if purpose != "deep":
        p = _fast_provider(s) or p
    allow_mock = bool(getattr(s, "llm_mock_fallback", False))

    # 只有显式选择 mock 时才允许返回 MockLLM。配置了真实 provider 却缺密钥时
    # 直接报错，避免 readiness 之外的后台任务静默产出假报告。
    if p != "mock":
        label = "LLM_FAST_PROVIDER" if purpose != "deep" and _fast_provider(s) else "LLM_PROVIDER"
        error = _provider_configuration_error(
            s, p, label, fast=(purpose != "deep"),
        )
        if error:
            raise ValueError(error)

    if p == "doubao":
        key = s.ark_api_key
        if key:
            deep = purpose == "deep"
            return OpenAICompatLLM(
                "doubao", _first(s.doubao_base_url,
                                  "https://ark.cn-beijing.volces.com/api/v3"), key,
                _first(
                    s.doubao_model if deep else s.doubao_fast_model,
                    s.llm_model,
                    "doubao-seed-2-1-pro-260628" if deep
                    else "doubao-seed-2-1-turbo-260628",
                ),
                extra_body=_doubao_extra_body(s, deep),
                timeout=_deep_timeout(s) if deep else 120,
                allow_mock_fallback=allow_mock,
                max_tokens=4096 if deep else 1024,
                retry_transient_once=deep,
            )

    if p == "hunyuan":
        key = s.hunyuan_api_key
        if key:
            return OpenAICompatLLM(
                "hunyuan", _first(s.hunyuan_base_url,
                                  "https://tokenhub.tencentmaas.com/v1"), key,
                _first(s.hunyuan_model, s.llm_model, "hy3"), timeout=_deep_timeout(s) if purpose == "deep" else 120,
                allow_mock_fallback=allow_mock,
                max_tokens=4096 if purpose == "deep" else 1024,
                retry_transient_once=purpose == "deep",
            )

    if p == "qwen":
        key = _first(s.qwen_api_key, s.aliyun_dashscope_api_key)
        if key:
            return OpenAICompatLLM(
                "qwen", _first(s.qwen_base_url,
                               "https://dashscope.aliyuncs.com/compatible-mode/v1"), key,
                _first(s.qwen_model, s.llm_model, "qwen-plus"), timeout=_deep_timeout(s) if purpose == "deep" else 120,
                extra_body={"enable_thinking": purpose == "deep"},
                allow_mock_fallback=allow_mock,
                max_tokens=4096 if purpose == "deep" else 1024,
                retry_transient_once=purpose == "deep",
            )

    if p in ("chatgpt", "compat"):
        key = _first(s.openai_api_key, s.llm_api_key)
        if key:
            return OpenAICompatLLM(
                "chatgpt", _first(s.openai_base_url, s.llm_base_url,
                                  "https://api.openai.com/v1"), key,
                _first(s.openai_model, s.llm_model, "gpt-5.6-terra"),
                timeout=_deep_timeout(s) if purpose == "deep" else 120, temperature=None,
                token_parameter="max_completion_tokens",
                allow_mock_fallback=allow_mock,
                max_tokens=4096 if purpose == "deep" else 1024,
                retry_transient_once=purpose == "deep",
            )

    # DeepSeek 官方 API：不作为豆包的隐式回退，也不在四家切换列表中展示。
    # 2026-09-27 起快速档经 LLM_FAST_PROVIDER=deepseek 走这里：快速档关闭思考（高频调用，要快要省），
    # 深度档不传 thinking，沿用模型默认。
    if p == "deepseek" and s.deepseek_api_key:
        deep = purpose == "deep"
        ds_deep = getattr(s, "deepseek_model", "")
        ds_fast = getattr(s, "deepseek_fast_model", "")

        def _ds_client(key: str, fallback: "OpenAICompatLLM | None" = None) -> OpenAICompatLLM:
            return OpenAICompatLLM(
                "deepseek", _first(getattr(s, "deepseek_base_url", ""), "https://api.deepseek.com"),
                key,
                _first(ds_deep if deep else ds_fast, ds_deep, ds_fast, s.llm_model, "deepseek-chat"),
                extra_body=None if deep else {"thinking": {"type": "disabled"}},
                timeout=_deep_timeout(s) if deep else 120,
                fallback=fallback,
                allow_mock_fallback=allow_mock,
                max_tokens=4096 if deep else 1024,
                retry_transient_once=deep,
            )

        # 2026-09-29 用户给了第二个账户的密钥：现用密钥调用失败（如余额用完）时，
        # 当次调用自动改用备用密钥，不用改配置也不用重启。
        backup_key = (getattr(s, "deepseek_api_key_backup", "") or "").strip()
        backup = (_ds_client(backup_key)
                  if backup_key and backup_key != s.deepseek_api_key else None)
        return _ds_client(s.deepseek_api_key, backup)

    return MockLLM()


def llm_status() -> dict:
    """返回可公开展示的当前配置，不包含任何 API Key。"""
    settings = get_settings()
    selected = canonical_provider(settings.llm_provider)
    try:
        client = get_llm("deep")
    except ValueError:
        client = MockLLM()
    configured = isinstance(client, OpenAICompatLLM)
    try:
        fast_client = get_llm("fast")
    except ValueError:
        fast_client = MockLLM()
    fast_configured = isinstance(fast_client, OpenAICompatLLM)
    return {
        "selected_provider": selected,
        "active_provider": client.name,
        "configured": configured,
        "configuration_error": llm_configuration_error(settings),
        "model": client.model if configured else "mock",
        "base_url": client.base_url if configured else "",
        "supported_providers": ["doubao", "hunyuan", "qwen", "deepseek", "chatgpt"],
        # 快速档可能由 LLM_FAST_PROVIDER 单独指定（如 DeepSeek 官方），一并展示便于核对
        "fast_provider": fast_client.name if fast_configured else "mock",
        "fast_model": fast_client.model if fast_configured else "mock",
    }


async def test_llm_connection() -> dict:
    """对当前选择的真实 provider 发起一次最小调用。"""
    try:
        client = get_llm("fast")
    except ValueError:
        s = get_settings()
        fast = _fast_provider(s)
        label, selected = (("LLM_FAST_PROVIDER", fast) if fast
                           else ("LLM_PROVIDER", canonical_provider(s.llm_provider)))
        raise ValueError(f"{label}={selected} 未配置对应 API Key") from None
    if not isinstance(client, OpenAICompatLLM):
        s = get_settings()
        fast = _fast_provider(s)
        label, selected = (("LLM_FAST_PROVIDER", fast) if fast
                           else ("LLM_PROVIDER", canonical_provider(s.llm_provider)))
        raise ValueError(f"{label}={selected} 未配置对应 API Key")
    content = await client.probe()
    return {
        "ok": True,
        "provider": client.name,
        "model": client.model,
        "reply": content.strip()[:120],
    }
