from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core import llm


def settings(provider: str, **overrides) -> SimpleNamespace:
    values = {
        "llm_provider": provider,
        "llm_base_url": "",
        "llm_model": "",
        "llm_api_key": "",
        "doubao_base_url": "",
        "doubao_model": "",
        "doubao_fast_model": "",
        "hunyuan_api_key": "",
        "hunyuan_base_url": "",
        "hunyuan_model": "",
        "qwen_api_key": "",
        "qwen_base_url": "",
        "qwen_model": "",
        "openai_api_key": "",
        "openai_base_url": "",
        "openai_model": "",
        "aliyun_dashscope_api_key": "",
        "deepseek_api_key": "",
        "ark_api_key": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class LLMProviderSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original_get_settings = llm.get_settings

    def tearDown(self) -> None:
        llm.get_settings = self.original_get_settings

    def use(self, value: SimpleNamespace) -> None:
        llm.get_settings = lambda: value

    def test_doubao_uses_doubao_models_without_deepseek_fallback(self) -> None:
        self.use(settings("doubao", ark_api_key="ark-secret"))
        fast = llm.get_llm("fast")
        deep = llm.get_llm("deep")
        self.assertEqual(fast.name, "doubao")
        self.assertEqual(fast.model, "doubao-seed-2-1-turbo-260628")
        self.assertEqual(deep.model, "doubao-seed-2-1-pro-260628")
        self.assertIsNone(fast.fallback)
        self.assertNotIn("deepseek", fast.model)

    def test_fast_provider_override_routes_fast_tier_to_deepseek_official(self) -> None:
        self.use(settings(
            "doubao", ark_api_key="ark-secret", deepseek_api_key="ds-secret",
            doubao_model="doubao-seed-2-1-pro-260915", llm_fast_provider="deepseek",
            deepseek_fast_model="deepseek-flash",
        ))
        fast = llm.get_llm("fast")
        deep = llm.get_llm("deep")
        self.assertEqual(fast.name, "deepseek")
        self.assertEqual(fast.base_url, "https://api.deepseek.com")
        self.assertEqual(fast.model, "deepseek-flash")
        self.assertEqual(fast.extra_body, {"thinking": {"type": "disabled"}})
        self.assertEqual(fast.max_tokens, 1024)
        self.assertEqual(deep.name, "doubao")
        self.assertEqual(deep.model, "doubao-seed-2-1-pro-260915")
        self.assertEqual(llm.llm_configuration_error(llm.get_settings()), "")
        status = llm.llm_status()
        self.assertEqual((status["active_provider"], status["fast_provider"]), ("doubao", "deepseek"))
        self.assertEqual(status["fast_model"], "deepseek-flash")
        self.assertNotIn("ds-secret", str(status))

    def test_fast_provider_without_key_is_reported_not_silently_mocked(self) -> None:
        self.use(settings("doubao", ark_api_key="ark-secret", doubao_model="doubao-seed-2-1-pro-260915",
                          llm_fast_provider="deepseek", deepseek_fast_model="deepseek-flash"))
        self.assertEqual(llm.llm_configuration_error(llm.get_settings()),
                         "LLM_FAST_PROVIDER=deepseek 缺少 API Key")

    def test_empty_fast_provider_keeps_single_provider_behaviour(self) -> None:
        self.use(settings("doubao", ark_api_key="ark-secret", llm_fast_provider=""))
        self.assertEqual(llm.get_llm("fast").name, "doubao")

    def test_hunyuan_can_be_selected_with_alias(self) -> None:
        self.use(settings("腾讯混元", hunyuan_api_key="hy-secret"))
        client = llm.get_llm("deep")
        self.assertEqual(client.name, "hunyuan")
        self.assertEqual(client.base_url, "https://tokenhub.tencentmaas.com/v1")
        self.assertEqual(client.model, "hy3")

    def test_qwen_prefers_new_key_and_allows_custom_api_host(self) -> None:
        self.use(settings(
            "qwen", qwen_api_key="qwen-secret", qwen_base_url="https://workspace.example/v1",
            qwen_model="qwen-custom",
        ))
        client = llm.get_llm()
        self.assertEqual(client.name, "qwen")
        self.assertEqual(client.base_url, "https://workspace.example/v1")
        self.assertEqual(client.model, "qwen-custom")

    def test_chatgpt_alias_uses_openai_specific_configuration(self) -> None:
        self.use(settings("openai", openai_api_key="openai-secret"))
        client = llm.get_llm()
        self.assertEqual(client.name, "chatgpt")
        self.assertEqual(client.base_url, "https://api.openai.com/v1")
        self.assertEqual(client.model, "gpt-5.6-terra")
        self.assertEqual(client.token_parameter, "max_completion_tokens")
        self.assertIsNone(client.temperature)

    def test_all_keys_can_coexist_and_provider_is_the_only_switch(self) -> None:
        common = {
            "ark_api_key": "ark-secret",
            "hunyuan_api_key": "hy-secret",
            "qwen_api_key": "qwen-secret",
            "openai_api_key": "openai-secret",
        }
        for provider in ("doubao", "hunyuan", "qwen", "chatgpt"):
            with self.subTest(provider=provider):
                self.use(settings(provider, **common))
                self.assertEqual(llm.get_llm("deep").name, provider)

    async def test_connection_test_rejects_missing_key_instead_of_using_mock(self) -> None:
        self.use(settings("hunyuan"))
        with self.assertRaisesRegex(ValueError, "未配置对应 API Key"):
            await llm.test_llm_connection()

    def test_public_status_never_contains_key(self) -> None:
        self.use(settings("chatgpt", openai_api_key="must-not-leak"))
        status = llm.llm_status()
        self.assertTrue(status["configured"])
        self.assertEqual(status["active_provider"], "chatgpt")
        self.assertNotIn("must-not-leak", str(status))
        self.assertEqual(
            status["supported_providers"], ["doubao", "hunyuan", "qwen", "deepseek", "chatgpt"],
        )

    async def test_failed_call_logs_only_safe_request_model_latency_and_code(self) -> None:
        client = llm.OpenAICompatLLM(
            "qwen", "https://dashscope.example/v1", "top-secret", "qwen-plus",
        )
        client._request = AsyncMock(side_effect=RuntimeError())
        with self.assertLogs("app.core.llm", level="WARNING") as captured:
            result = await client.complete("system", "user", json_mode=True)
        log_text = "\n".join(captured.output)
        self.assertRegex(log_text, r"request_id=[0-9a-f]{12}")
        self.assertIn("model=qwen-plus", log_text)
        self.assertRegex(log_text, r"elapsed_ms=\d+")
        self.assertIn("code=LLM_UNKNOWN", log_text)
        self.assertNotIn("RuntimeError", log_text)
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.requested_provider, "qwen")
        self.assertEqual(result.provider, "mock")

    async def test_fallback_log_redacts_credentials_url_query_and_request_content(self) -> None:
        client = llm.OpenAICompatLLM(
            "qwen", "https://dashscope.example/v1", "top-secret", "qwen-plus",
        )
        leaked = (
            "Authorization: Bearer abc.secret Token=token-value "
            "https://example.test/chat?api_key=query-secret "
            'messages=[{"content":"full private request"}]'
        )
        client._request = AsyncMock(side_effect=RuntimeError(leaked))
        with self.assertLogs("app.core.llm", level="WARNING") as captured:
            await client.complete("system secret", "user secret")
        log_text = "\n".join(captured.output)
        for secret in ("abc.secret", "token-value", "query-secret", "full private request"):
            self.assertNotIn(secret, log_text)
        self.assertIn("code=LLM_UNKNOWN", log_text)


if __name__ == "__main__":
    unittest.main()
