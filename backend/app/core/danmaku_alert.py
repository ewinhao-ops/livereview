"""弹幕异常的即时、免模型检测与主播优化建议。"""

from __future__ import annotations

from .danmaku_signal import classify_danmaku


def analyze_danmaku_alert(content: str) -> dict | None:
    text = " ".join((content or "").split())[:500]
    signal = classify_danmaku(text)
    if signal == "negative":
        return {
            "kind": "danmaku_anomaly",
            "severity": "warn",
            "title": "弹幕负向反馈",
            "message": "检测到负向弹幕，请及时确认用户真实问题，避免情绪继续扩散。",
            "quote": text,
            "suggestion": "先回应“看到你的反馈了”，再询问具体型号/订单问题，并交由场控跟进。",
        }
    if signal == "objection":
        return {
            "kind": "danmaku_anomaly",
            "severity": "warn",
            "title": "弹幕价格异议",
            "message": "检测到价格或价值异议，当前卖点解释可能不足。",
            "quote": text,
            "suggestion": "先说明适用人群和核心差异，再核对活动权益；不要临时编造优惠。",
        }
    return None
