from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core import ocr


def settings(**overrides) -> SimpleNamespace:
    values = {"ocr_provider": "mock", "ocr_model": "", "aliyun_dashscope_api_key": "",
              "deepseek_api_key": "", "deepseek_base_url": ""}
    values.update(overrides)
    return SimpleNamespace(**values)


class OCRProviderSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = ocr.get_settings

    def tearDown(self) -> None:
        ocr.get_settings = self.original

    def use(self, value: SimpleNamespace) -> None:
        ocr.get_settings = lambda: value

    def test_deepseek_official_vision_disables_thinking_and_caps_output(self) -> None:
        self.use(settings(ocr_provider="deepseek", ocr_model="deepseek-flash", deepseek_api_key="ds"))
        client = ocr.get_ocr()
        self.assertIsInstance(client, ocr.DeepSeekOCR)
        self.assertEqual(client.url, "https://api.deepseek.com/chat/completions")
        payload = client._payload(b"jpeg")
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["max_tokens"], 512)
        self.assertEqual(payload["messages"][0]["content"][0]["image_url"]["url"][:23],
                         "data:image/jpeg;base64,")

    def test_deepseek_defaults_to_flash_model_and_custom_host(self) -> None:
        self.use(settings(ocr_provider="deepseek", deepseek_api_key="ds",
                          deepseek_base_url="https://proxy.example/v1/"))
        client = ocr.get_ocr()
        self.assertEqual(client.model, "deepseek-flash")
        self.assertEqual(client.url, "https://proxy.example/v1/chat/completions")

    def test_dashscope_request_body_is_unchanged(self) -> None:
        self.use(settings(ocr_provider="dashscope", ocr_model="qwen-vl-max", aliyun_dashscope_api_key="ali"))
        client = ocr.get_ocr()
        self.assertIsInstance(client, ocr.DashScopeOCR)
        self.assertEqual(client.url, ocr.DashScopeOCR.URL)
        self.assertEqual(set(client._payload(b"jpeg")), {"model", "messages"})

    def test_missing_key_falls_back_to_mock_as_before(self) -> None:
        self.use(settings(ocr_provider="deepseek"))
        self.assertIsInstance(ocr.get_ocr(), ocr.MockOCR)


if __name__ == "__main__":
    unittest.main()
