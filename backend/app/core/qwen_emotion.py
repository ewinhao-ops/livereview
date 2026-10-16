"""Qwen3-ASR-Flash 语音情绪识别（离线，段级重算用）。

用阿里百炼 qwen3-asr-flash 对一段音频直接识别说话人情绪（固定 7 类），比"声学(RMS/ZCR)+LLM 猜"
更准。同步接口：单次 ≤5 分钟 / ≤10MB，返回整段一个情绪标签 + 转写文本，无逐句时间戳。
无 key / 失败返回 None，调用方保留原有情绪，不影响主流程。
"""

from __future__ import annotations

from ..config import get_settings
from .safe_error import safe_error_text, safe_exception_code

_MODEL = "qwen3-asr-flash"

# 官方 7 类情绪 → 直播复盘中文标签
EMO_ZH = {
    "surprised": "惊讶",
    "neutral": "平稳",
    "happy": "热情愉快",
    "sad": "低落",
    "disgusted": "厌烦",
    "angry": "急躁",
    "fearful": "紧张",
}


def enabled() -> bool:
    return bool(get_settings().aliyun_dashscope_api_key)


def recognize_emotion(audio_path: str) -> dict | None:
    """本地音频文件（≤5 分钟/≤10MB）→ {emotion, emotion_label, text}；失败返回 None。"""
    s = get_settings()
    key = s.aliyun_dashscope_api_key
    if not key:
        return None
    try:
        import dashscope
        resp = dashscope.MultiModalConversation.call(
            model=_MODEL, api_key=key,
            messages=[{"role": "user",
                       "content": [{"audio": f"file://{audio_path}"}]}],
            result_format="message", asr_options={"enable_itn": True}, stream=False,
        )
        if getattr(resp, "status_code", None) != 200 or not resp.output:
            print(
                "[qwen_emotion] status=failed; code=PROVIDER_RESPONSE_ERROR; "
                f"http_status={str(getattr(resp, 'status_code', 'unknown'))[:3]}; "
                f"error={safe_error_text(getattr(resp, 'message', 'provider error'))}"
            )
            return None
        msg = (resp.output.get("choices") or [{}])[0].get("message", {})
        ann = (msg.get("annotations") or [{}])[0]
        emo = (ann.get("emotion") or "").strip()
        text = ""
        c = msg.get("content")
        if isinstance(c, list) and c and isinstance(c[0], dict):
            text = c[0].get("text", "") or ""
        elif isinstance(c, str):
            text = c
        if not emo:
            return None
        return {"emotion": emo, "emotion_label": EMO_ZH.get(emo, emo), "text": text}
    except Exception as e:  # noqa: BLE001
        print(
            "[qwen_emotion] status=failed; "
            f"code={safe_exception_code(e, prefix='QWEN_EMOTION')}; "
            f"error={safe_error_text(e)}"
        )
        return None
