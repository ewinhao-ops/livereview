"""公屏购买意向 AI 精析（结合上下文判定真伪）。

规则层（danmaku_signal）按关键词把"想要/买它"都记成购买意向，但观众可能只是
开玩笑/跟风刷屏。本模块取每条意向弹幕前后的主播话术 + 相邻公屏消息，交给 LLM
综合判定：真实意向 / 疑似玩笑或跟风 / 咨询了解。结果落库（danmaku_refines，
每场一条覆盖式），列表接口直接带出，不必每次重算。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select

from ..db import SessionLocal
from ..models import DanmakuRefine, OcrRecord, Transcript
from .danmaku_signal import classify_danmaku
from .llm import get_llm

_VERDICTS = ("真实意向", "疑似玩笑或跟风", "咨询了解")

_SYSTEM = (
    "你是直播电商复盘分析师。逐条判断观众公屏发言是否是真实购买意向。"
    "结合主播当时的话术和相邻公屏氛围综合判断：观众可能在开玩笑、跟风刷屏、"
    "或只是咨询了解，并非真的要买。返回 JSON 对象："
    '{"items":[{"i":序号,"verdict":"真实意向|疑似玩笑或跟风|咨询了解",'
    '"reason":"不超过40字的判定理由"}]}。verdict 只能取这三个值。'
)


def _mmss(ms: int) -> str:
    s = int(ms // 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


def _gather(db, session_id: int):
    """意向弹幕 + 每条的上下文（前后 45s 主播话术、前后各 3 条公屏）。"""
    ocr = db.execute(select(OcrRecord).where(OcrRecord.session_id == session_id)
                     .order_by(OcrRecord.start_ms)).scalars().all()
    trs = db.execute(select(Transcript).where(Transcript.session_id == session_id)
                     .order_by(Transcript.start_ms)).scalars().all()
    intents = [(idx, o) for idx, o in enumerate(ocr) if classify_danmaku(o.text) == "intent"]
    items = []
    for idx, o in intents[:40]:   # 一场最多精析 40 条，防 prompt 爆长
        talk = [t for t in trs if abs(t.start_ms - o.start_ms) <= 45_000]
        talk_text = " / ".join((t.corrected_text or t.raw_text or "").strip() for t in talk)[:400]
        around = ocr[max(0, idx - 3): idx] + ocr[idx + 1: idx + 4]
        around_text = " | ".join((a.text or "").strip()[:40] for a in around)[:300]
        items.append({
            "at": o.start_ms,
            "text": (o.text or "")[:80],
            "talk": talk_text or "（该时段无主播话术记录）",
            "around": around_text or "（无相邻公屏）",
        })
    return items


async def refine_session_intents(session_id: int) -> dict:
    """精析一场的购买意向弹幕并落库；返回 {total, items}。"""
    with SessionLocal() as db:
        items = _gather(db, session_id)
    if not items:
        result = {"total": 0, "items": [], "refined_at": datetime.now().isoformat()}
        _save(session_id, result)
        return result

    lines = []
    for i, it in enumerate(items):
        lines.append(
            f"[{i}] {_mmss(it['at'])} 观众说：「{it['text']}」\n"
            f"    主播当时话术：{it['talk']}\n"
            f"    相邻公屏：{it['around']}"
        )
    user = "以下是本场直播公屏中命中\"购买意向\"关键词的发言，请逐条判定：\n\n" + "\n\n".join(lines)

    raw = await get_llm("deep").complete(_SYSTEM, user, json_mode=True)
    verdict_map: dict[int, dict] = {}
    try:
        parsed = json.loads(raw)
        for x in parsed.get("items", []):
            i = int(x.get("i", -1))
            v = str(x.get("verdict", "")).strip()
            if 0 <= i < len(items) and v in _VERDICTS:
                verdict_map[i] = {"verdict": v, "reason": str(x.get("reason", ""))[:60]}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    out_items = []
    for i, it in enumerate(items):
        v = verdict_map.get(i, {"verdict": "咨询了解", "reason": "AI 未返回判定，按保守归类"})
        out_items.append({
            "at": it["at"], "text": it["text"],
            "verdict": v["verdict"], "reason": v["reason"],
            "context": {"talk": it["talk"], "around": it["around"]},
        })
    result = {
        "total": len(out_items),
        "genuine": sum(1 for x in out_items if x["verdict"] == "真实意向"),
        "joking": sum(1 for x in out_items if x["verdict"] == "疑似玩笑或跟风"),
        "asking": sum(1 for x in out_items if x["verdict"] == "咨询了解"),
        "items": out_items,
        "refined_at": datetime.now().isoformat(),
    }
    _save(session_id, result)
    return result


def _save(session_id: int, result: dict) -> None:
    with SessionLocal() as db:
        row = db.execute(select(DanmakuRefine)
                         .where(DanmakuRefine.session_id == session_id)).scalar_one_or_none()
        if row is None:
            row = DanmakuRefine(session_id=session_id)
            db.add(row)
        row.data = json.dumps(result, ensure_ascii=False)
        row.created_at = datetime.now()
        db.commit()


def load_refine(session_id: int) -> dict | None:
    with SessionLocal() as db:
        row = db.execute(select(DanmakuRefine)
                         .where(DanmakuRefine.session_id == session_id)).scalar_one_or_none()
    if not row or not row.data:
        return None
    try:
        return json.loads(row.data)
    except json.JSONDecodeError:
        return None
