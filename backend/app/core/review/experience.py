"""成功经验技巧沉淀 + 复用（D4 配套）。

闭环：compass_review 的全部变化点 → ExperienceTipSample 持续采样 → 相似动作聚类
→ 频次/占比达标后 ExperienceTip(pending) → 人工采纳 → embed 入 KnowledgeChunk。
单次归因只处于 observing，不能进入知识库。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from difflib import SequenceMatcher

from sqlalchemy import func, or_, select, update

from ..embed import embed_one
from ...db import SessionLocal
from ...models import ExperienceTip, ExperienceTipSample, KnowledgeChunk, SystemSetting
from ..scene import scene_label
from ..safe_error import safe_error_text, safe_exception_code


_DEFAULT_MIN_MATCHES = 5
_DEFAULT_MIN_RATE = 0.50
_DEFAULT_SIMILARITY = 0.80
_LEXICAL_SIMILARITY = 0.72
_SAVE_LOCK = asyncio.Lock()

_EVENT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("retention", ("停留", "留存", "观看时长")),
    ("conversion", ("转化", "点击率", "进入率")),
    ("sale", ("成交", "支付", "订单", "销量", "gmv")),
    ("online", ("在线",)),
    ("engagement", ("互动", "点赞", "评论", "涨粉", "粉丝")),
    ("traffic", ("进房", "曝光", "观看")),
)


def _setting_number(db, key: str, default: float) -> float:
    row = db.get(SystemSetting, key)
    try:
        return float(row.value) if row and row.value else default
    except (TypeError, ValueError):
        return default


def _validation_thresholds(db) -> tuple[int, float, float]:
    """返回（最少命中次数，占比门槛，向量相似度）；配置异常时使用保守默认值。"""
    min_matches = max(2, int(_setting_number(
        db, "experience_min_matches", _DEFAULT_MIN_MATCHES,
    )))
    min_rate = min(1.0, max(0.0, _setting_number(
        db, "experience_min_rate", _DEFAULT_MIN_RATE,
    )))
    similarity = min(1.0, max(0.0, _setting_number(
        db, "experience_similarity", _DEFAULT_SIMILARITY,
    )))
    return min_matches, min_rate, similarity


def _event_key(metric: str, direction: str) -> str:
    """把罗盘同义指标归到稳定统计口径；方向必须分开，下降不能稀释上涨。"""
    raw = (metric or "未分类").strip()
    lowered = raw.lower()
    group = ""
    for name, words in _EVENT_GROUPS:
        if any(word in lowered for word in words):
            group = name
            break
    if not group:
        group = "metric:" + re.sub(r"\s+", "", lowered)[:40]
    normalized_direction = "down" if direction == "down" else "up"
    return f"{group}:{normalized_direction}"


def _normalized_action(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", (text or "").lower())


def _lexical_similarity(left: str, right: str) -> float:
    a, b = _normalized_action(left), _normalized_action(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    seq = SequenceMatcher(None, a, b).ratio()
    aa = {a[i:i + 2] for i in range(max(1, len(a) - 1))}
    bb = {b[i:i + 2] for i in range(max(1, len(b) - 1))}
    jac = len(aa & bb) / len(aa | bb) if aa | bb else 0.0
    return max(seq, jac)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    ln = sum(v * v for v in left) ** 0.5
    rn = sum(v * v for v in right) ** 0.5
    return dot / (ln * rn) if ln and rn else 0.0


def _centroid(tip: ExperienceTip) -> list[float]:
    try:
        value = json.loads(tip.centroid) if tip.centroid else []
        return value if isinstance(value, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _is_reusable(candidate: dict, action: str, direction: str) -> bool:
    explicit = candidate.get("reusable")
    reusable = bool(action) if explicit is None else bool(explicit)
    verdict = (candidate.get("verdict") or "").strip()
    return reusable and direction == "up" and verdict in {"", "动作驱动"}


def _owned_chunk(db, tip_id: int) -> KnowledgeChunk | None:
    """按不可复用的业务 provenance 找切片，不能只信可能悬空/复用的 chunk_id。"""
    return db.execute(select(KnowledgeChunk).where(
        KnowledgeChunk.origin_type == "experience_tip",
        KnowledgeChunk.origin_id == tip_id,
    )).scalars().first()


def _tip_chunk_text(tip: ExperienceTip) -> str:
    """采纳后写入向量库的文本——自带「本店验证有效」标识，点评召回时天然区别于机型知识。"""
    parts = [f"【本店验证有效·{scene_label(tip.scene) if tip.scene else '通用'}】"]
    if tip.matched_count and tip.change_sample_count:
        rate = round((tip.matched_rate or 0.0) * 100)
        parts.append(
            f"统计验证：同类变化 {tip.change_sample_count} 次中命中 "
            f"{tip.matched_count} 次（{rate}%）"
        )
    if tip.action:
        parts.append(f"有效动作：{tip.action}")
    if tip.template:
        parts.append(f"可套用话术：{tip.template}")
    if tip.effect:
        parts.append(f"实测效果：{tip.effect}")
    return "；".join(parts)


async def save_candidates(candidates: list[dict], streamer_id: int | None = None) -> int:
    """持续记录变化样本、归并相似动作，并把统计达标类别晋升为 pending。

    输入应是 compass_review 的全部变化点，而不只是 reusable 候选：自然波动、
    无法归因和下降同样落 ExperienceTipSample，确保同类上涨的统计分母真实。
    为兼容旧调用，缺少 reusable/direction 的候选按“有动作的上涨点”处理。
    返回本次新晋升为待采纳的技巧类别数，而不是样本数。
    """
    prepared: list[dict] = []
    for candidate in candidates or []:
        metric = str(candidate.get("metric") or "").strip()
        if not metric:
            continue
        direction = "down" if candidate.get("direction") == "down" else "up"
        action = str(
            candidate.get("tip_action") or candidate.get("action") or ""
        ).strip()
        reusable = _is_reusable(candidate, action, direction)
        sid = candidate.get("session_id")
        at_ms = int(candidate.get("at_ms") or 0)
        event_key = _event_key(metric, direction)
        # 同一罗盘点可能让“成交金额/件数/人数”同时跳变；按统计类别+时刻合并为
        # 一次变化，避免一笔成交被重复算成三次证据。
        source_key = f"session:{sid}:{event_key}:{at_ms}"
        sample_streamer_id = candidate.get("streamer_id")
        if sample_streamer_id is None:
            sample_streamer_id = streamer_id
        prepared.append({
            "source_key": source_key,
            "session_id": sid,
            "streamer_id": sample_streamer_id,
            "scene": str(candidate.get("scene") or ""),
            "event_key": event_key,
            "metric": metric,
            "direction": direction,
            "verdict": str(candidate.get("verdict") or ""),
            "effect": str(candidate.get("effect") or ""),
            "action": action,
            "quote": str(candidate.get("quote") or ""),
            "template": str(
                candidate.get("tip_template") or candidate.get("template") or ""
            ),
            "at_ms": at_ms,
            "reusable": reusable,
            "vector": [],
        })
    deduplicated: dict[str, dict] = {}
    for item in prepared:
        previous = deduplicated.get(item["source_key"])
        if previous is None:
            deduplicated[item["source_key"]] = item
            continue
        effects = [value for value in (previous["effect"], item["effect"]) if value]
        combined_effect = "；".join(dict.fromkeys(effects))[:80]
        if item["reusable"] and not previous["reusable"]:
            item["effect"] = combined_effect
            deduplicated[item["source_key"]] = item
        else:
            previous["effect"] = combined_effect
    prepared = list(deduplicated.values())
    if not prepared:
        return 0
    assigned_sources: set[str] = set()
    source_keys = [item["source_key"] for item in prepared]
    with SessionLocal() as db:
        for start in range(0, len(source_keys), 500):
            assigned_sources.update(db.scalars(select(
                ExperienceTipSample.source_key
            ).where(
                ExperienceTipSample.source_key.in_(source_keys[start:start + 500]),
                ExperienceTipSample.tip_id.is_not(None),
            )).all())
    vector_cache: dict[str, list[float] | None] = {}
    for item in prepared:
        if not item["reusable"] or item["source_key"] in assigned_sources:
            continue
        action = item["action"]
        if action not in vector_cache:
            try:
                vector_cache[action] = await embed_one(action)
            except Exception as exc:  # noqa: BLE001
                print(
                    "[experience] embedding status=failed; "
                    f"code={safe_exception_code(exc, prefix='EXPERIENCE_EMBED')}; "
                    f"error={safe_error_text(exc)}"
                )
                vector_cache[action] = None
        item["vector"] = vector_cache[action] or []

    # 单进程内串行完成“找组→落样本→重算阈值”，避免两个收播任务同时从空账本
    # 各建一个相同动作类别。数据库唯一约束继续负责同一事件的最终幂等保护。
    async with _SAVE_LOCK:
        with SessionLocal() as db:
            min_matches, min_rate, semantic_threshold = _validation_thresholds(db)
            affected_keys: set[str] = set()
            tips_by_key: dict[str, list[ExperienceTip]] = {}

            def tips_for(event_key: str) -> list[ExperienceTip]:
                if event_key not in tips_by_key:
                    tips_by_key[event_key] = db.execute(select(ExperienceTip).where(
                        ExperienceTip.event_key == event_key,
                        ExperienceTip.status.in_((
                            "observing", "pending", "adopted", "rejected",
                        )),
                    )).scalars().all()
                return tips_by_key[event_key]

            for item in prepared:
                sample = db.execute(select(ExperienceTipSample).where(
                    ExperienceTipSample.source_key == item["source_key"],
                )).scalars().first()

                # 重跑可补全一次失败的 LLM 归因，但同一数据变化永远只计一个样本。
                if sample is not None and sample.tip_id is not None:
                    affected_keys.add(sample.event_key)
                    continue

                matched_tip = None
                if item["reusable"]:
                    best_score = 0.0
                    for tip in tips_for(item["event_key"]):
                        old_vector = _centroid(tip)
                        semantic = bool(
                            item["vector"] and old_vector
                            and len(item["vector"]) == len(old_vector)
                        )
                        score = (
                            _cosine(item["vector"], old_vector)
                            if semantic else _lexical_similarity(item["action"], tip.action)
                        )
                        threshold = semantic_threshold if semantic else _LEXICAL_SIMILARITY
                        if score >= threshold and score > best_score:
                            matched_tip, best_score = tip, score
                    if matched_tip is None:
                        matched_tip = ExperienceTip(
                            session_id=item["session_id"],
                            streamer_id=item["streamer_id"],
                            scene=item["scene"],
                            metric=item["metric"],
                            effect=item["effect"],
                            action=item["action"],
                            quote=item["quote"],
                            template=item["template"],
                            at_ms=item["at_ms"],
                            event_key=item["event_key"],
                            centroid=(json.dumps(item["vector"]) if item["vector"] else ""),
                            status="observing",
                            last_seen_at=datetime.now(),
                        )
                        db.add(matched_tip)
                        db.flush()
                        tips_for(item["event_key"]).append(matched_tip)

                if sample is None:
                    sample = ExperienceTipSample(
                        source_key=item["source_key"],
                        session_id=item["session_id"],
                        streamer_id=item["streamer_id"],
                        scene=item["scene"],
                        event_key=item["event_key"],
                        metric=item["metric"],
                        direction=item["direction"],
                        verdict=item["verdict"],
                        effect=item["effect"],
                        action=item["action"],
                        quote=item["quote"],
                        template=item["template"],
                        at_ms=item["at_ms"],
                        reusable=item["reusable"],
                    )
                    db.add(sample)
                else:
                    # 保留唯一事件身份，只补齐后续重跑得到的归因内容。
                    sample.scene = item["scene"]
                    sample.verdict = item["verdict"]
                    sample.effect = item["effect"]
                    sample.action = item["action"]
                    sample.quote = item["quote"]
                    sample.template = item["template"]
                    sample.reusable = item["reusable"]
                if matched_tip is not None:
                    previous = db.scalar(select(func.count()).select_from(
                        ExperienceTipSample
                    ).where(ExperienceTipSample.tip_id == matched_tip.id)) or 0
                    sample.tip_id = matched_tip.id
                    matched_tip.last_seen_at = datetime.now()
                    old_vector = _centroid(matched_tip)
                    if item["vector"]:
                        if old_vector and len(old_vector) == len(item["vector"]):
                            # 仅移动聚类中心；卡片正文从首个代表样本冻结，避免采纳时
                            # embed 的文本被持续采样并发改写。
                            merged = [
                                (a * previous + b) / (previous + 1)
                                for a, b in zip(old_vector, item["vector"])
                            ]
                            matched_tip.centroid = json.dumps(merged)
                        elif not old_vector:
                            matched_tip.centroid = json.dumps(item["vector"])
                affected_keys.add(item["event_key"])

            db.flush()
            promoted = 0
            for event_key in affected_keys:
                total = db.scalar(select(func.count()).select_from(
                    ExperienceTipSample
                ).where(ExperienceTipSample.event_key == event_key)) or 0
                for tip in tips_for(event_key):
                    matched = db.scalar(select(func.count()).select_from(
                        ExperienceTipSample
                    ).where(ExperienceTipSample.tip_id == tip.id)) or 0
                    rate = matched / total if total else 0.0
                    tip.matched_count = matched
                    tip.change_sample_count = total
                    tip.matched_rate = rate
                    qualified = matched >= min_matches and rate >= min_rate
                    if tip.status == "observing" and qualified:
                        transitioned = db.execute(
                            update(ExperienceTip)
                            .where(
                                ExperienceTip.id == tip.id,
                                ExperienceTip.status == "observing",
                                ExperienceTip.version == tip.version,
                            )
                            .values(status="pending")
                            .execution_options(synchronize_session=False)
                        )
                        if transitioned.rowcount == 1:
                            promoted += 1
                    elif tip.status == "pending" and not qualified:
                        # 未人工采纳前门槛始终实时生效；adopted/rejected 尊重人工结论。
                        db.execute(
                            update(ExperienceTip)
                            .where(
                                ExperienceTip.id == tip.id,
                                ExperienceTip.status == "pending",
                                ExperienceTip.version == tip.version,
                            )
                            .values(status="observing")
                            .execution_options(synchronize_session=False)
                        )
            db.commit()
            return promoted


def list_tips(status: str | None = None, session_id: int | None = None) -> list[dict]:
    """技巧类别列表；按样本关联场次，跨场聚类不再依赖单一代表 session_id。"""
    with SessionLocal() as db:
        stmt = select(ExperienceTip)
        if status:
            stmt = stmt.where(ExperienceTip.status == status)
        if session_id is not None:
            linked = select(ExperienceTipSample.tip_id).where(
                ExperienceTipSample.session_id == session_id,
                ExperienceTipSample.tip_id.is_not(None),
            )
            stmt = stmt.where(or_(
                ExperienceTip.session_id == session_id,
                ExperienceTip.id.in_(linked),
            ))
        rows = db.execute(stmt.order_by(ExperienceTip.id.desc())).scalars().all()
        min_matches, min_rate, _similarity = _validation_thresholds(db)
        result = []
        for tip in rows:
            session_count = db.scalar(select(func.count(func.distinct(
                ExperienceTipSample.session_id
            ))).where(
                ExperienceTipSample.tip_id == tip.id,
                ExperienceTipSample.session_id.is_not(None),
            )) or 0
            evidence = db.execute(select(ExperienceTipSample).where(
                ExperienceTipSample.tip_id == tip.id,
            ).order_by(ExperienceTipSample.created_at.desc()).limit(5)).scalars().all()
            result.append({
                "id": tip.id,
                "session_id": tip.session_id,
                "streamer_id": tip.streamer_id,
                "scene": tip.scene,
                "scene_label": scene_label(tip.scene) if tip.scene else "通用",
                "metric": tip.metric,
                "event_key": tip.event_key,
                "effect": tip.effect,
                "action": tip.action,
                "quote": tip.quote,
                "template": tip.template,
                "at_ms": tip.at_ms,
                "status": tip.status,
                "matched_count": tip.matched_count,
                "change_sample_count": tip.change_sample_count,
                "matched_rate": round(tip.matched_rate or 0.0, 4),
                "session_count": session_count,
                "threshold": {
                    "min_matches": min_matches,
                    "min_rate": min_rate,
                },
                "evidence": [{
                    "session_id": sample.session_id,
                    "at_ms": sample.at_ms,
                    "effect": sample.effect,
                    "quote": sample.quote,
                } for sample in evidence],
                "created_at": (
                    tip.created_at.isoformat(timespec="seconds") + "+08:00"
                    if tip.created_at else None
                ),
                "last_seen_at": (
                    tip.last_seen_at.isoformat(timespec="seconds") + "+08:00"
                    if tip.last_seen_at else None
                ),
            })
        return result


async def adopt_tip(tip_id: int) -> dict:
    """采纳并写入检索库；驳回后的技巧允许人工重新采纳。

    embed 放在事务外，但提交前用 status + version CAS 再确认审核状态，避免
    embed 期间发生的驳回被旧请求覆盖。provenance 是切片归属的唯一依据。
    """
    with SessionLocal() as db:
        tip = db.get(ExperienceTip, tip_id)
        if tip is None:
            return {"ok": False, "error": "技巧不存在"}
        owned = _owned_chunk(db, tip.id)
        if tip.status == "adopted" and owned is not None:
            # 兼容旧数据中 chunk_id 未回填或已写错的情况，修复时仍使用 CAS，
            # 不会在并发驳回后重新挂回旧切片。
            if tip.chunk_id != owned.id:
                repaired = db.execute(
                    update(ExperienceTip)
                    .where(
                        ExperienceTip.id == tip.id,
                        ExperienceTip.status == "adopted",
                        ExperienceTip.version == tip.version,
                    )
                    .values(chunk_id=owned.id)
                )
                if repaired.rowcount == 1:
                    db.commit()
            return {"ok": True, "already": True, "chunk_id": owned.id,
                    "embedded": bool(owned.embedding)}
        if tip.status == "observing":
            return {"ok": False, "error": "该技巧仍在统计验证中，尚未达到采纳门槛"}
        if tip.status in {"pending", "rejected"}:
            min_matches, min_rate, _similarity = _validation_thresholds(db)
            qualified = (
                tip.matched_count >= min_matches
                and tip.change_sample_count > 0
                and tip.matched_rate >= min_rate
            )
            if not qualified:
                if tip.status == "pending":
                    demoted = db.execute(
                        update(ExperienceTip)
                        .where(
                            ExperienceTip.id == tip.id,
                            ExperienceTip.status == "pending",
                            ExperienceTip.version == tip.version,
                        )
                        .values(status="observing")
                    )
                    if demoted.rowcount == 1:
                        db.commit()
                return {"ok": False, "error": "统计样本或命中占比未达到采纳门槛"}
        if tip.status not in {"pending", "rejected", "adopted"}:
            return {"ok": False, "error": "技巧正在被其他审核操作处理，请刷新后重试"}
        expected_status = tip.status
        expected_version = tip.version
        text = _tip_chunk_text(tip)
        scene = tip.scene or ""
    vec = await embed_one(text)          # 网络调用放事务外
    with SessionLocal() as db:
        claimed = db.execute(
            update(ExperienceTip)
            .where(
                ExperienceTip.id == tip_id,
                ExperienceTip.status == expected_status,
                ExperienceTip.version == expected_version,
            )
            .values(status="adopting", version=expected_version + 1)
        )
        if claimed.rowcount != 1:
            db.rollback()
            current = db.get(ExperienceTip, tip_id)
            if current is None:
                return {"ok": False, "error": "技巧不存在"}
            owned = _owned_chunk(db, current.id)
            if current.status == "adopted" and owned is not None:
                return {"ok": True, "already": True, "chunk_id": owned.id,
                        "embedded": bool(owned.embedding)}
            return {"ok": False, "error": "技巧状态已变化，本次采纳已取消，请刷新后重试"}

        db.expire_all()
        tip = db.get(ExperienceTip, tip_id)
        chunk = _owned_chunk(db, tip.id)
        if chunk is None:
            chunk = KnowledgeChunk(
                source="复盘沉淀", kind="distilled", category="本店经验",
                scene=scene, text=text,
                origin_type="experience_tip", origin_id=tip.id,
                embedding=json.dumps(vec) if vec else "")
            db.add(chunk)
            db.flush()
        else:
            # 兼容旧版本遗留的 provenance 切片，并保证重采纳是幂等更新。
            chunk.source = "复盘沉淀"
            chunk.kind = "distilled"
            chunk.category = "本店经验"
            chunk.scene = scene
            chunk.text = text
            if vec:
                chunk.embedding = json.dumps(vec)
        tip.status = "adopted"
        tip.adopted_at = datetime.now()
        tip.chunk_id = chunk.id
        db.commit()
        return {"ok": True, "chunk_id": chunk.id, "embedded": bool(vec)}


def reject_tip(tip_id: int) -> dict:
    """原子驳回并撤下该技巧拥有的检索切片；重复调用幂等。"""
    # 若并发采纳恰好先提交，第二轮会基于新版本再次抢占并撤下切片。
    for _attempt in range(2):
        with SessionLocal() as db:
            tip = db.get(ExperienceTip, tip_id)
            if tip is None:
                return {"ok": False, "error": "技巧不存在"}
            owned = _owned_chunk(db, tip.id)
            # 即使当前已是 rejected 也必须推进 version：可能有一次“重新采纳”
            # 正在事务外做 embed，本次重复驳回需要让它的旧 CAS 失效。
            already_rejected = (
                tip.status == "rejected" and owned is None and tip.chunk_id is None
            )
            if tip.status not in {"pending", "adopted", "rejected"}:
                return {"ok": False, "error": "技巧正在被其他审核操作处理，请刷新后重试"}
            expected_version = tip.version
            claimed = db.execute(
                update(ExperienceTip)
                .where(
                    ExperienceTip.id == tip_id,
                    ExperienceTip.status.in_(("pending", "adopted", "rejected")),
                    ExperienceTip.version == expected_version,
                )
                .values(status="rejecting", version=expected_version + 1)
            )
            if claimed.rowcount != 1:
                db.rollback()
                continue
            db.expire_all()
            tip = db.get(ExperienceTip, tip_id)
            owned = _owned_chunk(db, tip.id)
            if owned is not None:
                db.delete(owned)
            tip.status = "rejected"
            tip.chunk_id = None
            tip.adopted_at = None
            db.commit()
            result = {"ok": True}
            if already_rejected:
                result["already"] = True
            return result
    return {"ok": False, "error": "技巧状态已变化，本次驳回未执行，请刷新后重试"}
