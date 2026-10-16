"""改进动作第二版规则（2026-10-03 晚）：一个问题只出一条（同类要点合并）、跨场按类别去重指回原来那条、
价格/违禁词/亚秒停顿/型号称呼不出、在练加新拟超过 8 条时再合并一轮；以及首批重整（计划只读、按计划写库、
写回报告）。模型一律用假的。"""

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
from app.core import llm
from app.core.review import focus, focus_rebuild, focus_store, report_data
from app.db import Base, get_db
from app.models import (Critique, CritiqueLevel, FocusAction, FocusCheck, FocusRun, ReplaySession,
                        SessionStatus, Severity, Streamer, StreamerTurn, Transcript)

DAY = datetime(2026, 9, 29, 10, 0, 0)
TOKEN = "unit-test-partner-token"
H = {"X-Partner-Token": TOKEN}
FILLER = "这款耳机的声场很开阔，人声细腻，大家可以戴上仔细听一下"

DETAILS = {
    "讲款完整": ("主播开始讲一款产品时", "需求、卖点、适配、促单至少讲到其中 2 样", "这款适合听人声的朋友，喜欢直接拍"),
    "报价指令": ("主播报完价后", "15 秒内说出几号链接和下单指令", "就是 X 号链接，喜欢直接拍"),
    "订单售后": ("观众说订单或售后问题时", "引导私信或官方客服，不在公屏处理", "订单问题麻烦私信客服哦"),
    "开场介绍": ("开播或新观众进来时", "30 秒内说清直播间定位和今天重点款", "欢迎来到官方直播间，今天主推这款"),
    "答疑承接": ("主播回答观众问题时", "先复述问题再给结论", "这位朋友问能不能直推，可以的"),
    "拉回主线": ("聊完个人话题后", "20 秒内拉回到产品", "好，说回这款耳机"),
    "空档讲品": ("找链接或换设备时", "边操作边讲产品", "我换一下设备，这款低频很干净"),
    "其他": ("出现这种情况时", "出现后 15 秒内说出对应的话", "示范一句"),
}


class FakeLLM:
    """假的快速档：梳理按 self.order 原样返回；补全按类别给细节；合并按 self.groups；核对一律没有时机。"""

    name = "fake"

    def __init__(self) -> None:
        self.order: list[dict] = []
        self.skip: list[dict] = []
        self.groups: list[list[str]] = []
        self.calls: list[tuple[str, str, str]] = []
        self.bad_once: set[str] = set()     # 第一次补细节时给这几条一个不合格的时机（提了改价）

    def __call__(self, purpose: str = "fast") -> "FakeLLM":
        return self

    async def complete(self, system: str, user: str, *, json_mode: bool = False):
        if "核对员" in system:
            kind = "check"
            numbers = re.findall(r"^(\d+)\. 动作：", user.split("【动作】", 1)[1], re.M)
            data = {"r": [{"id": int(n), "m": []} for n in numbers]}
        elif "条数偏多" in system:
            kind = "merge"
            data = {"groups": self.groups}
        elif "按重要程度从高到低排好" in system:
            kind = "rank"
            found = re.search(r"总结的主要问题】\n1\. (.+)", user)
            src = found.group(1)[:8] if found else ""
            data = {"order": [{"src": src, **item} if item.get("new") else item for item in self.order],
                    "skip": self.skip}
        else:
            kind = "detail"
            items = []
            for number, text, label in re.findall(r"^(\d+)\. (.+?)（(.+)）$", user.split("【要补全的动作】", 1)[1],
                                                  re.M):
                category = label.split("类别：", 1)[1].split("；", 1)[0] if "类别：" in label else "其他"
                trigger, criterion, example = DETAILS.get(category, DETAILS["其他"])
                if text in self.bad_once:
                    self.bad_once.discard(text)
                    trigger = "等客服改价的时候"
                items.append({"i": int(number), "text": text, "trigger": trigger, "criterion": criterion,
                              "example": example})
            data = {"items": items}
        self.calls.append((kind, system, user))
        await asyncio.sleep(0)
        return llm.LLMResult(json.dumps(data, ensure_ascii=False), provider="fake")

    def kinds(self) -> list[str]:
        return [kind for kind, _s, _u in self.calls]


DISTINCT = [
    "下播前五分钟预告下一场时间", "收到下单提醒后念出名字道谢", "试听前先说这段听什么细节",
    "放歌结束后问大家听出区别没有", "讲新品前先说和旧款的一个区别", "介绍耳放时先问观众用什么播放器",
    "观众说预算后十秒内推荐一款", "讲完一款问观众平时听什么歌", "被问发货时先说能不能当天发出",
    "开播第一分钟报出今天能试听的型号", "换耳机时先说这副的声音特点", "每半小时总结一次讲过的款",
]


