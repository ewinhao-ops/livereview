"""首批改进动作重整（一次性，2026-10-03 第二版规则）。

第一版上线后，补漏按旧规则给 4 位主播定了第一批动作（7~19 条）：拆得太碎、跨场重复，还混进了价格、
违禁词、亚秒停顿这类不该出的。这里按第二版规则重做：

plan_rebuild()  只读，不写库。找出要重整的主播（有在练动作的；这些动作必须都是 AI 定的、没人确认过、
                没核对过），把每位主播在 [since, until) 之间开播、已处理过的各场总结合在一起，调快速档按新规则
                生成一份新动作（几场的问题合并考虑，不是每场各生成一份再叠加）。返回可存成 JSON 的计划。
apply_rebuild() 先核对库里的情况还和生成计划时一样（同一批旧动作、都没确认、没核对），再一个事务里：
                旧的在练动作标 removed（updated_by=首批重整：不算人工删掉，同类问题以后还能再出）、写入新动作；
                然后把新动作和候选写回这些场次报告的 focus 块（next、candidates）和处理记录的候选。
current_state() 读出这几位主播现在的在练动作，给命令行打印用。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select

from ...db import SessionLocal
from ...models import FocusAction, FocusCheck, FocusRun, ReplaySession, Streamer
from ..usage import llm_context
from . import focus
from . import focus_store as store
from .report_data import merge_summary_data

PLAN_VERSION = 2


class RebuildError(Exception):
    """库里的情况和计划对不上、或不满足重整条件：停下，不改任何东西。"""


def _day(value: str | date) -> datetime:
    day = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return datetime.combine(day, datetime.min.time())


def _label(started: datetime | None) -> str:
    return f"{started.month}-{started.day:02d} 场" if started else "某场"


def find_targets(since: str | date, until: str | date) -> list[dict]:
    """要重整的主播：有在练动作的。每位带上旧动作、在窗口内处理过的场次、不满足条件的原因。"""
    start, end = _day(since), _day(until)
    with SessionLocal() as db:
        streamer_ids = sorted(set(db.execute(select(FocusAction.streamer_id).where(
            FocusAction.status == "active")).scalars()))
        targets = []
        for streamer_id in streamer_ids:
            streamer = db.get(Streamer, streamer_id)
            old = store.ordered_active(db, streamer_id)
            ids = [a.id for a in old]
            checks = db.execute(select(func.count(FocusCheck.id)).where(
                FocusCheck.action_id.in_(ids))).scalar() if ids else 0
            problems = []
            if any(a.acked_at is not None for a in old):
                problems.append("有主播已经点过「记住了」的动作")
            if checks:
                problems.append(f"有动作已经核对过（{checks} 条核对记录）")
            if any(a.created_by != "ai" for a in old):
                problems.append("有人工加的动作")
            runs = db.execute(select(FocusRun.session_id, ReplaySession.started_at).join(
                ReplaySession, ReplaySession.id == FocusRun.session_id,
            ).where(
                FocusRun.streamer_id == streamer_id,
                ReplaySession.started_at >= start, ReplaySession.started_at < end,
            ).order_by(ReplaySession.started_at)).all()
            targets.append({
                "streamer_id": streamer_id,
                "name": streamer.name if streamer else f"主播{streamer_id}",
                "ext_id": streamer.ext_id if streamer else None,
                "old": [store.action_out(a) for a in old],
                "sessions": [{"session_id": sid, "label": _label(started)} for sid, started in runs],
                "problems": problems,
            })
    return targets


async def _plan_one(target: dict) -> dict:
    sections, gaps, issues_ctx = [], [], None
    for item in target["sessions"]:
        try:
            ctx = focus._load_context(item["session_id"], target["streamer_id"])
        except focus.FocusNotReady as exc:
            raise RebuildError(f"{target['name']}：{item['label']}读不出来（{exc}）") from None
        material = focus.summary_material(ctx.summary)
        if focus.has_material(material):
            sections.append((item["label"], material))
        gaps += focus.find_gaps(ctx.sentences, ctx.turns)
        issues_ctx = ctx
    if not sections or issues_ctx is None:
        raise RebuildError(f"{target['name']}：窗口内的场次总结里没有问题清单")
    # 合并后的上下文：旧的在练动作马上要下线，不算在练；已过关、人工删掉过的照常列给模型
    issues_ctx.active = []
    issues_ctx.to_check = []
    sessions = len(target["sessions"])
    issues_ctx.silence_sec = focus.suggest_silence_sec(gaps, sessions)[0]
    latest = target["sessions"][-1]["session_id"]
    with llm_context(latest, "focus"):
        plan = await focus.plan_actions(issues_ctx, sections, focus.silence_profile(gaps, sessions))
    if plan.error:
        raise RebuildError(f"{target['name']}：{plan.error}")
    actions = []
    for entry in plan.order:
        if entry.item is None:
            continue
        item = dict(entry.item)
        item["again_id"] = entry.again_id
        actions.append(item)
    return {
        "streamer_id": target["streamer_id"], "name": target["name"], "ext_id": target["ext_id"],
        "sessions": [s["session_id"] for s in target["sessions"]],
        "session_labels": [s["label"] for s in target["sessions"]],
        "old_active_ids": [a["id"] for a in target["old"]],
        "old": [{"id": a["id"], "text": a["text"]} for a in target["old"]],
        "actions": actions,
        "candidates": plan.candidates,
        "note": plan.note,
    }


async def plan_rebuild(since: str | date = "2026-09-29", until: str | date = "2026-10-01") -> dict:
    targets = find_targets(since, until)
    if not targets:
        raise RebuildError("没有要重整的主播（没有在练动作）")
    bad = [f"{t['name']}：{'、'.join(t['problems'])}" for t in targets if t["problems"]]
    if bad:
        raise RebuildError("这些主播不满足重整条件，需要人工决定：" + "；".join(bad))
    missing = [t["name"] for t in targets if not t["sessions"]]
    if missing:
        raise RebuildError(f"这些主播在 {since}~{until} 之间没有处理过的场次：{'、'.join(missing)}")
    plans = [await _plan_one(t) for t in targets]
    return {"version": PLAN_VERSION, "generated_at": store.iso(datetime.now()),
            "since": str(since), "until": str(until), "streamers": plans}


def sanitize_plan(plan: dict) -> list[str]:
    """按现在的规则把计划里每条动作再清洗一遍（计划可能是早先生成、规则后来又收紧过）。返回改动说明。"""
    notes: list[str] = []
    for entry in plan.get("streamers") or []:
        kept = []
        for item in entry.get("actions") or []:
            cleaned = store.clean_generated(item)
            if cleaned is None:
                notes.append(f"{entry.get('name')}：「{item.get('text')}」按现在的规则不能出，跳过")
                continue
            cleaned["again_id"] = item.get("again_id")
            for key in ("trigger", "criterion", "example"):
                if cleaned[key] != (item.get(key) or ""):
                    notes.append(f"{entry.get('name')}：「{item.get('text')}」的{ {'trigger': '时机', 'criterion': '标准', 'example': '例句'}[key] }按现在的规则删了犯规的半句")
            kept.append(cleaned)
        entry["actions"] = kept
    return notes


def _check_unchanged(db, entry: dict) -> list[FocusAction]:
    old = store.ordered_active(db, entry["streamer_id"])
    ids = sorted(a.id for a in old)
    if ids != sorted(entry["old_active_ids"]):
        raise RebuildError(f"{entry['name']}：在练动作和生成计划时不一样（计划时 {len(entry['old_active_ids'])} 条、"
                           f"现在 {len(ids)} 条），请重新预检")
    if any(a.acked_at is not None for a in old):
        raise RebuildError(f"{entry['name']}：有动作已经被确认，停下")
    if ids and db.execute(select(func.count(FocusCheck.id)).where(FocusCheck.action_id.in_(ids))).scalar():
        raise RebuildError(f"{entry['name']}：有动作已经核对过，停下")
    for session_id in entry["sessions"]:
        if db.execute(select(FocusRun.id).where(
            FocusRun.session_id == session_id, FocusRun.streamer_id == entry["streamer_id"],
        )).first() is None:
            raise RebuildError(f"{entry['name']}：场次 {session_id} 没有处理记录，停下")
    return old


def apply_rebuild(plan: dict) -> dict:
    """按计划写库。先全部核对，有一位对不上就整个不做；写完在同一个事务里再核一遍才提交。"""
    if plan.get("version") != PLAN_VERSION or not plan.get("streamers"):
        raise RebuildError("计划文件不对（版本不符或是空的）")
    sanitize_plan(plan)
    now = datetime.now()
    mark = f"{store.REBUILD_MARK}（{now:%m-%d %H:%M}，按第二版规则用 9-29、9-30 的总结合并重做）"
    written: dict[int, dict] = {}
    with store.WRITE_LOCK, SessionLocal() as db:
        olds = {entry["streamer_id"]: _check_unchanged(db, entry) for entry in plan["streamers"]}
        for entry in plan["streamers"]:
            sid = entry["streamer_id"]
            for action in olds[sid]:
                action.status = "removed"
                action.ended_at = now
                action.updated_at = now
                action.updated_by = store.REBUILD_MARK
            db.flush()
            fresh = focus.Plan(order=[focus.RankEntry(item=item, again_id=item.get("again_id"))
                                      for item in entry["actions"]],
                               candidates=entry["candidates"])
            created, kept = focus.place_new_actions(
                db, sid, fresh, origin_session_id=entry["sessions"][-1], now=now, active=[])
            db.flush()
            if len(created) != len(entry["actions"]):      # 审过的和写进去的必须一模一样
                made = {a.text for a in created}
                lost = [item["text"] for item in entry["actions"] if item["text"] not in made]
                db.rollback()
                raise RebuildError(f"{entry['name']}：计划里有 {len(lost)} 条写不进去（{'、'.join(lost)}），"
                                   "已撤销，什么都没改")
            for session_id in entry["sessions"]:
                run = db.execute(select(FocusRun).where(
                    FocusRun.session_id == session_id, FocusRun.streamer_id == sid)).scalars().one()
                run.candidates = store.dumps(kept)
                run.note = "；".join(x for x in (mark, entry.get("note") or "") if x)
                if session_id == entry["sessions"][-1]:
                    run.new_count = len(created)
            written[sid] = {"created": [a.id for a in created], "candidates": kept}
        db.flush()
        for entry in plan["streamers"]:     # 写完再核一遍：在练的正好是这次新建的
            now_active = sorted(a.id for a in store.ordered_active(db, entry["streamer_id"]))
            if now_active != sorted(written[entry["streamer_id"]]["created"]):
                db.rollback()
                raise RebuildError(f"{entry['name']}：写入后在练动作对不上，已撤销，什么都没改")
        db.commit()
        for entry in plan["streamers"]:
            written[entry["streamer_id"]]["next"] = [
                store.action_out(a) for a in store.ordered_active(db, entry["streamer_id"])]
    blocks_missing = []
    for entry in plan["streamers"]:
        result = written[entry["streamer_id"]]

        def mutate(data: dict, result=result, entry=entry) -> None:
            block = data.get("focus") if isinstance(data.get("focus"), dict) else {}
            block.update({                  # 旧备注说的是已下线的那批动作，一并换掉
                "next": result["next"], "candidates": result["candidates"],
                "new_action_ids": result["created"], "rebuilt_at": store.iso(now), "note": mark,
            })
            block.setdefault("status", "done")
            block.setdefault("checked", [])
            data["focus"] = block

        for session_id in entry["sessions"]:
            try:
                if merge_summary_data(session_id, entry["streamer_id"], mutate) is None:
                    blocks_missing.append(session_id)
            except RuntimeError:
                blocks_missing.append(session_id)
    return {"applied_at": store.iso(now), "streamers": len(plan["streamers"]),
            "created": sum(len(r["created"]) for r in written.values()),
            "blocks_missing": blocks_missing}


def current_state(streamer_ids: list[int]) -> list[dict]:
    with SessionLocal() as db:
        out = []
        for sid in streamer_ids:
            streamer = db.get(Streamer, sid)
            out.append({"streamer_id": sid, "name": streamer.name if streamer else str(sid),
                        "active": [store.action_out(a) for a in store.ordered_active(db, sid)]})
        return out


def render_actions(items: list[dict]) -> list[str]:
    lines = []
    for n, item in enumerate(items, 1):
        kind = (f"不冷场 {item.get('threshold_sec')} 秒" if item.get("kind") == "silence"
                else item.get("category") or store.OTHER)
        lines.append(f"  {n}. [{kind}] {item['text']}")
        if item.get("kind") != "silence":
            lines.append(f"     时机：{item.get('trigger', '')}")
        lines.append(f"     标准：{item.get('criterion', '')}")
        if item.get("example"):
            lines.append(f"     例句：{item['example']}")
    return lines


def render_plan(plan: dict) -> str:
    lines = [f"计划生成于 {plan.get('generated_at')}（{plan.get('since')} ~ {plan.get('until')} 开播的场次）"]
    for entry in plan["streamers"]:
        lines += ["", f"===== {entry['name']}（复盘 id {entry['streamer_id']}，业绩 id {entry['ext_id']}）："
                      f"旧 {len(entry['old_active_ids'])} 条 → 新 {len(entry['actions'])} 条；"
                      f"输入：{'、'.join(entry['session_labels'])}的总结"]
        lines += render_actions(entry["actions"])
        if entry.get("candidates"):
            lines.append("  候选（没写成动作的，供参考）：")
            lines += [f"   - {c.get('text')}（{c.get('why', '')}）" for c in entry["candidates"]]
        if entry.get("note"):
            lines.append(f"  备注：{entry['note']}")
    return "\n".join(lines)
