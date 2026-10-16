"""跨场统计库（D4）：把多场复盘按主播沉淀成统计，看成长与共性问题。

供前端「数据统计」视图 + D5 实时预测提示的历史基线。纯读现有 Critique/summary。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Critique, CritiqueLevel, ReplaySession, SessionType, Streamer

router = APIRouter(prefix="/api/stats", tags=["stats"])


def _loads(data):
    try:
        return (json.loads(data) or {}) if data else {}
    except json.JSONDecodeError:
        return {}


def _item_text(x) -> str:
    if isinstance(x, dict):
        return str(x.get("action") or x.get("text") or x.get("issue") or x.get("title") or "")
    return str(x or "")


@router.get("/streamers")
def streamer_stats(db: Session = Depends(get_db)):
    """按主播跨场聚合：场次数/均分/维度均分/情绪分布/讲款闭环/常缺环节/高频问题。"""
    sname = {s.id: s.name for s in db.query(Streamer).all()}
    sums = db.execute(select(Critique).join(
        ReplaySession, ReplaySession.id == Critique.session_id,
    ).where(
        Critique.level == CritiqueLevel.summary,
        ReplaySession.session_type == SessionType.normal,
    )).scalars().all()

    agg: dict = defaultdict(lambda: {
        "sessions": set(), "scores": [], "dims": defaultdict(list),
        "issues": [], "loop_pcts": [], "often_missing": [], "emo": Counter()})
    for c in sums:
        d = _loads(c.data)
        a = agg[c.streamer_id]
        a["sessions"].add(c.session_id)
        if c.score is not None:
            a["scores"].append(c.score)
        for k, v in (d.get("dimensions") or {}).items():
            try:
                a["dims"][k].append(float(v))
            except (TypeError, ValueError):
                pass
        for it in (d.get("top_issues") or []):
            t = _item_text(it)
            if t:
                a["issues"].append(t[:40])
        lp = d.get("loop") or {}
        if lp.get("avg_completeness") is not None:
            a["loop_pcts"].append(lp["avg_completeness"])
        a["often_missing"] += lp.get("often_missing", [])
        for x in (d.get("emotion_overview") or {}).get("dist", []):
            a["emo"][x["label"]] += x.get("count", 0)

    def avg(xs):
        return round(sum(xs) / len(xs), 1) if xs else None

    out = []
    for sid, a in agg.items():
        emo_total = sum(a["emo"].values())
        out.append({
            "streamer_id": sid, "name": sname.get(sid) or "未指定主播",
            "sessions": len(a["sessions"]),
            "avg_score": avg(a["scores"]),
            "dims": {k: avg(v) for k, v in a["dims"].items()},
            "loop_avg": avg(a["loop_pcts"]),
            "often_missing": [w for w, _ in Counter(a["often_missing"]).most_common(3)],
            "emotion_dist": [{"label": k, "pct": round(v * 100 / emo_total)}
                             for k, v in a["emo"].most_common()] if emo_total else [],
            "top_issues": [t for t, _ in Counter(a["issues"]).most_common(5)],
        })
    out.sort(key=lambda x: (-x["sessions"], -(x["avg_score"] or 0)))
    return out
