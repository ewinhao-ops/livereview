"""模式挖掘（2026-07-10 整改·标准自生长生产线）——数据验证是唯一裁判。

五步闭环的 ①②③ 步（纯算法+向量，零 LLM）：
  ① 片段-效果对齐：每个话术段按罗盘数据反应打效果标签（对照同场景基线，
     杜绝"憋单普遍涨人就都算好"——只有显著优于一般水平的才计正向）。
  ② 同类讲法归组：同场景段按话术向量相似度并入模式账本（PatternLedger），
     数据里有什么讲法就长出什么组，不预设。
  ③ 验证记账 + 晋升/降级：比例制（默认样本≥8 且 正向率≥70% 晋升「验证有效」；
     正向率≤30% 沉淀「反面模式」），阈值存 system_settings 可调。
标准卡生成（LLM 总结）由 review/experience 侧在晋升时触发（第②批实现）。

入口 mine_session(session_id)：收播后台调用；历史回溯脚本也调它（幂等：
同一场次的段不会重复记账——evidence 按 (session_id,start_ms) 去重）。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select

from ..db import SessionLocal
from ..models import (
    CompassPoint, Critique, CritiqueLevel, PatternLedger,
    ReplaySession, SessionType, Transcript,
)
from .safe_error import safe_error_text, safe_exception_code

# 归组相似度阈值：同场景两段话术余弦 ≥ 此值视为同一种讲法
_SIM_THRESHOLD = 0.80
# 效果判定窗口：段结束后再看这么久的数据反应（话术起效有滞后）
_AFTER_MS = 150_000
# 参与挖掘的段：场景明确（非 general）且时长不短于 45s（太短的段噪声大）
_MIN_SEG_MS = 45_000


def _setting_int(db, key: str, default: int) -> int:
    from ..models import SystemSetting
    row = db.execute(select(SystemSetting).where(SystemSetting.key == key)).scalars().first()
    try:
        return int(row.value) if row and row.value else default
    except (TypeError, ValueError):
        return default


def _setting_float(db, key: str, default: float) -> float:
    from ..models import SystemSetting
    row = db.execute(select(SystemSetting).where(SystemSetting.key == key)).scalars().first()
    try:
        return float(row.value) if row and row.value else default
    except (TypeError, ValueError):
        return default


# ---------- ① 片段-效果对齐 ----------

def _num(v) -> float | None:
    try:
        n = float(str(v).replace(",", "").replace("，", "").replace("¥", "").strip())
        return n
    except (TypeError, ValueError):
        return None


def _load_points(db, session_id: int) -> list[tuple[int, dict]]:
    pts = db.execute(select(CompassPoint).where(CompassPoint.session_id == session_id)
                     .order_by(CompassPoint.start_ms)).scalars().all()
    out = []
    for p in pts:
        try:
            d = json.loads(p.data) if p.data else {}
        except json.JSONDecodeError:
            continue
        out.append((p.start_ms, d))
    return out


def _value_at(points: list[tuple[int, dict]], ms: int, key: str) -> float | None:
    """取 <=ms 最近一个数据点的指标值。"""
    val = None
    for t, d in points:
        if t > ms:
            break
        v = _num(d.get(key))
        if v is not None:
            val = v
    return val


def seg_outcome(points: list[tuple[int, dict]], start_ms: int, end_ms: int) -> dict | None:
    """段效果账：在线变化率 + 成交增量。无罗盘数据返回 None（不记账，不瞎猜）。"""
    if not points:
        return None
    online_a = _value_at(points, start_ms, "实时在线人数")
    online_b = _value_at(points, end_ms + _AFTER_MS, "实时在线人数")
    orders_a = _value_at(points, start_ms, "成交件数")
    orders_b = _value_at(points, end_ms + _AFTER_MS, "成交件数")
    if online_a is None and orders_a is None:
        return None
    online_rate = None
    if online_a is not None and online_b is not None and online_a > 0:
        online_rate = (online_b - online_a) / online_a
    orders_delta = None
    if orders_a is not None and orders_b is not None:
        orders_delta = orders_b - orders_a
    return {"online_rate": online_rate, "orders_delta": orders_delta}


def _judge(o: dict | None, baseline_online: float) -> int | None:
    """效果判定：+1 正向 / -1 负向 / 0 中性 / None 无数据。
    对照同场景基线：在线变化要显著优于本场同场景一般水平才算正向（防"全场都在涨"误判）。"""
    if o is None:
        return None
    if (o.get("orders_delta") or 0) > 0:
        return 1
    r = o.get("online_rate")
    if r is not None:
        if r - baseline_online >= 0.05:
            return 1
        if r - baseline_online <= -0.08:
            return -1
    return 0


# ---------- ②③ 归组 + 记账 + 晋升 ----------

def _seg_text(db, session_id: int, start_ms: int, end_ms: int, cap: int = 420) -> str:
    rows = db.execute(select(Transcript).where(
        Transcript.session_id == session_id,
        Transcript.start_ms >= start_ms, Transcript.start_ms < end_ms,
    ).order_by(Transcript.start_ms)).scalars().all()
    text = "".join((t.corrected_text or t.raw_text or "") for t in rows)
    return text[:cap]


def _cos(a: list[float], b: list[float]) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return s / (na * nb) if na and nb else 0.0


async def mine_session(session_id: int) -> dict:
    """主入口：对一场已收播场次做 效果对齐→归组→记账→晋升。幂等（按场次+段去重）。"""
    from .embed import embed_texts

    stats = {"segments": 0, "recorded": 0, "new_groups": 0, "promoted": 0, "demoted": 0}
    with SessionLocal() as db:
        session = db.get(ReplaySession, session_id)
        if session is not None and session.session_type == SessionType.observation:
            return {**stats, "disabled": True}
        segs = db.execute(select(Critique).where(
            Critique.session_id == session_id,
            Critique.level == CritiqueLevel.segment,
        ).order_by(Critique.start_ms)).scalars().all()
        points = _load_points(db, session_id)
        if not segs or not points:
            return stats   # 无段/无罗盘 → 不挖（没有数据验证就不下结论）

        # 场景过滤 + 时长过滤 + 效果账
        cand = []
        for c in segs:
            try:
                d = json.loads(c.data) if c.data else {}
            except json.JSONDecodeError:
                d = {}
            scene = d.get("scene") or ""
            if not scene or scene in ("general", "compliance"):
                continue
            if (c.end_ms - c.start_ms) < _MIN_SEG_MS:
                continue
            o = seg_outcome(points, c.start_ms, c.end_ms)
            if o is None:
                continue
            cand.append((c, scene, o))
        stats["segments"] = len(cand)
        if not cand:
            return stats

        # 同场景基线：本场该场景所有段在线变化率的中位数
        from statistics import median
        by_scene_rates: dict[str, list[float]] = {}
        for _c, scene, o in cand:
            if o.get("online_rate") is not None:
                by_scene_rates.setdefault(scene, []).append(o["online_rate"])
        baselines = {k: median(v) for k, v in by_scene_rates.items() if v}

        # 文本 + 向量（批量）
        texts = [_seg_text(db, session_id, c.start_ms, c.end_ms) for c, _s, _o in cand]
        embs = await embed_texts(texts)

        min_samples = _setting_int(db, "pattern_min_samples", 8)
        rate_thr = _setting_float(db, "pattern_positive_rate", 0.7)

        ledgers = db.execute(select(PatternLedger).where(
            PatternLedger.status.in_(("observing", "validated", "negative")))).scalars().all()
        # 内存化(向量解析一次)
        mem = []
        for lg in ledgers:
            try:
                vec = json.loads(lg.centroid) if lg.centroid else None
            except json.JSONDecodeError:
                vec = None
            mem.append({"row": lg, "vec": vec})

        for (c, scene, o), text, emb in zip(cand, texts, embs):
            if not emb or not text.strip():
                continue
            verdict = _judge(o, baselines.get(scene, 0.0))
            if verdict is None:
                continue
            # 找同场景最相似组
            best, best_sim = None, 0.0
            for m in mem:
                if m["row"].scene != scene or not m["vec"]:
                    continue
                sim = _cos(emb, m["vec"])
                if sim > best_sim:
                    best, best_sim = m, sim
            ev_item = {"session_id": session_id, "start_ms": c.start_ms, "end_ms": c.end_ms,
                       "outcome": verdict, "online_rate": o.get("online_rate"),
                       "orders_delta": o.get("orders_delta")}
            if best and best_sim >= _SIM_THRESHOLD:
                lg = best["row"]
                try:
                    ev = json.loads(lg.evidence) if lg.evidence else []
                except json.JSONDecodeError:
                    ev = []
                # 幂等：同场同段不重复记账（回溯可重跑）
                if any(e.get("session_id") == session_id and e.get("start_ms") == c.start_ms for e in ev):
                    continue
                ev.append(ev_item)
                lg.evidence = json.dumps(ev, ensure_ascii=False)
                lg.sample_count += 1
                if verdict > 0:
                    lg.positive_count += 1
                # centroid 移动平均
                n = lg.sample_count
                merged = [(x * (n - 1) + y) / n for x, y in zip(best["vec"], emb)]
                lg.centroid = json.dumps(merged)
                best["vec"] = merged
                lg.updated_at = datetime.now()
                stats["recorded"] += 1
                # 晋升/降级（比例制：大部分有效才算，不是死数字）
                if lg.status == "observing" and lg.sample_count >= min_samples:
                    rate = lg.positive_count / lg.sample_count
                    if rate >= rate_thr:
                        lg.status = "validated"
                        stats["promoted"] += 1
                        stats.setdefault("promoted_ids", []).append(lg.id)
                        print(f"[pattern] ✅ 模式#{lg.id}({scene}) 晋升验证有效："
                              f"{lg.positive_count}/{lg.sample_count} 正向")
                    elif rate <= 0.3:
                        lg.status = "negative"
                        stats["demoted"] += 1
                        print(f"[pattern] ⚠️ 模式#{lg.id}({scene}) 判为反面模式："
                              f"{lg.positive_count}/{lg.sample_count} 正向")
                # 已验证的模式若后续正向率跌破 50% → 降回观察（标准不是终身制）
                elif lg.status == "validated" and lg.sample_count >= min_samples:
                    if lg.positive_count / lg.sample_count < 0.5:
                        lg.status = "observing"
                        lg.standard_card = ""
                        print(f"[pattern] 模式#{lg.id} 正向率跌破 50%，降级回观察")
            else:
                lg = PatternLedger(
                    scene=scene, centroid=json.dumps(emb), rep_text=text[:160],
                    sample_count=1, positive_count=1 if verdict > 0 else 0,
                    evidence=json.dumps([ev_item], ensure_ascii=False),
                )
                db.add(lg)
                db.flush()
                mem.append({"row": lg, "vec": emb})
                stats["new_groups"] += 1
                stats["recorded"] += 1
        db.commit()
    # ④ 标准卡：新晋升的模式由 LLM 总结成可执行标准（低频调用；标准来自数据，LLM 只负责表述）
    for pid in stats.get("promoted_ids", []):
        try:
            await generate_standard_card(pid)
        except Exception as e:  # noqa: BLE001
            print(
                f"[pattern] standard_card={pid} status=failed; "
                f"code={safe_exception_code(e, prefix='PATTERN_CARD')}; "
                f"error={safe_error_text(e)}"
            )
    stats.pop("promoted_ids", None)
    return stats


_CARD_SYS = (
    "你是直播带货教练。下面是同一种讲法的多个真实话术片段，它们经过数据验证"
    "（多场直播中大部分带来在线上涨或成交）。请总结出这套讲法的可执行标准。"
    "只输出 JSON：{\"name\":\"讲法命名(≤12字)\",\"essentials\":[\"要素1\",\"要素2\",\"要素3\"],"
    "\"how\":\"一句话操作要领\"}。要素要具体可执行，禁止空话。"
)


async def generate_standard_card(ledger_id: int) -> None:
    """晋升后把模式总结成标准卡（存 standard_card），供段评对标与前端标准库展示。"""
    from .llm import get_llm

    with SessionLocal() as db:
        lg = db.get(PatternLedger, ledger_id)
        if not lg or lg.status != "validated" or lg.standard_card:
            return
        try:
            ev = json.loads(lg.evidence) if lg.evidence else []
        except json.JSONDecodeError:
            ev = []
        # 取正向证据片段的原文（最多 5 段）
        samples: list[str] = []
        for e in ev:
            if e.get("outcome", 0) > 0 and len(samples) < 5:
                txt = _seg_text(db, e["session_id"], e["start_ms"], e["end_ms"], cap=300)
                if txt.strip():
                    samples.append(txt)
        if not samples:
            samples = [lg.rep_text]
        rate = round(lg.positive_count * 100 / max(1, lg.sample_count))
        user = f"场景：{lg.scene}；验证：{lg.sample_count} 次出现 {rate}% 正向。\n话术片段：\n" + \
               "\n---\n".join(samples)
        raw = await get_llm("deep").complete(_CARD_SYS, user, json_mode=True)
        card = json.loads(raw)
        card["sample_count"] = lg.sample_count
        card["positive_rate"] = rate
        # 最佳案例锚点（供前端跳转）
        best = [e for e in ev if e.get("outcome", 0) > 0][:3]
        card["examples"] = [{"session_id": e["session_id"], "start_ms": e["start_ms"]} for e in best]
        lg.standard_card = json.dumps(card, ensure_ascii=False)
        lg.updated_at = datetime.now()
        db.commit()
        print(f"[pattern] 📋 标准卡生成：#{ledger_id} {card.get('name','')}（{rate}% 正向）")
