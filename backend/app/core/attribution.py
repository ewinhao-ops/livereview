"""成交归因（D3）：把成交时刻映射到当时话术段，判断是话术驱动还是自然成交。

成交事件两个来源：
  - 公屏下单信号（弹幕 intent：拍了/买了/上车…）
  - 罗盘成交增量（CompassPoint 里"成交/订单"数字增长）
对每个成交时刻，找其所在/紧邻之前的点评段，看是否处于逼单场景或讲款闭环有促单环：
  有 → 话术驱动；否则 → 自然成交（或引导缺失）。
"""

from __future__ import annotations

import json
import re

from sqlalchemy import select

from ..db import SessionLocal
from ..models import (
    Critique, CritiqueLevel, CompassPoint, OcrRecord,
    ReplaySession, SessionType,
)
from .danmaku_signal import classify_danmaku


def _loads(s):
    try:
        return (json.loads(s) or {}) if s else {}
    except json.JSONDecodeError:
        return {}


def _num(v) -> float | None:
    """从'成交10单/12.5万/1,234'提数；带'万/亿'换算，'%/率'等比率跳过（不是绝对量）。"""
    s = str(v or "")
    if "%" in s or "率" in s or "％" in s:
        return None
    m = re.search(r"[0-9][0-9,]*\.?[0-9]*", s)
    if not m:
        return None
    try:
        x = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    if "亿" in s:
        x *= 1e8
    elif "万" in s:
        x *= 1e4
    return x


def _seg_at(seg_index: list, at: int):
    """找最近的 start<=at 的段。seg_index: [(start, scene_label, scene, has_cta, comment)]。"""
    hit = None
    for s in seg_index:
        if s[0] <= at:
            hit = s
        else:
            break
    return hit


def analyze_attribution(session_id: int, streamer_id: int | None = None) -> dict:
    with SessionLocal() as db:
        session = db.get(ReplaySession, session_id)
        if session is not None and session.session_type == SessionType.observation:
            return {
                "events": [], "total": 0, "driven": 0, "natural": 0,
                "dominant": "", "tips": ["观摩学习场次不执行罗盘成交归因。"],
                "disabled": True,
            }
        seg_q = select(Critique).where(
            Critique.session_id == session_id, Critique.level == CritiqueLevel.segment)
        ocr_q = select(OcrRecord).where(OcrRecord.session_id == session_id)
        if streamer_id is not None:
            seg_q = seg_q.where(Critique.streamer_id == streamer_id)
            ocr_q = ocr_q.where(OcrRecord.streamer_id == streamer_id)
        segs = db.execute(seg_q.order_by(Critique.start_ms)).scalars().all()
        ocr = db.execute(ocr_q.order_by(OcrRecord.start_ms)).scalars().all()
        compass = db.execute(select(CompassPoint).where(CompassPoint.session_id == session_id)
                             .order_by(CompassPoint.start_ms)).scalars().all()

    seg_index = []
    for c in segs:
        d = _loads(c.data)
        scene = d.get("scene", "")
        covered = (d.get("loop") or {}).get("covered") or []
        has_cta = scene == "urge" or "促单下单" in covered
        seg_index.append((c.start_ms, d.get("scene_label") or "通用", scene, has_cta, c.comment or ""))

    def make_event(at, source, note):
        seg = _seg_at(seg_index, at)
        scene_label = seg[1] if seg else ""
        has_cta = seg[3] if seg else False
        verdict = "话术驱动" if has_cta else "自然/引导缺失"
        return {"at": at, "source": source, "note": note[:24],
                "scene": scene_label, "verdict": verdict}

    events = []
    for o in ocr:
        if classify_danmaku(o.text) == "intent":
            events.append(make_event(o.start_ms, "公屏下单", o.text))
    # 锁定单一成交指标键（序列中首个出现的），避免"成交单量"与"成交金额"跨量纲相减出假增量。
    # 键名=罗盘大屏标题原文（2026-07 采集脚本改全等匹配后），优先件数/订单（每一单=一个成交事件）
    sale_keys = ["成交件数", "成交订单数", "成交人数",
                 "直播间用户支付金额", "直播间成交金额", "成交金额",
                 "成交", "订单", "销量"]
    key = None
    for p in compass:
        d = _loads(p.data)
        key = next((k for k in sale_keys if _num(d.get(k)) is not None), None)
        if key:
            break
    prev = None
    if key:
        for p in compass:
            v = _num(_loads(p.data).get(key))
            if v is None:
                continue
            if prev is not None and v > prev:
                events.append(make_event(p.start_ms, "罗盘成交", f"{key}+{round(v - prev, 2)}"))
            prev = v
    events.sort(key=lambda e: e["at"])

    driven = sum(1 for e in events if e["verdict"] == "话术驱动")
    total = len(events)
    tips = []
    if total:
        pct = round(driven * 100 / total)
        tips.append(f"共捕捉到 {total} 个成交信号，其中 {driven} 个（{pct}%）发生在逼单/促单话术之后，"
                    "可视为话术驱动；其余发生在非促单时段，更接近自然成交或引导缺失。")
        if driven < total * 0.5:
            tips.append("过半成交并非紧跟促单话术，说明主播承接转化的能力有提升空间——"
                        "观察到购买信号时应立即逼单收口。")
    else:
        tips.append("暂无成交信号（需接入罗盘书签或公屏出现明确下单话术后才能归因）。")

    return {"events": events, "total": total, "driven": driven,
            "natural": total - driven,
            "dominant": ("话术驱动" if driven >= total - driven else "自然成交") if total else "",
            "tips": tips}
