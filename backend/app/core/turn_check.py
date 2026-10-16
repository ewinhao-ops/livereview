"""收播核对班次（2026-10-04）：拿业绩系统的上下播打卡（signal_events）对一遍每段班次记在谁名下。

起因：9-29 那场最后 17 分钟杨雪的话被记到了杨柳青名下（杨柳青当天 12:03 就下播了），杨雪零点的下播信号
因此被当成「当前主播非本人」忽略；换人的触发原因日志里已查不到。

只自动改最有把握的一种：这段记的主播这段时间里没有打卡，而且当天更早已经下播；同时只有另一位主播在打卡状态
（这段时间被他的打卡覆盖八成以上）→ 改到那位名下。其余对不上的只打日志、不改——有人忘了打卡时，
按打卡改反而会改错。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select, update

from ..db import SessionLocal
from ..models import (Critique, CritiqueLevel, LiveAlert, OcrRecord, ReplaySession, SignalEvent,
                      StreamerTurn, Transcript)

TOLERANCE = timedelta(minutes=10)     # 录音时间轴和墙上时间会差几分钟
MIN_TURN = timedelta(minutes=2)       # 太短的段不核对
LOOKBACK = timedelta(hours=14)        # 往前找多久的打卡（开录前就打卡的、早班已经下播的）

Interval = tuple[datetime, datetime | None]


def clock_intervals(events: list[tuple[int, str, datetime]], window_start: datetime) -> dict[int, list[Interval]]:
    """[(主播, on/off, 时间)] → 每位主播的打卡区间；off 前找不到 on 的当作窗口开始就在播，最后没 off 的到现在都算。"""
    by: dict[int, list[Interval]] = defaultdict(list)
    opened: dict[int, datetime] = {}
    for sid, action, at in sorted(events, key=lambda e: e[2]):
        if action == "on":
            opened.setdefault(sid, at)
        elif action == "off":
            by[sid].append((opened.pop(sid, window_start), at))
    for sid, at in opened.items():
        by[sid].append((at, None))
    return by


def _covered(start: datetime, end: datetime, intervals: list[Interval]) -> float:
    """[start, end] 被这些区间（各向外放宽 TOLERANCE）盖住的秒数。"""
    total = 0.0
    for a, b in intervals:
        lo = max(start, a - TOLERANCE)
        hi = min(end, (b + TOLERANCE) if b is not None else end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


def decide(turns: list[tuple[int, int, int, int]], started_at: datetime,
           intervals: dict[int, list[Interval]]) -> tuple[dict[int, int], list[str]]:
    """turns: [(行 id, 主播, start_ms, end_ms)] → ({行 id: 改到的主播}, 只提醒不改的说明)。"""
    moves: dict[int, int] = {}
    notes: list[str] = []
    for row_id, sid, a_ms, b_ms in turns:
        start = started_at + timedelta(milliseconds=a_ms)
        end = started_at + timedelta(milliseconds=b_ms)
        span = (end - start).total_seconds()
        if end - start < MIN_TURN:
            continue
        own = intervals.get(sid, [])
        if _covered(start, end, own) >= span * 0.5:
            continue
        others = [o for o, ivs in intervals.items() if o != sid and _covered(start, end, ivs) >= span * 0.8]
        left_earlier = any(b is not None and b + TOLERANCE <= start for _a, b in own)
        if _covered(start, end, own) == 0 and left_earlier and len(others) == 1:
            moves[row_id] = others[0]
        else:
            notes.append(f"主播{sid} {start:%H:%M}~{end:%H:%M} 这段和打卡对不上（本人打卡覆盖不到一半；"
                         f"这段时间在打卡的：{others or '没有'}），没自动改，请人工核对")
    return moves, notes


def reconcile_turns(session_id: int) -> set[int]:
    """核对并改正；返回拿到别人误记段落的主播（他们已出的总结要重出）。"""
    with SessionLocal() as db:
        sess = db.get(ReplaySession, session_id)
        if sess is None or sess.started_at is None:
            return set()
        started = sess.started_at
        window_end = (sess.ended_at or datetime.now()) + timedelta(hours=1)
        events = [(e.streamer_id, e.action, e.occurred_at) for e in db.execute(select(SignalEvent).where(
            SignalEvent.occurred_at >= started - LOOKBACK, SignalEvent.occurred_at <= window_end,
            SignalEvent.streamer_id.is_not(None), SignalEvent.action.in_(("on", "off")),
        )).scalars() if e.occurred_at is not None]
        if not events:
            return set()
        intervals = clock_intervals(events, started - LOOKBACK)
        rows = db.execute(select(StreamerTurn).where(StreamerTurn.session_id == session_id)
                          .order_by(StreamerTurn.start_ms, StreamerTurn.seq)).scalars().all()
        moves, notes = decide([(t.id, t.streamer_id, int(t.start_ms or 0), int(t.end_ms or 0)) for t in rows],
                              started, intervals)
        for note in notes:
            print(f"[turns] ⚠️ 场次#{session_id} {note}")
        if not moves:
            return set()
        gained: set[int] = set()
        by_id = {t.id: t for t in rows}
        for row_id, new_sid in moves.items():
            turn = by_id[row_id]
            old_sid, a_ms, b_ms = turn.streamer_id, int(turn.start_ms or 0), int(turn.end_ms or 0)
            for model, col in ((Transcript, Transcript.start_ms), (OcrRecord, OcrRecord.start_ms),
                               (LiveAlert, LiveAlert.at_ms)):
                db.execute(update(model).where(
                    model.session_id == session_id, model.streamer_id == old_sid, col >= a_ms, col < b_ms,
                ).values(streamer_id=new_sid))
            db.execute(update(Critique).where(
                Critique.session_id == session_id, Critique.streamer_id == old_sid,
                Critique.level != CritiqueLevel.summary, Critique.start_ms >= a_ms, Critique.start_ms < b_ms,
            ).values(streamer_id=new_sid))
            turn.streamer_id = new_sid
            gained.add(new_sid)
            print(f"[turns] 场次#{session_id} {started + timedelta(milliseconds=a_ms):%H:%M}~"
                  f"{started + timedelta(milliseconds=b_ms):%H:%M} 从主播{old_sid}改到主播{new_sid}"
                  f"（主播{old_sid}这段时间没打卡、更早已下播；只有主播{new_sid}在打卡）")
        # 同一位主播首尾相接的段合成一段（免得开场核对把它当成又一次接班）
        merged: list[StreamerTurn] = []
        for turn in sorted(rows, key=lambda t: (int(t.start_ms or 0), t.seq)):
            if merged and merged[-1].streamer_id == turn.streamer_id and int(turn.start_ms or 0) <= int(merged[-1].end_ms or 0):
                merged[-1].end_ms = max(int(merged[-1].end_ms or 0), int(turn.end_ms or 0))
                db.delete(turn)
            else:
                merged.append(turn)
        if sess.current_streamer_id is not None and merged:
            sess.current_streamer_id = merged[-1].streamer_id
        db.commit()
        return gained
