"""v2 深度复盘（2026-09-30）：各块并行 + 并发上限、单块/单批失败降级、只补跑失败块、
同一（场次, 主播）不重复生成、按场次的状态不互相覆盖、启动复位，以及接口对前端的约定。"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import sessions as sessions_api
from app.config import get_settings
from app.core import llm
from app.core.review import deep_analysis, report_data, v2_jobs
from app.core.usage import current_llm_context
from app.db import Base, get_db
from app.models import (CompassPoint, Critique, CritiqueLevel, OcrRecord, ReplaySession,
                        Severity, Streamer, Transcript)

ALL_BLOCKS = ["qc", "tone", "structure", "danmaku", "compass", "chapters", "products"]
_KIND = (
    ("直播情绪教练", "tone"), ("直播结构教练", "structure"), ("弹幕回复质检员", "danmaku"),
    ("罗盘归因分析师", "compass"), ("[C序号]", "chapters"), ("[P序号]", "products"),
)


class FakeLLM:
    """假的深度模型：记下每次调用是哪一块、先后顺序、同时在跑几个；可以让指定的调用失败。"""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.started: list[str] = []      # 按开始先后；章节带这一批的首个序号，如 chapters:C8
        self.finished: list[str] = []
        self.attempts: dict[str, int] = defaultdict(int)
        self.contexts: list = []          # 每次调用时的用量归属（场次 + 阶段）
        self.inflight = 0
        self.max_inflight = 0
        self.gate: asyncio.Event | None = None
        self.fail = lambda label, attempt: None   # 返回异常 = 这次调用失败

    def __call__(self, purpose: str = "fast") -> "FakeLLM":   # 顶替 deep_analysis.get_llm
        assert purpose == "deep"
        return self

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        assert json_mode is True
        kind = next(name for mark, name in _KIND if mark in system)
        label = kind
        if kind == "chapters":
            label = "chapters:C" + re.search(r"\[C(\d+)\|", user).group(1)
        self.attempts[label] += 1
        self.started.append(label)
        self.contexts.append(current_llm_context())
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(self.delay)
            error = self.fail(label, self.attempts[label])
            if error is not None:
                raise error
        finally:
            self.inflight -= 1
        self.finished.append(label)
        return json.dumps(self._answer(kind, user), ensure_ascii=False)

    @staticmethod
    def _answer(kind: str, user: str) -> dict:
        if kind == "tone":
            return {"verdict": "语气总评", "chips": ["平"], "issues": [
                {"title": "情绪偏平", "sev": "orange", "anchors": ["S0"], "detail": "d",
                 "suggest_emotion": "兴奋", "suggest_line": "l"}]}
        if kind == "structure":
            return {"verdict": "结构总评", "chips": [], "issues": [
                {"title": "缺促单", "sev": "red", "anchors": ["S0"], "why": "w", "how": "h"}],
                "frame": [{"name": "开场留人", "status": "ok", "note": "", "demo": ""}]}
        if kind == "danmaku":
            return {"items": [{"i": int(i), "cat": "intent", "strength": "strong", "basis": "b",
                               "verdict": "good", "problem": "", "better": ""}
                              for i in re.findall(r"\[D(\d+)\|", user)]}
        if kind == "compass":
            return {"items": [{"i": int(i), "title": "铺垫到位", "chain": "c", "advice": "a",
                               "moments": []} for i in re.findall(r"\[E(\d+)\|", user)]}
        if kind == "chapters":
            return {"items": [{"i": int(i), "type": "讲品", "structures": ["卖点讲解"], "missing": [],
                               "problems": [], "how_to": "h", "idle": "none", "score": 7}
                              for i in re.findall(r"\[C(\d+)\|", user)]}
        return {"items": [{"i": int(i), "verdict": "讲得完整", "structures": [], "missing": [],
                           "problems": [], "how_to": "h", "score": 7}
                          for i in re.findall(r"\[P(\d+)\]", user)]}


def _rejected(*_args) -> Exception:
    return llm.LLMCallError("LLM_REQUEST_REJECTED", "余额不足")


@pytest.fixture
def v2_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'v2.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    for module in (deep_analysis, report_data, v2_jobs):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(deep_analysis, "_slots", None)
    monkeypatch.setattr(deep_analysis, "_RATE_LIMIT_WAITS", (0.01, 0.01))
    v2_jobs._jobs.clear()
    v2_jobs._failed_sessions.clear()
    with factory() as db:
        a, b = Streamer(name="小陈"), Streamer(name="小柳")
        db.add_all([a, b])
        db.flush()
        row = ReplaySession(
            title="并行测试场", lineup=json.dumps([a.id, b.id]),
            started_at=datetime(2026, 9, 30, 8, 0, 0), ended_at=datetime(2026, 9, 30, 12, 0, 0),
        )
        db.add(row)
        db.flush()
        for s in (a, b):
            for i in range(20):      # 20 段、产品交替 → 聚成 20 个整段 → 章节分 3 批（8+8+4）
                start = i * 300_000
                db.add(Critique(
                    session_id=row.id, streamer_id=s.id, level=CritiqueLevel.segment,
                    severity=Severity.info, category="讲款", start_ms=start,
                    end_ms=start + 120_000, quote=f"第{i}段话术", comment="段评", score=5 + i % 4,
                    data=json.dumps({"emotion": "平淡", "scene_label": "讲品",
                                     "metrics": {"products": ["Demo Studio" if i % 2 else "Demo Air"]}},
                                    ensure_ascii=False),
                ))
                db.add(Transcript(
                    session_id=row.id, streamer_id=s.id, seq=i, start_ms=start + 1000,
                    end_ms=start + 9000, raw_text=f"这是第{i}段的讲解",
                    corrected_text=f"这是第{i}段的讲解",
                ))
            db.add(Critique(
                session_id=row.id, streamer_id=s.id, level=CritiqueLevel.summary,
                severity=Severity.info, category="整场总结", comment="整场总结", score=7,
                data=json.dumps({"summary": "总结正文", "structure": {}, "loop": {}},
                                ensure_ascii=False),
            ))
        db.add(OcrRecord(session_id=row.id, start_ms=60_000, text="路人甲：这个多少钱"))
        db.add(OcrRecord(session_id=row.id, start_ms=120_000, text="路人乙：和 Demo Air 有什么区别"))
        for ms, pay in ((0, 100), (600_000, 900), (1_200_000, 2600)):
            db.add(CompassPoint(session_id=row.id, start_ms=ms,
                                data=json.dumps({"直播间用户支付金额": pay}, ensure_ascii=False)))
        db.commit()
        ids = SimpleNamespace(session=row.id, a=a.id, b=b.id)
    try:
        yield factory, ids
    finally:
        v2_jobs._jobs.clear()
        v2_jobs._failed_sessions.clear()
        engine.dispose()


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(deep_analysis, "get_llm", fake)
    return fake


def _deep_status(factory, session_id: int) -> str:
    with factory() as db:
        return db.get(ReplaySession, session_id).deep_status


def _stored(factory, session_id: int, streamer_id: int) -> dict:
    with factory() as db:
        row = db.query(Critique).filter_by(
            session_id=session_id, streamer_id=streamer_id,
            level=CritiqueLevel.summary, category="整场总结",
        ).one()
        return json.loads(row.data)


async def _until(condition, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "等待超时"
        await asyncio.sleep(0.005)


# ---------- 并行与并发上限 ----------
def test_blocks_run_in_parallel_in_priority_order_within_limit(v2_env, fake_llm) -> None:
    _factory, ids = v2_env
    v2 = asyncio.run(deep_analysis.analyze_v2(ids.session, ids.a))

    assert set(v2) == {"generated_at", *ALL_BLOCKS}
    assert deep_analysis.failed_blocks(v2) == []
    assert len(v2["chapters"]["items"]) == 20
    assert [x["start_ms"] for x in v2["chapters"]["items"]] == [i * 300_000 for i in range(20)]
    assert {x["product"] for x in v2["products"]["items"]} == {"Demo Air", "Demo Studio"}
    assert v2["tone"]["issues"] and v2["structure"]["issues"]
    assert len(v2["danmaku"]["intent"]) == 2 and len(v2["compass"]["sales"]) == 2
    # 同时在跑的请求数用满上限、但不超过
    assert fake_llm.max_inflight == 4
    # 名额先给最慢的弹幕块和最长的那条链（章节各批），其余排后面；产品判词等章节全部出完才开始
    assert fake_llm.started == [
        "danmaku", "chapters:C0", "chapters:C8", "chapters:C16",
        "compass", "structure", "tone", "products",
    ]
    chapters_done = max(i for i, x in enumerate(fake_llm.finished) if x.startswith("chapters"))
    assert fake_llm.finished.index("products") > chapters_done


def test_concurrency_limit_is_shared_by_concurrent_generations(
    v2_env, fake_llm, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _factory, ids = v2_env
    monkeypatch.setattr(get_settings(), "v2_llm_concurrency", 2)

    async def scenario() -> list[dict]:
        return await asyncio.gather(
            deep_analysis.analyze_v2(ids.session, ids.a),
            deep_analysis.analyze_v2(ids.session, ids.b),
        )

    first, second = asyncio.run(scenario())
    assert deep_analysis.failed_blocks(first) == deep_analysis.failed_blocks(second) == []
    assert len(fake_llm.started) == 16
    assert fake_llm.max_inflight == 2     # 两位主播同时生成，合起来也不超过上限


# ---------- 失败降级与补跑 ----------
def test_one_block_failing_only_degrades_that_block(v2_env, fake_llm) -> None:
    factory, ids = v2_env
    fake_llm.fail = lambda label, _n: _rejected() if label == "compass" else None

    outcome = asyncio.run(deep_analysis.generate_v2(ids.session, ids.a))

    assert outcome.ok and outcome.failed == ["compass"] and outcome.reran == ALL_BLOCKS
    assert outcome.v2["compass"] == {
        "verdict": "生成失败，可稍后重试", "chips": [], "issues": [], "failed": True,
    }
    assert len(outcome.v2["chapters"]["items"]) == 20 and outcome.v2["tone"]["issues"]
    stored = _stored(factory, ids.session, ids.a)
    assert stored["summary"] == "总结正文"            # 只动 data.v2，不碰总结里别的内容
    assert stored["v2"] == outcome.v2


def test_failed_chapter_batch_keeps_the_rest_and_retry_only_fills_the_gap(
    v2_env, fake_llm,
) -> None:
    factory, ids = v2_env
    fake_llm.fail = lambda label, _n: _rejected() if label == "chapters:C8" else None
    first = asyncio.run(deep_analysis.generate_v2(ids.session, ids.a))

    assert first.ok and first.failed == ["chapters"]
    chapters = first.v2["chapters"]
    assert chapters["failed"] is True and len(chapters["items"]) == 12
    assert "另有 8 段没生成出来" in chapters["verdict"]
    assert first.v2["products"]["items"]              # 产品判词先按已出来的章节给

    # 补跑：只发缺的那一批章节 + 依赖章节的产品判词，其余块不再花钱
    fake_llm.fail = lambda label, _n: None
    fake_llm.started.clear()
    second = asyncio.run(deep_analysis.generate_v2(ids.session, ids.a, only_failed=True))

    assert fake_llm.started == ["chapters:C8", "products"]
    assert second.ok and second.failed == [] and second.reran == ["chapters", "products"]
    assert "failed" not in second.v2["chapters"] and len(second.v2["chapters"]["items"]) == 20
    for key in ("qc", "tone", "structure", "danmaku", "compass"):
        assert second.v2[key] == first.v2[key]
    assert _stored(factory, ids.session, ids.a)["v2"] == second.v2

    # 已经没有失败的块：再补跑不调模型、不动数据
    fake_llm.started.clear()
    third = asyncio.run(deep_analysis.generate_v2(ids.session, ids.a, only_failed=True))
    assert third.ok and third.reran == [] and fake_llm.started == []
    assert third.v2 == second.v2


def test_retry_that_fails_again_keeps_what_was_already_there(
    v2_env, fake_llm, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, ids = v2_env
    fake_llm.fail = lambda label, _n: _rejected() if label in ("chapters:C8", "compass") else None
    first = asyncio.run(deep_analysis.generate_v2(ids.session, ids.a))
    assert first.failed == ["compass", "chapters"] and len(first.v2["chapters"]["items"]) == 12

    # 补跑：罗盘这次成了；章节那一批还是不行 → 已有的 12 段不能丢，仍标记待补
    fake_llm.fail = lambda label, _n: _rejected() if label == "chapters:C8" else None
    second = asyncio.run(deep_analysis.generate_v2(ids.session, ids.a, only_failed=True))
    assert second.ok and second.failed == ["chapters"]
    assert len(second.v2["compass"]["sales"]) == 2
    assert len(second.v2["chapters"]["items"]) == 12 and second.v2["chapters"]["failed"] is True

    # 补跑时章节整块意外出错（不是模型拒绝）：同样保留已有的 12 段
    async def broken(*_args, **_kwargs):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(deep_analysis, "_chapters", broken)
    third = asyncio.run(deep_analysis.analyze_v2(ids.session, ids.a, base=second.v2))
    assert len(third["chapters"]["items"]) == 12 and third["chapters"]["failed"] is True
    assert _stored(factory, ids.session, ids.a)["v2"] == second.v2


def test_retry_recognizes_placeholder_blocks_saved_before_this_change(v2_env, fake_llm) -> None:
    factory, ids = v2_env
    full = asyncio.run(deep_analysis.generate_v2(ids.session, ids.a)).v2
    old = {**full, "danmaku": {"verdict": "生成失败，可稍后重试", "chips": [], "issues": []}}
    old.pop("products")                                # 更早的数据没有这一块
    report_data.merge_summary_data(ids.session, ids.a, lambda d: d.__setitem__("v2", old))
    assert deep_analysis.failed_blocks(old) == ["danmaku", "products"]

    fake_llm.started.clear()
    outcome = asyncio.run(deep_analysis.generate_v2(ids.session, ids.a, only_failed=True))

    assert sorted(fake_llm.started) == ["danmaku", "products"]
    assert outcome.ok and outcome.failed == [] and len(outcome.v2["danmaku"]["intent"]) == 2


def test_total_model_failure_saves_nothing_and_keeps_previous_result(v2_env, fake_llm) -> None:
    factory, ids = v2_env
    fake_llm.fail = _rejected
    outcome = asyncio.run(deep_analysis.generate_v2(ids.session, ids.a))
    assert not outcome.ok and "模型调用全部失败" in outcome.error
    assert "v2" not in _stored(factory, ids.session, ids.a)     # 没存一排「生成失败」占位

    fake_llm.fail = lambda label, _n: None
    good = asyncio.run(deep_analysis.generate_v2(ids.session, ids.a))
    assert good.ok
    fake_llm.fail = _rejected
    again = asyncio.run(deep_analysis.generate_v2(ids.session, ids.a))
    assert not again.ok and asyncio.run(deep_analysis.save_v2(ids.session, ids.a)) == {}
    assert _stored(factory, ids.session, ids.a)["v2"] == good.v2   # 已有的好结果没被盖掉


def test_rate_limited_call_waits_and_resends_then_gives_up(v2_env, fake_llm) -> None:
    _factory, ids = v2_env
    limited = llm.LLMCallError("LLM_RATE_LIMITED", "限流")
    fake_llm.fail = lambda label, n: limited if label == "tone" and n <= 2 else None
    v2 = asyncio.run(deep_analysis.analyze_v2(ids.session, ids.a))
    assert fake_llm.attempts["tone"] == 3 and deep_analysis.failed_blocks(v2) == []

    fake_llm.attempts.clear()
    fake_llm.fail = lambda label, n: limited if label == "tone" else None
    v2 = asyncio.run(deep_analysis.analyze_v2(ids.session, ids.a))
    assert fake_llm.attempts["tone"] == 3             # 等过两次还被限流就不再发
    assert deep_analysis.failed_blocks(v2) == ["tone"]


def test_provider_429_is_reported_as_rate_limited() -> None:
    response = httpx.Response(429, request=httpx.Request("POST", "https://provider.invalid/v1"))
    error = httpx.HTTPStatusError("too many requests", request=response.request, response=response)
    client = llm.OpenAICompatLLM(
        "doubao", "https://provider.invalid/v1", "secret", "model",
        timeout=180, retry_transient_once=True, allow_mock_fallback=False,
    )
    client._request = AsyncMock(side_effect=error)

    with pytest.raises(llm.LLMCallError) as captured:
        asyncio.run(client.complete("system", "user", json_mode=True))

    assert captured.value.code == "LLM_RATE_LIMITED"
    assert client._request.await_count == 1           # 通用层不自动重发，由深度复盘自己等一等


# ---------- 任务登记：防重复 + 状态 ----------
def _fake_generate(monkeypatch: pytest.MonkeyPatch, plan: dict | None = None):
    """顶替真正的生成：每位主播一个闸门，放行后按 plan 给结果（True 成功 / False 没存上 / 异常）。"""
    plan = plan or {}
    gates: dict = defaultdict(asyncio.Event)
    calls: list = []

    async def fake(session_id, streamer_id, *, only_failed=False):
        calls.append((session_id, streamer_id, only_failed))
        await gates[streamer_id].wait()
        result = plan.get(streamer_id, True)
        if isinstance(result, Exception):
            raise result
        if result:
            return deep_analysis.V2Outcome(ok=True, v2={"generated_at": "2026-09-30 19:00"})
        return deep_analysis.V2Outcome(error="没存上")

    monkeypatch.setattr(v2_jobs, "generate_v2", fake)
    return gates, calls


def test_second_request_joins_the_running_job_instead_of_generating_again(
    v2_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, ids = v2_env
    gates, calls = _fake_generate(monkeypatch)

    async def scenario() -> None:
        first, created_first = v2_jobs.start_job(ids.session, ids.a)
        second, created_second = v2_jobs.start_job(ids.session, ids.a, only_failed=True)
        assert created_first and not created_second and second is first
        assert _deep_status(factory, ids.session) == "running"
        status = v2_jobs.running_jobs(ids.session)
        assert set(status) == {ids.a} and status[ids.a]["status"] == "running"
        assert status[ids.a]["elapsed_sec"] >= 0 and status[ids.a]["only_failed"] is False

        waiters = [asyncio.create_task(v2_jobs.wait_job(job)) for job in (first, second)]
        await asyncio.sleep(0)
        gates[ids.a].set()
        outcomes = await asyncio.gather(*waiters)
        assert outcomes[0] is outcomes[1] and outcomes[0].ok
        assert calls == [(ids.session, ids.a, False)]            # 只生成了一次
        assert v2_jobs.running_jobs(ids.session) == {}
        assert _deep_status(factory, ids.session) == "done"

        third, created_third = v2_jobs.start_job(ids.session, ids.a)   # 跑完以后再点才新开一份
        assert created_third and third is not first
        assert (await v2_jobs.wait_job(third)).ok and len(calls) == 2

    asyncio.run(scenario())


def test_job_keeps_running_after_the_waiting_request_goes_away(
    v2_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, ids = v2_env
    gates, calls = _fake_generate(monkeypatch)

    async def scenario() -> None:
        job, _created = v2_jobs.start_job(ids.session, ids.a)
        waiter = asyncio.create_task(v2_jobs.wait_job(job))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not job.task.done() and ids.a in v2_jobs.running_jobs(ids.session)
        gates[ids.a].set()
        assert (await v2_jobs.wait_job(job)).ok
        assert _deep_status(factory, ids.session) == "done" and len(calls) == 1

    asyncio.run(scenario())


def test_session_status_stays_running_until_the_last_streamer_finishes(
    v2_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, ids = v2_env
    gates, _calls = _fake_generate(monkeypatch)

    async def scenario() -> None:
        job_a, _ = v2_jobs.start_job(ids.session, ids.a)
        job_b, _ = v2_jobs.start_job(ids.session, ids.b)
        gates[ids.a].set()
        assert (await v2_jobs.wait_job(job_a)).ok
        # 小陈先跑完，小柳还在跑：整场不能被盖成 done
        assert _deep_status(factory, ids.session) == "running"
        assert set(v2_jobs.running_jobs(ids.session)) == {ids.b}
        gates[ids.b].set()
        assert (await v2_jobs.wait_job(job_b)).ok
        assert _deep_status(factory, ids.session) == "done"

    asyncio.run(scenario())


def test_any_failure_in_the_round_marks_the_session_failed(
    v2_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, ids = v2_env
    gates, _calls = _fake_generate(monkeypatch, {ids.a: RuntimeError("boom"), ids.b: True})

    async def scenario() -> None:
        job_a, _ = v2_jobs.start_job(ids.session, ids.a)
        job_b, _ = v2_jobs.start_job(ids.session, ids.b)
        gates[ids.a].set()
        failed = await v2_jobs.wait_job(job_a)       # 任务自己兜住异常，等的人拿到「失败 + 原因」
        assert not failed.ok and failed.error and "boom" not in failed.error
        assert _deep_status(factory, ids.session) == "running"
        gates[ids.b].set()
        assert (await v2_jobs.wait_job(job_b)).ok
        assert _deep_status(factory, ids.session) == "failed"

        retry, created = v2_jobs.start_job(ids.session, ids.b)   # 下一轮全成功 → done
        assert created and (await v2_jobs.wait_job(retry)).ok
        assert _deep_status(factory, ids.session) == "done"

    asyncio.run(scenario())


def test_shutdown_cancels_jobs_and_marks_them_failed(
    v2_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, ids = v2_env
    _fake_generate(monkeypatch)

    async def scenario() -> None:
        running, _ = v2_jobs.start_job(ids.session, ids.a)
        await asyncio.sleep(0)                       # 这一份已经开跑
        not_started, _ = v2_jobs.start_job(ids.session, ids.b)   # 这一份还没轮到第一步
        await v2_jobs.cancel_all_jobs()
        assert running.task.cancelled() and not_started.task.cancelled()
        assert v2_jobs._jobs == {} and v2_jobs.running_jobs(ids.session) == {}
        assert _deep_status(factory, ids.session) == "failed"

    asyncio.run(scenario())


def test_startup_resets_status_left_running_by_the_previous_process(v2_env) -> None:
    factory, ids = v2_env
    with factory() as db:
        db.get(ReplaySession, ids.session).deep_status = "running"
        db.add_all([
            ReplaySession(title="排队中", deep_status="queued"),
            ReplaySession(title="已完成", deep_status="done"),
            ReplaySession(title="没点过", deep_status="not_requested"),
        ])
        db.commit()

    assert v2_jobs.reset_stale_deep_status() == 2
    assert v2_jobs.reset_stale_deep_status() == 0
    with factory() as db:
        statuses = {row.title: row.deep_status for row in db.query(ReplaySession).all()}
    assert statuses == {"并行测试场": "failed", "排队中": "failed", "已完成": "done",
                        "没点过": "not_requested"}


# ---------- 接口约定（前端靠这些判断生成完没完） ----------
def _client(factory) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(sessions_api.router)

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://replay.test")


def test_post_hangs_until_done_then_returns_ok_and_saves_under_summary_v2(
    v2_env, fake_llm,
) -> None:
    factory, ids = v2_env

    async def scenario() -> tuple[dict, list]:
        async with _client(factory) as client:
            done = await client.post(
                f"/api/sessions/{ids.session}/reanalyze_v2", params={"streamer_id": ids.a})
            assert done.status_code == 200
            streamers = await client.get(f"/api/sessions/{ids.session}/streamers")
            return done.json(), streamers.json()

    body, streamers = asyncio.run(scenario())
    assert body["ok"] is True and len(body["results"]) == 1
    result = body["results"][0]
    assert result["streamer_id"] == ids.a and result["ok"] is True
    assert sorted(result["blocks"]) == sorted(ALL_BLOCKS)
    assert result["failed_blocks"] == [] and result["joined"] is False
    mine = next(s for s in streamers if s["streamer_id"] == ids.a)
    other = next(s for s in streamers if s["streamer_id"] == ids.b)
    v2 = json.loads(mine["summary"]["data"])["v2"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", v2["generated_at"])
    assert len(v2["chapters"]["items"]) == 20
    assert "v2" not in json.loads(other["summary"]["data"])
    assert mine["v2_job"] is None and other["v2_job"] is None
    assert _deep_status(factory, ids.session) == "done"
    assert len(fake_llm.started) == 8
    # 并行的每一次调用，用量都还记在这一场的 v2 阶段下（查进度、算花费靠它）
    assert {(c.session_id, c.stage) for c in fake_llm.contexts} == {(ids.session, "v2")}


def test_two_devices_clicking_generate_only_generate_once(v2_env, fake_llm) -> None:
    factory, ids = v2_env

    async def scenario() -> tuple[list, list, list]:
        fake_llm.gate = asyncio.Event()
        async with _client(factory) as client:
            url = f"/api/sessions/{ids.session}/reanalyze_v2"
            posts = [asyncio.create_task(client.post(url, params={"streamer_id": ids.a}))
                     for _ in range(2)]
            await _until(lambda: len(fake_llm.started) == 4)     # 第一份已经跑起来、名额占满
            during = (await client.get(f"/api/sessions/{ids.session}/streamers")).json()
            assert _deep_status(factory, ids.session) == "running"
            fake_llm.gate.set()
            bodies = [(await post).json() for post in posts]
            after = (await client.get(f"/api/sessions/{ids.session}/streamers")).json()
            return bodies, during, after

    bodies, during, after = asyncio.run(scenario())
    assert all(body["ok"] is True for body in bodies)
    assert sorted(body["results"][0]["joined"] for body in bodies) == [False, True]
    assert len(fake_llm.started) == 8                  # 模型只跑了一份的量
    by_id = {s["streamer_id"]: s for s in during}
    assert by_id[ids.a]["v2_job"]["status"] == "running"      # 别的设备能看到「生成中」
    assert by_id[ids.b]["v2_job"] is None
    assert all(s["v2_job"] is None for s in after)


def test_all_streamers_request_registers_every_job_up_front(v2_env, fake_llm) -> None:
    factory, ids = v2_env

    async def scenario() -> list:
        fake_llm.gate = asyncio.Event()
        async with _client(factory) as client:
            url = f"/api/sessions/{ids.session}/reanalyze_v2"
            everyone = asyncio.create_task(client.post(url))
            await _until(lambda: len(fake_llm.started) == 4)
            assert set(v2_jobs.running_jobs(ids.session)) == {ids.a, ids.b}
            single = asyncio.create_task(client.post(url, params={"streamer_id": ids.b}))
            await asyncio.sleep(0.05)
            fake_llm.gate.set()
            return [(await everyone).json(), (await single).json()]

    everyone, single = asyncio.run(scenario())
    assert everyone["ok"] is True and single["ok"] is True
    assert [r["streamer_id"] for r in everyone["results"]] == [ids.a, ids.b]
    assert single["results"][0]["joined"] is True
    assert len(fake_llm.started) == 16                 # 两位主播各一份，没有第三份
    assert fake_llm.max_inflight == 4
    assert _deep_status(factory, ids.session) == "done"


def test_only_failed_parameter_reruns_just_the_failed_block(v2_env, fake_llm) -> None:
    factory, ids = v2_env

    async def scenario() -> tuple[dict, dict]:
        async with _client(factory) as client:
            url = f"/api/sessions/{ids.session}/reanalyze_v2"
            fake_llm.fail = lambda label, _n: _rejected() if label == "structure" else None
            first = (await client.post(url, params={"streamer_id": ids.a})).json()
            fake_llm.fail = lambda label, _n: None
            fake_llm.started.clear()
            second = (await client.post(
                url, params={"streamer_id": ids.a, "only_failed": 1})).json()
            return first, second

    first, second = asyncio.run(scenario())
    assert first["ok"] is True and first["results"][0]["failed_blocks"] == ["structure"]
    assert second["ok"] is True and second["results"][0]["failed_blocks"] == []
    assert second["results"][0]["rerun"] == ["structure"] and fake_llm.started == ["structure"]
    assert _stored(factory, ids.session, ids.a)["v2"]["structure"]["issues"]


def test_failures_come_back_as_ok_false_with_a_reason(v2_env, fake_llm) -> None:
    factory, ids = v2_env
    fake_llm.fail = _rejected

    async def scenario() -> tuple[dict, dict, int]:
        async with _client(factory) as client:
            url = f"/api/sessions/{ids.session}/reanalyze_v2"
            missing = (await client.post(url, params={"streamer_id": 999})).json()
            assert _deep_status(factory, ids.session) == "not_requested" and not fake_llm.started
            failed = (await client.post(url, params={"streamer_id": ids.a})).json()
            gone = await client.post("/api/sessions/987654/reanalyze_v2")
            return missing, failed, gone.status_code

    missing, failed, gone = asyncio.run(scenario())
    assert missing == {"ok": False, "error": "该场次没有整场总结（先完成收尾或指定正确主播）"}
    assert failed["ok"] is False and "模型调用全部失败" in failed["error"]
    assert failed["results"][0]["ok"] is False
    assert "v2" not in _stored(factory, ids.session, ids.a)
    assert _deep_status(factory, ids.session) == "failed"
    assert gone == 404
