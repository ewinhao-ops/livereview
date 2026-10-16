"""改进动作闭环（第一期，2026-10-03）：本场能检查的问题都变成具体动作，每场下播后自动核对、记账。

process_focus(session_id, streamer_id, force=False)：
  1. 幂等：这场这位主播已有处理记录（FocusRun）且没要求重跑 → 直接返回；同一（场次, 主播）同时只跑
     一份（下播触发和补漏循环撞上时，后来的等前一份的结果，不重复花钱）。
  2. 讲话太少（有效讲话不到 20 分钟或不到 2000 字）→ 记 skipped，不核对、不定新动作。
  3. 核对：在练、且创建早于这位主播本场班次开始的动作，逐条核对本场逐字稿——
     silence 类纯算法：相邻两句之间的空白在 [N 秒, 180 秒] 算空场，更长的多半是放歌试听或离场，
       单独计数、不算违规；没有空场 = pass；
     llm 类：逐字稿按句切成约 6000 字一块（每句带本场时间），每块每次最多判 4 条动作（快速档单次输出
       有上限，动作多了分几次问）；汇总后 时机 0 次 = na，做到 ≥ 2/3 = pass，否则 fail；
       有块重试后仍失败 → 这条动作本场记 na（核对失败），处理记录记 partial。
  4. 记账：主播还没确认（acked_at 为空或晚于本场开播）→ 这次只算基线，照存不动 streak；
     确认过的按场次先后重算：pass +1、fail 清零、na 不变，满 3 场 → 过关。
  5. 新动作（第二版规则，2026-10-03 晚）：快速档先把本场总结里的问题归到类别（focus_store.CATEGORIES）、
     按重要程度排好——一个问题只出一条，同一类的多个要点合成一条（「至少讲到 N 样」）；和在练动作同一类的
     一律指回原来那条（代码按类别兜底，跨场次也一样）；和已过关的同类又犯了的可以重新加。在练加新拟
     超过 8 条时再让模型合并一轮，合不了才保留并记一笔。然后分批补全时机、做到的标准、例句。
     不出动作的：价格/库存/优惠/券/定价/站外交易，违禁词和绝对化用语（合规模块管），亚秒级停顿、语气
     这类逐字稿判断不了的，型号叫全称还是昵称。值得一提但没写成动作的存进 FocusRun.candidates 供人工参考。
     在练动作按本场的重要程度重排 sort_order。
  6. 结果并进该主播「整场总结」data["focus"]（乐观锁，不覆盖别人同时写的内容；focus 不在
     summary_service._SUMMARY_KEYS 里，重新生成总结时保留）。next = 全部在练动作。
全程 llm_context(session_id, "focus") 记用量。

补漏（focus_backfill_loop）：每 10 分钟找 FOCUS_SINCE 以后开播、有整场总结、班次已结束、还没处理过的
（场次, 主播），按每位主播的时间先后处理；正在直播的主播整个跳过。服务重启丢掉的任务靠它补。
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from ...config import get_settings
from ...db import SessionLocal
from ...models import (Critique, CritiqueLevel, FocusAction, FocusCheck, FocusRun, ReplaySession,
                       SessionStatus, SessionType, Streamer, StreamerTurn, Transcript)
from ..llm import get_llm
from ..safe_error import safe_error_text, safe_exception_code
from ..usage import llm_context
from . import focus_store as store
from .asr_fix import fix_text
from .report_data import merge_summary_data

CHUNK_CHARS = 6000          # 逐字稿每块约多少字（只算说的话，不算时间戳）
MAX_CHUNKS = 30             # 超长场次把块放大，不让一场的调用次数失控
TAIL_SEC = 90               # 每块后面附的「后续参考」最多看多少秒
TAIL_CHARS = 600            # ……最多多少字
MIN_SPAN_MS = 20 * 60_000   # 有效讲话不到 20 分钟不核对
MIN_CHARS = 2000            # 不到 2000 字不核对
ACTIONS_PER_CALL = 2        # 一次核对调用最多判几条动作（2026-10-04 由 4 改 2：一次判 4 条时同一场两次结果差很多，还会被 1024 token 上限截断）
OCCASIONS_PER_CHUNK = 12    # 每块每条动作最多收几个时机
MERGE_WITHIN_MS = 15_000    # 同一条动作 15 秒内的几处时机算一处（连着几句说的是同一件事）
OPENING_WINDOW_MS = 60_000  # 「开播或接班」类开场动作只看每班开口后 60 秒（标准是 30 秒，留出识别误差和慢热）
EPISODE_GAP_MS = 120_000    # 「闲聊后拉回」：相邻两处闲聊隔不到 2 分钟算同一段，一段只算一处
DETAIL_BATCH = 4            # 补全时机/标准/例句时一次最多几条（带要点的标准更长）
LLM_CONCURRENCY = 3         # 一次处理里同时在跑的快速档请求数
RETRY_DELAY_SEC = 2.0
CANDIDATE_LIMIT = 8
MANY_NEW = 8                # 一场新加超过这么多条，记一笔（看是不是拆得太碎）
MAX_POINTS = 6              # 一条动作最多几个要点
SILENCE_PROFILE = (10, 20, 30, 60, 120)
SILENCE_STEPS = (30, 45, 60, 90, 120)   # 冷场秒数从这里挑：每场平均不超过 3 次空场的最小那档
# 已有不冷场动作时，文字里带这些词的话术动作算同一个问题（冷场类只用一条 silence 管）
SILENCE_WORDS = ("冷场", "空白", "静音", "停顿", "不说话", "没人说话", "空档")

BACKFILL_INTERVAL_SEC = 600
BACKFILL_FIRST_DELAY_SEC = 120
BACKFILL_MAX_PER_ROUND = 10
BACKFILL_MAX_ATTEMPTS = 3   # 同一（场次, 主播）在一个进程里最多试 3 次都没留下记录就不再试

_ACTIVE_SESSION = (SessionStatus.live, SessionStatus.processing, SessionStatus.parsing)


# ---------- 数据 ----------

@dataclass(frozen=True)
class Sent:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class Gap:
    start_ms: int      # 上一句说完
    end_ms: int        # 下一句开口
    before: str
    after: str

    @property
    def sec(self) -> float:
        return (self.end_ms - self.start_ms) / 1000


@dataclass
class Chunk:
    index: int
    main: list[Sent]
    tail: list[Sent]


@dataclass
class Occasion:
    at_ms: int
    ok: bool
    quote: str


@dataclass
class ActionInfo:
    id: int
    text: str
    trigger: str
    criterion: str
    kind: str
    threshold_sec: int
    status: str = "active"
    category: str = ""


@dataclass
class CheckResult:
    action_id: int
    verdict: str
    done: int | None
    total: int | None
    metric: dict
    evidence: list[dict]
    reason: str = ""
    llm_provider: str = ""
    baseline: bool = True


@dataclass
class Context:
    session_id: int
    streamer_id: int
    streamer_name: str
    streamer_ext_id: str | None
    started_at: datetime | None
    turns: list[tuple[int, int]]
    turn_started_at: datetime
    sentences: list[Sent]
    summary: dict
    to_check: list[ActionInfo]
    active: list[ActionInfo]      # 在练（按现在的 sort_order）
    passed: list[ActionInfo]      # 已过关（复查没做到才重新加）
    removed: list[ActionInfo]     # 最近被人工删掉的（别再出）
    run_status: str | None
    silence_sec: int | None = None   # 代码按空白统计定的冷场秒数（不让模型自己挑）
    recheck: list[ActionInfo] = field(default_factory=list)   # 本场之前已过关的：照样核对，不计连续场数
    regressed: set[int] = field(default_factory=set)          # 复查没做到的已过关动作 id（要重新加回）


@dataclass
class RankEntry:
    """梳理结果里的一条：要么指回一条在练动作，要么是一条新动作（这时 item 是补全后的字段）。"""
    existing_id: int | None = None
    item: dict | None = None
    again_id: int | None = None


@dataclass
class Plan:
    order: list[RankEntry] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    error: str = ""
    note: str = ""


class FocusNotReady(Exception):
    """还不能处理（场次/主播/整场总结不在）：不留处理记录，之后补漏循环会再来。"""


class _CallFailed(Exception):
    """快速档这次没拿到可用结果（已按规则重试过）；消息是给人看的中文原因。"""


# ---------- 时间 ----------

def hms(ms: int) -> str:
    sec = max(0, int(ms)) // 1000
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


_HMS_RE = re.compile(r"^\s*\[?(\d{1,3}):(\d{1,2}):(\d{1,2})\]?\s*$")


def parse_hms(value) -> int | None:
    match = _HMS_RE.match(str(value or ""))
    if not match:
        return None
    hours, minutes, seconds = (int(x) for x in match.groups())
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def wall_iso(started_at: datetime | None, ms: int) -> str | None:
    if started_at is None:
        return None
    return store.iso(started_at + timedelta(milliseconds=int(ms or 0)))


# ---------- 逐字稿：讲话时长、空白、切块 ----------

def _splits(turns: list[tuple[int, int]]) -> list[int]:
    """这位主播第 2 段起各班次的开始时刻：跨过它的两句不算相邻（中间是别人的班）。"""
    return sorted(start for start, _ in turns)[1:]


def _crosses(prev_start: int, next_start: int, splits: list[int]) -> bool:
    return any(prev_start < point <= next_start for point in splits)


def speech_span_ms(sentences: list[Sent], turns: list[tuple[int, int]]) -> int:
    """有效讲话时长：每个班次里从第一句到最后一句的跨度之和（返场之间的空档不算）。"""
    splits = _splits(turns)
    total, first, reach, prev = 0, None, 0, None
    for s in sentences:
        if first is None:
            first, reach = s, s.end_ms
        elif _crosses(prev.start_ms, s.start_ms, splits):
            total += max(0, reach - first.start_ms)
            first, reach = s, s.end_ms
        else:
            reach = max(reach, s.end_ms)
        prev = s
    if first is not None:
        total += max(0, reach - first.start_ms)
    return total


def find_gaps(sentences: list[Sent], turns: list[tuple[int, int]]) -> list[Gap]:
    """相邻两句之间的空白（上一句说完到下一句开口）；跨班次的不算。"""
    splits = _splits(turns)
    gaps: list[Gap] = []
    prev, reach = None, 0
    for s in sentences:
        crossed = prev is not None and _crosses(prev.start_ms, s.start_ms, splits)
        if prev is not None and not crossed and s.start_ms > reach:
            gaps.append(Gap(reach, s.start_ms, prev.text, s.text))
        reach = s.end_ms if prev is None or crossed else max(reach, s.end_ms)
        prev = s
    return gaps


def check_silence(gaps: list[Gap], threshold_sec: int,
                  started_at: datetime | None) -> tuple[str, dict, list[dict]]:
    cap = store.SILENCE_CAP_SEC
    hits = [g for g in gaps if threshold_sec <= g.sec <= cap]
    over = [g for g in gaps if g.sec > cap]
    longest = max((g.sec for g in hits), default=0.0)
    if hits:
        summary = f"{threshold_sec} 秒以上的空场 {len(hits)} 次，最长 {round(longest)} 秒"
    else:
        summary = f"没有 {threshold_sec} 秒以上的空场"
    if over:
        summary += f"；另有 {len(over)} 次超过 3 分钟（多半是放歌试听或离场，不算）"
    metric = {
        "threshold_sec": threshold_sec, "count": len(hits), "max_gap_sec": round(longest),
        "over_cap_count": len(over),
        "over_cap_max_sec": round(max(g.sec for g in over)) if over else 0,
        "cap_sec": cap, "summary": summary,
    }
    top = sorted(sorted(hits, key=lambda g: -g.sec)[:4], key=lambda g: g.start_ms)
    evidence = [{
        "at_ms": g.start_ms, "wall_time": wall_iso(started_at, g.start_ms), "ok": False,
        "gap_sec": round(g.sec),
        "quote": f"空了 {round(g.sec)} 秒｜前一句：{store.clip(g.before, 40)}｜"
                 f"后一句：{store.clip(g.after, 40)}",
        "before": store.clip(g.before, 60), "after": store.clip(g.after, 60),
    } for g in top]
    return ("fail" if hits else "pass"), metric, evidence


def suggest_silence_sec(gaps: list[Gap], sessions: int = 1) -> tuple[int, float]:
    """建议的冷场秒数：每场平均只有 ≤3 次 [N, 180] 秒空白的最小一档（都超过就取最大档）。返回 (秒数, 每场次数)。"""
    sessions = max(1, sessions)
    cap = store.SILENCE_CAP_SEC
    for step in SILENCE_STEPS:
        per = sum(1 for g in gaps if step <= g.sec <= cap) / sessions
        if per <= 3:
            return step, per
    step = SILENCE_STEPS[-1]
    return step, sum(1 for g in gaps if step <= g.sec <= cap) / sessions


def silence_profile(gaps: list[Gap], sessions: int = 1) -> str:
    cap = store.SILENCE_CAP_SEC
    inside = [g.sec for g in gaps if g.sec <= cap]
    parts = [f"≥{t} 秒 {sum(1 for sec in inside if sec >= t)} 次" for t in SILENCE_PROFILE]
    text = "；".join(parts) + f"；最长 {round(max(inside, default=0))} 秒"
    over = sum(1 for g in gaps if g.sec > cap)
    if over:
        text += f"；另有 {over} 次超过 3 分钟（放歌试听或离场，不算冷场）"
    if sessions > 1:
        text = f"{sessions} 场合计：{text}"
    sec, per = suggest_silence_sec(gaps, sessions)
    return text + f"\n建议秒数：{sec}（每场平均 {per:.1f} 次 ≥{sec} 秒的空白，下一场做到 0 次就算做到）"


def build_chunks(sentences: list[Sent], size: int | None = None) -> list[Chunk]:
    """按句切块（不拆句子）；每块附上紧跟其后的几句当「后续参考」，判断块尾的时机有没有做到。"""
    size = size or CHUNK_CHARS
    total = sum(len(s.text) for s in sentences)
    if total > size * MAX_CHUNKS:
        size = math.ceil(total / MAX_CHUNKS)
    groups: list[list[Sent]] = []
    current: list[Sent] = []
    chars = 0
    for s in sentences:
        if current and chars + len(s.text) > size:
            groups.append(current)
            current, chars = [], 0
        current.append(s)
        chars += len(s.text)
    if current:
        groups.append(current)
    chunks = []
    for index, main in enumerate(groups):
        tail: list[Sent] = []
        used = 0
        if index + 1 < len(groups):
            end = main[-1].end_ms
            for s in groups[index + 1]:
                if s.start_ms - end > TAIL_SEC * 1000 or used + len(s.text) > TAIL_CHARS:
                    break
                tail.append(s)
                used += len(s.text)
        chunks.append(Chunk(index, main, tail))
    return chunks


# ---------- llm 核对：提示词、解析、汇总 ----------

CHECK_SYSTEM = (
    "你是带货直播复盘的核对员，只做一件事：对照逐字稿，核对主播有没有做到给定的「改进动作」。\n"
    "输入有三部分：\n"
    "【正文】主播的一段逐字稿，每行开头方括号里是本场时间（时:分:秒），后面是这句话"
    "（语音识别出来的，可能有错别字、同音字、断句不准）；\n"
    "【后续参考】紧跟在正文后面的几句，只用来看正文末尾的时机后面有没有做到；\n"
    "【动作】若干条，每条有编号、动作、时机（什么情况出现时该做）、做到的标准。\n"
    "做法：\n"
    "1. 每条动作，在【正文】里找出「时机」真正出现的每一处；后续参考里的不算新时机；\n"
    "2. 每一处时机，看主播在那之后有没有按「做到的标准」做到；标准里有时限的，按行首时间算；\n"
    "3. 连着几句说的是同一件事（比如连着几句都在报同一个价），只算一处，取第一句；\n"
    "4. 时机要明确：主播确实在做时机描述的那件事才算，宁可少列、不要多列；模棱两可、判断不了的不列；"
    "某条动作在正文里一处时机都没有，就给空列表，不要硬凑；\n"
    "5. 动作和标准里引号中的话只是示范说法，主播说的意思对上了就算做到，不要求一字不差；"
    "标准里的时限前后差几秒属于识别误差，不要卡死；时机那一句本身已经满足标准的，也算做到；"
    "标准里写了「至少讲到其中 N 样」的，数这一处讲到了几样，够 N 样就算做到；\n"
    "6. 只看这个动作做没做，不评价价格对不对、参数准不准、话术好不好。\n"
    "只输出 JSON：{\"r\":[{\"id\":动作编号,\"m\":[[\"时机那句的时间\",1,\"做到那句的时间\"],"
    "[\"时机那句的时间\",0,\"\"]]}]}\n"
    "- 第二项 1 表示做到、0 表示没做到；没做到时第三项写空字符串；\n"
    "- 时间照抄行首的时:分:秒；\n"
    f"- 每条动作最多列 {OCCASIONS_PER_CHUNK} 处。"
)


def check_user(actions: list[ActionInfo], chunk: Chunk) -> str:
    """逐字稿放前面、动作放后面：动作多了要分批问同一块时，前缀一样，能用上模型服务的缓存。"""
    lines = ["【正文】"]
    lines += [f"[{hms(s.start_ms)}] {s.text}" for s in chunk.main]
    lines += ["", "【后续参考】"]
    lines += [f"[{hms(s.start_ms)}] {s.text}" for s in chunk.tail] or ["（没有）"]
    lines += ["", "【动作】"]
    for number, action in enumerate(actions, 1):
        lines += [f"{number}. 动作：{action.text}", f"   时机：{action.trigger}",
                  f"   做到的标准：{action.criterion}"]
    return "\n".join(lines)


def _nearest(secs: list[int], target: int | None, *, start: int = 0,
             tolerance: int = 15) -> int | None:
    if target is None:
        return None
    best, best_gap = None, None
    for index in range(start, len(secs)):
        gap = abs(secs[index] - target)
        if best_gap is None or gap < best_gap:
            best, best_gap = index, gap
    return best if best_gap is not None and best_gap <= tolerance else None


def _quote(trigger: Sent, done: Sent | None, ok: bool) -> str:
    if ok and done is not None and done is not trigger:
        return f"{store.clip(trigger.text, 45)} …… {store.clip(done.text, 45)}"
    return store.clip(trigger.text, 80)


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value or "").strip().lower() in ("1", "true", "yes", "是", "做到", "做到了")


def parse_check(data: dict, chunk: Chunk, count: int) -> dict[int, list[Occasion]]:
    """模型输出 → {动作序号(从 1 起): [时机]}。时间对不上正文的丢掉（宁缺勿滥），同一句只收一次。"""
    rows = data.get("r")
    if not isinstance(rows, list):
        raise ValueError("模型输出里没有 r 列表")
    main_secs = [s.start_ms // 1000 for s in chunk.main]
    every = chunk.main + chunk.tail
    every_secs = main_secs + [s.start_ms // 1000 for s in chunk.tail]
    out: dict[int, list[Occasion]] = {n: [] for n in range(1, count + 1)}
    seen: dict[int, set[int]] = {n: set() for n in out}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            number = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if number not in out or not isinstance(row.get("m"), list):
            continue
        for item in row["m"]:
            if len(out[number]) >= OCCASIONS_PER_CHUNK:
                break
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            index = _nearest(main_secs, parse_hms(item[0]))
            if index is None or index in seen[number]:
                continue
            seen[number].add(index)
            ok = _flag(item[1])
            done = None
            if ok and len(item) >= 3:
                done_index = _nearest(every_secs, parse_hms(item[2]), start=index)
                done = every[done_index] if done_index is not None else None
            trigger = chunk.main[index]
            out[number].append(Occasion(trigger.start_ms, ok, _quote(trigger, done, ok)))
    return out


OPENING_NOTE = ("【说明】正文是这位主播这一班开口后的头 60 秒，第一句就是「开播或接班」这个时机。"
                "只列这一处（时间写第一句的），判断这段里有没有按标准做到。")


def is_opening(a: ActionInfo) -> bool:
    """开播 / 接班开场动作：每班只在开头判一次（整场找时机会把每句「欢迎新进来的」都算成开场，
    中途接班的人又一次都找不到）。「新观众进来时」那种欢迎动作不算，照常整场找。"""
    words = f"{a.text}{a.trigger}"
    return (_cat(a) == "开场介绍" and any(w in words for w in ("开播", "接班", "开场"))
            and not any(w in a.trigger for w in ("新观众", "新进", "进来")))


def opening_windows(sentences: list[Sent], turns: list[tuple[int, int]]) -> list[list[Sent]]:
    """每班开头那几句：这一班里主播开口的第一句起 OPENING_WINDOW_MS 内的话。
    没有班次记录时把整场第一句当开头；这一班里没开口的跳过。"""
    spans = sorted(set(turns)) or ([(sentences[0].start_ms, sentences[-1].end_ms)] if sentences else [])
    windows: list[list[Sent]] = []
    seen: set[int] = set()
    for start, end in spans:
        first = next((s for s in sentences if start <= s.start_ms and (end <= start or s.start_ms < end)), None)
        if first is None or first.start_ms in seen:
            continue
        seen.add(first.start_ms)
        windows.append([s for s in sentences
                        if first.start_ms <= s.start_ms < first.start_ms + OPENING_WINDOW_MS])
    return windows


def opening_occasion(window: list[Sent], found: list[Occasion]) -> Occasion:
    """一班开头只算一处：模型说做到了就是做到，没列出来或说没做到都算没做到（证据用开头几句）。"""
    hit = next((o for o in found if o.ok), None)
    if hit is not None:
        return Occasion(window[0].start_ms, True, hit.quote)
    return Occasion(window[0].start_ms, False, store.clip(" ".join(s.text for s in window[:3]), 80))


def merge_episodes(occasions: list[Occasion], gap_ms: int = EPISODE_GAP_MS) -> list[Occasion]:
    """闲聊一段只算一处（2026-10-04）：相邻两处隔不到 gap_ms 的连成一段，时间取这段第一处；
    做没做到看这段最后一处——聊完之后有没有说回产品（证据也用最后一处）。
    以前每句闲聊都单独算一次，聊两分钟没拉回就记三四次「没做到」。"""
    merged: list[Occasion] = []
    last_at: int | None = None
    for item in sorted(occasions, key=lambda o: o.at_ms):
        if merged and last_at is not None and item.at_ms - last_at <= gap_ms:
            merged[-1] = Occasion(merged[-1].at_ms, item.ok, item.quote)
        else:
            merged.append(item)
        last_at = item.at_ms
    return merged


def is_episode_action(a: ActionInfo) -> bool:
    return _cat(a) == "拉回主线"


def merge_close(occasions: list[Occasion], within_ms: int | None = None) -> list[Occasion]:
    """同一条动作挨得很近的几处时机（模型把连着说的几句拆开了）合成一处：取第一句的时间，
    其中有一处做到就算做到（证据用做到的那句）。"""
    within = MERGE_WITHIN_MS if within_ms is None else within_ms
    merged: list[Occasion] = []
    for item in sorted(occasions, key=lambda o: o.at_ms):
        if merged and item.at_ms - merged[-1].at_ms <= within:
            if item.ok and not merged[-1].ok:
                merged[-1] = Occasion(merged[-1].at_ms, True, item.quote)
            continue
        merged.append(item)
    return merged


def pick_evidence(occasions: list[Occasion], started_at: datetime | None) -> list[dict]:
    """做到、没做到各挑两条（第一处和中间一处），按时间排。"""
    def spread(items: list[Occasion]) -> list[Occasion]:
        return items if len(items) <= 2 else [items[0], items[len(items) // 2]]

    picked = sorted(spread([o for o in occasions if o.ok]) + spread([o for o in occasions if not o.ok]),
                    key=lambda o: o.at_ms)
    return [{"at_ms": o.at_ms, "wall_time": wall_iso(started_at, o.at_ms), "ok": o.ok,
             "quote": o.quote} for o in picked]


def llm_result(action_id: int, occasions: list[Occasion], chunks: int, failed: int,
               started_at: datetime | None, provider: str = "") -> CheckResult:
    total = len(occasions)
    done = sum(1 for o in occasions if o.ok)
    if failed:
        summary = f"有 {failed}/{chunks} 段逐字稿没核对成（模型调用失败），本场不计"
        metric = {"chunks": chunks, "failed_chunks": failed, "partial_total": total,
                  "partial_done": done, "summary": summary}
        return CheckResult(action_id, "na", None, None, metric, pick_evidence(occasions, started_at),
                           f"核对失败：{summary}", provider)
    if total == 0:
        verdict, reason, summary = "na", "本场没有出现该做的时机", "本场没有出现该做的时机"
    else:
        verdict = "pass" if done * 3 >= total * 2 else "fail"
        reason, summary = "", f"该做 {total} 次，做到 {done} 次（{round(done * 100 / total)}%）"
    metric = {"chunks": chunks, "failed_chunks": 0, "occasions": total, "done": done,
              "rate": round(done / total, 2) if total else None, "summary": summary}
    return CheckResult(action_id, verdict, done, total, metric,
                       pick_evidence(occasions, started_at), reason, provider)


# ---------- 新动作：梳理排序 + 补全细节 ----------

def _category_lines() -> str:
    return "\n".join(f"- {name}：{desc}" for name, desc in store.CATEGORIES.items())


RANK_SYSTEM = (
    "你是高客单 HiFi 耳机直播间（舒缓专业型自播，不是激情冲量播）的带货教练。任务：把这位主播直播总结里的问题，"
    "整理成下一场就能练、只看逐字稿就能判断做没做到的「改进动作」，按重要程度从高到低排好。\n"
    "每条动作先归到一个类别，类别只能从下面选：\n"
    f"{_category_lines()}\n"
    "规则：\n"
    "1. 一个问题只出一条动作；同一类行为的多个要点合成一条，要点放进 points（比如讲款缺需求、缺卖点、缺适配、"
    "缺促单，就只出一条「讲款要讲全」，points 写这几样），不要拆成好几条；除了「其他」，每个类别最多一条；"
    "合成的动作文字写目标（如「讲一款就把要点讲全」），不要写「六步讲完」这种样样都要做到的说法，"
    "做到几样由标准定；\n"
    "2. 【在练动作】里已经有同一类的，一律写 {\"same\":\"A编号\"} 指回原来那条，不新加（说法不同也一样）；\n"
    "3. 【已过关动作】系统每场会按逐字稿复查，真没做到会自动加回；不要出和它们同一类的新动作；\n"
    "4. 【人工删掉过的动作】不要再出；\n"
    "5. 这些话题一律不出动作，也不要放进 skip：价格、库存、优惠、券、定价、改价、站外交易（价格不做考核）；"
    "违禁词、绝对化用语（合规模块另外管）；型号叫全称还是昵称（要产品知识，语音识别也转不准）；\n"
    "6. 只看逐字稿判断不了的不出（语气、气场、自信、感染力、语速、停顿几分之一秒），值得一提的放进 skip；\n"
    "7. 冷场用 kind=\"silence\"、cat=\"冷场\"，只管「相邻两句话之间空白不超过 N 秒」，N 用【空白统计】里给的"
    "建议秒数（写在 sec）；最多一条；\n"
    "8. 加上在练的，一位主播一般 3~8 条就够：只挑真正影响成交和观感的，不凑数；类别清单不是要逐个填满的，"
    "总结里没提到的问题不要出；\n"
    "9. 每条新动作（冷场除外）都要在 src 里照抄总结里对应问题的原话（连续 6~20 个字，一字不改），抄不出来的就不要出。\n"
    "只输出 JSON：{\"order\":[{\"same\":\"A1\"},"
    "{\"new\":\"动作，≤30字\",\"cat\":\"类别\",\"kind\":\"llm\",\"points\":[\"要点\",\"要点\"],"
    "\"src\":\"总结原话\"},"
    "{\"new\":\"冷场不超过60秒\",\"cat\":\"冷场\",\"kind\":\"silence\",\"sec\":60},"
    "{\"new\":\"动作\",\"cat\":\"类别\",\"kind\":\"llm\",\"again\":\"P1\",\"src\":\"总结原话\"}],"
    "\"skip\":[{\"text\":\"值得一提但没写成动作的\",\"why\":\"原因\"}]}\n"
    f"order 按重要程度从高到低，最多 10 条；skip 最多 5 条；points 每条最多 {MAX_POINTS} 个、每个不超过 8 个字。"
)

DETAIL_SYSTEM = (
    "你是高客单 HiFi 耳机直播间的带货教练（卖的是耳机、耳放、播放器、线材，舒缓专业型自播）。"
    "下面每条是主播下一场要练的「改进动作」（带类别，有的带要点），给每条补三样：\n"
    "- trigger：时机，从主播自己说的话就能认出来的情况，30 字以内（比如主播开始讲一款产品、主播报完价、"
    "主播回答观众的问题、主播欢迎新观众）；不要写只有看弹幕或画面才知道的情况；\n"
    "- criterion：做到的标准，只看逐字稿就能判断，60 字以内；核对时是一处时机一处时机地看，标准要写成"
    "「每一处时机里要做到什么」。要点是一次里要讲到的内容（比如需求、卖点、适配、促单）时，写成"
    "「A、B、C、D 至少讲到其中 N 样」，N 取要点数的一半左右（至少 2）；要点是几种不同的情况"
    "（比如找链接、等客服、离场前）时，把这几种都写进 trigger，criterion 只写每次要做到什么；"
    "要点里不要放价格；意思到了就算，不要求原话；有时限的给 15~30 秒；\n"
    "- example：一句主播下一场可以直接照着说的口语例句，40 字以内，贴合 HiFi 耳机直播（聊音色、曲风、"
    "佩戴、前端搭配、试听），不写具体价格和参数数字（用「X 号链接」「这款」代替）。\n"
    "不要改动作本身，一条动作只回一条，不要拆。时机、标准、例句里都不要提价格数字、库存、优惠、券、改价、"
    "付款方式、违禁词，也不要虚构稀缺（「库存有限」「最后几台」之类）；标准里不要加「参数以知识库为准」"
    "「参数要核实」这类话（参数对不对不考核）。标了「不冷场」的只写 example。\n"
    "只输出 JSON：{\"items\":[{\"i\":序号,\"text\":\"动作原文照抄\",\"trigger\":\"…\",\"criterion\":\"…\","
    "\"example\":\"…\"}]}"
)

MERGE_SYSTEM = (
    "你是带货直播教练。下面是一位主播的改进动作清单（A 开头是在练的，N 开头是这次新拟的），条数偏多。"
    "把说的是同一个问题、或者同一类行为的找出来分成组，以后合成一条练；不同的问题不要硬合。\n"
    "只输出 JSON：{\"groups\":[[\"A1\",\"N2\"],[\"N3\",\"N5\"]]}，每组至少两条；没有能合的就给 {\"groups\":[]}。"
)


def _item_text(value) -> str:
    if isinstance(value, dict):
        for key in ("action", "issue", "title", "text", "problem", "desc"):
            if str(value.get(key) or "").strip():
                return str(value[key])
        return ""
    return str(value or "")


def summary_material(summary: dict) -> dict:
    """从整场总结里取素材：主要问题、改进建议，有 v2 章节就带上常缺的环节和点出的问题。"""
    def unique(values, limit: int, width: int) -> list[str]:
        out: list[str] = []
        for value in values or []:
            text = store.clip(_item_text(value), width)
            if text and text not in out:
                out.append(text)
        return out[:limit]

    issues = unique(summary.get("top_issues"), 12, 120)
    actions = unique(summary.get("action_items"), 12, 150)
    chapters = []
    v2 = summary.get("v2")
    if isinstance(v2, dict) and isinstance(v2.get("chapters"), dict):
        items = v2["chapters"].get("items")
        chapters = [c for c in items if isinstance(c, dict)] if isinstance(items, list) else []
    missing: Counter = Counter()
    problems: Counter = Counter()
    for chapter in chapters:
        for value in chapter.get("missing") or []:
            if store.clip(value, 30):
                missing[store.clip(value, 30)] += 1
        for value in chapter.get("problems") or []:
            if store.clip(value, 60):
                problems[store.clip(value, 60)] += 1
    return {"issues": issues, "actions": actions,
            "missing": missing.most_common(8), "problems": problems.most_common(10)}


def has_material(material: dict) -> bool:
    return bool(material["issues"] or material["actions"] or material["missing"] or material["problems"])


def _numbered(items: list[str]) -> list[str]:
    return [f"{n}. {text}" for n, text in enumerate(items, 1)] or ["（没有）"]


def _detail_label(a: ActionInfo) -> str:
    return f"不冷场，{a.threshold_sec} 秒" if a.kind == "silence" else "话术"


def _cat(a: ActionInfo) -> str:
    return store.normalize_category(a.category, a.kind)


def rank_user(ctx: Context, sections: list[tuple[str, dict]], profile: str) -> str:
    """sections：[(标签, 素材)]；平时一场，首批重整时一位主播的几场合在一起。"""
    multi = len(sections) > 1
    lines = [f"【主播】{ctx.streamer_name}"]
    for label, material in sections:
        prefix = f"{label} " if multi else "本场"
        lines += ["", f"【{prefix}总结的主要问题】", *_numbered(material["issues"]),
                  "", f"【{prefix}总结的改进建议】", *_numbered(material["actions"])]
        if material["missing"] or material["problems"]:
            lines += ["", f"【{prefix}深度复盘：各段经常缺的环节】（都属于「讲款完整」这一类，合成一条）"]
            lines += [f"- {text}（{count} 段缺）" for text, count in material["missing"]] or ["（没有）"]
            lines += ["", f"【{prefix}深度复盘：点出的问题】"]
            lines += [f"- {text}（{count} 段）" for text, count in material["problems"]] or ["（没有）"]
    if multi:
        lines += ["", "（以上几场的问题合在一起考虑：同一类问题只出一条，几场都出现的更重要）"]
    lines += ["", "【空白统计】（相邻两句之间没说话的时长）", profile, "", "【在练动作】"]
    lines += [f"A{n}. [{_cat(a)}] {a.text}（{_detail_label(a)}）"
              for n, a in enumerate(ctx.active, 1)] or ["（没有）"]
    lines += ["", "【已过关动作】（系统每场按逐字稿复查，不用你再出）"]
    lines += [f"P{n}. [{_cat(a)}] {a.text}（{_detail_label(a)}）"
              for n, a in enumerate(ctx.passed, 1)] or ["（没有）"]
    lines += ["", "【人工删掉过的动作（不要再出）】"]
    lines += [f"- {a.text}" for a in ctx.removed] or ["（没有）"]
    return "\n".join(lines)


def _label_id(value, prefix: str, actions: list[ActionInfo]) -> int | None:
    match = re.fullmatch(rf"\s*{prefix}?(\d+)\s*", str(value or ""), re.I)
    if not match:
        return None
    index = int(match.group(1)) - 1
    return actions[index].id if 0 <= index < len(actions) else None


def _points(value) -> list[str]:
    out: list[str] = []
    for item in value if isinstance(value, list) else []:
        text = store.clip(item, 12)
        if text and text not in out and not store.is_excluded_item(text):
            out.append(text)
    return out[:MAX_POINTS]


def material_blob(sections: list[tuple[str, dict]]) -> str:
    """总结素材拼成一段（去掉标点空白），用来核对新动作引的原话是不是真在总结里。"""
    parts: list[str] = []
    for _label, material in sections:
        parts += material["issues"] + material["actions"]
        parts += [text for text, _count in material["missing"]] + [text for text, _count in material["problems"]]
    return store.norm(" ".join(parts))


def grounded(src: str, blob: str) -> bool:
    """引的原话在总结里：整段能找到；长一点的允许模型抄错几个字——有连续 6 个字对得上就算。"""
    quote = store.norm(src)
    if len(quote) < 4 or not blob:
        return False
    if quote in blob:
        return True
    return len(quote) >= 6 and any(quote[i:i + 6] in blob for i in range(len(quote) - 5))


def parse_rank(data: dict, ctx: Context, blob: str | None = None) -> tuple[list[dict], list[dict]]:
    """梳理输出 → ([{existing_id} 或 {text, kind, sec, category, points, again_id}], skip 候选)。

    不该出的话题（价格/违禁词/亚秒停顿/型号称呼）直接丢掉；要点里混进的这类要点单独去掉。
    给了 blob 时，话术动作必须引到总结里的原话（src），引不到的算凑数，不出（冷场看数据，不用引）。
    """
    order = data.get("order")
    if not isinstance(order, list):
        raise ValueError("模型输出里没有 order 列表")
    entries: list[dict] = []
    skipped: list[dict] = []
    for raw in order[:12]:
        if not isinstance(raw, dict):
            continue
        if raw.get("same") is not None:
            existing = _label_id(raw.get("same"), "A", ctx.active)
            if existing is not None and all(e.get("existing_id") != existing for e in entries):
                entries.append({"existing_id": existing})
            continue
        text = " ".join(str(raw.get("new") or "").split()).rstrip("。.;；")
        if not text or store.is_excluded_item(text):
            continue
        if len(text) > store.TEXT_LIMIT:
            skipped.append({"text": store.clip(text, 60), "why": "动作超过 30 字，没收"})
            continue
        kind = "silence" if str(raw.get("kind") or "").strip().lower() == "silence" else "llm"
        try:
            sec = int(float(raw.get("sec") or 60))
        except (TypeError, ValueError):
            sec = 60
        if kind == "silence" and getattr(ctx, "silence_sec", None):
            sec = ctx.silence_sec            # 秒数按空白统计定，不用模型挑的
        if blob is not None and kind != "silence" and not grounded(str(raw.get("src") or ""), blob):
            skipped.append({"text": text, "why": "总结里找不到对应的原话，没出"})
            continue
        entries.append({"text": text, "kind": kind, "sec": min(150, max(10, sec)),
                        "category": store.normalize_category(raw.get("cat"), kind),
                        "points": _points(raw.get("points")),
                        "again_id": _label_id(raw.get("again"), "P", ctx.passed)})
    for raw in data.get("skip") or []:
        if isinstance(raw, dict) and str(raw.get("text") or "").strip():
            text = store.clip(raw["text"], 60)
            if not store.is_excluded_item(text):
                skipped.append({"text": text, "why": store.clip(raw.get("why") or "没法从逐字稿判断", 40)})
    return entries, skipped[:8]


def apply_categories(entries: list[dict], ctx: Context) -> tuple[list[dict], list[dict]]:
    """代码兜底「同一类只留一条」（「其他」除外）：和在练的同类 → 指回那条；
    新拟的同类 → 并进排在前面的那条（要点合在一起）。返回 (整理后的条目, 被并掉的)。"""
    active_by_cat: dict[str, ActionInfo] = {}
    for a in ctx.active:
        category = _cat(a)
        if category != store.OTHER:
            active_by_cat.setdefault(category, a)
    pointed = {e["existing_id"] for e in entries if "existing_id" in e}
    out: list[dict] = []
    dropped: list[dict] = []
    new_by_cat: dict[str, dict] = {}
    for entry in entries:
        if "existing_id" in entry:
            out.append(entry)
            continue
        category = entry["category"]
        if category != store.OTHER and category in active_by_cat:
            peer = active_by_cat[category]
            if peer.id not in pointed:
                out.append({"existing_id": peer.id})
                pointed.add(peer.id)
            dropped.append({"text": entry["text"], "kind": entry["kind"],
                            "why": f"和在练的「{peer.text}」是同一类（{category}），继续练那条"})
            continue
        if category != store.OTHER and category in new_by_cat:
            keep = new_by_cat[category]
            for point in entry["points"]:
                if point not in keep["points"] and len(keep["points"]) < MAX_POINTS:
                    keep["points"].append(point)
            dropped.append({"text": entry["text"], "kind": entry["kind"],
                            "why": f"和「{keep['text']}」是同一类（{category}），合成一条"})
            continue
        if category != store.OTHER:
            new_by_cat[category] = entry
        out.append(entry)
    return out, dropped


def merge_user(ctx: Context, entries: list[dict]) -> str:
    lines = ["【在练】"]
    lines += [f"A{n}. [{_cat(a)}] {a.text}" for n, a in enumerate(ctx.active, 1)] or ["（没有）"]
    lines += ["", "【新拟】"]
    lines += [f"N{n}. [{e['category']}] {e['text']}"
              for n, e in enumerate((e for e in entries if "text" in e), 1)] or ["（没有）"]
    return "\n".join(lines)


def parse_merge(data: dict) -> list[list[str]]:
    groups = data.get("groups")
    if not isinstance(groups, list):
        raise ValueError("模型输出里没有 groups 列表")
    out = []
    for group in groups:
        if isinstance(group, list):
            labels = [str(x).strip().upper() for x in group if re.fullmatch(r"\s*[AN]\d+\s*", str(x), re.I)]
            if len(set(labels)) >= 2:
                out.append(list(dict.fromkeys(labels)))
    return out


def apply_merge(groups: list[list[str]], ctx: Context, entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """按模型给的分组合并：组里有在练的 → 新拟的并进那条（指回）；全是新拟的 → 留排在最前的，要点合并。"""
    new_entries = [e for e in entries if "text" in e]
    by_label: dict[str, object] = {f"A{n}": a for n, a in enumerate(ctx.active, 1)}
    by_label.update({f"N{n}": e for n, e in enumerate(new_entries, 1)})
    removed: set[int] = set()
    pointer_of: dict[int, int] = {}          # 并进在练动作的新拟条目 → 那条在练动作的 id
    dropped: list[dict] = []
    for group in groups:
        members = [by_label[label] for label in group if label in by_label]
        actives = [m for m in members if isinstance(m, ActionInfo)]
        news = [m for m in members if isinstance(m, dict) and id(m) not in removed]
        if actives:
            keep = actives[0]
            for item in news:
                removed.add(id(item))
                dropped.append({"text": item["text"], "kind": item["kind"],
                                "why": f"和在练的「{keep.text}」是同一个问题，继续练那条"})
                pointer_of[id(item)] = keep.id
        elif len(news) >= 2:
            news.sort(key=lambda item: entries.index(item))
            keep = news[0]
            for item in news[1:]:
                removed.add(id(item))
                for point in item["points"]:
                    if point not in keep["points"] and len(keep["points"]) < MAX_POINTS:
                        keep["points"].append(point)
                dropped.append({"text": item["text"], "kind": item["kind"],
                                "why": f"和「{keep['text']}」是同一个问题，合成一条"})
    pointed = {e["existing_id"] for e in entries if "existing_id" in e}
    out: list[dict] = []
    for entry in entries:
        if "text" in entry and id(entry) in removed:
            target = pointer_of.get(id(entry))
            if target is not None and target not in pointed:
                out.append({"existing_id": target})
                pointed.add(target)
            continue
        out.append(entry)
    return out, dropped


def detail_user(issues: list[str], items: list[dict]) -> str:
    lines = ["【这位主播总结里的主要问题（参考）】", *_numbered(issues[:8]), "", "【要补全的动作】"]
    for n, item in enumerate(items, 1):
        if item["kind"] == "silence":
            label = f"不冷场，{item['sec']} 秒；只写 example"
        else:
            points = f"；要点：{'、'.join(item['points'])}" if item.get("points") else ""
            label = f"类别：{item['category']}{points}"
        lines.append(f"{n}. {item['text']}（{label}）")
    return "\n".join(lines)


def parse_detail(data: dict, items: list[dict] | int) -> dict[int, dict]:
    """补全输出 → {第几条(从 1 起): 那条的细节}。模型回了动作原文就按原文对（编号它有时会排错），
    没回原文才按编号对；对不上的不要。"""
    rows = data.get("items")
    if not isinstance(rows, list):
        raise ValueError("模型输出里没有 items 列表")
    texts = [store.norm(item["text"]) for item in items] if isinstance(items, list) else []
    count = len(items) if isinstance(items, list) else int(items)
    out: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        number = None
        # 模型常把「（类别：…；要点：…）」也抄回来：先去掉再对
        echoed = store.norm(re.split(r"（(?:类别|不冷场)", str(row.get("text") or ""), 1)[0])
        if echoed and texts:
            matches = [n for n, text in enumerate(texts, 1) if text == echoed]
            if not matches:
                matches = [n for n, text in enumerate(texts, 1) if store.similar(text, echoed)]
            number = matches[0] if len(matches) == 1 else None
        elif not echoed:
            try:
                number = int(row.get("i"))
            except (TypeError, ValueError):
                number = None
        if number is not None and 1 <= number <= count and number not in out:
            out[number] = row
    return out


# ---------- 读库 ----------

def _info(a: FocusAction) -> ActionInfo:
    return ActionInfo(a.id, a.text, a.trigger, a.criterion, a.kind, a.threshold_sec, a.status,
                      a.category or "")


def _load_context(session_id: int, streamer_id: int) -> Context:
    with SessionLocal() as db:
        sess = db.get(ReplaySession, session_id)
        if sess is None:
            raise FocusNotReady("场次不存在")
        if sess.session_type == SessionType.observation:
            raise FocusNotReady("观摩场次不做改进动作")
        streamer = db.get(Streamer, streamer_id)
        if streamer is None:
            raise FocusNotReady("主播不存在")
        summary_row = db.execute(select(Critique).where(
            Critique.session_id == session_id, Critique.streamer_id == streamer_id,
            Critique.level == CritiqueLevel.summary, Critique.category == "整场总结",
        ).order_by(Critique.id).limit(1)).scalars().first()
        if summary_row is None:
            raise FocusNotReady("这位主播本场还没有整场总结")
        turns = [(int(t.start_ms or 0), int(t.end_ms or 0)) for t in db.execute(
            select(StreamerTurn).where(
                StreamerTurn.session_id == session_id, StreamerTurn.streamer_id == streamer_id,
            ).order_by(StreamerTurn.start_ms, StreamerTurn.seq)).scalars()]
        sentences: list[Sent] = []
        for start, end, corrected, raw in db.execute(select(
            Transcript.start_ms, Transcript.end_ms, Transcript.corrected_text, Transcript.raw_text,
        ).where(
            Transcript.session_id == session_id, Transcript.streamer_id == streamer_id,
        ).order_by(Transcript.start_ms, Transcript.seq)).all():
            text = fix_text(" ".join(str(corrected or raw or "").split()))
            if text:
                begin = int(start or 0)
                sentences.append(Sent(begin, max(begin, int(end or 0)), text))
        started_at = sess.started_at or sess.created_at
        first_ms = min((start for start, _ in turns),
                       default=sentences[0].start_ms if sentences else 0)
        turn_started_at = (started_at or datetime.now()) + timedelta(milliseconds=first_ms)
        actions = db.execute(select(FocusAction).where(
            FocusAction.streamer_id == streamer_id,
        ).order_by(FocusAction.sort_order, FocusAction.id)).scalars().all()
        run_status = db.execute(select(FocusRun.status).where(
            FocusRun.session_id == session_id, FocusRun.streamer_id == streamer_id,
        )).scalar()
        # 已过关动作最后一次核对是哪一场（过关是按那几场判的）；早于本场的才复查
        last_checked = dict(db.execute(select(FocusCheck.action_id, func.max(FocusCheck.turn_started_at)).where(
            FocusCheck.action_id.in_([a.id for a in actions if a.status == "passed"] or [0]),
        ).group_by(FocusCheck.action_id)).all())
        summary = store.loads(summary_row.data, {})
        name = streamer.name or f"主播{streamer_id}"
        ext_id = streamer.ext_id
    active = [a for a in actions if a.status == "active"]
    to_check = [_info(a) for a in active
                if a.created_at is not None and a.created_at < turn_started_at]
    passed = sorted((a for a in actions if a.status == "passed"),
                    key=lambda a: a.passed_at or datetime.min, reverse=True)[:20]
    # 首批重整下线的旧动作不算「人工删掉」：同类问题以后还能再出
    removed = sorted((a for a in actions if a.status == "removed" and not store.is_rebuild_removed(a)),
                     key=lambda a: a.ended_at or datetime.min, reverse=True)[:10]
    # 本场之前就已过关的（2026-10-04）：每场照样按逐字稿核对，没做到才重新加回——不再凭总结里提到同类就加回。
    # 「之前」按过关所依据的最后一场核对算（过关时间记的是处理那一刻，补跑旧场次时会晚于下一场开播）；没核对过的看过关时间
    recheck = [_info(a) for a in passed
               if (last_checked.get(a.id) or a.passed_at or a.ended_at or datetime.min) < turn_started_at]
    return Context(session_id, streamer_id, name, ext_id, started_at, turns, turn_started_at,
                   sentences, summary, to_check, [_info(a) for a in active],
                   [_info(a) for a in passed], [_info(a) for a in removed], run_status,
                   recheck=recheck)


# ---------- 调快速档 ----------

def _steady(client):
    """核对、梳理都是判断题：随机度设 0（快速档默认 0.3，同一场判两次结果会差很多；2026-10-04）。
    只改这次用的副本，备用密钥的客户端一起改；不支持随机度的客户端原样用。"""
    if not hasattr(client, "temperature"):
        return client
    steady = copy.copy(client)
    steady.temperature = 0
    if getattr(client, "fallback", None) is not None and hasattr(client.fallback, "temperature"):
        steady.fallback = copy.copy(client.fallback)
        steady.fallback.temperature = 0
    return steady


async def _ask(system: str, user: str, parse):
    """调一次快速档拿 JSON，按约定解析；失败重试一次（4xx 拒绝、没配好不重试）。"""
    try:
        client = _steady(get_llm("fast"))
    except ValueError as exc:
        raise _CallFailed(f"快速档模型没配好（{safe_error_text(exc, limit=80)}）") from None
    reason = "模型调用失败"
    for attempt in (1, 2):
        try:
            out = await client.complete(system, user, json_mode=True)
            data = json.loads(out)
            if not isinstance(data, dict):
                raise ValueError("模型没有返回 JSON 对象")
            provider = str(getattr(out, "provider", "") or getattr(client, "name", "") or "")
            return parse(data), provider
        except (ValueError, TypeError, KeyError) as exc:
            reason = f"模型输出格式不对（{safe_error_text(exc, limit=80)}）"
        except Exception as exc:  # noqa: BLE001  LLMCallError 等：只留稳定错误码
            code = str(getattr(exc, "code", "") or safe_exception_code(exc, prefix="FOCUS_LLM"))
            reason = f"模型调用失败（{code}）"
            if code == "LLM_REQUEST_REJECTED":      # 4xx（余额不足、鉴权等）重试也没用
                break
        if attempt == 1:
            await asyncio.sleep(RETRY_DELAY_SEC)
    raise _CallFailed(reason)


async def _check_openings(ctx: Context, actions: list[ActionInfo]) -> tuple[list[CheckResult], int, int]:
    """开播 / 接班开场动作：每班开头 60 秒单独问一次，每班只算一处。返回 (结果, 调用次数, 没成的次数)。"""
    windows = opening_windows(ctx.sentences, ctx.turns)
    results: list[CheckResult] = []
    calls = failed_calls = 0
    for action in actions:
        occasions: list[Occasion] = []
        failed = 0
        providers: set[str] = set()
        for index, window in enumerate(windows):
            chunk = Chunk(index, window, [])
            calls += 1
            try:
                found, provider = await _ask(
                    CHECK_SYSTEM, check_user([action], chunk) + "\n\n" + OPENING_NOTE,
                    lambda data, chunk=chunk: parse_check(data, chunk, 1).get(1, []))
            except _CallFailed as exc:
                failed += 1
                failed_calls += 1
                print(f"[focus] 第 {index + 1} 班开头没核对成 session={ctx.session_id} "
                      f"sid={ctx.streamer_id} action={action.id}; reason={safe_error_text(exc)}")
                continue
            if provider:
                providers.add(provider)
            occasions.append(opening_occasion(window, found))
        results.append(llm_result(action.id, occasions, len(windows), failed, ctx.started_at,
                                  ",".join(sorted(providers))))
    return results, calls, failed_calls


async def _run_checks(ctx: Context) -> tuple[list[CheckResult], int, int]:
    """逐条核对；返回 (结果, 调用次数, 没成的调用次数)。"""
    results: list[CheckResult] = []
    seen_ids = {a.id for a in ctx.to_check}
    checking = [*ctx.to_check, *(a for a in ctx.recheck if a.id not in seen_ids)]
    silence = [a for a in checking if a.kind == "silence"]
    talk = [a for a in checking if a.kind != "silence"]
    openings = [a for a in talk if is_opening(a)]
    llm_actions = [a for a in talk if not is_opening(a)]
    if silence:
        gaps = find_gaps(ctx.sentences, ctx.turns)
        for action in silence:
            verdict, metric, evidence = check_silence(gaps, action.threshold_sec, ctx.started_at)
            results.append(CheckResult(action.id, verdict, None, None, metric, evidence))
    open_calls = open_failed = 0
    if openings:
        opened, open_calls, open_failed = await _check_openings(ctx, openings)
        results += opened
    if not llm_actions:
        return results, open_calls, open_failed
    chunks = build_chunks(ctx.sentences)
    groups = [llm_actions[i:i + ACTIONS_PER_CALL] for i in range(0, len(llm_actions), ACTIONS_PER_CALL)]
    # 按批次排：同一块逐字稿的第二批隔一会儿再问，前一批的缓存已经建好
    jobs = [(chunk, group) for group in groups for chunk in chunks]
    slots = asyncio.Semaphore(LLM_CONCURRENCY)

    async def one(chunk: Chunk, group: list[ActionInfo]):
        async with slots:
            return await _ask(CHECK_SYSTEM, check_user(group, chunk),
                              lambda data: parse_check(data, chunk, len(group)))

    outcomes = await asyncio.gather(*(one(c, g) for c, g in jobs), return_exceptions=True)
    found: dict[int, list[Occasion]] = defaultdict(list)
    failed_by: Counter = Counter()
    providers: set[str] = set()
    failed_calls = 0
    for (chunk, group), outcome in zip(jobs, outcomes):
        if isinstance(outcome, BaseException):
            failed_calls += 1
            for action in group:
                failed_by[action.id] += 1
            detail = (str(outcome) if isinstance(outcome, _CallFailed)
                      else safe_exception_code(outcome, prefix="FOCUS_CHUNK"))
            print(f"[focus] 逐字稿第 {chunk.index + 1}/{len(chunks)} 块没核对成 "
                  f"session={ctx.session_id} sid={ctx.streamer_id} actions={[a.id for a in group]}; "
                  f"reason={detail}")
            continue
        parsed, provider = outcome
        if provider:
            providers.add(provider)
        for number, items in parsed.items():
            found[group[number - 1].id].extend(items)
    provider = ",".join(sorted(providers))
    for action in llm_actions:
        occasions = merge_close(found[action.id])
        if is_episode_action(action):
            occasions = merge_episodes(occasions)
        results.append(llm_result(action.id, occasions, len(chunks), failed_by[action.id],
                                  ctx.started_at, provider))
    return results, len(jobs) + open_calls, failed_calls + open_failed


async def merge_round(ctx: Context, entries: list[dict]) -> tuple[list[dict], list[dict], str]:
    """在练加新拟超过 8 条：让模型再合并一轮；合不了才保留，并返回一句备注。"""
    new_count = sum(1 for e in entries if "text" in e)
    total = len(ctx.active) + new_count
    if total <= store.SOFT_MAX_ACTIVE or new_count == 0:
        return entries, [], ""
    try:
        groups, _provider = await _ask(MERGE_SYSTEM, merge_user(ctx, entries), parse_merge)
    except _CallFailed as exc:
        note = f"在练加新拟共 {total} 条（超过 {store.SOFT_MAX_ACTIVE} 条），再合并一轮没做成（{exc}），先全部保留"
        print(f"[focus] {note} sid={ctx.streamer_id}")
        return entries, [], note
    merged, dropped = apply_merge(groups, ctx, entries)
    after = len(ctx.active) + sum(1 for e in merged if "text" in e)
    note = ""
    if after > store.SOFT_MAX_ACTIVE:
        note = (f"在练加新拟共 {total} 条，再合并一轮后还有 {after} 条（超过 {store.SOFT_MAX_ACTIVE} 条，"
                "都是不同的问题，先保留）")
        print(f"[focus] {note} sid={ctx.streamer_id}")
    return merged, dropped, note


async def plan_actions(ctx: Context, sections: list[tuple[str, dict]], profile: str) -> Plan:
    """梳理（一次）→ 代码按类别兜底 → 超过 8 条再合并一轮 → 补全细节（每 4 条一次）。"""
    blob = material_blob(sections)
    try:
        (entries, skipped), _provider = await _ask(
            RANK_SYSTEM, rank_user(ctx, sections, profile), lambda data: parse_rank(data, ctx, blob))
    except _CallFailed as exc:
        return Plan(error=f"新动作没定成：{exc}")
    entries, dropped = apply_categories(entries, ctx)
    entries, merged_away, note = await merge_round(ctx, entries)
    plan = Plan(candidates=[{**item, "usable": False} for item in skipped + dropped + merged_away],
                note=note)
    new_items = [e for e in entries if "text" in e]
    issues: list[str] = []
    for _label, material in sections:
        issues += [text for text in material["issues"] if text not in issues]
    detailed: dict[int, dict] = {}
    failures: list[str] = []
    batches = [new_items[i:i + DETAIL_BATCH] for i in range(0, len(new_items), DETAIL_BATCH)]
    slots = asyncio.Semaphore(LLM_CONCURRENCY)

    async def one(batch: list[dict]):
        async with slots:
            return await _ask(DETAIL_SYSTEM, detail_user(issues, batch),
                              lambda data: parse_detail(data, batch))

    def take(batch: list[dict], outcome) -> None:
        if isinstance(outcome, BaseException):
            failures.append(str(outcome) if isinstance(outcome, _CallFailed)
                            else safe_exception_code(outcome, prefix="FOCUS_DETAIL"))
            return
        rows, _provider = outcome
        for number, entry in enumerate(batch, 1):
            row = rows.get(number) or {}
            item = store.clean_generated({
                "text": entry["text"], "kind": entry["kind"], "threshold_sec": entry["sec"],
                "category": entry["category"], "trigger": row.get("trigger"),
                "criterion": row.get("criterion"), "example": row.get("example"),
            })
            if item is not None:
                detailed[id(entry)] = item

    outcomes = await asyncio.gather(*(one(b) for b in batches), return_exceptions=True)
    for batch, outcome in zip(batches, outcomes):
        take(batch, outcome)
    # 调用成功、但个别条目的细节没对上或不合格（比如时机里提了改价）：这几条单独再补一次
    answered = [e for batch, outcome in zip(batches, outcomes)
                if not isinstance(outcome, BaseException) for e in batch]
    retry = [e for e in answered if id(e) not in detailed]
    if retry:
        take(retry, (await asyncio.gather(one(retry), return_exceptions=True))[0])
    for entry in entries:
        if "existing_id" in entry:
            plan.order.append(RankEntry(existing_id=entry["existing_id"]))
        elif id(entry) in detailed:
            plan.order.append(RankEntry(item=detailed[id(entry)], again_id=entry.get("again_id")))
        else:
            plan.candidates.append({"text": entry["text"], "kind": entry["kind"], "usable": False,
                                    "why": "这次没生成出能检查的时机和标准"})
    if failures:
        plan.error = f"有 {len(failures)} 批新动作没补全细节：{failures[0]}"
    return plan


async def _run_plan(ctx: Context) -> Plan:
    material = summary_material(ctx.summary)
    if not has_material(material):
        return Plan(note="整场总结里没有问题清单，这次没定新动作")
    gaps = find_gaps(ctx.sentences, ctx.turns)
    ctx.silence_sec = suggest_silence_sec(gaps)[0]
    return await plan_actions(ctx, [("本场", material)], silence_profile(gaps))


# ---------- 写库 ----------

def _save_checks(ctx: Context, results: list[CheckResult]) -> list[int]:
    """存核对结果（同一动作同一场只一行），按场次先后重算连续场数，满 3 场的标过关。"""
    if not results:
        return []
    passed: list[int] = []
    with store.WRITE_LOCK, SessionLocal() as db:
        now = datetime.now()
        rows: list[FocusAction] = []
        for result in results:
            action = db.get(FocusAction, result.action_id)
            if action is None:
                continue
            rechecking = action.status != "active"     # 已过关的复查：只留记录，不计连续场数
            result.baseline = rechecking or not store.acked_before(action.acked_at, ctx.turn_started_at)
            row = db.execute(select(FocusCheck).where(
                FocusCheck.action_id == result.action_id, FocusCheck.session_id == ctx.session_id,
            )).scalars().first()
            if row is None:
                row = FocusCheck(action_id=result.action_id, session_id=ctx.session_id,
                                 streamer_id=ctx.streamer_id, created_at=now)
                db.add(row)
            row.verdict = result.verdict
            row.baseline = result.baseline
            row.done = result.done
            row.total = result.total
            row.metric = store.dumps(result.metric)
            row.evidence = store.dumps(result.evidence)
            row.reason = result.reason
            row.llm_provider = result.llm_provider[:64]
            row.turn_started_at = ctx.turn_started_at
            if not rechecking:
                rows.append(action)
        db.flush()
        for action in rows:
            history = db.execute(select(
                FocusCheck.turn_started_at, FocusCheck.verdict, FocusCheck.baseline,
            ).where(FocusCheck.action_id == action.id)).all()
            action.streak = store.compute_streak(action.acked_at, history)
            if action.status == "active" and action.streak >= store.PASS_STREAK:
                action.status = "passed"
                action.passed_at = now
                action.ended_at = now
                passed.append(action.id)
        db.commit()
    return passed


def _candidate_out(index: int, item: dict) -> dict:
    usable = bool(item.get("usable", True)) and bool(item.get("criterion"))
    kind = item.get("kind") or "llm"
    return {"index": index, "text": item["text"], "why": item.get("why", ""),
            "usable": usable, "trigger": item.get("trigger", ""),
            "criterion": item.get("criterion", ""), "example": item.get("example", ""),
            "example_source": item.get("example_source") or ("AI 示范" if usable else ""),
            "kind": kind, "kind_label": store.KIND_LABELS.get(kind, kind),
            "threshold_sec": item.get("threshold_sec") if kind == "silence" else None,
            "used_by": None}


def place_new_actions(db, streamer_id: int, plan: Plan | None, *, origin_session_id: int | None,
                      now: datetime, active: list[FocusAction],
                      regressed: set[int] | None = None) -> tuple[list[FocusAction], list[dict]]:
    """按梳理结果写入新动作并重排在练动作（调用方负责事务和锁）。返回 (新建的动作, 候选)。

    落库这一刻再兜一次底：和在练的同一个问题或同一类 → 不加（原来那条排到这个位置）；人工删掉过的不再出；
    已经有不冷场动作时，带冷场字眼的话术动作不加。
    已过关的（2026-10-04 起）：不凭总结里提到同类就加回；只有本场复查没做到的（regressed）按原动作加回，排在最前。
    """
    regressed = set(regressed or ())
    by_id = {a.id: a for a in active}
    passed = db.execute(select(FocusAction).where(
        FocusAction.streamer_id == streamer_id, FocusAction.status == "passed",
    )).scalars().all()
    removed = [a for a in db.execute(select(FocusAction).where(
        FocusAction.streamer_id == streamer_id, FocusAction.status == "removed",
    )).scalars().all() if not store.is_rebuild_removed(a)]
    created: list[FocusAction] = []
    ranked: list[FocusAction] = []
    candidates: list[dict] = list(plan.candidates) if plan else []
    order = plan.order if plan else []
    has_silence = any(a.kind == "silence" for a in active) or any(
        e.item and e.item["kind"] == "silence" for e in order)
    for entry in order:
        if entry.existing_id is not None:
            action = by_id.get(entry.existing_id)
            if action is not None and action not in ranked:
                ranked.append(action)
            continue
        item = entry.item
        dup = store.duplicate_of(item, [*active, *created])
        peer = dup or store.same_category(item, [*active, *created])
        if peer is not None:
            why = (f"和在练的「{peer.text}」是同一个问题" if dup is not None
                   else f"和在练的「{peer.text}」是同一类（{item['category']}），继续练那条")
            candidates.append({**item, "usable": True, "why": why})
            if peer in by_id.values() and peer not in ranked:
                ranked.append(peer)                    # 这个问题本场还在：原来那条排到这个位置
            continue
        if (item["kind"] == "llm" and item.get("category", store.OTHER) == store.OTHER and has_silence
                and any(w in item["text"] for w in SILENCE_WORDS)):
            # 没归类的「别冷场」式话术动作：和不冷场动作重复。归了类的（比如空档讲品）管的是空档讲什么，照常加
            candidates.append({**item, "usable": True, "why": "冷场类问题由不冷场动作统一管"})
            continue
        if store.duplicate_of(item, removed) is not None:
            continue                                   # 人工删掉过的不再出
        again = next((p for p in passed if p.id == entry.again_id), None) if entry.again_id else None
        again = again or store.duplicate_of(item, passed) or store.same_category(item, passed)
        if again is not None:
            why = ("已过关的动作本场复查没做到，按原动作加回" if again.id in regressed
                   else "已过关，本场按逐字稿复查仍做到（或没出现），不加回")
            candidates.append({**item, "usable": True, "why": why})
            continue
        action = FocusAction(
            streamer_id=streamer_id, text=item["text"], trigger=item["trigger"],
            criterion=item["criterion"], example=item["example"],
            example_source=item.get("example_source") or "AI 示范", kind=item["kind"],
            threshold_sec=item["threshold_sec"], status="active", streak=0, created_by="ai",
            origin_session_id=origin_session_id, created_at=now, reopened_from=None,
            category=item["category"],
        )
        db.add(action)
        created.append(action)
        ranked.append(action)
    # 复查没做到的已过关动作：按原动作重新加回（已经有同一类在练的就不重复加）
    back: list[FocusAction] = []
    for old in passed:
        if old.id not in regressed:
            continue
        info = {"text": old.text, "kind": old.kind, "threshold_sec": old.threshold_sec,
                "category": old.category or store.OTHER}
        if store.duplicate_of(info, [*active, *created]) or store.same_category(info, [*active, *created]):
            continue
        action = FocusAction(
            streamer_id=streamer_id, text=old.text, trigger=old.trigger, criterion=old.criterion,
            example=old.example, example_source=old.example_source or "AI 示范", kind=old.kind,
            threshold_sec=old.threshold_sec, status="active", streak=0, created_by="ai",
            origin_session_id=origin_session_id, created_at=now, reopened_from=old.id,
            category=old.category,
        )
        db.add(action)
        created.append(action)
        back.append(action)
    if order or back:
        rest = [a for a in active if a not in ranked]
        for position, action in enumerate([*back, *ranked, *rest], 1):
            action.sort_order = position
    kept = [_candidate_out(index, item) for index, item in enumerate(candidates[:CANDIDATE_LIMIT])]
    return created, kept


def _finish_run(ctx: Context, plan: Plan | None, status: str, error: str) -> dict:
    """新加动作、按本场重要程度重排在练动作、留候选、写处理记录——一个事务。"""
    with store.WRITE_LOCK, SessionLocal() as db:
        now = datetime.now()
        active = store.ordered_active(db, ctx.streamer_id)
        created, kept = place_new_actions(db, ctx.streamer_id, plan, origin_session_id=ctx.session_id,
                                          now=now, active=active, regressed=ctx.regressed)
        notes = [plan.note] if plan and plan.note else []
        if len(created) > MANY_NEW:
            notes.append(f"本场新加 {len(created)} 条动作（超过 {MANY_NEW} 条），留意是不是拆得太碎")
        db.flush()
        active_after = store.ordered_active(db, ctx.streamer_id)
        if len(active_after) > store.SOFT_MAX_ACTIVE and not (plan and plan.note):
            notes.append(f"在练动作有 {len(active_after)} 条（超过 {store.SOFT_MAX_ACTIVE} 条）")
        note = "；".join(notes)
        run = db.execute(select(FocusRun).where(
            FocusRun.session_id == ctx.session_id, FocusRun.streamer_id == ctx.streamer_id,
        )).scalars().first()
        if run is None:
            run = FocusRun(session_id=ctx.session_id, streamer_id=ctx.streamer_id, created_at=now)
            db.add(run)
        run.status = status
        run.error = error
        run.note = note
        run.new_count = len(created)
        run.candidates = store.dumps(kept)
        run.turn_started_at = ctx.turn_started_at
        run.finished_at = now
        db.commit()
        next_actions = [store.action_out(a) for a in store.ordered_active(db, ctx.streamer_id)]
    if note:
        print(f"[focus] {note} session={ctx.session_id} sid={ctx.streamer_id}")
    return {"created": [a.id for a in created], "next": next_actions, "candidates": kept,
            "note": note, "processed_at": now}


def _write_block(ctx: Context, results: list[CheckResult], finish: dict,
                 status: str, reason: str) -> bool:
    """并进该主播「整场总结」data["focus"]；写不上返回 False（核对和动作已经落库，不受影响）。"""
    infos = {a.id: a for a in [*ctx.recheck, *ctx.to_check]}
    recheck_ids = {a.id for a in ctx.recheck}
    checked = []
    for r in results:
        info = infos.get(r.action_id)
        rechecked = r.action_id in recheck_ids
        checked.append({
            "rechecked": rechecked,
            "action_id": r.action_id,
            "text": info.text if info else "",
            "kind": info.kind if info else "",
            "trigger": info.trigger if info else "",
            "criterion": info.criterion if info else "",
            "verdict": r.verdict,
            "verdict_label": store.VERDICT_LABELS.get(r.verdict, r.verdict),
            "baseline": r.baseline,
            "baseline_label": (("已过关动作复查：" + ("这场没做到，已重新加回" if r.verdict == "fail"
                                                      else "这场照样做到"))
                               if rechecked else
                               "基线（主播还没确认这条，不计入连续场数）" if r.baseline else "计入连续场数"),
            "done": r.done,
            "total": r.total,
            "metric": r.metric,
            "evidence": r.evidence,
            "reason": r.reason,
        })
    block = {
        "status": status,
        "status_label": store.RUN_LABELS.get(status, status),
        "reason": reason,
        "note": finish.get("note", ""),
        "streamer": {"id": ctx.streamer_id, "name": ctx.streamer_name, "ext_id": ctx.streamer_ext_id},
        "turn_started_at": store.iso(ctx.turn_started_at),
        "checked": checked,
        "next": finish["next"],
        "new_action_ids": finish["created"],
        "candidates": finish["candidates"],
        "processed_at": store.iso(finish["processed_at"]),
    }
    try:
        stored = merge_summary_data(ctx.session_id, ctx.streamer_id,
                                    lambda data: data.__setitem__("focus", block))
    except Exception as exc:  # noqa: BLE001  报告写不上不影响已落库的核对和动作
        print(f"[focus] 结果没写进整场报告 session={ctx.session_id} sid={ctx.streamer_id}; "
              f"code={safe_exception_code(exc, prefix='FOCUS_BLOCK')}; error={safe_error_text(exc)}")
        return False
    return stored is not None


def _note_block_missing(ctx: Context) -> None:
    with store.WRITE_LOCK, SessionLocal() as db:
        run = db.execute(select(FocusRun).where(
            FocusRun.session_id == ctx.session_id, FocusRun.streamer_id == ctx.streamer_id,
        )).scalars().first()
        if run is not None:
            if run.status == "done":
                run.status = "partial"
            run.error = "；".join(x for x in (run.error, "结果没写进整场报告（核对和动作已保存）") if x)
            db.commit()


def _record_failure(session_id: int, streamer_id: int, error: str) -> bool:
    """处理出错也留一条 failed 记录，免得补漏循环反复花钱重跑；已有成果的不改成失败。"""
    with store.WRITE_LOCK, SessionLocal() as db:
        if db.get(ReplaySession, session_id) is None or db.get(Streamer, streamer_id) is None:
            return False
        run = db.execute(select(FocusRun).where(
            FocusRun.session_id == session_id, FocusRun.streamer_id == streamer_id,
        )).scalars().first()
        now = datetime.now()
        if run is None:
            run = FocusRun(session_id=session_id, streamer_id=streamer_id, created_at=now)
            db.add(run)
        elif run.status in ("done", "partial", "skipped"):
            return True
        run.status = "failed"
        run.error = error[:500]
        run.finished_at = now
        db.commit()
        return True


# ---------- 主流程 ----------

_inflight: dict[tuple[int, int], asyncio.Task] = {}


def is_inflight(session_id: int, streamer_id: int) -> bool:
    task = _inflight.get((session_id, streamer_id))
    return task is not None and not task.done()


async def process_focus(session_id: int, streamer_id: int, force: bool = False) -> dict:
    """处理一个（场次, 主播）。同一对同时只跑一份，后来的等这一份的结果。

    返回 {"status": done/partial/skipped/failed/not_ready, "recorded": 有没有留下处理记录, ...}。
    """
    key = (int(session_id), int(streamer_id))
    task = _inflight.get(key)
    if task is None or task.done():
        task = asyncio.create_task(_guarded(key[0], key[1], force))
        _inflight[key] = task

        def release(done: asyncio.Task) -> None:
            if _inflight.get(key) is done:
                _inflight.pop(key, None)

        task.add_done_callback(release)
    return await asyncio.shield(task)


async def _guarded(session_id: int, streamer_id: int, force: bool) -> dict:
    try:
        with llm_context(session_id, "focus"):
            return await _process(session_id, streamer_id, force)
    except FocusNotReady as exc:
        return {"status": "not_ready", "recorded": False, "reason": exc.args[0] if exc.args else ""}
    except Exception as exc:  # noqa: BLE001  后台任务：出错只记日志和一条 failed 记录
        reason = safe_error_text(exc, fallback="处理失败")
        print(f"[focus] 处理出错 session={session_id} sid={streamer_id}; "
              f"code={safe_exception_code(exc, prefix='FOCUS')}; error={reason}")
        recorded = False
        try:
            recorded = await asyncio.to_thread(_record_failure, session_id, streamer_id,
                                               f"处理出错：{reason}")
        except Exception as again:  # noqa: BLE001
            print(f"[focus] 失败记录也没写上 session={session_id} sid={streamer_id}; "
                  f"code={safe_exception_code(again, prefix='FOCUS')}; "
                  f"error={safe_error_text(again)}")
        return {"status": "failed", "recorded": recorded, "reason": reason}


async def _process(session_id: int, streamer_id: int, force: bool) -> dict:
    ctx = await asyncio.to_thread(_load_context, session_id, streamer_id)
    if ctx.run_status and not force:
        return {"status": ctx.run_status, "recorded": True, "existing": True}
    span = speech_span_ms(ctx.sentences, ctx.turns)
    chars = sum(len(s.text) for s in ctx.sentences)
    if span < MIN_SPAN_MS or chars < MIN_CHARS:
        reason = (f"本场有效讲话约 {span // 60000} 分钟、{chars} 字（不到 20 分钟或不到 2000 字），"
                  "不核对、不定新动作")
        finish = await asyncio.to_thread(_finish_run, ctx, None, "skipped", reason)
        if not await asyncio.to_thread(_write_block, ctx, [], finish, "skipped", reason):
            await asyncio.to_thread(_note_block_missing, ctx)
        print(f"[focus] 跳过 session={session_id} sid={streamer_id}; speech_min={span // 60000}; "
              f"chars={chars}")
        return {"status": "skipped", "recorded": True, "reason": reason}

    results, calls, failed_calls = await _run_checks(ctx)
    passed = await asyncio.to_thread(_save_checks, ctx, results)
    recheck_ids = {a.id for a in ctx.recheck}
    ctx.regressed = {r.action_id for r in results if r.action_id in recheck_ids and r.verdict == "fail"}
    if passed:                                   # 刚过关的不再算「在练」：梳理时按已过关列给模型
        moved = [a for a in ctx.active if a.id in passed]
        ctx.active = [a for a in ctx.active if a.id not in passed]
        ctx.passed = [ActionInfo(a.id, a.text, a.trigger, a.criterion, a.kind, a.threshold_sec,
                                 "passed") for a in moved] + ctx.passed
    plan = await _run_plan(ctx)
    problems = []
    if failed_calls:
        problems.append(f"有 {failed_calls}/{calls} 次核对调用没成，相关动作本场不计")
    if plan.error:
        problems.append(plan.error)
    status = "partial" if problems else "done"
    reason = "；".join(problems + ([plan.note] if plan.note else []))
    finish = await asyncio.to_thread(_finish_run, ctx, plan, status, reason)
    if not await asyncio.to_thread(_write_block, ctx, results, finish, status, reason):
        await asyncio.to_thread(_note_block_missing, ctx)
    print(f"[focus] 处理完 session={session_id} sid={streamer_id}; status={status}; "
          f"checked={len(results)}; calls={calls}; failed_calls={failed_calls}; "
          f"passed={len(passed)}; new={len(finish['created'])}; active={len(finish['next'])}; "
          f"candidates={len(finish['candidates'])}")
    return {"status": status, "recorded": True, "checked": len(results), "passed": passed,
            "created": finish["created"], "active": len(finish["next"]),
            "candidates": len(finish["candidates"]), "reason": reason}


# ---------- 补漏 ----------

_attempts: Counter = Counter()


def _since() -> datetime:
    raw = str(getattr(get_settings(), "focus_since", "") or "").strip()
    try:
        return datetime.combine(date.fromisoformat(raw[:10]), datetime.min.time())
    except ValueError:
        return datetime(2026, 9, 29)


def find_pending(limit: int = BACKFILL_MAX_PER_ROUND) -> list[tuple[int, int]]:
    """还没处理的（场次, 主播），按开播先后排；同一主播更早的场次没收尾/正在处理，更晚的先不排。"""
    since = _since()
    with SessionLocal() as db:
        sessions = {s.id: s for s in db.execute(select(ReplaySession).where(
            ReplaySession.started_at >= since, ReplaySession.session_type == SessionType.normal,
        )).scalars()}
        live_now = {int(x) for x in db.execute(select(ReplaySession.current_streamer_id).where(
            ReplaySession.status.in_(_ACTIVE_SESSION), ReplaySession.ended_at.is_(None),
            ReplaySession.current_streamer_id.is_not(None),
        )).scalars()}
        if not sessions:
            return []
        ids = list(sessions)
        firsts = db.execute(select(
            StreamerTurn.session_id, StreamerTurn.streamer_id, func.min(StreamerTurn.start_ms),
        ).where(StreamerTurn.session_id.in_(ids)).group_by(
            StreamerTurn.session_id, StreamerTurn.streamer_id)).all()
        summaries = {(int(a), int(b)) for a, b in db.execute(select(
            Critique.session_id, Critique.streamer_id,
        ).where(
            Critique.session_id.in_(ids), Critique.level == CritiqueLevel.summary,
            Critique.category == "整场总结", Critique.streamer_id.is_not(None),
        ).distinct()).all()}
        done = {(int(a), int(b)) for a, b in db.execute(select(
            FocusRun.session_id, FocusRun.streamer_id).where(FocusRun.session_id.in_(ids))).all()}
    by_streamer: dict[int, list[tuple[datetime, int]]] = defaultdict(list)
    for session_id, streamer_id, first_ms in firsts:
        if streamer_id is None:
            continue
        sess = sessions[session_id]
        start = (sess.started_at or sess.created_at) + timedelta(milliseconds=int(first_ms or 0))
        by_streamer[int(streamer_id)].append((start, int(session_id)))
    pending: list[tuple[datetime, int, int]] = []
    for streamer_id, items in by_streamer.items():
        if streamer_id in live_now:
            continue                                   # 正在直播的主播整个跳过
        for start, session_id in sorted(items):
            if (session_id, streamer_id) in done:
                continue
            if is_inflight(session_id, streamer_id):
                break                                  # 正在处理：更晚的等它做完
            sess = sessions[session_id]
            active = sess.status in _ACTIVE_SESSION and sess.ended_at is None
            ended = not active or sess.current_streamer_id != streamer_id
            if ended and (session_id, streamer_id) in summaries:
                pending.append((start, session_id, streamer_id))
                continue
            if active:
                break                                  # 这场还没收尾：更晚的场次先等着
            # 已收尾但没有整场总结（总结失败、没讲几句）：处理不了，也不挡后面的场次
    pending.sort()
    return [(session_id, streamer_id) for _start, session_id, streamer_id in pending[:limit]]


async def backfill_once(limit: int = BACKFILL_MAX_PER_ROUND) -> int:
    """补漏一轮：按时间先后处理；某位主播的一场没留下记录，这一轮就不处理他更晚的场次。"""
    pairs = await asyncio.to_thread(find_pending, limit)
    blocked: set[int] = set()
    handled = 0
    for session_id, streamer_id in pairs:
        if streamer_id in blocked:
            continue
        key = (session_id, streamer_id)
        if _attempts[key] >= BACKFILL_MAX_ATTEMPTS:
            blocked.add(streamer_id)
            continue
        _attempts[key] += 1
        result = await process_focus(session_id, streamer_id)
        if result.get("recorded"):
            handled += 1
        else:
            blocked.add(streamer_id)
            print(f"[focus] 补漏这一场没处理成 session={session_id} sid={streamer_id}; "
                  f"status={result.get('status')}; reason={result.get('reason', '')}")
    return handled


async def focus_backfill_loop(interval_sec: int = BACKFILL_INTERVAL_SEC,
                              first_delay_sec: int = BACKFILL_FIRST_DELAY_SEC) -> None:
    """应用启动后常驻：每 10 分钟补一轮。FOCUS_ENABLED=false 时空转。"""
    await asyncio.sleep(first_delay_sec)
    while True:
        try:
            if getattr(get_settings(), "focus_enabled", True):
                handled = await backfill_once()
                if handled:
                    print(f"[focus] 补漏处理了 {handled} 个（场次, 主播）")
        except Exception as exc:  # noqa: BLE001  一轮出错不影响下一轮
            print(f"[focus] 补漏这一轮出错; code={safe_exception_code(exc, prefix='FOCUS_BACKFILL')}; "
                  f"error={safe_error_text(exc)}")
        await asyncio.sleep(interval_sec)