def new(text: str, cat: str, points=None, kind: str = "llm", **extra) -> dict:
    return {"new": text, "cat": cat, "kind": kind, "points": points or [], **extra}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'v2.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    for module in (focus, focus_store, focus_rebuild, report_data):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(focus, "RETRY_DELAY_SEC", 0)
    fake = FakeLLM()
    monkeypatch.setattr(focus, "get_llm", fake)
    monkeypatch.setattr(get_settings(), "partner_token", TOKEN)
    focus._inflight.clear()
    ns = SimpleNamespace(factory=factory, fake=fake)

    def streamer(name: str, ext_id: str | None = None) -> int:
        with factory() as db:
            row = Streamer(name=name, ext_id=ext_id)
            db.add(row)
            db.commit()
            return row.id

    def session(sid: int, start: datetime, issues: list[str], minutes: int = 30) -> int:
        with factory() as db:
            row = ReplaySession(title=f"场次{start:%m%d}", status=SessionStatus.done, started_at=start,
                                ended_at=start + timedelta(hours=1))
            db.add(row)
            db.flush()
            db.add(StreamerTurn(session_id=row.id, streamer_id=sid, seq=0, start_ms=0,
                                end_ms=minutes * 60_000))
            for i in range(minutes * 60 // 9):
                db.add(Transcript(session_id=row.id, streamer_id=sid, seq=i, start_ms=i * 9000,
                                  end_ms=i * 9000 + 6000, raw_text=FILLER, corrected_text=FILLER))
            db.add(Critique(session_id=row.id, streamer_id=sid, level=CritiqueLevel.summary,
                            severity=Severity.info, category="整场总结", comment="总结",
                            data=json.dumps({"summary": "总结", "top_issues": issues,
                                             "action_items": [{"action": "每次报价后补一句几号链接"}]},
                                            ensure_ascii=False)))
            db.commit()
            return row.id

    def action(sid: int, text: str, category: str = "", **fields) -> int:
        with factory() as db:
            row = FocusAction(streamer_id=sid, text=text, trigger="t", criterion="c",
                              kind=fields.pop("kind", "llm"), category=category, created_by="ai",
                              created_at=fields.pop("created_at", DAY - timedelta(days=5)), **fields)
            db.add(row)
            db.commit()
            return row.id

    def rows(model, *where):
        with factory() as db:
            return db.execute(select(model).where(*where)).scalars().all()

    def active(sid: int):
        with factory() as db:
            return focus_store.ordered_active(db, sid)

    def block(session_id: int, sid: int) -> dict:
        with factory() as db:
            row = db.execute(select(Critique).where(Critique.session_id == session_id,
                                                    Critique.streamer_id == sid)).scalars().one()
            return json.loads(row.data).get("focus")

    ns.streamer, ns.session, ns.action, ns.rows, ns.active, ns.block = (
        streamer, session, action, rows, active, block)
    try:
        yield ns
    finally:
        focus._inflight.clear()
        engine.dispose()


def run(coro):
    return asyncio.run(coro)


# ======================= 排除规则 =======================

@pytest.mark.parametrize("text", [
    "报价后立刻说券是否可用、有效期和点击几号链接",
    "每款报价后补一句：活动库存以小黄车实时页面为准",
    "每款报价后做一次价格或权益对比再给指令",
    "每段讲款必须包含一句价格锚定，如日常价或券后价",
    "停止口播提交订单别支付、联系客服改价",
    "不提站外交易平台，优惠赠品库存发货以官方后台确认为准",
    "不口播内部定价或议价策略",
    "观众提价格异议时，先讲两个卖点或适配价值再回应价格",
    "不说‘最好’‘第一’‘一定不会’等绝对化或保价承诺",
    "删掉“最好的线材”，改说本场线材方案里哪款适合什么",
    "每款讲完留0.5到1秒再开口，不抢话",
    "每款至少说一次完整型号，不用谐音或昵称",
    "讲款时全程用产品全称，不用简称或含糊称谓",
    "报价前先核实价格是否准确",
    "报链接号、提交待支付、等客服改价",
    "规范价格表达",
])
def test_topics_that_never_become_actions(text):
    assert focus_store.is_excluded_item(text)


@pytest.mark.parametrize("text", [
    "每次报价后必须说链接号和明确拍车指令",
    "报完价格后紧跟一句X号链接",
    "高客单旗舰先问有没有台机、是否线下听过再报价",
    "报价前先讲1-2个核心卖点再报价格",
    "开播第一分钟说清今天能试听哪几款",
    "订单号引导走私信或官方客服，不在公屏说",
    "讲款要讲全：需求、卖点、适配、促单至少讲到3样",
    "闲聊个人话题只做一次简短说明，随后必须承接产品",
])
def test_ordinary_actions_are_not_excluded(text):
    assert not focus_store.is_excluded_item(text)


def test_quoting_as_a_time_point_is_fine_in_any_wording():
    for trigger in ("主播报出价格后", "主播报完价格后", "说出到手价时", "每次报价时"):
        assert not focus_store.is_excluded_item("报价后紧跟链接号和下单指令", trigger,
                                                "15 秒内说出几号链接和下单指令")


def test_silence_threshold_is_picked_from_the_data_not_by_the_model():
    def gaps(*secs):
        return [focus.Gap(0, int(sec * 1000), "a", "b") for sec in secs]

    assert focus.suggest_silence_sec(gaps(31, 35, 40, 50, 70))[0] == 45       # ≥30 有 5 次，≥45 只有 2 次
    assert focus.suggest_silence_sec(gaps(*[65] * 8), sessions=2)[0] == 90    # 两场平均每场 4 次 ≥60
    assert focus.suggest_silence_sec(gaps(*[150] * 9))[0] == 120               # 都超过：取最大档
    assert "建议秒数：45" in focus.silence_profile(gaps(31, 35, 40, 50, 70))
    ctx = SimpleNamespace(active=[], passed=[], silence_sec=60)
    entries, _ = focus.parse_rank({"order": [new("冷场不超过20秒", "冷场", kind="silence", sec=20)]}, ctx)
    assert entries[0]["sec"] == 60


def test_detail_prompt_is_about_hifi_headphones():
    assert "HiFi 耳机" in focus.DETAIL_SYSTEM and "付款方式" in focus.DETAIL_SYSTEM


def test_check_prompt_understands_at_least_n_items():
    assert "至少讲到其中 N 样" in focus.CHECK_SYSTEM
    assert "合成一条" in focus.RANK_SYSTEM and "每个类别最多一条" in focus.RANK_SYSTEM
    for name in focus_store.CATEGORIES:
        assert f"- {name}：" in focus.RANK_SYSTEM
    assert "至少讲到其中 N 样" in focus.DETAIL_SYSTEM


# ======================= 一个问题只出一条 =======================

def test_split_pieces_of_one_problem_are_merged_into_one_action(env):
    sid = env.streamer("小陈", "s-chen")
    sess = env.session(sid, DAY, ["讲款结构不完整：缺适配、缺风险逆转、缺需求挖掘"])
    env.fake.order = [
        new("讲款要讲全", "讲款完整", ["需求", "卖点"]),
        new("每段讲款必须包含一句适配建议", "讲款完整", ["适配"]),
        new("每段讲款必须包含一句需求挖掘", "讲款完整", ["需求", "场景"]),
        new("每段讲款必须包含一句价格锚定", "讲款完整", ["价格锚定"]),       # 价格：直接不出
        new("每次报价后说链接号和拍车指令", "报价指令"),
        new("说话再笃定一些", "其他"),
    ]
    env.fake.skip = [{"text": "语气更自信", "why": "逐字稿判断不了"}]
    result = run(focus.process_focus(sess, sid))
    assert result["status"] == "done"
    active = env.active(sid)
    assert [(a.text, a.category) for a in active] == [
        ("讲款要讲全", "讲款完整"), ("每次报价后说链接号和拍车指令", "报价指令"), ("说话再笃定一些", "其他")]
    detail_prompt = next(u for k, _s, u in env.fake.calls if k == "detail")
    assert "讲款要讲全（类别：讲款完整；要点：需求、卖点、适配、场景）" in detail_prompt
    assert active[0].criterion == "需求、卖点、适配、促单至少讲到其中 2 样"
    candidates = json.loads(env.rows(FocusRun)[0].candidates)
    whys = {c["text"]: c["why"] for c in candidates}
    assert "合成一条" in whys["每段讲款必须包含一句适配建议"]
    assert "合成一条" in whys["每段讲款必须包含一句需求挖掘"]
    assert "每段讲款必须包含一句价格锚定" not in whys          # 价格类连候选都不进
    assert whys["语气更自信"] == "逐字稿判断不了"


def test_points_mentioning_excluded_topics_are_dropped_not_the_action():
    ctx = SimpleNamespace(active=[], passed=[])
    entries, _ = focus.parse_rank({"order": [
        new("讲款要讲全", "讲款完整", ["需求", "价格锚定", "券后价", "卖点", "从众"]),
        new("说出到手价后马上报链接号", "报价指令"),
        new("每款讲完留0.5秒再开口", "其他"),
    ]}, ctx)
    assert [e["text"] for e in entries] == ["讲款要讲全", "说出到手价后马上报链接号"]
    assert entries[0]["points"] == ["需求", "卖点", "从众"]


def test_actions_must_quote_the_summary_or_they_are_not_made(env):
    sid = env.streamer("杨雪", "s-xue")
    sess = env.session(sid, DAY, ["开场没有说清直播间定位和今天主推的款", "闲聊太多"])
    env.fake.order = [
        {**new("开播 30 秒说清定位和主推", "开场介绍"), "src": "开场没有说清直播间定位"},      # 原话对得上
        {**new("聊完马上说回产品", "拉回主线"), "src": "闲聊太多了"},                       # 抄错一个字，短，对不上
        {**new("订单问题引导私信客服", "订单售后"), "src": "订单号在公屏说"},                 # 总结里根本没有
        {**new("讲款要讲全", "讲款完整"), "src": "开场没有说清楚直播间定位"},               # 长一点、抄错几个字：算
        new("冷场不超过60秒", "冷场", kind="silence", sec=60),                               # 冷场看数据，不用引
    ]
    run(focus.process_focus(sess, sid))
    assert [a.category for a in env.active(sid)] == ["开场介绍", "讲款完整", "冷场"]
    whys = {c["text"]: c["why"] for c in json.loads(env.rows(FocusRun)[0].candidates)}
    assert whys["订单问题引导私信客服"] == "总结里找不到对应的原话，没出"
    assert whys["聊完马上说回产品"] == "总结里找不到对应的原话，没出"


def test_examples_lose_the_clauses_streamers_must_not_say():
    assert focus_store.clean_example("价格就是X号链接，库存有限，拍下就是赚到。") == "价格就是X号链接，拍下就是赚到。"
    assert focus_store.clean_example("库存有限，快拍。") == ""
    assert focus_store.clean_example("领券再拍更划算") == ""
    item = focus_store.clean_generated({"text": "报完价马上说几号链接", "trigger": "主播报完价后",
                                        "criterion": "15 秒内说出几号链接", "category": "报价指令",
                                        "example": "就是X号链接，最后几台库存，赶紧拍！"})
    assert item["example"] == "就是X号链接，赶紧拍！"                       # 只删「最后几台库存」那半句


def test_boilerplate_clauses_in_the_standard_are_cut_not_the_whole_action():
    item = focus_store.clean_generated({
        "text": "讲一款就把要点讲全", "category": "讲款完整", "trigger": "主播开始讲一款产品时",
        "criterion": "每一处讲款中，需求、卖点、适配、促单至少讲到其中3样，且参数以知识库为准。",
        "example": "这款适合听人声的朋友"})
    assert item is not None and item["criterion"] == "每一处讲款中，需求、卖点、适配、促单至少讲到其中3样。"
    item = focus_store.clean_generated({
        "text": "售后引导私信或官方客服", "category": "订单售后", "trigger": "观众在公屏问订单时",
        "criterion": "引导走平台客服，公屏不处理订单，不引导站外私信。"})
    assert item is not None and "站外" not in item["criterion"]
    assert focus_store.clean_generated({          # 动作本身就是价格话题：整条不要
        "text": "报价后说清券后价", "category": "其他", "trigger": "报完价后", "criterion": "说清券后价和有效期"}) is None
    assert focus_store.clean_generated({          # 删完剩不下能检查的标准：不要
        "text": "讲一款就把要点讲全", "category": "讲款完整", "trigger": "讲款时",
        "criterion": "参数以知识库为准"}) is None


def test_echoed_label_is_ignored_when_matching_details():
    items = [{"text": "讲一款就把要点讲全"}, {"text": "报价后紧跟链接号"}]
    rows = focus.parse_detail({"items": [
        {"i": 1, "text": "报价后紧跟链接号（类别：报价指令；要点：链接号、下单）", "criterion": "b"},
        {"i": 2, "text": "讲一款就把要点讲全（类别：讲款完整；要点：需求、卖点）", "criterion": "a"},
    ]}, items)
    assert {n: r["criterion"] for n, r in rows.items()} == {1: "a", 2: "b"}


def test_item_whose_detail_was_rejected_gets_one_more_try(env):
    sid = env.streamer("小陈", "s-chen")
    sess = env.session(sid, DAY, ["等客服时没人讲话，空档太多"])
    env.fake.order = [new("空档时边等边讲产品", "空档讲品"), new("讲款要讲全", "讲款完整")]
    env.fake.bad_once = {"空档时边等边讲产品"}
    run(focus.process_focus(sess, sid))
    assert [a.text for a in env.active(sid)] == ["空档时边等边讲产品", "讲款要讲全"]
    assert env.fake.kinds().count("detail") == 2
    retry_prompt = [u for k, _s, u in env.fake.calls if k == "detail"][1]
    assert "空档时边等边讲产品" in retry_prompt and "讲款要讲全" not in retry_prompt


def test_detail_is_matched_by_echoed_text_not_by_number():
    items = [{"text": "空档也要讲品或留人"}, {"text": "闲聊后及时拉回产品"}, {"text": "订单售后引导私信客服"}]
    rows = focus.parse_detail({"items": [
        {"i": 1, "text": "空档也要讲品或留人", "criterion": "a"},
        {"i": 2, "text": "离场前要留人", "criterion": "模型自己多拆出来的一条"},
        {"i": 3, "text": "闲聊后及时拉回产品", "criterion": "b"},
        {"i": 4, "text": "订单售后引导私信客服", "criterion": "c"},
    ]}, items)
    assert {n: r["criterion"] for n, r in rows.items()} == {1: "a", 2: "b", 3: "c"}
    legacy = focus.parse_detail({"items": [{"i": 2, "criterion": "x"}]}, items)   # 没回原文：按编号
    assert legacy == {2: {"i": 2, "criterion": "x"}}


# ======================= 跨场去重 =======================

def test_same_category_in_a_later_session_points_back_even_if_worded_differently(env):
    sid = env.streamer("杨柳青", "s-yang")
    first = env.session(sid, DAY, ["订单号在公屏说"])
    env.fake.order = [new("订单号引导走私信或官方客服，不在公屏说", "订单售后"),
                      new("每款报价后必须接一句拍链接指令", "报价指令")]
    run(focus.process_focus(first, sid))
    order_id = next(a.id for a in env.active(sid) if a.category == "订单售后")
    second = env.session(sid, DAY + timedelta(days=1), ["订单问题处理不规范"])
    # 模型没认出是同一个问题，换了说法当新的写——代码按类别指回原来那条
    env.fake.order = [new("订单问题统一说请私信或联系平台客服处理", "订单售后"),
                      new("不引导用户公屏发订单号", "订单售后"),
                      new("报价后紧跟一句拍车指令", "报价指令"),
                      new("聊完个人话题马上说回产品", "拉回主线")]
    run(focus.process_focus(second, sid))
    active = env.active(sid)
    assert [a.category for a in active] == ["订单售后", "报价指令", "拉回主线"]
    assert active[0].id == order_id                       # 原来那条，按本场重要程度排第一
    run2 = env.rows(FocusRun, FocusRun.session_id == second)[0]
    assert run2.new_count == 1
    whys = [c["why"] for c in json.loads(run2.candidates)]
    assert sum("同一类（订单售后）" in w for w in whys) == 2 and any("同一类（报价指令）" in w for w in whys)
    rank_prompt = [u for k, _s, u in env.fake.calls if k == "rank"][-1]
    assert "A1. [订单售后] 订单号引导走私信或官方客服，不在公屏说" in rank_prompt


def test_old_uncategorized_actions_still_match_by_wording_and_silence_by_kind(env):
    sid = env.streamer("许文璐", "s-xu")
    env.action(sid, "报价后30秒内说出链接号并叫观众下单")           # 第一版留下的，没有类别
    env.action(sid, "相邻两句话之间空白不超过60秒", kind="silence", threshold_sec=60)
    sess = env.session(sid, DAY, ["冷场"])
    env.fake.order = [new("报价后30秒内说出链接号，叫观众下单", "报价指令"),
                      new("冷场不超过30秒", "冷场", kind="silence", sec=30)]
    run(focus.process_focus(sess, sid))
    assert len(env.active(sid)) == 2


def test_passed_action_of_the_same_category_can_come_back(env, monkeypatch):
    # 2026-10-04 起：已过关的同类动作只有本场复查没做到才加回（按原动作），总结里提到同类不再算数
    sid = env.streamer("杨雪", "s-xue")
    old = env.action(sid, "报价后必须说链接号", "报价指令", status="passed", passed_at=DAY)
    sess = env.session(sid, DAY + timedelta(days=1), ["报价后又不说链接号了"])
    env.fake.order = [new("报完价马上说几号链接和怎么拍", "报价指令")]
    original = FakeLLM.complete

    async def failing_check(system, user, *, json_mode=False):
        if "核对员" in system and "报价后必须说链接号" in user:
            first = re.search(r"^\[(\d\d:\d\d:\d\d)\]", user, re.M).group(1)
            numbers = re.findall(r"^(\d+)\. 动作：", user.split("【动作】", 1)[1], re.M)
            data = {"r": [{"id": int(n), "m": [[first, 0, ""]]} for n in numbers]}
            return llm.LLMResult(json.dumps(data, ensure_ascii=False), provider="fake")
        return await original(env.fake, system, user, json_mode=json_mode)

    monkeypatch.setattr(env.fake, "complete", failing_check)
    run(focus.process_focus(sess, sid))
    again = env.active(sid)
    assert len(again) == 1 and again[0].reopened_from == old and again[0].streak == 0
    assert again[0].text == "报价后必须说链接号"                     # 按原动作加回


def test_passed_action_still_done_does_not_come_back(env):
    sid = env.streamer("杨雪", "s-xue")
    env.action(sid, "报价后必须说链接号", "报价指令", status="passed", passed_at=DAY)
    sess = env.session(sid, DAY + timedelta(days=1), ["报价后又不说链接号了"])
    env.fake.order = [new("报完价马上说几号链接和怎么拍", "报价指令")]
    run(focus.process_focus(sess, sid))                                # 复查没出现时机（不算没做到）
    assert env.active(sid) == []


def test_manual_add_of_an_existing_category_is_refused(env):
    sid = env.streamer("小陈", "s-chen")
    env.action(sid, "讲款要讲全", "讲款完整")
    app = FastAPI()
    app.include_router(partner.router)

    def override_db():
        with env.factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    body = {"text": "每款讲完问一句听什么歌", "trigger": "讲完一款时", "criterion": "问一句听歌习惯",
            "actor": "管理员"}
    refused = client.post(f"/api/partner/focus/streamers/{sid}/actions", headers=H,
                          json={**body, "category": "讲款完整"})
    assert refused.status_code == 409 and "同一类只留一条" in refused.json()["detail"]
    assert client.post(f"/api/partner/focus/streamers/{sid}/actions", headers=H,
                       json={**body, "category": "不存在的类"}).status_code == 400
    ok = client.post(f"/api/partner/focus/streamers/{sid}/actions", headers=H, json=body)
    assert ok.status_code == 200 and ok.json()["action"]["category"] == "其他"
    client.close()


# ======================= 超过 8 条再合并一轮 =======================

def test_over_eight_triggers_one_merge_round(env, capsys):
    sid = env.streamer("小陈", "s-chen")
    existing = [env.action(sid, DISTINCT[n], "其他", sort_order=n + 1) for n in range(6)]
    sess = env.session(sid, DAY, ["问题很多"])
    env.fake.order = [new(DISTINCT[6], "其他"), new(DISTINCT[7], "其他"),
                      new(DISTINCT[8], "其他"), new(DISTINCT[9], "其他")]
    env.fake.groups = [["A1", "N1"], ["N2", "N3"]]       # N1 并进在练的 A1；N2、N3 合成一条
    run(focus.process_focus(sess, sid))
    assert env.fake.kinds().count("merge") == 1
    texts = [a.text for a in env.active(sid)]
    assert len(texts) == 8 and DISTINCT[6] not in texts and DISTINCT[8] not in texts
    assert DISTINCT[7] in texts and DISTINCT[9] in texts
    assert env.active(sid)[0].id == existing[0]           # 指回的那条排在新拟的位置上
    assert env.rows(FocusRun)[0].note == ""
    merge_prompt = next(u for k, _s, u in env.fake.calls if k == "merge")
    assert f"A6. [其他] {DISTINCT[5]}" in merge_prompt and f"N4. [其他] {DISTINCT[9]}" in merge_prompt
    assert "超过 8 条" not in capsys.readouterr().out


def test_when_nothing_can_be_merged_everything_is_kept_and_noted(env, capsys):
    sid = env.streamer("小陈", "s-chen")
    for n in range(7):
        env.action(sid, DISTINCT[n], "其他", sort_order=n + 1)
    sess = env.session(sid, DAY, ["问题很多"])
    env.fake.order = [new(DISTINCT[7], "其他"), new(DISTINCT[8], "其他")]
    run(focus.process_focus(sess, sid))
    assert len(env.active(sid)) == 9
    note = env.rows(FocusRun)[0].note
    assert "还有 9 条" in note and "超过 8 条" in note
    assert "超过 8 条" in capsys.readouterr().out
    assert "超过 8 条" in env.block(sess, sid)["note"]


def test_no_merge_call_when_eight_or_fewer(env):
    sid = env.streamer("小陈", "s-chen")
    sess = env.session(sid, DAY, ["问题很多"])
    env.fake.order = [new(DISTINCT[n], "其他") for n in range(8)]
    run(focus.process_focus(sess, sid))
    assert len(env.active(sid)) == 8 and "merge" not in env.fake.kinds()


# ======================= 首批重整 =======================

@pytest.fixture
def first_batch(env):
    """仿线上：两位主播，旧规则留下的一堆碎动作；一位有两场、一位一场。"""
    chen = env.streamer("小陈", "s-chen")
    xue = env.streamer("杨雪", "s-xue")
    s20 = env.session(chen, datetime(2026, 9, 29, 8), ["讲款结构不完整", "订单号在公屏说"])
    s21 = env.session(chen, datetime(2026, 9, 30, 10), ["报价后不说链接号", "闲聊太多"])
    s20b = env.session(xue, datetime(2026, 9, 29, 16), ["开场没说清主推"])
    later = env.session(chen, datetime(2026, 10, 5, 10), ["以后的场次不算"])
    for sess, sid in ((s20, chen), (s21, chen), (s20b, xue), (later, chen)):
        with env.factory() as db:
            db.add(FocusRun(session_id=sess, streamer_id=sid, status="done", candidates="[]",
                            turn_started_at=datetime(2026, 9, 29)))
            row = db.execute(select(Critique).where(Critique.session_id == sess)).scalars().one()
            data = json.loads(row.data)
            data["focus"] = {"status": "done", "checked": [], "next": [], "candidates": [], "note": ""}
            row.data = json.dumps(data, ensure_ascii=False)
            db.commit()
    old_chen = [env.action(chen, f"旧的碎动作{n}", created_at=datetime(2026, 10, 3, 12, 39)) for n in range(19)]
    old_xue = [env.action(xue, f"杨雪旧动作{n}", created_at=datetime(2026, 10, 3, 12, 39)) for n in range(7)]
    env.action(chen, "很早以前人工删掉的动作", status="removed", updated_by="管理员")
    return SimpleNamespace(chen=chen, xue=xue, s20=s20, s21=s21, s20b=s20b, later=later,
                           old_chen=old_chen, old_xue=old_xue)


def rebuild_order(user: str) -> list[dict]:
    if "小陈" in user:
        return [new("讲款要讲全", "讲款完整", ["需求", "卖点", "适配", "促单"]),
                new("报完价马上说几号链接", "报价指令"), new("订单问题引导私信客服", "订单售后"),
                new("聊完个人话题马上说回产品", "拉回主线")]
    return [new("开播 30 秒说清定位和主推", "开场介绍"), new("空档也要讲品或留人", "空档讲品"),
            new("冷场不超过60秒", "冷场", kind="silence", sec=60)]


def test_rebuild_plan_is_read_only_and_merges_sessions_per_streamer(env, first_batch, monkeypatch):
    calls = []

    async def fake_complete(system, user, *, json_mode=False):
        if "按重要程度从高到低排好" in system:
            calls.append(user)
            env.fake.order = rebuild_order(user)
        return await FakeLLM.complete(env.fake, system, user, json_mode=json_mode)

    monkeypatch.setattr(env.fake, "complete", fake_complete)
    plan = run(focus_rebuild.plan_rebuild("2026-09-29", "2026-10-01"))
    assert len(env.rows(FocusAction, FocusAction.status == "active")) == 26     # 没写库
    by_name = {p["name"]: p for p in plan["streamers"]}
    chen = by_name["小陈"]
    assert chen["sessions"] == [first_batch.s20, first_batch.s21]             # 10-05 那场不在窗口里
    assert sorted(chen["old_active_ids"]) == sorted(first_batch.old_chen)
    assert [a["text"] for a in chen["actions"]] == [
        "讲款要讲全", "报完价马上说几号链接", "订单问题引导私信客服", "聊完个人话题马上说回产品"]
    assert by_name["杨雪"]["sessions"] == [first_batch.s20b] and len(by_name["杨雪"]["actions"]) == 3
    chen_prompt = next(u for u in calls if "小陈" in u)
    assert "【9-29 场 总结的主要问题】" in chen_prompt and "【9-30 场 总结的主要问题】" in chen_prompt
    assert "以上几场的问题合在一起考虑" in chen_prompt
    assert "【在练动作】\n（没有）" in chen_prompt                              # 旧的马上下线，不算在练
    assert "很早以前人工删掉的动作" in chen_prompt
    assert sum(1 for u in calls if "小陈" in u) == 1                          # 两场合在一起只生成一次
    text = focus_rebuild.render_plan(plan)
    assert "小陈" in text and "旧 19 条 → 新 4 条" in text and "标准：需求、卖点、适配、促单至少讲到其中 2 样" in text
    json.dumps(plan, ensure_ascii=False)                                     # 能存成文件


def test_rebuild_apply_replaces_old_actions_and_writes_reports(env, first_batch, monkeypatch):
    async def fake_complete(system, user, *, json_mode=False):
        if "按重要程度从高到低排好" in system:
            env.fake.order = rebuild_order(user)
        return await FakeLLM.complete(env.fake, system, user, json_mode=json_mode)

    monkeypatch.setattr(env.fake, "complete", fake_complete)
    plan = run(focus_rebuild.plan_rebuild("2026-09-29", "2026-10-01"))
    result = focus_rebuild.apply_rebuild(json.loads(json.dumps(plan)))
    assert result["created"] == 7 and result["blocks_missing"] == []       # 计划里几条就写几条
    xue = [(a.category, a.text) for a in env.active(first_batch.xue)]
    assert ("空档讲品", "空档也要讲品或留人") in xue and ("冷场", "冷场不超过60秒") in xue
    removed = env.rows(FocusAction, FocusAction.id.in_(first_batch.old_chen + first_batch.old_xue))
    assert all(a.status == "removed" and a.updated_by == "首批重整" and a.ended_at for a in removed)
    chen_active = env.active(first_batch.chen)
    assert [a.sort_order for a in chen_active] == [1, 2, 3, 4]
    assert all(a.origin_session_id == first_batch.s21 and a.created_by == "ai" and a.category
               for a in chen_active)
    for sess in (first_batch.s20, first_batch.s21):
        block = env.block(sess, first_batch.chen)
        assert [n["text"] for n in block["next"]] == [a.text for a in chen_active]
        assert "首批重整" in block["note"] and block["rebuilt_at"].endswith("+08:00")
        run_row = env.rows(FocusRun, FocusRun.session_id == sess, FocusRun.streamer_id == first_batch.chen)[0]
        assert "首批重整" in run_row.note
    assert env.block(first_batch.later, first_batch.chen)["next"] == []      # 窗口外的报告不动
    # 首批重整下线的旧动作不算「人工删掉」：之后的梳理里不会列进「不要再出」
    ctx = focus._load_context(first_batch.s21, first_batch.chen)
    assert [a.text for a in ctx.removed] == ["很早以前人工删掉的动作"]
    # 同一份计划不能再用一次（旧动作已经不在练了）
    with pytest.raises(focus_rebuild.RebuildError):
        focus_rebuild.apply_rebuild(plan)
    state = focus_rebuild.current_state([first_batch.chen])
    assert len(state[0]["active"]) == 4


def test_rebuild_refuses_a_plan_that_cannot_be_written_exactly(env, first_batch, monkeypatch):
    async def fake_complete(system, user, *, json_mode=False):
        if "按重要程度从高到低排好" in system:
            env.fake.order = rebuild_order(user)
        return await FakeLLM.complete(env.fake, system, user, json_mode=json_mode)

    monkeypatch.setattr(env.fake, "complete", fake_complete)
    plan = run(focus_rebuild.plan_rebuild("2026-09-29", "2026-10-01"))
    xue = next(e for e in plan["streamers"] if e["name"] == "杨雪")
    xue["actions"].append(dict(xue["actions"][0]))       # 同一条写两遍：第二条写不进去
    with pytest.raises(focus_rebuild.RebuildError, match="写不进去"):
        focus_rebuild.apply_rebuild(plan)
    assert len(env.rows(FocusAction, FocusAction.status == "active")) == 26     # 整个撤销


def test_older_plans_are_cleaned_with_todays_rules_before_writing():
    plan = {"version": focus_rebuild.PLAN_VERSION, "streamers": [{"name": "杨柳青", "actions": [
        {"text": "讲一款就把要点讲全", "trigger": "主播开始讲一款产品时", "category": "讲款完整", "kind": "llm",
         "threshold_sec": 10, "criterion": "需求、卖点、适配至少讲到其中2样，且不出现未验证参数。",
         "example": "这款适合听人声，库存有限快拍。"},
        {"text": "报价后说清券后价", "trigger": "报完价后", "category": "其他", "kind": "llm",
         "threshold_sec": 10, "criterion": "说清券后价"},
    ]}]}
    notes = focus_rebuild.sanitize_plan(plan)
    actions = plan["streamers"][0]["actions"]
    assert len(actions) == 1 and actions[0]["criterion"] == "需求、卖点、适配至少讲到其中2样。"
    assert actions[0]["example"] == "这款适合听人声。"                        # 只删「库存有限」那半句
    assert any("跳过" in n for n in notes) and any("标准" in n for n in notes)


def test_rebuild_refuses_when_something_changed_or_was_acked(env, first_batch, monkeypatch):
    async def fake_complete(system, user, *, json_mode=False):
        if "按重要程度从高到低排好" in system:
            env.fake.order = rebuild_order(user)
        return await FakeLLM.complete(env.fake, system, user, json_mode=json_mode)

    monkeypatch.setattr(env.fake, "complete", fake_complete)
    plan = run(focus_rebuild.plan_rebuild("2026-09-29", "2026-10-01"))
    extra = env.action(first_batch.chen, "计划之后又多出来一条")
    with pytest.raises(focus_rebuild.RebuildError, match="和生成计划时不一样"):
        focus_rebuild.apply_rebuild(plan)
    assert len(env.rows(FocusAction, FocusAction.status == "active")) == 27   # 什么都没改
    with env.factory() as db:
        db.delete(db.get(FocusAction, extra))
        acked = db.get(FocusAction, first_batch.old_xue[0])
        acked.acked_at = datetime.now()
        db.commit()
    with pytest.raises(focus_rebuild.RebuildError, match="确认"):
        focus_rebuild.apply_rebuild(plan)
    with pytest.raises(focus_rebuild.RebuildError, match="需要人工决定"):
        run(focus_rebuild.plan_rebuild("2026-09-29", "2026-10-01"))
    with env.factory() as db:
        db.get(FocusAction, first_batch.old_xue[0]).acked_at = None
        db.add(FocusCheck(action_id=first_batch.old_chen[0], session_id=first_batch.later,
                          streamer_id=first_batch.chen, verdict="pass"))
        db.commit()
    with pytest.raises(focus_rebuild.RebuildError, match="核对过"):
        run(focus_rebuild.plan_rebuild("2026-09-29", "2026-10-01"))
