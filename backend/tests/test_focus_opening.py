"""开播 / 接班开场动作：每班开头 60 秒只判一次（2026-10-04）。模型一律用假的。

以前开场动作和别的动作一样整场找时机：每句「欢迎新进来的」都被算成一次开场（一场算出 11 次），
中途接班的主播又一次都找不到（永远「本场不计」）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.core.review import focus
from app.core.review.focus import ActionInfo, Context, Sent

CRITERION = "每次开播或接班后30秒内，直播间定位、本场重点款至少讲到其中1样"


def act(id_: int, text: str, trigger: str, category: str = "开场介绍") -> ActionInfo:
    return ActionInfo(id_, text, trigger, CRITERION, "llm", 10, "active", category)


OPEN = act(1, "开播或接班30秒内说清定位和重点", "主播开播或接班后的前30秒")
WELCOME = act(2, "新观众进来先欢迎再介绍直播间", "新观众进来时")
QUOTE = act(3, "报价后紧跟链接号和下单指令", "主播报完价后", category="报价指令")


def sents(*items: tuple[int, str]) -> list[Sent]:
    return [Sent(sec * 1000, sec * 1000 + 2000, text) for sec, text in items]


def make_ctx(sentences: list[Sent], turns: list[tuple[int, int]], actions: list[ActionInfo]) -> Context:
    day = datetime(2026, 10, 4, 9, 0)
    return Context(1, 1, "测试主播", None, day, turns, day, sentences, {}, actions, actions, [], [], None)


TWO_TURNS = sents(
    (5, "大家好，欢迎来到官方直播间，今天重点讲平板耳机"),
    (30, "有想听的曲风打在公屏上"),
    (90, "欢迎新进来的朋友"),                 # 开头 60 秒以外，不算开场
    (1210, "我接一下班哈"),
    (1250, "刚才说到哪了"),
    (1500, "欢迎新进来的小哥"),
)
TURNS = [(0, 600_000), (1_200_000, 1_800_000), (2_000_000, 2_100_000)]   # 第三班没开口


def run(coro):
    return asyncio.run(coro)


def test_is_opening_only_for_start_of_turn_actions():
    assert focus.is_opening(OPEN)
    assert not focus.is_opening(WELCOME)          # 「新观众进来时」照常整场找
    assert not focus.is_opening(QUOTE)


def test_opening_windows_one_per_turn():
    windows = focus.opening_windows(TWO_TURNS, TURNS)
    assert [[s.start_ms // 1000 for s in w] for w in windows] == [[5, 30], [1210, 1250]]
    # 没有班次记录：整场第一句当开头
    assert [[s.start_ms // 1000 for s in w] for w in focus.opening_windows(TWO_TURNS, [])] == [[5, 30]]
    assert focus.opening_windows([], TURNS) == []


def test_opening_checked_once_per_turn(monkeypatch):
    seen: list[str] = []

    async def fake_ask(system, user, parse):
        seen.append(user)
        if "【说明】" in user:
            # 第一班开头做到了；第二班模型什么都没列（说明没做到）
            data = {"r": [{"id": 1, "m": [["00:00:05", 1, "00:00:05"]]}]} if "00:00:05" in user else {"r": []}
        else:
            data = {"r": [{"id": 1, "m": []}]}
        return parse(data), "fake"

    monkeypatch.setattr(focus, "_ask", fake_ask)
    results, calls, failed = run(focus._run_checks(make_ctx(TWO_TURNS, TURNS, [OPEN, QUOTE])))
    by_id = {r.action_id: r for r in results}
    opening = by_id[OPEN.id]
    assert (opening.done, opening.total, opening.verdict) == (1, 2, "fail")
    assert [e["ok"] for e in opening.evidence] == [False, True] or [e["ok"] for e in opening.evidence] == [True, False]
    assert "我接一下班哈" in " ".join(e["quote"] for e in opening.evidence)
    opening_calls = [u for u in seen if "【说明】" in u]
    assert len(opening_calls) == 2                                   # 两班开口，各问一次
    assert all("欢迎新进来的小哥" not in u for u in opening_calls)   # 只给开头 60 秒
    whole = [u for u in seen if "【说明】" not in u]
    assert whole and all(OPEN.text not in u for u in whole)          # 开场动作不再整场找
    assert by_id[QUOTE.id].verdict == "na"
    assert calls == len(seen) and failed == 0


def test_opening_pass_when_every_turn_done(monkeypatch):
    async def fake_ask(system, user, parse):
        first = "00:00:05" if "00:00:05" in user else "00:20:10"
        return parse({"r": [{"id": 1, "m": [[first, 1, first]]}]}), "fake"

    monkeypatch.setattr(focus, "_ask", fake_ask)
    results, _calls, _failed = run(focus._run_checks(make_ctx(TWO_TURNS, TURNS, [OPEN])))
    assert (results[0].done, results[0].total, results[0].verdict) == (2, 2, "pass")


def test_opening_call_failure_counts_as_not_checked(monkeypatch):
    async def fake_ask(system, user, parse):
        raise focus._CallFailed("模型调用失败（TEST）")

    monkeypatch.setattr(focus, "_ask", fake_ask)
    results, calls, failed = run(focus._run_checks(make_ctx(TWO_TURNS, TURNS, [OPEN])))
    assert results[0].verdict == "na" and "核对失败" in results[0].reason
    assert calls == 2 and failed == 2


def test_welcome_action_still_checked_over_whole_session(monkeypatch):
    seen: list[str] = []

    async def fake_ask(system, user, parse):
        seen.append(user)
        return parse({"r": [{"id": 1, "m": [["00:01:30", 1, "00:01:30"]]}]}), "fake"

    monkeypatch.setattr(focus, "_ask", fake_ask)
    results, _calls, _failed = run(focus._run_checks(make_ctx(TWO_TURNS, TURNS, [WELCOME])))
    assert all("【说明】" not in u for u in seen)
    assert results[0].total == 1 and results[0].done == 1
