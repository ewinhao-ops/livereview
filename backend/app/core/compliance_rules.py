"""Unified forbid/allow compliance rules, immutable session snapshots and decisions."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import (
    ComplianceDecision,
    ComplianceRule,
    KnowledgeItem,
    LiveRoom,
    ReplaySession,
    SessionComplianceRuleSnapshot,
    SessionComplianceSnapshot,
    SessionScriptCompliancePlan,
)
from .knowledge import SEED_FORBIDDEN, forbidden_context_is_exempt


VALID_ACTIONS = {"allow", "forbid"}
ACTIVE_SCOPE_TYPES = {"all", "session", "streamer", "room"}
KNOWN_SCOPE_TYPES = ACTIVE_SCOPE_TYPES | {"product"}
_SCOPE_SPECIFICITY = {"all": 0, "product": 0, "streamer": 1, "room": 1, "session": 2}


class ComplianceRuleConflict(RuntimeError):
    pass


class ComplianceRuleInUse(RuntimeError):
    pass


class ComplianceSnapshotLocked(RuntimeError):
    pass


def normalize_keyword(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", value).strip().casefold()


def validate_scope(scope_type: str, scope_id: object = "") -> tuple[str, str]:
    kind = str(scope_type or "all").strip().lower()
    if kind not in KNOWN_SCOPE_TYPES:
        raise ValueError("scope_type 必须是 all、session、streamer、room 或 product")
    value = str(scope_id or "").strip()
    if kind == "all":
        value = ""
    elif not value:
        raise ValueError(f"{kind} 作用域必须提供 scope_id")
    if kind == "product":
        raise ValueError("产品作用域第一版暂不开放")
    return kind, value


def _rule_dict(row: ComplianceRule) -> dict:
    return {
        "id": row.id,
        "action": row.action,
        "keyword": row.keyword,
        "scope_type": row.scope_type,
        "scope_id": row.scope_id,
        "enabled": bool(row.enabled),
        "note": row.note,
        "priority": row.priority,
        "version": row.version,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
        "deleted_at": row.deleted_at.isoformat(timespec="seconds") if row.deleted_at else None,
    }


def create_rule(values: dict) -> dict:
    action = str(values.get("action") or "").lower()
    if action not in VALID_ACTIONS:
        raise ValueError("action 只能是 allow 或 forbid")
    keyword = str(values.get("keyword") or "").strip()
    normalized = normalize_keyword(keyword)
    if not normalized:
        raise ValueError("关键词或短语不能为空")
    scope_type, scope_id = validate_scope(
        values.get("scope_type", "all"), values.get("scope_id", ""),
    )
    with SessionLocal() as db:
        row = ComplianceRule(
            action=action,
            keyword=keyword,
            normalized_keyword=normalized,
            scope_type=scope_type,
            scope_id=scope_id,
            enabled=bool(values.get("enabled", True)),
            note=str(values.get("note") or "").strip(),
            priority=int(values.get("priority", 0)),
            source_type=str(values.get("source_type") or "")[:32],
            source_id=str(values.get("source_id") or "")[:64],
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ComplianceRuleConflict("同一作用域下已存在相同类型和关键词的规则") from exc
        db.refresh(row)
        return _rule_dict(row)


def get_rule(rule_id: int, *, include_deleted: bool = False) -> dict | None:
    with SessionLocal() as db:
        row = db.get(ComplianceRule, rule_id)
        if row is None or (row.deleted_at is not None and not include_deleted):
            return None
        return _rule_dict(row)


def list_rules(
    *, action: str | None = None, enabled: bool | None = None,
    scope_type: str | None = None, scope_id: str | None = None,
    page: int = 1, page_size: int = 50,
) -> dict:
    if action is not None and action not in VALID_ACTIONS:
        raise ValueError("action 只能是 allow 或 forbid")
    if scope_type is not None and scope_type not in KNOWN_SCOPE_TYPES:
        raise ValueError("无效的 scope_type")
    with SessionLocal() as db:
        stmt = select(ComplianceRule).where(ComplianceRule.deleted_at.is_(None))
        if action is not None:
            stmt = stmt.where(ComplianceRule.action == action)
        if enabled is not None:
            stmt = stmt.where(ComplianceRule.enabled.is_(enabled))
        if scope_type is not None:
            stmt = stmt.where(ComplianceRule.scope_type == scope_type)
        if scope_id is not None:
            stmt = stmt.where(ComplianceRule.scope_id == str(scope_id))
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(db.scalar(count_stmt) or 0)
        rows = db.execute(
            stmt.order_by(
                ComplianceRule.priority.desc(),
                ComplianceRule.scope_type.asc(),
                ComplianceRule.normalized_keyword.asc(),
                ComplianceRule.action.asc(),
                ComplianceRule.id.asc(),
            ).limit(page_size).offset((page - 1) * page_size)
        ).scalars().all()
        return {
            "items": [_rule_dict(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }


def patch_rule(rule_id: int, values: dict) -> dict:
    with SessionLocal() as db:
        row = db.get(ComplianceRule, rule_id)
        if row is None or row.deleted_at is not None:
            raise LookupError("合规规则不存在")
        action = str(values.get("action", row.action)).lower()
        if action not in VALID_ACTIONS:
            raise ValueError("action 只能是 allow 或 forbid")
        keyword = str(values.get("keyword", row.keyword)).strip()
        normalized = normalize_keyword(keyword)
        if not normalized:
            raise ValueError("关键词或短语不能为空")
        scope_type, scope_id = validate_scope(
            values.get("scope_type", row.scope_type),
            values.get("scope_id", row.scope_id),
        )
        row.action = action
        row.keyword = keyword
        row.normalized_keyword = normalized
        row.scope_type = scope_type
        row.scope_id = scope_id
        for field in ("enabled", "note", "priority"):
            if field in values:
                setattr(row, field, values[field])
        row.note = str(row.note or "").strip()
        row.priority = int(row.priority or 0)
        row.version += 1
        row.updated_at = datetime.now()
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ComplianceRuleConflict("同一作用域下已存在相同类型和关键词的规则") from exc
        db.refresh(row)
        return _rule_dict(row)


def delete_rule(rule_id: int) -> dict:
    with SessionLocal() as db:
        row = db.get(ComplianceRule, rule_id)
        if row is None:
            raise LookupError("合规规则不存在")
        if row.deleted_at is not None:
            return {"deleted": True, "already_deleted": True, "id": rule_id}
        used = db.execute(select(SessionComplianceRuleSnapshot.id).where(
            SessionComplianceRuleSnapshot.rule_id == rule_id,
        ).limit(1)).scalar_one_or_none()
        if used is not None:
            raise ComplianceRuleInUse("规则已被场次快照引用，不允许删除")
        row.enabled = False
        row.deleted_at = datetime.now()
        row.updated_at = row.deleted_at
        row.version += 1
        db.commit()
        return {"deleted": True, "already_deleted": False, "id": rule_id}


def _legacy_rule(db, source_id: int) -> ComplianceRule | None:  # noqa: ANN001
    return db.execute(select(ComplianceRule).where(
        ComplianceRule.source_type == "knowledge_item",
        ComplianceRule.source_id == str(source_id),
    )).scalars().first()


def sync_legacy_forbidden(db, item: KnowledgeItem) -> ComplianceRule:  # noqa: ANN001
    """Keep the old knowledge API compatible while the new table remains authoritative."""
    keyword = str(item.name or "").strip()
    normalized = normalize_keyword(keyword)
    if not normalized:
        raise ValueError("违禁词不能为空")
    source = _legacy_rule(db, item.id)
    target = db.execute(select(ComplianceRule).where(
        ComplianceRule.action == "forbid",
        ComplianceRule.normalized_keyword == normalized,
        ComplianceRule.scope_type == "all",
        ComplianceRule.scope_id == "",
    )).scalars().first()
    now = datetime.now()
    if source is not None and target is not None and source.id != target.id:
        source.enabled = False
        source.deleted_at = source.deleted_at or now
        source.updated_at = now
        source.version += 1
        source = target
    if source is None:
        source = target or ComplianceRule(
            action="forbid", keyword=keyword, normalized_keyword=normalized,
            scope_type="all", scope_id="", enabled=True, priority=0,
        )
        if target is None:
            db.add(source)
    changed = (
        source.keyword != keyword or source.normalized_keyword != normalized
        or not source.enabled or source.deleted_at is not None
    )
    source.keyword = keyword
    source.normalized_keyword = normalized
    source.enabled = True
    source.deleted_at = None
    source.source_type = "knowledge_item"
    source.source_id = str(item.id)
    source.note = item.note or source.note or ""
    if changed:
        source.version += 1
    source.updated_at = now
    db.flush()
    return source


def remove_legacy_forbidden(db, item_id: int) -> None:  # noqa: ANN001
    item = db.get(KnowledgeItem, item_id)
    normalized = normalize_keyword(item.name) if item is not None else ""
    row = _legacy_rule(db, item_id)
    if row is None and normalized:
        row = db.execute(select(ComplianceRule).where(
            ComplianceRule.action == "forbid",
            ComplianceRule.normalized_keyword == normalized,
            ComplianceRule.scope_type == "all",
            ComplianceRule.scope_id == "",
        )).scalars().first()
    if row is None or row.deleted_at is not None:
        return
    duplicates = db.execute(select(KnowledgeItem).where(
        KnowledgeItem.kind == "forbidden",
        KnowledgeItem.id != item_id,
    )).scalars().all()
    survivor = next(
        (other for other in duplicates if normalize_keyword(other.name) == normalized), None,
    )
    if survivor is not None:
        row.source_type = "knowledge_item"
        row.source_id = str(survivor.id)
        return
    row.enabled = False
    row.deleted_at = datetime.now()
    row.updated_at = row.deleted_at
    row.version += 1


def migrate_legacy_forbidden_rules() -> int:
    """Idempotently import seeded and kind=forbidden knowledge entries."""
    created = 0
    with SessionLocal() as db:
        for keyword in SEED_FORBIDDEN:
            normalized = normalize_keyword(keyword)
            exists = db.execute(select(ComplianceRule.id).where(
                ComplianceRule.action == "forbid",
                ComplianceRule.normalized_keyword == normalized,
                ComplianceRule.scope_type == "all",
                ComplianceRule.scope_id == "",
            )).scalar_one_or_none()
            if exists is None:
                db.add(ComplianceRule(
                    action="forbid", keyword=keyword, normalized_keyword=normalized,
                    scope_type="all", scope_id="", enabled=True, priority=0,
                    source_type="legacy_seed", source_id=normalized[:64],
                    note="由历史内置违禁词迁移",
                ))
                created += 1
        db.flush()
        items = db.execute(select(KnowledgeItem).where(
            KnowledgeItem.kind == "forbidden",
        ).order_by(KnowledgeItem.id)).scalars().all()
        for item in items:
            before = db.execute(select(ComplianceRule.id).where(
                ComplianceRule.action == "forbid",
                ComplianceRule.normalized_keyword == normalize_keyword(item.name),
                ComplianceRule.scope_type == "all",
                ComplianceRule.scope_id == "",
            )).scalar_one_or_none()
            sync_legacy_forbidden(db, item)
            if before is None:
                created += 1
        db.commit()
    return created


def _snapshot_dict(row: SessionComplianceSnapshot) -> dict:
    return {
        "session_id": row.session_id,
        "required_script_count": row.required_count,
        "forbid_rule_count": row.forbid_count,
        "allow_rule_count": row.allow_count,
        "snapshot_version": row.version,
        "snapshot_created_at": row.created_at.isoformat(timespec="seconds"),
        "locked": bool(row.locked_at),
    }


def get_session_snapshot(session_id: int, *, db: Session | None = None) -> dict | None:
    owns_session = db is None
    db = db or SessionLocal()
    try:
        row = db.execute(select(SessionComplianceSnapshot).where(
            SessionComplianceSnapshot.session_id == session_id,
        )).scalars().first()
        return _snapshot_dict(row) if row else None
    except SQLAlchemyError:
        # Rolling deployments and legacy maintenance tests can briefly expose the old schema.
        return None
    finally:
        if owns_session:
            db.close()


def lock_session_snapshot(session_id: int, selected_rule_ids: list[int] | None = None) -> dict:
    """Create the immutable snapshot once; concurrent/repeated starts replay it."""
    with SessionLocal() as db:
        session = db.get(ReplaySession, session_id)
        if session is None:
            raise LookupError("场次不存在")
        existing = db.execute(select(SessionComplianceSnapshot).where(
            SessionComplianceSnapshot.session_id == session_id,
        )).scalars().first()
        if existing is not None:
            return _snapshot_dict(existing)
        try:
            lineup = [str(int(value)) for value in json.loads(session.lineup or "[]")]
        except (ValueError, TypeError, json.JSONDecodeError):
            lineup = []
        if session.current_streamer_id is not None:
            lineup.append(str(session.current_streamer_id))
        selected = list(dict.fromkeys(int(value) for value in (selected_rule_ids or [])))
        scope_conditions = [ComplianceRule.scope_type == "all"]
        scope_conditions.append(
            (ComplianceRule.scope_type == "session")
            & (ComplianceRule.scope_id == str(session_id))
        )
        if session.room_id:
            room_scope_ids = {str(session.room_id)}
            room_rows = db.execute(select(LiveRoom).where(
                LiveRoom.room_id == str(session.room_id),
            )).scalars().all()
            room_scope_ids.update(str(row.id) for row in room_rows)
            scope_conditions.append(
                (ComplianceRule.scope_type == "room")
                & ComplianceRule.scope_id.in_(room_scope_ids)
            )
        if lineup:
            scope_conditions.append(
                (ComplianceRule.scope_type == "streamer")
                & ComplianceRule.scope_id.in_(set(lineup))
            )
        if selected:
            scope_conditions.append(ComplianceRule.id.in_(selected))
        rules = db.execute(select(ComplianceRule).where(
            ComplianceRule.enabled.is_(True),
            ComplianceRule.deleted_at.is_(None),
            or_(*scope_conditions),
        ).order_by(ComplianceRule.id)).scalars().all()
        found = {row.id for row in rules}
        unavailable = [rule_id for rule_id in selected if rule_id not in found]
        if unavailable:
            raise ValueError("选择的合规规则不存在、已停用或已删除：" + ",".join(map(str, unavailable)))
        required_count = int(db.scalar(select(func.count()).select_from(
            SessionScriptCompliancePlan,
        ).where(SessionScriptCompliancePlan.session_id == session_id)) or 0)
        now = datetime.now()
        snapshot = SessionComplianceSnapshot(
            session_id=session_id,
            version=1,
            required_count=required_count,
            forbid_count=sum(row.action == "forbid" for row in rules),
            allow_count=sum(row.action == "allow" for row in rules),
            created_at=now,
            locked_at=now,
        )
        db.add(snapshot)
        try:
            db.flush()
            for rule in rules:
                payload = _rule_dict(rule)
                db.add(SessionComplianceRuleSnapshot(
                    snapshot_id=snapshot.id,
                    session_id=session_id,
                    rule_id=rule.id,
                    rule_version=rule.version,
                    action=rule.action,
                    keyword=rule.keyword,
                    scope_type=rule.scope_type,
                    scope_id=rule.scope_id,
                    priority=rule.priority,
                    rule_snapshot=json.dumps(payload, ensure_ascii=False),
                ))
            db.commit()
        except IntegrityError:
            db.rollback()
            concurrent = db.execute(select(SessionComplianceSnapshot).where(
                SessionComplianceSnapshot.session_id == session_id,
            )).scalars().one()
            return _snapshot_dict(concurrent)
        return _snapshot_dict(snapshot)


def _all_occurrences(text: str, keyword: str) -> list[tuple[int, int]]:
    haystack, needle = text.casefold(), keyword.casefold()
    if not needle:
        return []
    result: list[tuple[int, int]] = []
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return result
        result.append((index, index + len(needle)))
        start = index + max(1, len(needle))


def _winner_key(item: dict) -> tuple[int, int, int, int]:
    row = item["row"]
    return (
        _SCOPE_SPECIFICITY.get(row.scope_type, 0),
        int(row.priority or 0),
        1 if row.action == "allow" else 0,
        -int(row.rule_id),
    )


def evaluate_sentence(
    session_id: int, transcript_id: int | None, text: str, at_ms: int,
) -> list[dict]:
    """Evaluate one sentence against its locked snapshot and persist safe decisions."""
    with SessionLocal() as db:
        rows = db.execute(select(SessionComplianceRuleSnapshot).where(
            SessionComplianceRuleSnapshot.session_id == session_id,
        ).order_by(SessionComplianceRuleSnapshot.id)).scalars().all()
        matches: list[dict] = []
        for row in rows:
            for start, end in _all_occurrences(text or "", row.keyword):
                matches.append({"row": row, "start": start, "end": end})
        forbids = [item for item in matches if item["row"].action == "forbid"]
        decisions: list[dict] = []
        seen: set[tuple[str, int, int]] = set()
        for forbidden in forbids:
            normalized = normalize_keyword(forbidden["row"].keyword)
            signature = (normalized, forbidden["start"], forbidden["end"])
            if signature in seen:
                continue
            seen.add(signature)
            same_forbids = [
                item for item in forbids
                if normalize_keyword(item["row"].keyword) == normalized
                and item["start"] == forbidden["start"]
            ]
            allows = [
                item for item in matches
                if item["row"].action == "allow"
                and item["start"] < forbidden["end"]
                and item["end"] > forbidden["start"]
            ]
            candidates = same_forbids + allows
            winner = max(candidates, key=_winner_key)
            context_exempt = forbidden_context_is_exempt(
                text, forbidden["start"], forbidden["row"].keyword,
            )
            exempted = context_exempt or winner["row"].action == "allow"
            winning_row = winner["row"] if not context_exempt else max(same_forbids, key=_winner_key)["row"]
            result = "context_exempted" if context_exempt else "exempted" if exempted else "violation"
            safe_summary = (
                f"sha256:{hashlib.sha256((text or '').encode('utf-8')).hexdigest()[:16]};"
                f"len:{len(text or '')}"
            )
            decision = {
                "keyword": forbidden["row"].keyword,
                "rule_id": winning_row.rule_id,
                "rule_version": winning_row.rule_version,
                "rule_scope": f"{winning_row.scope_type}:{winning_row.scope_id}",
                "exempted": exempted,
                "result": result,
                "at_ms": int(at_ms or 0),
                "safe_summary": safe_summary,
                "alert_type": "compliance",
            }
            decisions.append(decision)
            db.add(ComplianceDecision(
                session_id=session_id,
                transcript_id=transcript_id,
                **decision,
            ))
        try:
            db.commit()
        except IntegrityError:
            # Replayed transcript evaluation is idempotent; all user-visible decisions are deterministic.
            db.rollback()
        return decisions
