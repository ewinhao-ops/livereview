"""「闲聊后拉回产品」一段闲聊只算一处（2026-10-04）。模型一律用假的。

以前每句闲聊都单独算一次时机：聊两分钟没拉回会被记成三四次「没做到」（一场 53 次，其实 22 段）。
现在相邻两处隔不到 2 分钟的连成一段，做没做到看这段最后一处。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.core.review import focus
from app.core.review.focus import ActionInfo, Context, Occasion, Sent

PULL = ActionInfo(1, "闲聊后60秒内说回产品", "闲聊或玩梗后（2分钟内连着聊的算一段，只算一次）",
                  "这段闲聊结束后60秒内说回产品", "llm", 10, "active", "拉回主线")
QUOTE = ActionInfo(2, "报价后紧跟链接号和下单指令", "主播报完价后", "紧接着说出几号链接和下单指令",
                   "llm", 10, "active", "报价指令")


def occ(sec: int, ok: bool) -> Occasion:
    return Occasion(sec * 1000, ok, f"第{sec}秒")


def test_merge_episodes_chains_close_occasions_and_uses_the_last():
    merged = focus.merge_episodes([occ(0, False), occ(40, False), occ(150, True),    # 一段：0→40→150（间隔都 ≤120 秒）
                                   occ(400, False), occ(700, True), occ(790, False)])
    assert [(o.at_ms // 1000, o.ok, o.quote) for o in merged] == [
        (0, True, "第150秒"),       # 这段最后拉回了
        (400, False, "第400秒"),    # 单独一段，没拉回
        (700, False, "第790秒"),    # 中途说了产品，最后又聊开了没拉回
    ]
    assert focus.merge_episodes([]) == []


def test_only_pullback_actions_use_episodes():
    assert focus.is_episode_action(PULL)
    assert not focus.is_episode_action(QUOTE)


def test_run_checks_counts_one_occasion_per_chat_episode(monkeypatch):
    sentences = [Sent(sec * 1000, sec * 1000 + 2000, f"第{sec}秒的话") for sec in (0, 40, 90, 150, 400, 470)]
    day = datetime(2026, 10, 4, 9, 0)
    ctx = Context(1, 1, "测试主播", None, day, [(0, 600_000)], day, sentences, {},
                  [PULL, QUOTE], [PULL, QUOTE], [], [], None)

    async def fake_ask(system, user, parse):
        # 拉回：0、40、90 秒连着三句闲聊（90 秒那处拉回了），400 秒一处没拉回；报价：40、90 秒两处都没做到
        return parse({"r": [
            {"id": 1, "m": [["00:00:00", 0, ""], ["00:00:40", 0, ""], ["00:01:30", 1, "00:02:30"], ["00:06:40", 0, ""]]},
            {"id": 2, "m": [["00:00:40", 0, ""], ["00:01:30", 0, ""]]},
        ]}), "fake"

    monkeypatch.setattr(focus, "_ask", fake_ask)
    results, _calls, _failed = asyncio.run(focus._run_checks(ctx))
    by_id = {r.action_id: r for r in results}
    assert (by_id[PULL.id].done, by_id[PULL.id].total) == (1, 2)       # 两段：第一段拉回了，第二段没有
    assert (by_id[QUOTE.id].done, by_id[QUOTE.id].total) == (0, 2)     # 别的动作照旧逐处算
