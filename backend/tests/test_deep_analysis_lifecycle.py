from __future__ import annotations

import asyncio
import gc
import warnings
from types import SimpleNamespace
from unittest.mock import patch

from app.core.review import deep_analysis


class _ScalarResult:
    def scalars(self):
        return self

    def first(self):
        return None


class _Database:
    def execute(self, _query):
        return _ScalarResult()

    def close(self) -> None:
        return None


def test_cancelled_analysis_stops_running_blocks_and_never_starts_the_later_one() -> None:
    """各块并行后（2026-09-30）：取消时正在跑的块全部停下，排在章节之后的产品判词根本不会开始，
    也不留下没人等的协程（不会继续花钱）。"""
    started: list[str] = []
    cancelled: list[str] = []
    later_calls = 0

    def blocked(name: str):
        async def run(*_args, **_kwargs):
            started.append(name)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise
        return run

    async def later_block(*_args, **_kwargs):
        nonlocal later_calls
        later_calls += 1
        raise AssertionError("cancelled analysis must not start later paid blocks")

    async def scenario() -> None:
        task = asyncio.create_task(deep_analysis.analyze_v2(1, 2))
        for _ in range(5):                 # 让五块都跑起来
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("analysis should have been cancelled")

    sess = SimpleNamespace(started_at=None)
    with patch.object(
        deep_analysis, "_load", return_value=(sess, [], [], [], [], []),
    ), patch.object(deep_analysis, "SessionLocal", return_value=_Database()), patch.object(
        deep_analysis, "_tone", blocked("tone"),
    ), patch.object(deep_analysis, "_structure", blocked("structure")), patch.object(
        deep_analysis, "_danmaku", blocked("danmaku"),
    ), patch.object(deep_analysis, "_compass", blocked("compass")), patch.object(
        deep_analysis, "_chapters", blocked("chapters"),
    ), patch.object(deep_analysis, "_products", later_block), warnings.catch_warnings(
        record=True,
    ) as caught:
        warnings.simplefilter("always")
        asyncio.run(scenario())
        gc.collect()

    assert sorted(started) == ["chapters", "compass", "danmaku", "structure", "tone"]
    assert sorted(cancelled) == sorted(started)
    assert later_calls == 0
    assert not any("was never awaited" in str(item.message) for item in caught)
