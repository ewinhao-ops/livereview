"""2026-10-04 复盘准确性修正：识别错字纠正、总结提示（价格不核对 / 不用关键词讲款百分比 / 冷场看长空白）、
报告状态刷新、深度复盘价格分级、收播班次核对。模型一律用假的。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core import summary_service, turn_check
from app.core.review import asr_fix, summary
from app.db import Base
from app.models import (Critique, CritiqueLevel, OcrRecord, ReplaySession, SessionStatus, SignalEvent,
                        Streamer, StreamerTurn, Transcript)


# ---------------- 识别错字纠正 ----------------

@pytest.mark.parametrize("raw, fixed", [
    ("像咱家的中额这个耳机", "像咱家的中鹅这个耳机"),
    ("大额和中俄的区别", "大鹅和中鹅的区别"),
    ("小额也可以", "小鹅也可以"),
    ("叉S和叉V还有叉威", "XS和XV还有XV"),
    ("今天有大额券可以领", "今天有大额券可以领"),            # 正常用词不动
    ("小额支付也行", "小额支付也行"),
    ("额度不够", "额度不够"),
    ("金额是多少", "金额是多少"),
    ("", ""),
])
def test_fix_text(raw, fixed):
    assert asr_fix.fix_text(raw) == fixed


# ---------------- 总结提示 ----------------

def _capture_prompt(monkeypatch, rhythm=None, scenes=None, loops=None):
    seen = {}

    class FakeLLM:
        async def complete(self, system, user, json_mode=False):
            seen["system"], seen["user"] = system, user
            return json.dumps({"summary": "总评", "top_issues": [], "action_items": []}, ensure_ascii=False)

    async def fake_embed(_text):
        return None

    async def fake_retrieve(*_a, **_k):
        return [{"kind": "产品", "text": "中鹅 官方价 449 元"}]

    async def fake_personal(*_a, **_k):
        return [], "（无）", "（无）"

    monkeypatch.setattr(summary, "get_llm", lambda _tier: FakeLLM())
    monkeypatch.setattr(summary, "embed_one", fake_embed)
    monkeypatch.setattr(summary, "retrieve_detailed", fake_retrieve)
    monkeypatch.setattr(summary, "_retrieve_personalized_experience", fake_personal)
    data = asyncio.run(summary.review_summary(
        "逐字稿", [], ["段1"], rhythm=rhythm, emotions=[], scenes=scenes or ["产品讲款", "逼单促单"],
        loops=loops or [{"steps": [{"step": "核心卖点", "ok": False}], "pct": 17}]))
    return seen, data


def test_summary_prompt_rules(monkeypatch):
    rhythm = {"avg_wpm": 220, "total_cold_opens": 318, "max_pause_ms": 47000, "avg_repeat_ratio": 0.1,
              "total_ask": 10, "total_danmaku": 5, "products": [], "segments": 3,
              "long_gaps": {"over_45": 2, "over_90": 0, "max_sec": 70, "over_cap": 1, "cap_sec": 180}}
    seen, data = _capture_prompt(monkeypatch, rhythm=rhythm)
    system, user = seen["system"], seen["user"]
    assert "价格不做核对" in system and "不要拿知识库或官网的价格去核对" in system
    assert "识别错字不算口误" in system and "冷场只看长空白" in system
    assert "价格随直播活动变化，不用来核对主播口播价" in user
    assert "讲款闭环诊断" not in user and "完整度" not in user            # 关键词百分比不再给模型
    assert "按关键词粗估" in user
    assert "45 秒以上 2 次、90 秒以上 0 次，最长 70 秒" in user
    assert ">3s" not in user and "318" not in user                        # 3 秒级停顿不当冷场
    assert data["loop"]                                                     # 数据照存（导出/内部用）


def test_summary_prompt_without_long_gap_stats(monkeypatch):
    seen, _ = _capture_prompt(monkeypatch, rhythm={"avg_wpm": 200})
    assert "冷场：本场没有可用的长空白统计" in seen["user"]


# ---------------- 总结服务：纠错、长空白、报告状态 ----------------

@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'acc.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(summary_service, "SessionLocal", factory)
    monkeypatch.setattr(turn_check, "SessionLocal", factory)
    summary_service._inflight.clear()
    with factory() as s:
        for sid in (1, 2, 3, 4, 5):
            s.add(Streamer(id=sid, name=f"主播{sid}"))
        s.commit()
    try:
        yield factory
    finally:
        summary_service._inflight.clear()
        engine.dispose()


def _session(factory, **fields) -> int:
    with factory() as s:
        values = {"title": "t", "status": SessionStatus.done, "started_at": datetime(2026, 9, 29, 8, 0),
                  "ended_at": datetime(2026, 9, 30, 0, 5), **fields}
        row = ReplaySession(**values)
        s.add(row)
        s.commit()
        return row.id


def test_summary_inputs_fix_asr_and_count_long_gaps(db, monkeypatch):
    sid = _session(db)
    with db() as s:
        s.add(StreamerTurn(session_id=sid, streamer_id=1, seq=0, start_ms=0, end_ms=600_000))
        for i, (start, text) in enumerate([(0, "中额这款"), (10_000, "大额也行"), (80_000, "空了一分多钟"),
                                           (100_000, "再说一句"), (400_000, "放歌回来了")]):
            s.add(Transcript(session_id=sid, streamer_id=1, seq=i, start_ms=start, end_ms=start + 3000,
                             raw_text=text, corrected_text=text))
        s.add(Critique(session_id=sid, streamer_id=1, level=CritiqueLevel.segment, category="段",
                       comment="主播说中俄", start_ms=0, end_ms=1000, data="{}"))
        s.commit()
    inputs = summary_service._read_inputs(sid, 1)
    assert "中鹅这款" in inputs["digest"] and "大鹅也行" in inputs["digest"]
    assert inputs["segment_cards"] == ["主播说中鹅"]
    gaps = inputs["long_gaps"]
    assert gaps["over_45"] == 1 and gaps["over_90"] == 0 and gaps["max_sec"] == 67   # 13s→80s 空了 67 秒
    assert gaps["over_cap"] == 1                                                         # 103s→400s 近 5 分钟另计


def test_refresh_report_status(db):
    sid = _session(db, report_status="partial", error_code="SUMMARY_GENERATION_FAILED", error_msg="整场总结生成失败")
    with db() as s:
        s.add(Transcript(session_id=sid, streamer_id=1, seq=0, start_ms=0, end_ms=1, raw_text="a"))
        s.add(Transcript(session_id=sid, streamer_id=2, seq=1, start_ms=5, end_ms=6, raw_text="b"))
        s.add(Critique(session_id=sid, streamer_id=1, level=CritiqueLevel.summary, category="整场总结", data="{}"))
        s.commit()
    assert summary_service.refresh_report_status(sid) is False          # 主播 2 还没总结
    with db() as s:
        s.add(Critique(session_id=sid, streamer_id=2, level=CritiqueLevel.summary, category="整场总结", data="{}"))
        s.commit()
    assert summary_service.refresh_report_status(sid) is True
    with db() as s:
        row = s.get(ReplaySession, sid)
        assert (row.report_status, row.error_code, row.error_msg) == ("ready", "", "")
    other = _session(db, report_status="partial", error_code="MEDIA_X", error_msg="别的原因")
    assert summary_service.refresh_report_status(other) is False       # 别的失败原因不动


def test_generate_summary_refreshes_status(db, monkeypatch):
    sid = _session(db, report_status="partial", error_code="SUMMARY_GENERATION_FAILED", error_msg="x")
    with db() as s:
        s.add(Transcript(session_id=sid, streamer_id=1, seq=0, start_ms=0, end_ms=1, raw_text="a"))
        s.commit()

    async def fake_review(*_a, **_k):
        return {"summary": "ok", "overall_score": 6}

    monkeypatch.setattr(summary_service, "review_summary", fake_review)
    asyncio.run(summary_service.generate_summary(sid, 1))
    with db() as s:
        assert s.get(ReplaySession, sid).report_status == "ready"


# ---------------- 深度复盘：价格不进质检 ----------------

def test_deep_analysis_price_rule():
    from app.core.review import deep_analysis
    assert deep_analysis._PRICE_RE.search("报价 445 元和知识库不一致")
    assert not deep_analysis._PRICE_RE.search("动圈单元的参数讲错了")


# ---------------- 收播班次核对 ----------------

DAY = datetime(2026, 9, 29, 8, 9)


def _iv(*pairs):
    return [(DAY.replace(hour=a[0], minute=a[1]), None if b is None else DAY.replace(hour=b[0], minute=b[1]))
            for a, b in pairs]


def test_decide_moves_only_the_clear_case():
    ms = lambda h, m: int(((DAY.replace(hour=h, minute=m)) - DAY).total_seconds() * 1000)
    intervals = {3: _iv(((8, 0), (12, 3))), 1: _iv(((20, 0), (23, 59)))}
    turns = [(10, 3, 0, ms(12, 3)), (11, 1, ms(20, 0), ms(23, 46)), (12, 3, ms(23, 46), ms(23, 59))]  # noqa: E501
    moves, notes = turn_check.decide(turns, DAY, intervals)
    assert moves == {12: 1} and notes == []
    # 本人当天没打过卡（可能忘了打卡）：只提醒、不改
    moves, notes = turn_check.decide([(13, 5, ms(21, 0), ms(21, 30))], DAY, intervals)
    assert moves == {} and len(notes) == 1
    # 同时有两个人在打卡：不改
    two = {3: _iv(((8, 0), (12, 3))), 1: _iv(((20, 0), (23, 59))), 4: _iv(((20, 0), (23, 59)))}
    moves, notes = turn_check.decide([(12, 3, ms(23, 0), ms(23, 30))], DAY, two)
    assert moves == {} and len(notes) == 1


def test_reconcile_turns_reassigns_and_merges(db):
    sid = _session(db, started_at=DAY)
    ms = lambda h, m: int(((DAY.replace(hour=h, minute=m)) - DAY).total_seconds() * 1000)
    with db() as s:
        s.get(ReplaySession, sid).current_streamer_id = 3
        for i, (who, a, b) in enumerate([(3, 0, ms(12, 3)), (1, ms(20, 0), ms(23, 46)), (3, ms(23, 46), ms(23, 59))]):
            s.add(StreamerTurn(session_id=sid, streamer_id=who, seq=i, start_ms=a, end_ms=b))
        s.add(Transcript(session_id=sid, streamer_id=3, seq=0, start_ms=1000, end_ms=2000, raw_text="早班"))
        s.add(Transcript(session_id=sid, streamer_id=3, seq=1, start_ms=ms(23, 50), end_ms=ms(23, 50) + 2000,
                         raw_text="又下班了家人们"))
        s.add(OcrRecord(session_id=sid, streamer_id=3, start_ms=ms(23, 51), end_ms=ms(23, 51), text="观众：拜拜"))
        s.add(Critique(session_id=sid, streamer_id=3, level=CritiqueLevel.segment, category="段",
                       start_ms=ms(23, 50), end_ms=ms(23, 55), data="{}"))
        for who, action, at in [(3, "on", DAY.replace(hour=8, minute=0)), (3, "off", DAY.replace(hour=12, minute=3)),
                                (1, "on", DAY.replace(hour=20, minute=0))]:
            s.add(SignalEvent(event_id=f"{who}{action}{at:%H%M}", streamer_id=who, action=action,
                              occurred_at=at, status="applied"))
        s.commit()
    gained = turn_check.reconcile_turns(sid)
    assert gained == {1}
    with db() as s:
        turns = [(t.streamer_id, t.start_ms, t.end_ms) for t in s.execute(
            select(StreamerTurn).where(StreamerTurn.session_id == sid).order_by(StreamerTurn.start_ms)).scalars()]
        assert turns == [(3, 0, ms(12, 3)), (1, ms(20, 0), ms(23, 59))]       # 合成一段
        owners = {t.raw_text: t.streamer_id for t in s.execute(select(Transcript)).scalars()}
        assert owners == {"早班": 3, "又下班了家人们": 1}
        assert s.execute(select(OcrRecord.streamer_id)).scalar() == 1
        assert s.execute(select(Critique.streamer_id)).scalar() == 1
        assert s.get(ReplaySession, sid).current_streamer_id == 1
    assert turn_check.reconcile_turns(sid) == set()                           # 再跑一遍不再改
