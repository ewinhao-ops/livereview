"""罗盘多指标变化归因 + 成功动作提炼（D4）。

attribution.py 只做"成交"单指标的话术驱动判定；本模块把罗盘**全部**指标
（在线/转化率/点赞/进房/停留/GMV…）的显著变化点，对齐到当时话术段，
用 LLM 归因"什么动作带来了正向变化 / 什么导致了下滑"，并把全部变化交给
review.experience 持续采样；可复用动作只有统计达标后才进入待采纳列表。

分工：本模块负责【变化点检测(纯算法) + 话术对齐 + AI 归因】；
LLM 分批输出每个变化点的归因；展示仍限流，统计样本不截断。
"""

from __future__ import annotations

import json
import re

from sqlalchemy import select

from ..db import SessionLocal
from ..models import (
    CompassPoint, Critique, CritiqueLevel, ReplaySession, SessionType, Transcript,
)
from .llm import get_llm

# 指标按【名字特征】自动分类（键名=罗盘大屏页面标题原文，由采集脚本全等抓取上报）：
#   成交类累计 → 每一单都是事件（低单量店铺单笔常 <30%，相对阈值会漏掉核心的"话术促成下单"）；
#   含"率/占比" → 比率类，看百分点变化；含"时长" → 时长类；其余 → 绝对量。
# 2026-07-06 真实场次校准：非成交类改【滑动窗对比】——变化必须在短窗口内发生才算"事件"，
# 消除累计指标慢漂移伪事件（曾把 27 分钟累积的"累计观看+92"误报成 27 分处的单点事件，误导归因）。
_REL_THRESH = 0.30            # 绝对量：窗口内相对变化 ≥30% 记为显著
_RATE_THRESH = 1.5            # 比率：窗口内 ≥1.5 个百分点记为显著
_DUR_MIN_SEC = 20             # 时长：窗口内绝对变化 ≥20 秒才算显著
_EVENT_WINDOW_MS = 5 * 60 * 1000   # 事件窗：与 ~5 分钟前对比；命中后同窗去重
_MAX_PER_METRIC = 3           # 非成交指标每个最多留幅度 top3，避免刷屏
_MAX_SALES_EVENTS = 12        # 成交类上限放宽：每一单都值得归因
_ANALYSIS_BATCH_SIZE = 12     # 统计采样不截断，但分批归因，避免单次提示词过长

_SALES_WORDS = ("支付金额", "成交金额", "成交件数", "成交人数", "成交订单")


def _kind_of(key: str) -> str:
    # 派生比率指标（GPM=成交额÷观看×1000 等）：观看一涨它就跌，纯稀释噪音，
    # 成交和观看各自已单独检测，它不含新信息——排除出事件检测。
    if "千次" in key:
        return "derived"
    if any(w in key for w in _SALES_WORDS):
        return "sales"
    if "率" in key or "占比" in key:
        return "rate"
    if "时长" in key:
        return "duration"
    return "count"


def _min_abs_for(key: str) -> float:
    """绝对量指标的变化下限（防小值噪音：在线 2→3 是 +50% 但无意义）。"""
    if "金额" in key or "支付" in key:
        return 100
    if "客单价" in key:
        return 50
    if "点赞" in key:
        return 100
    if "曝光" in key:
        return 100
    if "观看" in key or "进入" in key or "人次" in key:
        return 30
    if "在线" in key:
        return 5      # 小直播间在线个位数，5→10 翻倍是真事件
    if "粉丝" in key:
        return 5
    if "评论" in key or "分享" in key:
        return 8
    return 5


def _loads(s):
    try:
        return (json.loads(s) or {}) if s else {}
    except json.JSONDecodeError:
        return {}


def _count_num(v) -> float | None:
    """绝对量：'12.5万/1,234' → 数；带 %/率 跳过（那是比率不是量）。"""
    s = str(v or "")
    if "%" in s or "率" in s or "％" in s:
        return None
    m = re.search(r"[0-9][0-9,]*\.?[0-9]*", s)
    if not m:
        return None
    try:
        x = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    if "亿" in s:
        x *= 1e8
    elif "万" in s:
        x *= 1e4
    return x


def _rate_num(v) -> float | None:
    """比率：'5.2%' → 5.2；纯数亦可。"""
    s = str(v or "")
    m = re.search(r"[0-9]+\.?[0-9]*", s)
    return float(m.group(0)) if m else None


