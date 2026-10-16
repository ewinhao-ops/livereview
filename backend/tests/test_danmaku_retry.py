"""弹幕抓不到不再整场放弃（2026-10-03）：同一个浏览器、同一个页面隔一段时间重开直播间，直到挂上弹幕列表；
页面崩了在同一个浏览器里重开；整场没采到时场次记 missing，整场总结的输入里加一句、报告里注明。"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import types
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core import danmaku as danmaku_module
from app.core import pipeline as pipeline_module
from app.core import summary_service
from app.core.danmaku import DanmakuScraper
from app.core.review import summary as summary_module
from app.db import Base
from app.models import Critique, ReplaySession, SessionStatus, Transcript


class World:
    """假直播间：第 hook_on 次打开页面后弹幕列表才出现；可以让某次打开超时、某次检查时页面崩掉。"""

    def __init__(self, hook_on: int | None = 3) -> None:
        self.hook_on = hook_on
        self.gotos = 0
        self.goto_errors: set[int] = set()
        self.crash_on_check: set[int] = set()
        self.checks = 0
        self.handler = None


class FakePage:
    def __init__(self, world: World) -> None:
        self.world = world
        self.closed = False
        self.hooked_here = False

    def is_closed(self) -> bool:
        return self.closed

    def expose_function(self, name, fn) -> None:  # noqa: ANN001
        assert name == "_pushDanmaku"
        self.world.handler = fn

    def goto(self, url, timeout, wait_until) -> None:  # noqa: ANN001
        self.world.gotos += 1
        if self.world.gotos in self.world.goto_errors:
            raise RuntimeError("Timeout 30000ms exceeded")
        self.hooked_here = self.world.hook_on is not None and self.world.gotos >= self.world.hook_on

    def evaluate(self, script: str):
        if script.strip().startswith("() => !!window.__dmHooked"):
            self.world.checks += 1
            if self.world.checks in self.world.crash_on_check:
                raise RuntimeError("Target page, context or browser has been closed")
            return self.hooked_here
        return True

    def wait_for_timeout(self, ms: int) -> None:
        time.sleep(0.002)

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, world: World) -> None:
        self.world = world
        self.pages: list[FakePage] = []

    def new_page(self) -> FakePage:
        page = FakePage(self.world)
        self.pages.append(page)
        return page

    def route(self, *_args) -> None:
        pass


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(danmaku_module, "RETRY_SEC", 0.05)
    monkeypatch.setattr(danmaku_module, "HOOK_POLL_SEC", 0)
    monkeypatch.setattr(danmaku_module, "CRASH_RETRY_SEC", 0.02)


def watch(scraper: DanmakuScraper, ctx: FakeContext, until, timeout: float = 3.0) -> None:
    thread = threading.Thread(target=scraper._watch, args=(ctx,), daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not until():
        time.sleep(0.005)
    scraper.stop()
    thread.join(2)
    assert not thread.is_alive()


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(2)
    loop.close()


def test_retries_on_the_same_page_until_the_list_shows_up(fast, loop):
    world = World(hook_on=3)
    ctx = FakeContext(world)
    hooked_calls = []

    async def on_hooked():
        hooked_calls.append(True)

    async def on_danmaku(nick, content):
        return None

    scraper = DanmakuScraper("https://live.douyin.com/1", on_danmaku, loop, on_hooked=on_hooked)
    watch(scraper, ctx, lambda: scraper.hooked)
    assert scraper.hooked and scraper.status == "ok"
    assert world.gotos == 3 and scraper.attempts == 3
    assert len(ctx.pages) == 1                             # 一直复用同一个页面，不反复开
    time.sleep(0.05)
    assert hooked_calls == [True]


def test_stays_hooked_without_reloading(fast, loop):
    world = World(hook_on=1)
    ctx = FakeContext(world)
    scraper = DanmakuScraper("u", lambda *a: None, loop)
    watch(scraper, ctx, lambda: False, timeout=0.3)        # 挂上后跑一会儿
    assert scraper.hooked and world.gotos == 1             # 挂上了就不再重开


def test_never_found_means_missing_but_keeps_trying(fast, loop):
    world = World(hook_on=None)
    world.goto_errors = {2}                                # 中间有一次打开超时，也接着试
    ctx = FakeContext(world)
    scraper = DanmakuScraper("u", lambda *a: None, loop)
    watch(scraper, ctx, lambda: world.gotos >= 5)
    assert world.gotos >= 5 and scraper.status == "missing"
    assert len(ctx.pages) == 1


def test_a_crashed_page_is_replaced_inside_the_same_browser(fast, loop):
    world = World(hook_on=1)
    world.crash_on_check = {1}
    ctx = FakeContext(world)
    scraper = DanmakuScraper("u", lambda *a: None, loop)
    watch(scraper, ctx, lambda: scraper.hooked)
    assert scraper.hooked and len(ctx.pages) == 2 and ctx.pages[0].closed


def test_captured_danmaku_counts_as_ok(loop):
    received = []

    async def on_danmaku(nick, content):
        received.append((nick, content))

    scraper = DanmakuScraper("u", on_danmaku, loop)
    assert scraper.status == "missing"
    scraper._handle("小王：这款能直推吗")
    scraper._handle("小王：这款能直推吗")                   # 重复的只算一次
    scraper._handle("欢迎来到直播间")                       # 系统提示不算
    assert scraper.captured == 1 and scraper.status == "ok"
    time.sleep(0.05)
    assert received == [("小王", "这款能直推吗")]


def test_run_in_thread_launches_one_browser_and_closes_it(fast, monkeypatch, loop):
    world = World(hook_on=2)
    ctx = FakeContext(world)
    launched, closed = [], []

    class Browser:
        def new_context(self, **_kwargs):
            return ctx

        def close(self):
            closed.append(True)

    class Chromium:
        def launch(self, **kwargs):
            launched.append(kwargs)
            return Browser()

    class PW:
        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: PW()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    scraper = DanmakuScraper("u", lambda *a: None, loop)
    scraper.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not scraper.hooked:
        time.sleep(0.005)
    scraper.stop()
    scraper.join(2)
    assert scraper.hooked and len(launched) == 1 and closed == [True]


# ======================= 场次标记与整场总结 =======================

@pytest.fixture
def db(tmp_path: Path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'dm.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(summary_service, "SessionLocal", factory)
    monkeypatch.setattr(pipeline_module, "SessionLocal", factory)
    summary_service._inflight.clear()
    with factory() as s:
        row = ReplaySession(title="弹幕测试", status=SessionStatus.done, started_at=datetime(2026, 9, 30, 21))
        s.add(row)
        s.flush()
        s.add(Transcript(session_id=row.id, streamer_id=None, seq=0, start_ms=0, end_ms=1000,
                         raw_text="大家好", corrected_text="大家好"))
        s.commit()
        session_id = row.id
    yield factory, session_id
    summary_service._inflight.clear()
    engine.dispose()


def _status(factory, session_id):
    with factory() as s:
        return s.get(ReplaySession, session_id).danmaku_status


def test_pipeline_marks_pending_then_ok(db):
    factory, session_id = db
    p = pipeline_module.ReplayPipeline(session_id)
    p._set_danmaku_status("pending")
    assert _status(factory, session_id) == "pending"
    asyncio.run(p._on_danmaku_hooked())
    assert _status(factory, session_id) == "ok"
    p._set_danmaku_status("missing")
    assert _status(factory, session_id) == "missing"


@pytest.mark.parametrize("status, missing", [("missing", True), ("pending", True), ("ok", False),
                                             ("", False)])
def test_summary_input_and_report_note_when_danmaku_was_not_captured(db, monkeypatch, status, missing):
    factory, session_id = db
    with factory() as s:
        s.get(ReplaySession, session_id).danmaku_status = status
        s.commit()
    seen = {}

    async def fake_review(*_args, **kwargs):
        seen.update(kwargs)
        return {"summary": "整体还行", "overall_score": 7, "llm": "test"}

    monkeypatch.setattr(summary_service, "review_summary", fake_review)
    data = asyncio.run(summary_service.generate_summary(session_id, None))
    if missing:
        assert seen["notes"] == ["本场弹幕没有采集到，不要评价公屏互动"]
        assert data["summary"].startswith("（本场弹幕未采集到")
        assert data["data_notes"] == ["本场弹幕未采集到"] and data["danmaku_captured"] is False
    else:
        assert seen["notes"] == []
        assert data["summary"] == "整体还行" and "data_notes" not in data
    with factory() as s:
        row = s.execute(select(Critique)).scalars().one()
    assert json.loads(row.data)["summary"] == data["summary"] and row.comment == data["summary"]


def test_note_is_dropped_when_regenerated_after_danmaku_became_ok(db, monkeypatch):
    factory, session_id = db

    async def fake_review(*_args, **_kwargs):
        return {"summary": "整体还行", "overall_score": 7, "llm": "test"}

    monkeypatch.setattr(summary_service, "review_summary", fake_review)
    with factory() as s:
        s.get(ReplaySession, session_id).danmaku_status = "pending"
        s.commit()
    asyncio.run(summary_service.generate_summary(session_id, None))
    with factory() as s:
        s.get(ReplaySession, session_id).danmaku_status = "ok"
        s.commit()
    asyncio.run(summary_service.generate_summary(session_id, None))
    with factory() as s:
        data = json.loads(s.execute(select(Critique)).scalars().one().data)
    assert data["summary"] == "整体还行" and "data_notes" not in data and "danmaku_captured" not in data


def test_review_summary_puts_the_note_right_after_the_rhythm_metrics(monkeypatch):
    prompts = []

    class FakeLLM:
        async def complete(self, system, user, *, json_mode=False):
            prompts.append((system, user))
            from app.core.llm import LLMResult
            return LLMResult(json.dumps({"summary": "s", "overall_score": 6}), provider="fake")

    async def nothing(*_args, **_kwargs):
        return []

    async def no_vector(*_args, **_kwargs):
        return None

    async def no_experience(*_args, **_kwargs):
        return [], "（无）", "（无）"

    monkeypatch.setattr(summary_module, "embed_one", no_vector)
    monkeypatch.setattr(summary_module, "retrieve_detailed", nothing)
    monkeypatch.setattr(summary_module, "_retrieve_personalized_experience", no_experience)
    monkeypatch.setattr(summary_module, "get_llm", lambda purpose="deep": FakeLLM())
    asyncio.run(summary_module.review_summary("逐字稿", [], [], rhythm={"total_danmaku": 0},
                                              notes=["本场弹幕没有采集到，不要评价公屏互动"]))
    asyncio.run(summary_module.review_summary("逐字稿", [], [], rhythm={"total_danmaku": 0}))
    with_note, without = prompts[0][1], prompts[1][1]
    assert "弹幕 0 条" in with_note
    metrics = with_note.split("【全场实测节奏指标】\n", 1)[1].split("\n\n", 1)[0]
    assert metrics.endswith("（本场弹幕没有采集到，不要评价公屏互动）")
    assert "没有采集到" not in without
    assert prompts[0][0] == prompts[1][0]                   # 系统提示词一字不动
    assert with_note.replace("\n（本场弹幕没有采集到，不要评价公屏互动）", "") == without
