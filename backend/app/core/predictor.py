"""实时预测提示引擎（D5）：直播中按该主播历史规律，对当前段发预警 + 概率。

原理：查该主播历史所有讲款/逼单段的讲款闭环，统计各环节的历史缺失率；
若当前段也缺该环，且历史缺失率高，就实时提示"你历史 X% 讲款段漏了促单，本段也缺"。
样本不足(<3 段)不预测，避免噪音。同步 DB 查，pipeline 用 asyncio.to_thread 调。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Critique, CritiqueLevel

_WATCH_STEPS = ["促单下单", "价格", "适配建议", "痛点人群", "核心卖点"]


def predict(streamer_id: int | None, scene: str, cur_loop: dict | None,
            current_session_id: int | None = None) -> list[dict]:
    """当前讲款/逼单段 + 该主播【往期其它场次】历史 → 预警提示列表 [{tip, prob, step}]。

    历史基线必须排除当前场次，否则会把本场刚结算的段算进去，自我强化 + 用当前段预测当前段。
    """
    if not streamer_id or scene not in ("product", "urge") or not cur_loop:
        return []
    cur_missing = set(cur_loop.get("missing") or [])
    if not cur_missing:
        return []
    with SessionLocal() as db:
        q = select(Critique).where(Critique.streamer_id == streamer_id,
                                   Critique.level == CritiqueLevel.segment)
        if current_session_id:
            q = q.where(Critique.session_id != current_session_id)
        segs = db.execute(q).scalars().all()
    loops = []
    for c in segs:
        try:
            lp = (json.loads(c.data) or {}).get("loop") if c.data else None
        except json.JSONDecodeError:
            lp = None
        if lp and lp.get("steps"):
            loops.append(lp)
    n = len(loops)
    if n < 3:                      # 历史样本太少，不预测（新主播先积累）
        return []

    tips = []
    for step in _WATCH_STEPS:
        if step not in cur_missing:
            continue
        miss = sum(1 for lp in loops
                   if step in [s["step"] for s in lp["steps"] if not s.get("ok")])
        prob = round(miss * 100 / n)
        if prob >= 50:
            tips.append({
                "step": step, "prob": prob,
                "tip": f"该主播历史 {prob}% 的讲款段漏了「{step}」，本段目前也缺——"
                       f"建议立刻补上（如报价后马上「点小黄车拍下」）。",
            })
    tips.sort(key=lambda x: -x["prob"])
    return tips[:2]