def _dur_num(v) -> float | None:
    """时长：'2分钟46秒' → 166；'46秒' → 46；'2分钟' → 120。"""
    s = str(v or "")
    mm = re.search(r"([0-9]+)\s*分", s)
    ms = re.search(r"([0-9]+)\s*秒", s)
    if mm or ms:
        return (int(mm.group(1)) * 60 if mm else 0) + (int(ms.group(1)) if ms else 0)
    m = re.search(r"[0-9]+\.?[0-9]*", s)
    return float(m.group(0)) if m else None


def _series(compass, key, parse) -> list[tuple[int, float]]:
    out = []
    for p in compass:
        v = parse(_loads(p.data).get(key))
        if v is not None:
            out.append((p.start_ms, v))
    return out


def detect_changes(
    compass,
    max_per_metric: int | None = _MAX_PER_METRIC,
    max_sales_events: int | None = _MAX_SALES_EVENTS,
) -> list[dict]:
    """全指标显著变化点检测（纯算法，可复现）。

    键名不写死：遍历数据点里实际出现的指标（=罗盘页面标题原文），按名字特征分类。
    - sales（成交金额/件数/人数）：相邻点任何变化都是事件——每一单都值得归因，
      不设相对阈值（低单量店铺单笔 <30% 会被相对阈值漏掉）。
    - 其余（rate/duration/count）：与 ~5 分钟前的滑动窗基点对比，短窗内跳变才算事件；
      命中后同窗去重。累计指标的慢漂移（半小时涨 30%）不再被误报成单点事件。
    返回 [{metric, at_ms, direction:'up'|'down', from, to, delta, magnitude, kind}]。
    """
    keys: list[str] = []
    for p in compass:
        for k in _loads(p.data):
            if k not in keys:
                keys.append(k)

    changes: list[dict] = []
    for key in keys:
        kind = _kind_of(key)
        if kind == "derived":
            continue
        parse = {"rate": _rate_num, "duration": _dur_num}.get(kind, _count_num)
        ser = _series(compass, key, parse)
        if len(ser) < 2:
            continue
        found: list[dict] = []

        if kind == "sales":
            prev_v = ser[0][1]
            for at_ms, v in ser[1:]:
                if v != prev_v:
                    delta = v - prev_v
                    found.append({
                        "metric": key, "at_ms": at_ms,
                        "direction": "up" if delta > 0 else "down",
                        "from": round(prev_v, 2), "to": round(v, 2),
                        "delta": round(delta, 2),
                        "magnitude": round(abs(delta) / max(prev_v, 1), 3),
                        "kind": kind,
                    })
                prev_v = v
            if max_sales_events is not None:
                found = found[:max_sales_events]
        else:
            j = 0
            last_hit_ms = float("-inf")
            for i in range(1, len(ser)):
                at_ms, v = ser[i]
                while j < i - 1 and ser[j][0] < at_ms - _EVENT_WINDOW_MS:
                    j += 1
                base_v = ser[j][1]
                if at_ms - last_hit_ms < _EVENT_WINDOW_MS:
                    continue                      # 同一波动只报一次
                delta = v - base_v
                if kind == "rate":
                    sig = abs(delta) >= _RATE_THRESH
                    mag = abs(delta)
                elif kind == "duration":
                    if base_v <= 0:
                        continue
                    sig = abs(delta) >= _DUR_MIN_SEC and abs(delta) / base_v >= _REL_THRESH
                    mag = abs(delta) / base_v
                else:
                    if base_v <= 0:
                        continue
                    sig = (abs(delta) / base_v >= _REL_THRESH
                           and abs(delta) >= _min_abs_for(key))
                    mag = abs(delta) / base_v
                if sig:
                    found.append({
                        "metric": key, "at_ms": at_ms,
                        "direction": "up" if delta > 0 else "down",
                        "from": round(base_v, 2), "to": round(v, 2),
                        "delta": round(delta, 2), "magnitude": round(mag, 3),
                        "kind": kind,
                    })
                    last_hit_ms = at_ms
            found.sort(key=lambda c: c["magnitude"], reverse=True)
            if max_per_metric is not None:
                found = found[:max_per_metric]
        changes.extend(found)
    changes.sort(key=lambda c: c["at_ms"])
    return changes


