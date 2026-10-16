"""改进动作闭环（第一期，2026-10）：空白计算与 180 秒上限、llm 汇总与 2/3 判定、基线规则、
连续场数与过关、补位最多 3 条且不重复、幂等、补漏循环的时间顺序与跳过直播中主播、
下播收尾挂任务、报告写回与重新生成总结后保留、对接接口的鉴权和行为。模型一律用假的。"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api import partner
from app.config import get_settings
from app.core import llm, summary_service
from app.core.review import focus, focus_store, report_data
from app.core.usage import current_llm_context
from app.db import Base, get_db
from app.models import (Critique, CritiqueLevel, FocusAction, FocusCheck, FocusRun, ReplaySession,
                        SessionStatus, Severity, Streamer, StreamerTurn, Transcript)

TOKEN = "unit-test-partner-token"
H = {"X-Partner-Token": TOKEN}
DAY = datetime(2026, 9, 1, 10, 0, 0)     # 在「现在」之前：下播后才新加的动作不会被当成开播前就有

FILLER = "这款耳机的声场很开阔，人声细腻，大家可以戴上仔细听一下"
PRICE = "这款今天到手价特别合适"
LINK = "就是三号链接，喜欢的直接去拍"

POOL = [
    {"text": "报价后30秒内说出链接号并叫观众下单", "kind": "llm", "trigger": "说出到手价时",
     "criterion": "报价后30秒内说出几号链接，并明确叫观众去拍", "example": "这款就是X号链接，喜欢直接拍"},
    {"text": "冷场不超过60秒", "kind": "silence", "sec": 60, "example": "我先放首歌大家听"},
    {"text": "新观众进来时一句话说清今天主推哪款", "kind": "llm", "trigger": "有人说刚来、新进来时",
     "criterion": "30秒内说出今天主推的型号和能试听", "example": "刚来的朋友，今天主推这款，可以试听"},
    {"text": "回答弹幕问题前先复述一遍问题", "kind": "llm", "trigger": "读到观众提问时",
     "criterion": "回答前先把问题念出来", "example": "这位朋友问能不能直推，我们来看看"},
    {"text": "报价前先核实价格是否准确", "kind": "llm", "trigger": "报价前",
     "criterion": "价格和后台一致", "example": "我看一下后台价格"},
]
EXTRA = [
    {"text": text, "kind": "llm", "trigger": f"出现第{n}种情况时", "criterion": f"出现后10秒内说出第{n}种话术",
     "example": f"示范第{n}句"}
    for n, text in enumerate([
        "开播第一分钟报出今天能试听的型号", "每讲完一组参数补一句适合什么人", "讲完一款问观众平时听什么歌",
        "观众说预算后十秒内推荐一款", "介绍耳放时先问观众用什么播放器", "试听前先说这段听什么细节",
        "放歌结束后马上问大家听出区别没有", "被问发货时先说能不能当天发出", "收到下单提醒后念出名字道谢",
        "讲新品前先说和旧款的一个区别", "下播前五分钟预告下一场时间",
    ], 1)
]
BY_TEXT = {item["text"]: item for item in POOL + EXTRA}


class FakeLLM:
    """假的快速档。核对：按「报价后 30 秒内有没有说几号链接」判；梳理：把 self.pool 按顺序列出，
    和在练动作同名的指回 A 编号、和已过关同名的标 again；补全：按动作原文从池子里取细节。"""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.contexts: list = []
        self.purposes: list[str] = []
        self.pool = [dict(item) for item in POOL]
        self.skip = [{"text": "说话再笃定一些", "why": "没法从逐字稿判断"}]
        self.fail = lambda kind, attempt: None   # 返回异常 = 这次调用失败
        self.attempts = {"check": 0, "rank": 0, "detail": 0, "merge": 0}
        self.max_actions_per_call = 0
        self.merge_groups: list[list[str]] = []

    def __call__(self, purpose: str = "fast") -> "FakeLLM":
        self.purposes.append(purpose)
        return self

    @staticmethod
    def kind_of(system: str) -> str:
        if "核对员" in system:
            return "check"
        if "条数偏多" in system:
            return "merge"
        return "rank" if "按重要程度从高到低排好" in system else "detail"

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        assert json_mode is True
        kind = self.kind_of(system)
        self.attempts[kind] += 1
        self.calls.append((kind, user))
        self.contexts.append(current_llm_context())
        await asyncio.sleep(0)
        error = self.fail(kind, self.attempts[kind])
        if error is not None:
            raise error
        data = {"check": self._check, "rank": self._rank, "detail": self._detail,
                "merge": lambda _user: {"groups": self.merge_groups}}[kind](user)
        return llm.LLMResult(json.dumps(data, ensure_ascii=False), provider="fake-fast")

    @staticmethod
    def _lines(part: str) -> list[tuple[int, str, str]]:
        return [(focus.parse_hms(m.group(1)), m.group(1), m.group(2))
                for m in re.finditer(r"^\[(\d\d:\d\d:\d\d)\] (.+)$", part, re.M)]

    def _check(self, user: str) -> dict:
        assert user.startswith("【正文】") and user.index("【后续参考】") < user.index("【动作】")
        body, rest = user.split("【正文】", 1)[1].split("【后续参考】", 1)
        tail, head = rest.split("【动作】", 1)
        main, after = self._lines(body), self._lines(tail)
        out = []
        numbers = re.findall(r"^(\d+)\. 动作：(.+)$", head, re.M)
        self.max_actions_per_call = max(self.max_actions_per_call, len(numbers))
        for number, text in numbers:
            occasions = []
            if "链接" in text:
                for index, (sec, stamp, line) in enumerate(main):
                    if "到手价" not in line:
                        continue
                    later = [x for x in main[index + 1:] + after if 0 < x[0] - sec <= 30]
                    hit = next((x for x in later if "号链接" in x[2]), None)
                    occasions.append([stamp, 1 if hit else 0, hit[1] if hit else ""])
            out.append({"id": int(number), "m": occasions})
        return {"r": out}

    @staticmethod
    def _section(user: str, title: str) -> list[tuple[str, str]]:
        part = user.split(title, 1)[1].split("\n\n", 1)[0]
        return re.findall(r"^([AP]\d+)\. \[[^\]]*\] (.+?)（", part, re.M)

    @staticmethod
    def quote(user: str) -> str:
        """照抄总结里第一条问题的前 10 个字当 src（第二版规则：新动作要引到总结原话）。"""
        found = re.search(r"总结的主要问题】\n1\. (.+)", user)
        return found.group(1)[:10] if found else ""

    def _rank(self, user: str) -> dict:
        active = {text: label for label, text in self._section(user, "【在练动作】")}
        passed = {text: label for label, text in self._section(user, "【已过关动作】")}
        src = self.quote(user)
        order = []
        for item in self.pool:
            if item["text"] in active:
                order.append({"same": active[item["text"]]})
                continue
            entry = {"new": item["text"], "kind": item["kind"], "cat": item.get("cat", "其他"), "src": src}
            if item["kind"] == "silence":
                entry["sec"] = item.get("sec", 60)
            if item["text"] in passed:
                entry["again"] = passed[item["text"]]
            order.append(entry)
        return {"order": order, "skip": self.skip}

    def _detail(self, user: str) -> dict:
        part = user.split("【要补全的动作】", 1)[1]
        items = []
        for number, text in re.findall(r"^(\d+)\. (.+?)（", part, re.M):
            ref = BY_TEXT.get(text, {})
            items.append({"i": int(number), "trigger": ref.get("trigger") or "这种情况出现时",
                          "criterion": ref.get("criterion") or "出现后十秒内说出对应的话",
                          "example": ref.get("example") or "示范一句"})
        return {"items": items}


def speech(minutes: int = 30, *, prices: int = 6, done=None, every: int = 9) -> list[tuple]:
    """造一段逐字稿：每 every 秒一句；均匀报 prices 次价，done[k] 为真时紧跟一句说链接号。"""
    done = list(done) if done is not None else [True] * prices
    total = minutes * 60 // every
    slots = [int((k + 0.5) * total / prices) for k in range(prices)]
    rows = []
    for i in range(total):
        start = i * every * 1000
        if i in slots:
            text = PRICE
        elif i - 1 in slots and done[slots.index(i - 1)]:
            text = LINK
        else:
            text = FILLER
        rows.append((start, start + 6000, text))
    return rows


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'focus.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    for module in (focus, focus_store, report_data, summary_service):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(focus, "RETRY_DELAY_SEC", 0)
    fake = FakeLLM()
    monkeypatch.setattr(focus, "get_llm", fake)
    monkeypatch.setattr(get_settings(), "partner_token", TOKEN)
    monkeypatch.setattr(get_settings(), "focus_since", "2026-09-29")
    focus._inflight.clear()
    focus._attempts.clear()
    summary_service._inflight.clear()
    ns = SimpleNamespace(factory=factory, fake=fake)

    def streamer(name: str, ext_id: str | None = None) -> int:
        with factory() as db:
            row = Streamer(name=name, ext_id=ext_id)
            db.add(row)
            db.commit()
            return row.id

    def session(start: datetime, turns: list[tuple[int, int, int]], *, status=SessionStatus.done,
                ended: bool = True, current: int | None = None) -> int:
        with factory() as db:
            row = ReplaySession(title=f"场次{start:%m%d}", status=status, started_at=start,
                                ended_at=(start + timedelta(hours=5)) if ended else None,
                                current_streamer_id=current)
            db.add(row)
            db.flush()
            for seq, (sid, start_ms, end_ms) in enumerate(turns):
                db.add(StreamerTurn(session_id=row.id, streamer_id=sid, seq=seq,
                                    start_ms=start_ms, end_ms=end_ms))
            db.commit()
            return row.id

    def transcript(session_id: int, streamer_id: int, rows, offset_ms: int = 0) -> None:
        with factory() as db:
            for seq, (start, end, text) in enumerate(rows):
                db.add(Transcript(session_id=session_id, streamer_id=streamer_id, seq=seq,
                                  start_ms=start + offset_ms, end_ms=end + offset_ms,
                                  raw_text=text, corrected_text=text))
            db.commit()

    def summary(session_id: int, streamer_id: int, data: dict | None = None) -> None:
        data = data if data is not None else {
            "summary": "整场还行", "overall_score": 7,
            "top_issues": ["报价后经常不说链接号，观众不知道拍哪个", "冷场偏多"],
            "action_items": [{"action": "每次报价后补一句几号链接", "source": "[通用经验]"}],
        }
        with factory() as db:
            db.add(Critique(session_id=session_id, streamer_id=streamer_id,
                            level=CritiqueLevel.summary, severity=Severity.info,
                            category="整场总结", comment=data.get("summary", ""),
                            data=json.dumps(data, ensure_ascii=False)))
            db.commit()

    def action(streamer_id: int, text: str, *, kind: str = "llm", created_at: datetime | None = None,
               acked_at: datetime | None = None, threshold: int = 10, status: str = "active",
               streak: int = 0, sort_order: int = 0) -> int:
        with factory() as db:
            row = FocusAction(
                streamer_id=streamer_id, text=text, kind=kind, threshold_sec=threshold,
                trigger="说出到手价时" if kind == "llm" else "整场直播中",
                criterion="报价后30秒内说出几号链接" if kind == "llm" else "空白不到阈值",
                created_at=created_at or DAY - timedelta(days=1), acked_at=acked_at,
                acked_by="小美" if acked_at else "", status=status, streak=streak,
                sort_order=sort_order,
            )
            db.add(row)
            db.commit()
            return row.id

    ns.streamer, ns.session, ns.transcript, ns.summary, ns.action = (
        streamer, session, transcript, summary, action)

    def get(model, ident):
        with factory() as db:
            return db.get(model, ident)

    def rows(model, *where):
        with factory() as db:
            return db.execute(select(model).where(*where)).scalars().all()

    def summary_data(session_id: int, streamer_id: int) -> dict:
        with factory() as db:
            row = db.execute(select(Critique).where(
                Critique.session_id == session_id, Critique.streamer_id == streamer_id,
                Critique.category == "整场总结")).scalars().one()
            return json.loads(row.data)

    ns.get, ns.rows, ns.summary_data = get, rows, summary_data
    try:
        yield ns
    finally:
        focus._inflight.clear()
        focus._attempts.clear()
        engine.dispose()


def run(coro):
    return asyncio.run(coro)


def standard(env, *, start=DAY, done=None, minutes=30, name="小美", ext_id="s-mei"):
    """一位主播一场：30 分钟逐字稿 + 整场总结。返回 (主播 id, 场次 id)。"""
    sid = env.streamer(name, ext_id)
    sess = env.session(start, [(sid, 0, minutes * 60_000)])
    env.transcript(sess, sid, speech(minutes, done=done))
    env.summary(sess, sid)
    return sid, sess


# ======================= 纯算法：空白、切块、汇总 =======================

def S(start_s: float, end_s: float, text: str = "话") -> focus.Sent:
    return focus.Sent(int(start_s * 1000), int(end_s * 1000), text)


def test_silence_gaps_use_previous_end_to_next_start_and_skip_turn_boundaries():
    sentences = [S(0, 5, "甲"), S(15, 20, "乙"), S(21, 80, "长句"), S(30, 35, "被长句盖住"),
                 S(100, 105, "丙"), S(4000, 4005, "返场第一句")]
    gaps = focus.find_gaps(sentences, [(0, 200_000), (3_990_000, 4_100_000)])
    assert [(g.start_ms, g.end_ms) for g in gaps] == [(5000, 15000), (20000, 21000), (80000, 100000)]
    assert gaps[0].before == "甲" and gaps[0].after == "乙"
    assert gaps[2].sec == 20


def test_silence_check_counts_threshold_to_180_and_keeps_longer_ones_separate():
    sentences = [S(0, 1)]
    t = 1.0
    for gap in (9.9, 10, 45, 180, 181, 600, 12):
        sentences.append(S(t + gap, t + gap + 1))
        t += gap + 1
    gaps = focus.find_gaps(sentences, [])
    verdict, metric, evidence = focus.check_silence(gaps, 10, DAY)
    assert verdict == "fail"
    assert metric["count"] == 4              # 10、45、180、12；9.9 不到阈值
    assert metric["max_gap_sec"] == 180
    assert metric["over_cap_count"] == 2     # 181、600 多半是放歌试听或离场，不算违规
    assert "不算" in metric["summary"]
    assert len(evidence) == 4 and all(e["ok"] is False for e in evidence)
    assert [e["at_ms"] for e in evidence] == sorted(e["at_ms"] for e in evidence)
    assert evidence[0]["wall_time"].endswith("+08:00") and "空了" in evidence[0]["quote"]

    only_long = focus.find_gaps([S(0, 1), S(300, 301)], [])
    verdict, metric, evidence = focus.check_silence(only_long, 10, DAY)
    assert (verdict, metric["count"], metric["over_cap_count"], evidence) == ("pass", 0, 1, [])


def test_speech_span_sums_each_turn_and_ignores_the_gap_between_returns():
    sentences = [S(0, 10), S(600, 610), S(3600, 3610), S(4200, 4210)]
    assert focus.speech_span_ms(sentences, [(0, 700_000), (3_500_000, 4_300_000)]) == 1_220_000
    assert focus.speech_span_ms(sentences, []) == 4_210_000


def test_chunks_cut_on_sentences_and_carry_a_short_tail(monkeypatch):
    sentences = [S(i * 10, i * 10 + 5, "字" * 100) for i in range(30)]
    chunks = focus.build_chunks(sentences, size=1000)
    assert [len(c.main) for c in chunks] == [10, 10, 10]
    assert sum(len(c.main) for c in chunks) == 30
    assert chunks[0].tail and chunks[0].tail[0] is chunks[1].main[0]
    assert sum(len(s.text) for s in chunks[0].tail) <= focus.TAIL_CHARS
    assert chunks[-1].tail == []
    monkeypatch.setattr(focus, "MAX_CHUNKS", 2)
    assert len(focus.build_chunks(sentences, size=1000)) == 2      # 超长时放大块，不让调用数失控


def test_parse_check_maps_times_to_sentences_and_drops_made_up_ones():
    chunk = focus.Chunk(0, [S(60, 62, "到手价甲"), S(70, 72, "三号链接"), S(200, 202, "到手价乙")],
                        [S(215, 218, "五号链接去拍")])
    data = {"r": [
        {"id": 1, "m": [["00:01:00", 1, "00:01:10"], ["00:01:00", 0, ""],   # 同一句只收一次
                        ["00:03:20", 1, "00:03:35"], ["09:00:00", 1, ""]]},  # 编出来的时间丢掉
        {"id": 7, "m": [["00:01:00", 1, ""]]},                             # 不存在的动作编号
        "垃圾",
    ]}
    out = focus.parse_check(data, chunk, 1)
    assert [(o.at_ms, o.ok) for o in out[1]] == [(60_000, True), (200_000, True)]
    assert out[1][0].quote == "到手价甲 …… 三号链接"
    assert out[1][1].quote == "到手价乙 …… 五号链接去拍"            # 做到的那句在后续参考里
    with pytest.raises(ValueError):
        focus.parse_check({"x": []}, chunk, 1)


def test_close_occasions_of_one_action_are_merged():
    occ = [focus.Occasion(264_000, False, "有朋友问能不能直推"),
           focus.Occasion(268_000, True, "我们来看这位朋友问的能不能直推"),
           focus.Occasion(1_003_000, False, "问戴眼镜压不压"),
           focus.Occasion(1_010_000, False, "不压的放心"),
           focus.Occasion(2_053_000, True, "这位朋友问有没有现货")]
    merged = focus.merge_close(occ)
    assert [(o.at_ms, o.ok) for o in merged] == [(264_000, True), (1_003_000, False), (2_053_000, True)]
    assert merged[0].quote == "我们来看这位朋友问的能不能直推"
    assert focus.llm_result(1, merged, 1, 0, DAY).verdict == "pass"            # 2/3


def test_llm_verdict_two_thirds_rule_and_na_cases():
    def occ(*flags):
        return [focus.Occasion(i * 1000, bool(f), f"q{i}") for i, f in enumerate(flags)]

    assert focus.llm_result(1, occ(1, 1, 0), 2, 0, DAY).verdict == "pass"       # 正好 2/3
    assert focus.llm_result(1, occ(1, 0), 2, 0, DAY).verdict == "fail"          # 1/2
    assert focus.llm_result(1, occ(1, 1, 1, 0, 0, 0), 2, 0, DAY).verdict == "fail"
    none = focus.llm_result(1, [], 2, 0, DAY)
    assert (none.verdict, none.total, none.done, none.reason) == ("na", 0, 0, "本场没有出现该做的时机")
    failed = focus.llm_result(1, occ(1, 1, 1), 3, 1, DAY)
    assert (failed.verdict, failed.done, failed.total) == ("na", None, None)
    assert failed.reason.startswith("核对失败") and failed.metric["partial_done"] == 3
    picked = focus.llm_result(1, occ(1, 1, 1, 1, 0, 0, 0, 0), 2, 0, DAY).evidence
    assert len(picked) == 4 and [e["ok"] for e in picked].count(True) == 2


def test_streak_counts_only_checks_after_ack_and_not_baseline():
    t = [DAY + timedelta(days=i) for i in range(6)]
    acked = t[1] - timedelta(hours=1)
    history = [(t[0], "pass", True), (t[1], "pass", False), (t[2], "na", False),
               (t[3], "fail", False), (t[4], "pass", False), (t[5], "pass", False)]
    assert focus_store.compute_streak(acked, history) == 2
    assert focus_store.compute_streak(None, history) == 0
    assert focus_store.compute_streak(t[4] + timedelta(minutes=10), history) == 1   # 开播后才重新确认：这场和之前的都不算
    assert focus_store.compute_streak(t[4] + timedelta(seconds=2), history) == 2    # 接班那一刻在上播弹窗里点的算开播前（2026-10-04）


def test_generated_actions_are_cleaned_and_price_accuracy_ones_dropped():
    good = focus_store.clean_generated(POOL[0])
    assert good["kind"] == "llm" and good["example_source"] == "AI 示范"
    silence = focus_store.clean_generated({**POOL[1], "threshold_sec": 999})
    assert silence["threshold_sec"] == 150 and "150 秒" in silence["criterion"]
    for bad in (
        {**POOL[0], "text": "报价前先核实价格是否准确"},
        {**POOL[0], "text": "参数说准确", "criterion": "参数和知识库一致"},
        {**POOL[0], "text": "这条动作写得特别特别长，远远超过了三十个字的限制，所以不能要它"},
        {**POOL[0], "criterion": ""},
        "不是对象",
    ):
        assert focus_store.clean_generated(bad) is None
    assert focus_store.similar("报价后30秒内说出链接号并叫观众下单", "报价后 30 秒内说出链接号，叫观众下单")
    assert not focus_store.similar("回答弹幕问题前先复述一遍问题", "冷场不超过60秒")


def test_rank_output_is_parsed_into_existing_refs_new_items_and_skips():
    ctx = SimpleNamespace(
        active=[focus.ActionInfo(11, "甲动作", "t", "c", "llm", 10)],
        passed=[focus.ActionInfo(22, "乙动作", "t", "c", "llm", 10, "passed")],
    )
    entries, skipped = focus.parse_rank({"order": [
        {"same": "A1"}, {"same": "A9"}, {"new": "新动作一", "kind": "llm"},
        {"new": "冷场不超过40秒", "kind": "silence", "sec": 400},
        {"new": "乙动作再练", "kind": "llm", "again": "P1"},
        {"new": "报价前先核实价格是否准确", "kind": "llm"},
        {"new": "长" * 31, "kind": "llm"}, "垃圾",
    ], "skip": [{"text": "说话更笃定", "why": "没法判断"}, {"text": "参数说准确", "why": "x"}]}, ctx)
    assert entries[0] == {"existing_id": 11}
    assert [e.get("text") for e in entries[1:]] == ["新动作一", "冷场不超过40秒", "乙动作再练"]
    assert entries[2]["sec"] == 150 and entries[3]["again_id"] == 22
    assert [s["text"] for s in skipped] == ["长" * 31, "说话更笃定"]
    with pytest.raises(ValueError):
        focus.parse_rank({"skip": []}, ctx)


# ======================= 整个处理流程 =======================

def active_rows(env, sid):
    return sorted(env.rows(FocusAction, FocusAction.streamer_id == sid, FocusAction.status == "active"),
                  key=lambda a: (a.sort_order, a.id))


def test_first_session_turns_every_checkable_problem_into_an_action(env):
    sid, sess = standard(env)
    result = run(focus.process_focus(sess, sid))
    assert result["status"] == "done" and result["recorded"]
    active = active_rows(env, sid)
    # 池子里 5 条：4 条能检查的都成了动作（不凑也不截到 3 条），「核实价格」那条不出
    assert [a.text for a in active] == [POOL[0]["text"], POOL[1]["text"], POOL[2]["text"], POOL[3]["text"]]
    assert [a.sort_order for a in active] == [1, 2, 3, 4]               # 按重要程度排
    assert {a.kind for a in active} == {"llm", "silence"}
    silence = next(a for a in active if a.kind == "silence")
    # 秒数按本场空白统计定（这段逐字稿句间只空 3 秒 → 最低一档 30 秒），不用模型给的 60
    assert silence.threshold_sec == 30 and "30 秒" in silence.criterion
    assert all(a.created_by == "ai" and a.origin_session_id == sess and a.acked_at is None
               and a.trigger and a.criterion for a in active)
    run_row = env.rows(FocusRun)[0]
    assert (run_row.status, run_row.new_count, run_row.note) == ("done", 4, "")
    candidates = json.loads(run_row.candidates)
    assert [c["text"] for c in candidates] == ["说话再笃定一些"]           # 值得做但没法检查的
    assert candidates[0]["usable"] is False and candidates[0]["why"]
    block = env.summary_data(sess, sid)["focus"]
    assert block["checked"] == [] and len(block["next"]) == 4
    assert all(item["needs_ack"] for item in block["next"])
    assert block["streamer"] == {"id": sid, "name": "小美", "ext_id": "s-mei"}
    assert block["processed_at"].endswith("+08:00")
    assert [kind for kind, _ in env.fake.calls] == ["rank", "detail"]   # 没有动作要核对
    assert {(c.session_id, c.stage) for c in env.fake.contexts} == {(sess, "focus")}
    assert set(env.fake.purposes) == {"fast"}


def test_check_with_fake_model_counts_occasions_across_chunks(env, monkeypatch):
    monkeypatch.setattr(focus, "CHUNK_CHARS", 1500)
    sid, sess = standard(env, done=[True, True, False, True, True, False])
    link = env.action(sid, POOL[0]["text"], acked_at=DAY - timedelta(hours=2))
    quiet = env.action(sid, "冷场不超过5秒", kind="silence", threshold=5)
    result = run(focus.process_focus(sess, sid))
    assert result["status"] == "done"
    checks = {c.action_id: c for c in env.rows(FocusCheck)}
    assert (checks[link].verdict, checks[link].done, checks[link].total) == ("pass", 4, 6)
    assert checks[link].baseline is False
    assert checks[quiet].verdict == "pass" and checks[quiet].baseline is True
    assert json.loads(checks[quiet].metric)["count"] == 0
    evidence = json.loads(checks[link].evidence)
    assert [e["ok"] for e in evidence].count(False) == 2 and len(evidence) == 4
    assert all("到手价" in e["quote"] for e in evidence)
    assert sum(1 for kind, _ in env.fake.calls if kind == "check") >= 3   # 切成了好几块
    assert env.get(FocusAction, link).streak == 1
    block = env.summary_data(sess, sid)["focus"]
    assert {c["action_id"]: c["verdict"] for c in block["checked"]} == {link: "pass", quiet: "pass"}
    active = active_rows(env, sid)
    # 梳理：链接那条指回在练的；冷场已经有一条在练的，不再加；新加 2 条
    assert len(active) == 4 and env.rows(FocusRun)[0].new_count == 2
    assert active[0].id == link                       # 本场最重要的问题排第一
    assert len(block["next"]) == 4


def test_many_actions_are_checked_in_batches(env, monkeypatch):
    # 2026-10-04：一次判的条数由 4 改成 ACTIONS_PER_CALL（=2），判得更稳、不被输出上限截断
    monkeypatch.setattr(focus, "CHUNK_CHARS", 3000)
    sid, sess = standard(env)
    ids = [env.action(sid, f"第{n}个能检查的小动作要说出口") for n in range(1, 7)]
    ids.append(env.action(sid, POOL[0]["text"]))
    run(focus.process_focus(sess, sid))
    chunks = len(focus.build_chunks(focus._load_context(sess, sid).sentences, 3000))
    checks = [user for kind, user in env.fake.calls if kind == "check"]
    groups = -(-7 // focus.ACTIONS_PER_CALL)
    assert env.fake.max_actions_per_call == focus.ACTIONS_PER_CALL and len(checks) == chunks * groups
    # 先把第一批问完所有块，再问下一批：同一块的几次请求前缀相同、而且隔开了
    first_lines = [user.split("\n", 2)[1] for user in checks]
    for g in range(1, groups):
        assert first_lines[g * chunks:(g + 1) * chunks] == first_lines[:chunks]
    assert len(set(first_lines[:chunks])) == chunks
    assert {c.action_id for c in env.rows(FocusCheck)} == set(ids)


def test_baseline_rule_never_moves_the_streak_before_ack(env):
    sid, sess = standard(env)
    never = env.action(sid, POOL[0]["text"], streak=1)
    late = env.action(sid, "报价后马上说是第几号链接", acked_at=DAY + timedelta(minutes=5), streak=2)
    run(focus.process_focus(sess, sid))
    checks = {c.action_id: c for c in env.rows(FocusCheck)}
    assert checks[never].verdict == "pass" and checks[never].baseline is True
    assert checks[late].verdict == "pass" and checks[late].baseline is True
    # 照存结果但不动连续场数：没确认的重算为 0，确认时间晚于开播的也只算基线
    assert env.get(FocusAction, never).streak == 0
    assert env.get(FocusAction, late).streak == 0
    assert env.get(FocusAction, late).status == "active"


def test_three_acked_passes_pass_the_action_and_a_repeat_offence_reopens_it(env):
    sid = env.streamer("小美", "s-mei")
    link = env.action(sid, POOL[0]["text"], acked_at=DAY - timedelta(days=2))
    sessions = []
    for day, done in enumerate(([True] * 6, [True] * 5 + [False], [True, True, False] * 2,
                                [False] * 6)):
        sess = env.session(DAY + timedelta(days=day), [(sid, 0, 1_800_000)])
        env.transcript(sess, sid, speech(30, done=done))
        env.summary(sess, sid)
        sessions.append(sess)
    env.fake.pool = [POOL[2]]                       # 前三场：报价那条不在本场问题里
    run(focus.process_focus(sessions[0], sid))
    run(focus.process_focus(sessions[1], sid))
    assert env.get(FocusAction, link).streak == 2 and env.get(FocusAction, link).status == "active"
    result = run(focus.process_focus(sessions[2], sid))
    passed = env.get(FocusAction, link)
    assert (passed.status, passed.streak) == ("passed", 3) and passed.passed_at and passed.ended_at
    assert result["passed"] == [link]
    assert link not in {a.id for a in active_rows(env, sid)}
    # 第四场：同一个问题又犯了 → 重新加一条（新 id，streak 从 0 开始，记下是哪条重开的）
    env.fake.pool = [POOL[0], POOL[2]]
    run(focus.process_focus(sessions[3], sid))
    again = [a for a in active_rows(env, sid) if a.text == POOL[0]["text"]]
    assert len(again) == 1 and again[0].id != link
    assert (again[0].streak, again[0].reopened_from, again[0].acked_at) == (0, link, None)
    rank_prompt = [u for k, u in env.fake.calls if k == "rank"][-1]
    assert "P1. [其他] " + POOL[0]["text"] in rank_prompt


def test_fail_resets_and_na_keeps_the_streak(env):
    sid = env.streamer("小美", "s-mei")
    link = env.action(sid, POOL[0]["text"], acked_at=DAY - timedelta(days=2), streak=0)
    plan = [([True] * 6, 6), ([False] * 6, 6), ([True] * 6, 6), (None, 0)]
    for day, (done, prices) in enumerate(plan):
        sess = env.session(DAY + timedelta(days=day), [(sid, 0, 1_800_000)])
        rows = speech(30, prices=max(prices, 1), done=done) if prices else \
            [(i * 9000, i * 9000 + 6000, FILLER) for i in range(200)]
        env.transcript(sess, sid, rows)
        env.summary(sess, sid)
        run(focus.process_focus(sess, sid))
        streak = env.get(FocusAction, link).streak
        assert streak == [1, 0, 1, 1][day]
    last = env.rows(FocusCheck, FocusCheck.action_id == link)[-1]
    assert (last.verdict, last.total) == ("na", 0)


def test_no_cap_but_same_problem_is_never_added_twice(env):
    sid, sess = standard(env)
    kept = env.action(sid, POOL[3]["text"])
    env.action(sid, "开播第一分钟说清今天能试听哪几款", status="removed")
    env.fake.pool = [
        POOL[0],
        {**POOL[3], "text": "回答弹幕问题前，先复述一遍问题"},   # 和在练的同一个问题、模型却当新的写了
        {**POOL[0], "text": "报价前先核实价格是否准确"},        # 价格准不准：不出
        {**POOL[2], "text": "开播第一分钟说清今天能试听哪几款"},  # 人工删掉过：不再出
        POOL[1], POOL[2],
    ]
    run(focus.process_focus(sess, sid))
    active = active_rows(env, sid)
    texts = [a.text for a in active]
    assert texts.count(POOL[3]["text"]) == 1 and kept in {a.id for a in active}
    assert POOL[0]["text"] in texts and POOL[1]["text"] in texts and POOL[2]["text"] in texts
    assert "开播第一分钟说清今天能试听哪几款" not in texts
    assert not any("核实" in t for t in texts)
    candidates = json.loads(env.rows(FocusRun)[0].candidates)
    dup = next(c for c in candidates if c["text"] == "回答弹幕问题前，先复述一遍问题")
    assert dup["usable"] is True and "同一个问题" in dup["why"]
    # 第二场：问题一样，不再新加
    sess2 = env.session(DAY + timedelta(days=1), [(sid, 0, 1_800_000)])
    env.transcript(sess2, sid, speech(30))
    env.summary(sess2, sid)
    run(focus.process_focus(sess2, sid))
    assert [a.text for a in active_rows(env, sid)] == texts
    assert env.rows(FocusRun, FocusRun.session_id == sess2)[0].new_count == 0


def test_more_than_eight_new_actions_are_noted(env, capsys):
    sid, sess = standard(env)
    env.fake.pool = [dict(item) for item in EXTRA[:10]]
    run(focus.process_focus(sess, sid))
    assert len(active_rows(env, sid)) == 10                         # 合不了的才保留，不设硬上限
    assert [kind for kind, _ in env.fake.calls].count("merge") == 1  # 超过 8 条先再合并一轮
    run_row = env.rows(FocusRun)[0]
    assert run_row.new_count == 10 and "超过 8 条" in run_row.note
    assert "超过 8 条" in env.summary_data(sess, sid)["focus"]["note"]
    assert "超过 8 条" in capsys.readouterr().out
    assert sum(1 for kind, _ in env.fake.calls if kind == "detail") == 3   # 每批 4 条补细节


def test_only_one_silence_action_at_a_time(env):
    sid, sess = standard(env)
    env.action(sid, "冷场不超过90秒", kind="silence", threshold=90)
    env.fake.pool = [POOL[1], {**POOL[1], "text": "空白不超过30秒", "sec": 30}, POOL[2]]
    run(focus.process_focus(sess, sid))
    active = active_rows(env, sid)
    assert sum(a.kind == "silence" for a in active) == 1 and len(active) == 2


def test_silence_like_talk_action_is_not_added_next_to_a_silence_action(env):
    sid, sess = standard(env)
    env.fake.pool = [POOL[1], {**POOL[2], "text": "换耳机调设备时边操作边讲解，不静音"}, POOL[0]]
    run(focus.process_focus(sess, sid))
    texts = [a.text for a in active_rows(env, sid)]
    assert texts == [POOL[1]["text"], POOL[0]["text"]]
    why = {c["text"]: c["why"] for c in json.loads(env.rows(FocusRun)[0].candidates)}
    assert "不冷场" in why["换耳机调设备时边操作边讲解，不静音"]


def test_rank_reorders_existing_actions_by_this_sessions_problems(env):
    sid, sess = standard(env)
    first = env.action(sid, POOL[2]["text"], sort_order=1)
    second = env.action(sid, POOL[3]["text"], sort_order=2)
    third = env.action(sid, "只在上一场出现过的问题", sort_order=3)
    env.fake.pool = [POOL[3], POOL[0], POOL[2]]
    run(focus.process_focus(sess, sid))
    order = [a.id for a in active_rows(env, sid)]
    new = next(a.id for a in active_rows(env, sid) if a.text == POOL[0]["text"])
    assert order == [second, new, first, third]


def test_repeated_and_concurrent_calls_check_only_once(env):
    sid, sess = standard(env)
    env.action(sid, POOL[0]["text"])

    async def both():
        return await asyncio.gather(focus.process_focus(sess, sid), focus.process_focus(sess, sid))

    first, second = run(both())
    assert first == second and first["status"] == "done"
    calls = len(env.fake.calls)
    again = run(focus.process_focus(sess, sid))
    assert again == {"status": "done", "recorded": True, "existing": True}
    assert len(env.fake.calls) == calls                          # 没有再调模型
    assert len(env.rows(FocusCheck)) == 1 and len(env.rows(FocusRun)) == 1


def test_force_rerun_overwrites_the_same_rows_and_does_not_double_count(env):
    sid, sess = standard(env)
    link = env.action(sid, POOL[0]["text"], acked_at=DAY - timedelta(days=1))
    run(focus.process_focus(sess, sid))
    count = len(active_rows(env, sid))
    run(focus.process_focus(sess, sid, force=True))
    assert len(env.rows(FocusCheck)) == 1 and len(env.rows(FocusRun)) == 1
    assert env.get(FocusAction, link).streak == 1
    assert len(active_rows(env, sid)) == count                    # 同样的问题不会再加一遍


def test_too_little_speech_is_skipped_without_model_calls(env):
    sid = env.streamer("小美", "s-mei")
    sess = env.session(DAY, [(sid, 0, 600_000)])
    env.transcript(sess, sid, speech(10))                         # 10 分钟
    env.summary(sess, sid)
    env.action(sid, POOL[0]["text"])
    result = run(focus.process_focus(sess, sid))
    assert result["status"] == "skipped" and env.fake.calls == []
    assert env.rows(FocusCheck) == [] and env.rows(FocusRun)[0].status == "skipped"
    block = env.summary_data(sess, sid)["focus"]
    assert block["status"] == "skipped" and "不到 20 分钟" in block["reason"]
    assert len(env.rows(FocusAction)) == 1                        # 没定新动作

    sid2 = env.streamer("小红")
    sess2 = env.session(DAY, [(sid2, 0, 3_600_000)])
    env.transcript(sess2, sid2, [(i * 60_000, i * 60_000 + 2000, "嗯好的") for i in range(40)])
    env.summary(sess2, sid2)
    assert run(focus.process_focus(sess2, sid2))["status"] == "skipped"   # 时间够、字太少


def test_not_ready_without_summary_leaves_no_record(env):
    sid = env.streamer("小美")
    sess = env.session(DAY, [(sid, 0, 1_800_000)])
    env.transcript(sess, sid, speech(30))
    result = run(focus.process_focus(sess, sid))
    assert result == {"status": "not_ready", "recorded": False, "reason": "这位主播本场还没有整场总结"}
    assert env.rows(FocusRun) == []


def test_only_actions_created_before_the_turn_started_are_checked(env):
    sid = env.streamer("小美")
    sess = env.session(DAY, [(sid, 0, 1_800_000)])
    env.transcript(sess, sid, speech(30))
    env.summary(sess, sid)
    old = env.action(sid, POOL[0]["text"], created_at=DAY - timedelta(minutes=1))
    env.action(sid, POOL[3]["text"], created_at=DAY + timedelta(minutes=1))
    run(focus.process_focus(sess, sid))
    assert [c.action_id for c in env.rows(FocusCheck)] == [old]


def test_failed_chunk_makes_llm_actions_na_and_run_partial_after_one_retry(env, monkeypatch):
    monkeypatch.setattr(focus, "CHUNK_CHARS", 1500)
    sid, sess = standard(env)
    link = env.action(sid, POOL[0]["text"], acked_at=DAY - timedelta(days=1), streak=2)
    quiet = env.action(sid, "冷场不超过5秒", kind="silence", threshold=5)
    calls = []
    original = env.fake.complete

    async def complete(system, user, *, json_mode=False):
        kind = FakeLLM.kind_of(system)
        calls.append(kind)
        if kind == "check" and "[00:00:00]" in user:      # 第一块：两次都失败
            raise llm.LLMCallError("LLM_PROVIDER_UNAVAILABLE", "x")
        return await original(system, user, json_mode=json_mode)

    monkeypatch.setattr(env.fake, "complete", complete)
    result = run(focus.process_focus(sess, sid))
    assert result["status"] == "partial"
    checks = {c.action_id: c for c in env.rows(FocusCheck)}
    assert checks[link].verdict == "na" and checks[link].reason.startswith("核对失败")
    assert checks[quiet].verdict == "pass"                       # 纯算法的不受影响
    assert env.get(FocusAction, link).streak == 0                # na 不变：历史上只有这一场（未计入）
    chunks = len(focus.build_chunks(focus._load_context(sess, sid).sentences, 1500))
    assert calls.count("check") == chunks + 1                    # 失败那块重试了一次
    assert env.rows(FocusRun)[0].status == "partial"
    assert "没成" in env.summary_data(sess, sid)["focus"]["reason"]


def test_retry_once_recovers_and_rejected_requests_are_not_retried(env):
    sid, sess = standard(env)
    env.fake.fail = lambda kind, attempt: (
        llm.LLMCallError("LLM_REQUEST_TIMEOUT", "x") if attempt == 1 else None)
    assert run(focus.process_focus(sess, sid))["status"] == "done"
    assert env.fake.attempts["rank"] == 2 and env.fake.attempts["detail"] == 2

    sid2, sess2 = standard(env, name="小红", ext_id="s-hong")
    env.fake.attempts = {"check": 0, "rank": 0, "detail": 0, "merge": 0}
    env.fake.fail = lambda kind, attempt: llm.LLMCallError("LLM_REQUEST_REJECTED", "余额不足")
    result = run(focus.process_focus(sess2, sid2))
    assert result["status"] == "partial" and env.fake.attempts["rank"] == 1
    assert env.rows(FocusAction, FocusAction.streamer_id == sid2) == []
    assert "新动作没定成" in env.rows(FocusRun, FocusRun.streamer_id == sid2)[0].error


def test_detail_failure_keeps_the_ranked_items_as_candidates(env):
    sid, sess = standard(env)
    env.fake.fail = lambda kind, attempt: (
        llm.LLMCallError("LLM_PROVIDER_UNAVAILABLE", "x") if kind == "detail" else None)
    result = run(focus.process_focus(sess, sid))
    assert result["status"] == "partial"
    assert active_rows(env, sid) == []
    candidates = json.loads(env.rows(FocusRun)[0].candidates)
    assert {c["text"] for c in candidates} >= {POOL[0]["text"], POOL[2]["text"]}
    assert all(c["usable"] is False for c in candidates)


def test_unexpected_error_is_recorded_as_failed_so_backfill_does_not_loop(env, monkeypatch):
    sid, sess = standard(env)

    def boom(*_args):
        raise RuntimeError("数据库坏了 api_key=secret-123")

    monkeypatch.setattr(focus, "_save_checks", boom)
    env.action(sid, "冷场不超过5秒", kind="silence", threshold=5)
    result = run(focus.process_focus(sess, sid))
    assert result["status"] == "failed" and result["recorded"] is True
    row = env.rows(FocusRun)[0]
    assert row.status == "failed" and "secret-123" not in row.error
    assert focus.find_pending() == []


def test_focus_block_survives_summary_regeneration(env, monkeypatch):
    sid, sess = standard(env)
    run(focus.process_focus(sess, sid))
    block = env.summary_data(sess, sid)["focus"]

    async def fake_review(*_args, **_kwargs):
        return {"summary": "新的总结", "overall_score": 8, "llm": "test", "top_issues": ["新问题"]}

    monkeypatch.setattr(summary_service, "review_summary", fake_review)
    run(summary_service.generate_summary(sess, sid))
    data = env.summary_data(sess, sid)
    assert data["summary"] == "新的总结" and data["focus"] == block


def test_concurrent_writer_on_the_report_is_not_overwritten(env, monkeypatch):
    sid, sess = standard(env)
    original = report_data.merge_summary_data
    raced = []

    def merge_after_someone_else(session_id, streamer_id, mutate, **kwargs):
        if not raced:
            raced.append(True)
            original(session_id, streamer_id, lambda d: d.__setitem__("emotion_overview", {"x": 1}))
        return original(session_id, streamer_id, mutate, **kwargs)

    monkeypatch.setattr(focus, "merge_summary_data", merge_after_someone_else)
    run(focus.process_focus(sess, sid))
    data = env.summary_data(sess, sid)
    assert data["emotion_overview"] == {"x": 1} and "focus" in data


def test_deleting_a_session_removes_its_runs_and_checks(env):
    sid, sess = standard(env)
    link = env.action(sid, "报价后30秒内说出链接号并叫观众下单")
    run(focus.process_focus(sess, sid))
    with env.factory() as db:
        db.delete(db.get(ReplaySession, sess))
        db.commit()
    assert env.rows(FocusRun) == [] and env.rows(FocusCheck) == []
    assert env.get(FocusAction, link) is not None
    assert all(a.origin_session_id is None for a in env.rows(FocusAction))


# ======================= 补漏循环 =======================

def test_backfill_goes_in_time_order_per_streamer_and_skips_live_ones(env):
    a = env.streamer("小美", "s-mei")
    b = env.streamer("小红", "s-hong")
    c = env.streamer("小陈", "s-chen")
    old = env.session(datetime(2026, 9, 20, 10), [(a, 0, 1_800_000)])       # FOCUS_SINCE 之前
    env.summary(old, a)
    s1 = env.session(datetime(2026, 9, 29, 10), [(a, 0, 1_800_000), (b, 1_800_000, 3_600_000)])
    s2 = env.session(datetime(2026, 9, 30, 10), [(a, 0, 1_800_000)])
    live = env.session(datetime(2026, 10, 3, 10), [(c, 0, 1_800_000), (b, 1_800_000, 1_800_000)],
                       status=SessionStatus.live, ended=False, current=b)
    for sess, sid in ((s1, a), (s1, b), (s2, a), (live, c)):
        env.summary(sess, sid)
    # 小红正在直播：她的 9-29 那场也先不处理；小陈已经下了（班次结束、总结也出了）
    assert focus.find_pending() == [(s1, a), (s2, a), (live, c)]
    handled = run(focus.backfill_once())
    assert handled == 3
    order = [(r.session_id, r.streamer_id) for r in sorted(env.rows(FocusRun), key=lambda r: r.id)]
    assert order == [(s1, a), (s2, a), (live, c)]
    assert focus.find_pending() == []


def test_backfill_waits_for_an_unfinished_earlier_session_but_not_a_dead_one(env):
    a = env.streamer("小美")
    unfinished = env.session(datetime(2026, 10, 1, 10), [(a, 0, 1_800_000)],
                             status=SessionStatus.processing, ended=False)
    later = env.session(datetime(2026, 10, 2, 10), [(a, 0, 1_800_000)])
    env.summary(later, a)
    assert focus.find_pending() == []                 # 更早那场还在收尾（总结没出）：更晚的先等
    with env.factory() as db:
        row = db.get(ReplaySession, unfinished)
        row.status, row.ended_at = SessionStatus.done, datetime(2026, 10, 1, 15)
        db.commit()
    assert focus.find_pending() == [(later, a)]        # 收尾了但没总结：处理不了，也不挡后面


def test_backfill_stops_a_streamer_chain_when_an_earlier_one_leaves_no_record(env, monkeypatch):
    a = env.streamer("小美")
    s1 = env.session(datetime(2026, 10, 1, 10), [(a, 0, 1_800_000)])
    s2 = env.session(datetime(2026, 10, 2, 10), [(a, 0, 1_800_000)])
    for sess in (s1, s2):
        env.transcript(sess, a, speech(30))
        env.summary(sess, a)
    seen = []

    async def fake_process(session_id, streamer_id, force=False):
        seen.append(session_id)
        return {"status": "not_ready", "recorded": False}

    monkeypatch.setattr(focus, "process_focus", fake_process)
    assert run(focus.backfill_once()) == 0
    assert seen == [s1]                                # 更早的没处理完，不处理更晚的
    for _ in range(5):
        run(focus.backfill_once())
    assert seen.count(s1) == focus.BACKFILL_MAX_ATTEMPTS and s2 not in seen


def test_backfill_loop_respects_the_switch(env, monkeypatch):
    calls = []

    async def fake_once(limit=10):
        calls.append(limit)
        return 0

    async def stop(_seconds):
        if len(slept) >= 2:
            raise asyncio.CancelledError
        slept.append(_seconds)

    slept = []
    monkeypatch.setattr(focus, "backfill_once", fake_once)
    monkeypatch.setattr(focus.asyncio, "sleep", stop)
    monkeypatch.setattr(get_settings(), "focus_enabled", False)
    with pytest.raises(asyncio.CancelledError):
        run(focus.focus_backfill_loop(interval_sec=1, first_delay_sec=1))
    assert calls == []
    slept.clear()
    monkeypatch.setattr(get_settings(), "focus_enabled", True)
    with pytest.raises(asyncio.CancelledError):
        run(focus.focus_backfill_loop(interval_sec=1, first_delay_sec=1))
    assert calls == [10, 10]          # 先等一会儿，然后每隔一段时间补一轮


# ======================= 下播收尾挂任务 =======================

def _pipeline(env, monkeypatch, sid: int, sess: int):
    from app.core import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "SessionLocal", env.factory)
    p = pipeline_module.ReplayPipeline(sess)
    p._sent_by[sid] = ["一句话"]
    return p


def test_finalize_summary_starts_focus_in_background_and_failures_stay_inside(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "focus_enabled", True)
    sid, sess = standard(env)
    started = []

    async def fake_generate(session_id, streamer_id):
        return {"summary": "ok"}

    async def fake_focus(session_id, streamer_id, force=False):
        started.append((session_id, streamer_id))
        raise RuntimeError("核对炸了")

    monkeypatch.setattr(summary_service, "generate_summary", fake_generate)
    monkeypatch.setattr(focus, "process_focus", fake_focus)
    async def go(pipeline):
        await pipeline._finalize_streamer_summary(sid)
        pending = list(pipeline._background_tasks)
        await asyncio.gather(*pending)          # 后台任务自己兜住异常：这里不会抛
        return len(pending)

    assert run(go(_pipeline(env, monkeypatch, sid, sess))) == 1
    assert started == [(sess, sid)]

    monkeypatch.setattr(get_settings(), "focus_enabled", False)
    assert run(go(_pipeline(env, monkeypatch, sid, sess))) == 0     # 开关关掉：不挂任务
    assert started == [(sess, sid)]


def test_failed_summary_does_not_start_focus(env, monkeypatch):
    sid, sess = standard(env)
    started = []

    async def failing_generate(session_id, streamer_id):
        raise llm.LLMCallError("LLM_REQUEST_TIMEOUT", "x")

    async def fake_focus(session_id, streamer_id, force=False):
        started.append(session_id)

    monkeypatch.setattr(summary_service, "generate_summary", failing_generate)
    monkeypatch.setattr(focus, "process_focus", fake_focus)
    p = _pipeline(env, monkeypatch, sid, sess)
    with pytest.raises(llm.LLMCallError):
        run(p._finalize_streamer_summary(sid))
    assert started == [] and not p._background_tasks


# ======================= 对接接口 =======================

@pytest.fixture
def api(env):
    app = FastAPI()
    app.include_router(partner.router)

    def override_db():
        with env.factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    sid, sess = standard(env)
    run(focus.process_focus(sess, sid))
    actions = env.rows(FocusAction, FocusAction.streamer_id == sid, FocusAction.status == "active")
    other = env.streamer("小红", "s-hong")
    try:
        yield SimpleNamespace(client=client, sid=sid, sess=sess, other=other,
                              actions=sorted(a.id for a in actions))
    finally:
        client.close()


def test_every_focus_endpoint_requires_token_and_loopback(api):
    c = api.client
    a = api.actions[0]
    requests = [
        ("get", f"/api/partner/focus/streamers/{api.sid}", None),
        ("get", "/api/partner/focus/streamers/by-ext/s-mei", None),
        ("get", f"/api/partner/focus/sessions/{api.sess}?streamer_id={api.sid}", None),
        ("post", f"/api/partner/focus/actions/{a}/ack", {"ext_id": "s-mei"}),
        ("put", f"/api/partner/focus/actions/{a}", {"text": "新说法", "actor": "管理员"}),
        ("post", f"/api/partner/focus/actions/{a}/replace", {"candidate_index": 0, "actor": "管理员"}),
        ("post", f"/api/partner/focus/actions/{a}/remove", {"actor": "管理员"}),
        ("post", f"/api/partner/focus/streamers/{api.sid}/actions", {"actor": "管理员"}),
        ("post", "/api/partner/focus/streamers/by-ext/s-mei/actions", {"actor": "管理员"}),
        ("get", "/api/partner/admin/focus/overview", None),
    ]
    for method, url, body in requests:
        assert getattr(c, method)(url, **({"json": body} if body else {})).status_code == 403, url
        bad = getattr(c, method)(url, headers={"X-Partner-Token": "wrong"},
                                 **({"json": body} if body else {}))
        assert bad.status_code == 403, url
        proxied = getattr(c, method)(url, headers={**H, "X-Forwarded-For": "1.2.3.4"},
                                     **({"json": body} if body else {}))
        assert proxied.status_code == 403, url


def test_streamer_view_by_id_and_by_ext_and_unknown_is_empty(api):
    c = api.client
    by_id = c.get(f"/api/partner/focus/streamers/{api.sid}", headers=H).json()
    by_ext = c.get("/api/partner/focus/streamers/by-ext/s-mei", headers=H).json()
    by_key = c.get("/api/partner/focus/streamers/s-mei", headers=H).json()
    assert by_id == by_ext == by_key
    assert by_id["found"] is True and by_id["streamer"]["ext_id"] == "s-mei"
    assert by_id["streamer"]["name"] == "小美"
    assert len(by_id["active"]) == 4 and all(a["needs_ack"] for a in by_id["active"])
    assert (by_id["active_count"], by_id["needs_ack_count"], by_id["all_acked"]) == (4, 4, False)
    assert by_id["history"][0]["session_id"] == api.sess
    assert by_id["history"][0]["date"] == DAY.strftime("%Y-%m-%d")
    assert "3 条" not in json.dumps(by_id["rules"], ensure_ascii=False)
    for url in ("/api/partner/focus/streamers/by-ext/s-tester", "/api/partner/focus/streamers/99999",
                "/api/partner/focus/streamers/s-tester"):
        empty = c.get(url, headers=H)
        assert empty.status_code == 200
        body = empty.json()
        assert body["found"] is False and body["active"] == [] and body["all_acked"] is True


def test_session_view_returns_the_report_block(api):
    c = api.client
    by_id = c.get(f"/api/partner/focus/sessions/{api.sess}", params={"streamer_id": api.sid},
                  headers=H).json()
    by_ext = c.get(f"/api/partner/focus/sessions/{api.sess}", params={"ext_id": "s-mei"},
                   headers=H).json()
    assert by_id == by_ext and by_id["available"] is True
    assert by_id["focus"]["status"] == "done" and len(by_id["focus"]["next"]) == 4
    assert all(item["current"]["needs_ack"] for item in by_id["focus"]["next"])
    assert by_id["run"]["status_label"] == "已核对" and by_id["run"]["new_count"] == 4
    missing = c.get(f"/api/partner/focus/sessions/{api.sess}", params={"ext_id": "s-tester"},
                    headers=H).json()
    assert missing["found"] is False and missing["focus"] is None


def test_ack_checks_ownership_and_is_idempotent(api, env):
    c = api.client
    a = api.actions[0]
    url = f"/api/partner/focus/actions/{a}/ack"
    assert c.post(url, json={}, headers=H).status_code == 400
    assert c.post(url, json={"ext_id": "s-hong"}, headers=H).status_code == 403
    assert c.post(url, json={"ext_id": "s-tester"}, headers=H).status_code == 403
    assert c.post(url, json={"streamer_id": api.other}, headers=H).status_code == 403
    first = c.post(url, json={"ext_id": "s-mei"}, headers=H)
    assert first.status_code == 200
    body = first.json()
    assert body["changed"] is True and body["action"]["needs_ack"] is False
    assert body["action"]["acked_by"] == "小美" and body["needs_ack_count"] == 3
    assert body["streamer"] == {"id": api.sid, "name": "小美", "ext_id": "s-mei", "deleted": False}
    acked_at = env.get(FocusAction, a).acked_at
    again = c.post(url, json={"ext_id": "s-mei", "actor": "别人"}, headers=H).json()
    assert again["changed"] is False and env.get(FocusAction, a).acked_at == acked_at
    assert env.get(FocusAction, a).acked_by == "小美"
    for other in api.actions[1:]:
        c.post(f"/api/partner/focus/actions/{other}/ack", json={"streamer_id": api.sid}, headers=H)
    view = c.get("/api/partner/focus/streamers/by-ext/s-mei", headers=H).json()
    assert view["all_acked"] is True and view["needs_ack_count"] == 0
    assert c.post("/api/partner/focus/actions/99999/ack", json={"ext_id": "s-mei"},
                  headers=H).status_code == 404


def test_edit_keeps_the_id_and_asks_for_a_new_ack(api, env):
    c = api.client
    a = api.actions[0]
    c.post(f"/api/partner/focus/actions/{a}/ack", json={"ext_id": "s-mei"}, headers=H)
    url = f"/api/partner/focus/actions/{a}"
    assert c.put(url, json={"text": "新说法"}, headers=H).status_code == 400          # 没 actor
    assert c.put(url, json={"actor": "管理员"}, headers=H).status_code == 400          # 没内容
    assert c.put(url, json={"text": "长" * 31, "actor": "管理员"}, headers=H).status_code == 400
    r = c.put(url, json={"text": "报价后马上说清是第几号链接", "example": "就是二号链接",
                         "actor": "管理员"}, headers=H)
    assert r.status_code == 200
    action = r.json()["action"]
    assert action["id"] == a and action["needs_ack"] is True and action["acked_at"] is None
    assert action["text"] == "报价后马上说清是第几号链接" and action["example_source"] == "管理员"
    assert action["updated_by"] == "管理员" and r.json()["streamer"]["ext_id"] == "s-mei"
    same = c.put(url, json={"text": "报价后马上说清是第几号链接", "actor": "管理员"}, headers=H).json()
    assert same["changed"] is False


def test_replace_makes_a_new_action_and_keeps_the_old_one(api, env):
    c = api.client
    a, b = api.actions[0], api.actions[1]
    c.post(f"/api/partner/focus/actions/{a}/ack", json={"ext_id": "s-mei"}, headers=H)
    # 「没法检查」的候选不能直接拿来用
    assert c.post(f"/api/partner/focus/actions/{a}/replace",
                  json={"candidate_index": 0, "actor": "管理员"}, headers=H).status_code == 400
    with env.factory() as db:
        run_row = db.execute(select(FocusRun)).scalars().one()
        run_row.candidates = json.dumps([{
            "index": 0, "text": "讲完一款后问观众平时听什么歌", "why": "", "usable": True,
            "trigger": "一款讲完时", "criterion": "讲完一分钟内问听歌习惯", "example": "大家平时听什么歌？",
            "example_source": "AI 示范", "kind": "llm", "threshold_sec": None, "used_by": None,
        }], ensure_ascii=False)
        db.commit()
    r = c.post(f"/api/partner/focus/actions/{a}/replace",
               json={"candidate_index": 0, "actor": "管理员"}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replaced"]["id"] == a and body["replaced"]["status"] == "replaced"
    assert body["action"]["id"] != a and body["action"]["needs_ack"] is True
    assert body["action"]["created_by"] == "管理员" and body["action"]["origin_session_id"] == api.sess
    assert env.get(FocusAction, a).status == "replaced" and env.get(FocusAction, a).ended_at
    used = json.loads(env.rows(FocusRun)[0].candidates)[0]
    assert used["used_by"] == body["action"]["id"]
    assert c.post(f"/api/partner/focus/actions/{b}/replace",
                  json={"candidate_index": 0, "actor": "管理员"}, headers=H).status_code == 409
    manual = c.post(f"/api/partner/focus/actions/{b}/replace", headers=H, json={
        "text": "讲完一款后报一句今天还剩几款没讲", "trigger": "一款讲完时",
        "criterion": "讲完后马上说今天还要讲哪几款", "example": "下面还有两款，大家别走", "actor": "管理员"})
    assert manual.status_code == 200 and manual.json()["action"]["kind"] == "llm"
    assert c.post(f"/api/partner/focus/actions/{a}/replace", json={"text": "随便", "trigger": "t",
                  "criterion": "c", "actor": "管理员"}, headers=H).status_code == 409   # 已经换掉了
    assert len(active_rows(env, api.sid)) == 4


def test_add_has_no_cap_but_rejects_duplicates_and_remove_works(api, env):
    c = api.client
    url = f"/api/partner/focus/streamers/{api.sid}/actions"
    fields = {"text": "讲完一款后问观众平时听什么歌", "trigger": "一款讲完时",
              "criterion": "讲完一分钟内问听歌习惯", "actor": "管理员"}
    added = c.post(url, json=fields, headers=H)
    assert added.status_code == 200 and added.json()["active_count"] == 5          # 不设上限
    assert added.json()["action"]["created_by_label"] == "人工设定"
    assert c.post(url, json=fields, headers=H).status_code == 409                   # 同一个问题
    assert c.post(url, json={**fields, "text": "冷场别超过一分半", "kind": "silence",
                             "threshold_sec": 90}, headers=H).status_code == 409    # 已有一条不冷场
    assert c.post(url, json={**fields, "text": "x", "kind": "silence", "threshold_sec": 999},
                  headers=H).status_code == 400
    by_ext = c.post("/api/partner/focus/streamers/by-ext/s-mei/actions", headers=H,
                    json={**fields, "text": "每讲完参数补一句适合什么人"})
    assert by_ext.status_code == 200 and by_ext.json()["active_count"] == 6
    removed = c.post(f"/api/partner/focus/actions/{api.actions[2]}/remove", json={"actor": "管理员"},
                     headers=H).json()["action"]
    assert removed["status"] == "removed" and removed["status_label"] == "已删掉"
    assert c.post("/api/partner/focus/streamers/by-ext/s-tester/actions", json=fields,
                  headers=H).status_code == 404
    assert c.post(url, data="不是JSON", headers={**H, "Content-Type": "application/json"}).status_code == 400


def test_overview_lists_streamers_with_ext_id_name_and_recent_checks(api, env):
    c = api.client
    data = c.get("/api/partner/admin/focus/overview", headers=H).json()
    assert data["ok"] is True and data["generated_at"].endswith("+08:00")
    entry = next(s for s in data["streamers"] if s["streamer_id"] == api.sid)
    assert (entry["name"], entry["ext_id"]) == ("小美", "s-mei")
    assert entry["streamer"]["ext_id"] == "s-mei"
    assert len(entry["active"]) == 4 and entry["needs_ack_count"] == 4
    assert entry["recent"][0]["session_id"] == api.sess
    text = json.dumps(data, ensure_ascii=False)
    assert "李总" not in text


def test_no_returned_text_names_a_specific_person():
    labels = json.dumps([focus_store.STATUS_LABELS, focus_store.KIND_LABELS, focus_store.VERDICT_LABELS,
                         focus_store.RUN_LABELS], ensure_ascii=False)
    source = (Path(focus.__file__).read_text("utf-8") + Path(focus_store.__file__).read_text("utf-8"))
    assert "李总" not in labels and "李总" not in source
