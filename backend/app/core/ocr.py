"""抽帧 OCR 抽象。

接口：recognize(jpeg_bytes) -> str，返回该帧画面上的文字。
图片本身由 capture 在回调后立即删除，OCR 只吃字节、不落盘。
去重/稳定化在 pipeline 里做（连续相同文字合并为一条带时段的 OcrRecord）。

- mock：按帧序循环产出"价格牌/型号牌"文字（含与口播不一致样例），用于无密钥演示。
- dashscope：阿里云百炼 Qwen-VL 多模态做 OCR（OpenAI 兼容 vision 接口）。
- deepseek：DeepSeek 官方 deepseek-flash（V4.1-Flash，支持看图）做 OCR，关闭思考（2026-09-27 起默认）。
"""

from __future__ import annotations

import base64

import httpx

from ..config import get_settings
from .safe_error import safe_error_text, safe_exception_code

# mock 弹幕（模拟观众评论）
_MOCK_SCREENS = [
    "主播声音好温柔 | 这个耳机多少钱 | 求链接",
    "Demo Air和演示旗舰哪个好 | 有优惠吗 | 已下单支持",
    "客服怎么联系 | 平板耳机好推吗 | 蹲一个秒杀",
]

# 只挑弹幕：让多模态模型判断、过滤广告/商品/价格/字幕等非弹幕文字
_OCR_PROMPT = (
    "这是一张直播间画面截图。请【只】提取画面中的弹幕（观众发送的滚动评论，"
    "通常位于画面左下方或中下方、半透明的文字流，常带观众昵称）。"
    "每条弹幕单独占一行，按出现顺序输出，可去掉昵称只保留评论内容。"
    "务必忽略：广告/活动横幅、商品名称与卖点、价格、主播昵称、按钮文字、"
    "贴纸、水印、画面字幕等一切非弹幕文字。若画面中没有弹幕，只回复两个字：无。"
)


class MockOCR:
    name = "mock"

    def __init__(self) -> None:
        self._i = 0

    async def recognize(self, jpeg: bytes) -> str:
        text = _MOCK_SCREENS[self._i % len(_MOCK_SCREENS)]
        self._i += 1
        return text


class OpenAICompatOCR:
    """OpenAI 兼容 vision 接口做 OCR 的公共实现（DashScope / DeepSeek 共用）。"""

    name = "openai_compat"
    URL = ""

    def __init__(self, api_key: str, model: str, *, url: str = "",
                 extra_body: dict | None = None, max_tokens: int | None = None) -> None:
        self.api_key = api_key
        self.model = model
        self.url = url or self.URL
        self.extra_body = extra_body or {}
        self.max_tokens = max_tokens

    def _payload(self, jpeg: bytes) -> dict:
        b64 = base64.b64encode(jpeg).decode()
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": _OCR_PROMPT},
                ],
            }],
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        payload.update(self.extra_body)
        return payload

    async def recognize(self, jpeg: bytes) -> str:
        payload = self._payload(jpeg)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(self.url, json=payload, headers=headers)
                r.raise_for_status()
                return (r.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:  # noqa: BLE001  OCR 出错不应中断采集
            print(
                f"[ocr:{self.name}] status=failed; "
                f"code={safe_exception_code(e, prefix='OCR')}; "
                f"error={safe_error_text(e)}"
            )
            return ""


class DashScopeOCR(OpenAICompatOCR):
    """阿里云百炼 Qwen-VL 做 OCR（OpenAI 兼容 vision 接口）。请求体与改造前一致。"""

    name = "dashscope"
    URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(api_key, model)


class DeepSeekOCR(OpenAICompatOCR):
    """DeepSeek 官方 deepseek-flash 看图挑弹幕。每 3 秒一帧，关闭思考、限制输出长度控成本。"""

    name = "deepseek"
    URL = "https://api.deepseek.com/chat/completions"

    def __init__(self, api_key: str, model: str, base_url: str = "") -> None:
        url = f"{base_url.rstrip('/')}/chat/completions" if base_url.strip() else ""
        super().__init__(api_key, model, url=url,
                         extra_body={"thinking": {"type": "disabled"}}, max_tokens=512)


def get_ocr():
    s = get_settings()
    provider = (s.ocr_provider or "mock").lower()
    if provider in ("dashscope", "aliyun") and s.aliyun_dashscope_api_key:
        return DashScopeOCR(s.aliyun_dashscope_api_key, s.ocr_model or "qwen-vl-max-latest")
    if provider == "deepseek" and s.deepseek_api_key:
        return DeepSeekOCR(s.deepseek_api_key, s.ocr_model or "deepseek-flash",
                           getattr(s, "deepseek_base_url", "") or "")
    return MockOCR()
