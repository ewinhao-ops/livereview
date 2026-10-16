"""已过关动作复查（2026-10-04）：已过关的每场照样按逐字稿核对，没做到才按原动作加回；
总结里又提到同类问题、但复查做到了（或没出现）的，不加回。模型一律用假的。"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core import llm
from app.core.review import focus, focus_store, report_data
from app.db import Base
from app.models import (Critique, CritiqueLevel, FocusAction, FocusCheck, FocusRun, ReplaySession,
                        SessionStatus, Severity, Streamer, StreamerTurn, Transcript)

DAY = datetime(2026, 10, 4, 9, 0, 0)
FILLER = "这款耳机的声场很开阔，人声细腻，大家可以戴上仔细听一下"
DETAILS = {
    "讲款完整": ("主播开始讲一款产品时", "需求、卖点、适配、促单至少讲到其中 2 样", "这款适合听人声的朋友"),
    "报价指令": ("主播报完价后", "15 秒内说出几号链接和下单指令", "就是 X 号链接，喜欢直接拍"),
}


class FakeLLM:
    """核对：fail_texts 里的动作每块都「没做到」、pass_texts 里的「做到」、其余没有时机；梳理按 order 原样返回。"""

    name = "fake"

    def __init__(self) -> None:
        self.order: list[dict] = []
        self.fail_texts: set[str] = set()
        self.pass_texts: set[str] = set()
        self.checked: list[str] = []

    def __call__(self, purpose: str = "fast") -> "FakeLLM":
        return self

    async def complete(self, system: str, user: str, *, json_mode: bool = False):
        if "核对员" in system:
            first = re.search(r"^\[(\d\d:\d\d:\d\d)\]", user, re.M).group(1)
            rows = []
            for number, text in re.findall(r"^(\d+)\. 动作：(.+)$", user.split("【动作】", 1)[1], re.M):
                self.checked.append(text)
                if text in self.fail_texts:
                    rows.append({"id": int(number), "m": [[first, 0, ""]]})
                elif text in self.pass_texts:
                    rows.append({"id": int(number), "m": [[first, 1, first]]})
                else:
                    rows.append({"id": int(number), "m": []})
            data = {"r": rows}
        elif "条数偏多" in system:
            data = {"groups": []}
        elif "按重要程度从高到低排好" in system:
            found = re.search(r"总结的主要问题】\n1\. (.+)", user)
            src = found.group(1)[:8] if found else ""
            data = {"order": [{"src": src, **item} if item.get("new") else item for item in self.order], "skip": []}
        else:
            items = []
            for number, text, label in re.findall(r"^(\d+)\. (.+?)（(.+)）$", user.split("【要补全的动作】", 1)[1], re.M):
                category = label.split("类别：", 1)[1].split("；", 1)[0] if "类别：" in label else "其他"
                trigger, criterion, example = DETAILS.get(category, ("出现时", "做到", "示范"))
                items.append({"i": int(number), "text": text, "trigger": trigger, "criterion": criterion,
                              "example": example})
            data = {"items": items}
        await asyncio.sleep(0)
        return llm.LLMResult(json.dumps(data, ensure_ascii=False), provider="fake")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'recheck.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    for module in (focus, focus_store, report_data):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(focus, "RETRY_DELAY_SEC", 0)
    fake = FakeLLM()
    monkeypatch.setattr(focus, "get_llm", fake)
    focus._inflight.clear()
    with factory() as db:
        db.add(Streamer(id=1, name="杨雪"))
        row = ReplaySession(title="今天", status=SessionStatus.done, started_at=DAY, ended_at=DAY + timedelta(hours=1))
        db.add(row)
        db.flush()
        db.add(StreamerTurn(session_id=row.id, streamer_id=1, seq=0, start_ms=0, end_ms=30 * 60_000))
        for i in range(30 * 60 // 9):
            db.add(Transcript(session_id=row.id, streamer_id=1, seq=i, start_ms=i * 9000, end_ms=i * 9000 + 6000,
                              raw_text=FILLER, corrected_text=FILLER))
        db.add(Critique(session_id=row.id, streamer_id=1, level=CritiqueLevel.summary, severity=Severity.info,
                        category="整场总结", comment="总结",
                        data=json.dumps({"summary": "总结", "top_issues": ["讲款闭环不完整，缺卖点和适配"],
                                         "action_items": [{"action": "讲一款就把要点讲全"}]}, ensure_ascii=False)))
        db.commit()
        session_id = row.id

    def action(text: str, category: str, status: str = "active", **fields) -> int:
        with factory() as db:
            row = FocusAction(streamer_id=1, text=text, trigger="t", criterion="c", kind=fields.pop("kind", "llm"),
                              category=category, created_by="ai", status=status,
                              created_at=DAY - timedelta(days=3), **fields)
            if status == "passed":
                row.passed_at = row.ended_at = DAY - timedelta(hours=10)
            db.add(row)
            db.commit()
            return row.id

    def rows(model, *where):
        with factory() as db:
            return db.execute(select(model).where(*where)).scalars().all()

    try:
        yield SimpleNamespace(fake=fake, session_id=session_id, action=action, rows=rows, factory=factory)
    finally:
        focus._inflight.clear()
        engine.dispose()


def run(coro):
    return asyncio.run(coro)


def test_passed_action_done_again_is_not_readded(env):
    passed = env.action("讲一款就把要点讲全", "讲款完整", status="passed")
    env.action("报价后紧跟链接号和下单指令", "报价指令")
    env.fake.pass_texts = {"讲一款就把要点讲全"}
    env.fake.order = [{"new": "讲款要讲全", "cat": "讲款完整", "kind": "llm", "points": ["卖点", "适配"]}]
    out = run(focus.process_focus(env.session_id, 1))
    assert out["status"] in ("done", "partial")
    assert "讲一款就把要点讲全" in env.fake.checked                       # 已过关的也核对了
    actives = env.rows(FocusAction, FocusAction.status == "active")
    assert [a.category for a in actives] == ["报价指令"]                    # 没有加回讲款
    check = env.rows(FocusCheck, FocusCheck.action_id == passed)[0]
    assert check.verdict == "pass" and check.baseline is True
    run_row = env.rows(FocusRun)[0]
    assert any("不加回" in c.get("why", "") for c in json.loads(run_row.candidates))
    with env.factory() as db:
        block = json.loads(db.execute(select(Critique)).scalars().first().data)["focus"]
    item = next(c for c in block["checked"] if c["action_id"] == passed)
    assert item["rechecked"] is True and "照样做到" in item["baseline_label"]


def test_passed_action_failed_is_readded_first(env):
    passed = env.action("讲一款就把要点讲全", "讲款完整", status="passed")
    env.action("报价后紧跟链接号和下单指令", "报价指令")
    env.fake.fail_texts = {"讲一款就把要点讲全"}
    env.fake.order = [{"new": "讲款要讲全", "cat": "讲款完整", "kind": "llm", "points": ["卖点"]}]
    run(focus.process_focus(env.session_id, 1))
    actives = sorted(env.rows(FocusAction, FocusAction.status == "active"), key=lambda a: a.sort_order)
    assert [(a.text, a.reopened_from) for a in actives] == [
        ("讲一款就把要点讲全", passed), ("报价后紧跟链接号和下单指令", None)]
    assert env.rows(FocusAction, FocusAction.id == passed)[0].status == "passed"   # 原来那条留着当历史
    with env.factory() as db:
        block = json.loads(db.execute(select(Critique)).scalars().first().data)["focus"]
    item = next(c for c in block["checked"] if c["action_id"] == passed)
    assert "已重新加回" in item["baseline_label"]


def test_passed_silence_action_rechecked(env):
    passed = env.action("冷场不超过45秒", "冷场", status="passed", kind="silence", threshold_sec=45)
    with env.factory() as db:                                                # 挖一个 60 秒的空白
        for row in db.execute(select(Transcript).where(Transcript.start_ms.between(100_000, 160_000))).scalars():
            db.delete(row)
        db.commit()
    run(focus.process_focus(env.session_id, 1))
    check = env.rows(FocusCheck, FocusCheck.action_id == passed)[0]
    assert check.verdict == "fail"
    actives = env.rows(FocusAction, FocusAction.status == "active")
    assert [(a.kind, a.threshold_sec, a.reopened_from) for a in actives] == [("silence", 45, passed)]


def test_ack_a_moment_after_takeover_still_counts():
    """接班时在上播弹窗里点的「记住了」比录音时间轴的接班时刻晚一两秒，也算开播前确认（2026-10-04）。"""
    start = DAY
    assert focus_store.acked_before(start + timedelta(seconds=2), start)
    assert focus_store.acked_before(start - timedelta(hours=5), start)
    assert not focus_store.acked_before(start + timedelta(minutes=5), start)
    assert not focus_store.acked_before(None, start)
    history = [(start, "pass", False), (start + timedelta(days=1), "pass", False)]
    assert focus_store.compute_streak(start + timedelta(seconds=2), history) == 2
    assert focus_store.compute_streak(start + timedelta(minutes=10), history) == 1


def test_ack_two_seconds_after_turn_start_is_not_baseline(env):
    aid = env.action("报价后紧跟链接号和下单指令", "报价指令", acked_at=DAY + timedelta(seconds=2))
    env.fake.pass_texts = {"报价后紧跟链接号和下单指令"}
    run(focus.process_focus(env.session_id, 1))
    check = env.rows(FocusCheck, FocusCheck.action_id == aid)[0]
    assert check.baseline is False
    assert env.rows(FocusAction, FocusAction.id == aid)[0].streak == 1