def _fmt_effect(c: dict) -> str:
    """变化 → 人读效果串：平均在线人数 200→350(+150) / 成交转化率 2%→5%(+3pp) / 时长 +46秒。"""
    kind = c.get("kind", "count")
    sign = "+" if c["delta"] > 0 else ""
    if kind == "rate":
        return f"{c['metric']} {c['from']}%→{c['to']}%（{sign}{c['delta']}pp）"
    if kind == "duration":
        return f"{c['metric']} {int(c['from'])}秒→{int(c['to'])}秒（{sign}{int(c['delta'])}秒）"
    return f"{c['metric']} {c['from']}→{c['to']}（{sign}{c['delta']}）"


def _context_window(transcripts, at_ms, before=60000, after=15000) -> str:
    """变化点前 before / 后 after 毫秒的话术原文（判断'变化前做了什么动作'）。"""
    lines = [t.corrected_text or t.raw_text for t in transcripts
             if (at_ms - before) <= t.start_ms <= (at_ms + after)
             and (t.corrected_text or t.raw_text)]
    return "\n".join(f"- {s}" for s in lines[-12:]) or "（该时段无话术记录）"


def _streamer_at(transcripts, at_ms: int) -> int | None:
    """取变化发生前最近一句话的主播；无归属时由流水线提供场次级兜底。"""
    hit = None
    for transcript in transcripts:
        if transcript.start_ms > at_ms:
            break
        if transcript.streamer_id is not None:
            hit = transcript.streamer_id
    return hit


def _seg_at(seg_index, at_ms):
    hit = None
    for s in seg_index:
        if s[0] <= at_ms:
            hit = s
        else:
            break
    return hit


_SYSTEM = (
    "你是直播带货增长复盘专家。会给你一场直播里若干【数据变化点】——每个点标了是哪个"
    "罗盘指标(在线/转化率/点赞/进房/成交/停留…)的上升或下降、变化幅度、发生时刻，"
    "以及该时刻【前后主播的话术原文】和所处话术场景。\n"
    "你的任务：对每个变化点做因果归因，判断这个数据变化到底是不是主播的某个具体动作/话术带来的。\n"
    "规则：\n"
    "① 上升点：找出触发它的具体动作（如『抛了扣1抽福袋钩子』『报了限时价+促单』『讲到痛点人群』"
    "『上了新款』），写进 action；若前后话术看不出明确动作、更像自然波动或外部流量，verdict 标『自然波动』。\n"
    "② 下降点：找出可能的流失原因（如『连续讲参数无互动』『长时间冷场』『跳品太快』），写进 action。\n"
    "③ 只有 verdict=『动作驱动』且是上升点、且动作可复制的，才在 reusable=true 并给出可复用技巧："
    "tip_action(一句话动作)、tip_template(可套用的话术模板)。自然波动/无法归因/下降点 reusable=false。\n"
    "④ 动作要具体到'做了什么'，不要空泛说'话术好'；引用能佐证的原话到 quote。\n"
    "⑤ tip_action 会用于跨多次变化做相似归类；涉及具体产品、人群或卖点时必须保留该身份，"
    "不要概括成泛泛的『介绍产品』。\n"
    "只输出 JSON：{\"items\":[{\"idx\":变化点序号, \"verdict\":\"动作驱动|自然波动|流失\", "
    "\"action\":\"触发动作/流失原因\", \"quote\":\"佐证原话\", \"reusable\":true/false, "
    "\"tip_action\":\"可复用动作(reusable时)\", \"tip_template\":\"可套用话术模板(reusable时)\"}]}。"
)


async def analyze_compass_review(session_id: int) -> dict:
    """罗盘变化归因主入口：变化点检测 → 对齐话术 → LLM 归因。

    返回 {changes:[限流展示], samples:[全部归因样本], tip_candidates:[兼容字段], summary}。
    samples 由 review.experience 持续记账，单次候选不会直接成为 pending。
    """
    with SessionLocal() as db:
        session = db.get(ReplaySession, session_id)
        if session is not None and session.session_type == SessionType.observation:
            return {
                "changes": [], "samples": [], "tip_candidates": [],
                "summary": "观摩学习场次不执行罗盘归因。", "disabled": True,
            }
        compass = db.execute(select(CompassPoint).where(
            CompassPoint.session_id == session_id).order_by(CompassPoint.start_ms)).scalars().all()
        segs = db.execute(select(Critique).where(
            Critique.session_id == session_id,
            Critique.level == CritiqueLevel.segment).order_by(Critique.start_ms)).scalars().all()
        transcripts = db.execute(select(Transcript).where(
            Transcript.session_id == session_id).order_by(Transcript.start_ms)).scalars().all()

    if len(compass) < 2:
        return {"changes": [], "samples": [], "tip_candidates": [],
                "summary": "本场未接入罗盘书签或数据点太少，无法做数据变化归因。"}

    # 展示接口过去每指标只取 top3；统计验证必须保留全部显著事件，否则分母会被
    # 人为截断（例如一天十次在线上涨最终只记三次）。LLM 归因在下方分批完成。
    changes = detect_changes(compass, max_per_metric=None, max_sales_events=None)
    display_ids = {
        (item["metric"], item["direction"], item["at_ms"])
        for item in detect_changes(compass)
    }
    if not changes:
        return {"changes": [], "samples": [], "tip_candidates": [],
                "summary": "罗盘各指标本场无显著变化点（数据平稳）。"}

    seg_index = []
    for c in segs:
        d = _loads(c.data)
        seg_index.append((c.start_ms, d.get("scene_label") or "通用", d.get("scene", "")))

    # 组装喂 LLM 的变化点上下文
    blocks = []
    for i, c in enumerate(changes):
        seg = _seg_at(seg_index, c["at_ms"])
        scene = seg[1] if seg else "未分场景"
        c["scene_label"] = scene
        c["scene"] = seg[2] if seg else ""
        c["streamer_id"] = _streamer_at(transcripts, c["at_ms"])
        c["effect"] = _fmt_effect(c)
        ctx = _context_window(transcripts, c["at_ms"])
        blocks.append(
            f"[变化点 {i}] {'📈上升' if c['direction'] == 'up' else '📉下降'} "
            f"{c['effect']}；时刻 {c['at_ms'] // 60000}分{c['at_ms'] // 1000 % 60}秒；"
            f"话术场景：{scene}\n该时刻前后话术：\n{ctx}")
    by_idx: dict[int, dict] = {}
    analysis_failed: set[int] = set()
    try:
        llm = get_llm("deep")
    except Exception:  # noqa: BLE001
        llm = None
        analysis_failed.update(range(len(blocks)))
    if llm is not None:
        for start in range(0, len(blocks), _ANALYSIS_BATCH_SIZE):
            batch = blocks[start:start + _ANALYSIS_BATCH_SIZE]
            batch_indices = set(range(start, start + len(batch)))
            try:
                out = await llm.complete(_SYSTEM, "\n\n".join(batch), json_mode=True)
                items = (json.loads(out) or {}).get("items", [])
                parsed = {}
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        idx = int(item.get("idx"))
                    except (TypeError, ValueError):
                        continue
                    if idx in batch_indices:
                        parsed[idx] = item
                by_idx.update(parsed)
                analysis_failed.update(batch_indices - set(parsed))
            except Exception:  # noqa: BLE001
                analysis_failed.update(batch_indices)

    tip_candidates = []
    for i, c in enumerate(changes):
        it = by_idx.get(i, {})
        c["session_id"] = session_id
        c["verdict"] = it.get("verdict", "分析失败" if i in analysis_failed else "")
        c["action"] = it.get("action", "")
        c["quote"] = it.get("quote", "")
        c["reusable"] = bool(
            it.get("reusable")
            and c["direction"] == "up"
            and c["verdict"] == "动作驱动"
        )
        c["tip_action"] = it.get("tip_action", "") if c["reusable"] else ""
        c["tip_template"] = it.get("tip_template", "") if c["reusable"] else ""
        if c["reusable"]:
            tip_candidates.append({
                "session_id": session_id, "scene": c.get("scene", ""),
                "metric": c["metric"], "effect": c["effect"], "at_ms": c["at_ms"],
                "action": c["tip_action"] or c["action"],
                "quote": it.get("quote", ""),
                "template": c["tip_template"],
            })

    ups = [c for c in changes if c["direction"] == "up" and c["verdict"] == "动作驱动"]
    downs = [c for c in changes if c["direction"] == "down"]
    summary = (
        f"捕捉到 {len(changes)} 个显著数据变化点：其中 {len(ups)} 个正向变化"
        f"可归因到具体动作、{len(tip_candidates)} 个动作已纳入持续统计验证；"
        f"{len(downs)} 个下滑点提示流失风险。"
    )
    display_changes = [
        change for change in changes
        if (change["metric"], change["direction"], change["at_ms"]) in display_ids
    ]
    return {
        "changes": display_changes,
        "samples": changes,
        "tip_candidates": tip_candidates,
        "summary": summary,
    }
