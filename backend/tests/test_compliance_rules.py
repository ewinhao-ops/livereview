from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core import compliance_rules as service
from app.db import Base, _install_b12_guards
from app.models import (
    ComplianceDecision,
    ComplianceRule,
    KnowledgeItem,
    ReplaySession,
    SessionComplianceRuleSnapshot,
    SessionComplianceSnapshot,
    SessionStatus,
    Streamer,
)


@pytest.fixture
def rules_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'rules.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _install_b12_guards(conn)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(service, "SessionLocal", factory)
    with factory() as db:
        streamer = Streamer(name="主播甲")
        db.add(streamer)
        db.flush()
        session = ReplaySession(
            title="规则快照", room_id="room-1", lineup=json.dumps([streamer.id]),
            current_streamer_id=streamer.id, status=SessionStatus.created,
        )
        other = ReplaySession(title="其他场次", status=SessionStatus.created)
        db.add_all([session, other])
        db.commit()
        ids = session.id, other.id, streamer.id
    try:
        yield factory, ids
    finally:
        engine.dispose()


def test_crud_constraints_versioning_stable_sort_and_duplicate(rules_db):
    _factory, (_session_id, _other_id, _streamer_id) = rules_db
    first = service.create_rule({
        "action": "forbid", "keyword": " 绝对 ", "scope_type": "all", "priority": 1,
    })
    second = service.create_rule({
        "action": "allow", "keyword": "绝对", "scope_type": "all", "priority": 3,
    })
    with pytest.raises(service.ComplianceRuleConflict):
        service.create_rule({"action": "forbid", "keyword": "绝对", "scope_type": "all"})
    with pytest.raises(ValueError):
        service.create_rule({"action": "block", "keyword": "错误"})
    with pytest.raises(ValueError):
        service.create_rule({"action": "forbid", "keyword": "  "})

    updated = service.patch_rule(first["id"], {"note": "更新", "priority": 5})
    assert updated["version"] == first["version"] + 1
    listing = service.list_rules(page=1, page_size=10)
    assert [item["id"] for item in listing["items"]] == [first["id"], second["id"]]

    deleted = service.delete_rule(second["id"])
    assert deleted == {"deleted": True, "already_deleted": False, "id": second["id"]}
    assert service.delete_rule(second["id"])["already_deleted"] is True


def test_snapshot_is_idempotent_locked_historical_and_recovers_after_restart(
    rules_db, monkeypatch: pytest.MonkeyPatch,
):
    factory, (session_id, other_id, streamer_id) = rules_db
    global_forbid = service.create_rule({
        "action": "forbid", "keyword": "绝对", "scope_type": "all", "priority": 999,
    })
    session_allow = service.create_rule({
        "action": "allow", "keyword": "绝对", "scope_type": "session",
        "scope_id": str(session_id), "priority": 0,
    })
    service.create_rule({
        "action": "allow", "keyword": "绝对", "scope_type": "session",
        "scope_id": str(other_id), "priority": 1000,
    })
    disabled = service.create_rule({
        "action": "forbid", "keyword": "停用词", "scope_type": "all", "enabled": False,
    })

    first = service.lock_session_snapshot(session_id)
    second = service.lock_session_snapshot(session_id)
    assert first == second
    assert first["locked"] is True
    assert first["forbid_rule_count"] == 1
    assert first["allow_rule_count"] == 1
    with factory() as db:
        snapshots = db.execute(select(SessionComplianceSnapshot).where(
            SessionComplianceSnapshot.session_id == session_id,
        )).scalars().all()
        assert len(snapshots) == 1
        snap_rules = db.execute(select(SessionComplianceRuleSnapshot).where(
            SessionComplianceRuleSnapshot.session_id == session_id,
        )).scalars().all()
        assert {row.rule_id for row in snap_rules} == {global_forbid["id"], session_allow["id"]}
        assert disabled["id"] not in {row.rule_id for row in snap_rules}

    result = service.evaluate_sentence(session_id, None, "这个效果绝对很好", 12_000)
    assert len(result) == 1
    assert result[0]["result"] == "exempted"
    assert result[0]["rule_id"] == session_allow["id"]

    # Rules created after locking cannot affect history.
    service.create_rule({"action": "forbid", "keyword": "后来词", "scope_type": "all"})
    assert service.evaluate_sentence(session_id, None, "命中后来词", 13_000) == []
    patched = service.patch_rule(session_allow["id"], {"priority": 100})
    assert patched["version"] == 2
    with factory() as db:
        historical = db.execute(select(SessionComplianceRuleSnapshot).where(
            SessionComplianceRuleSnapshot.session_id == session_id,
            SessionComplianceRuleSnapshot.rule_id == session_allow["id"],
        )).scalars().one()
        assert historical.rule_version == 1
    with pytest.raises(service.ComplianceRuleInUse):
        service.delete_rule(global_forbid["id"])

    # Simulate a service restart: construct a new engine/session factory from the DB file.
    restarted_engine = create_engine(
        str(factory.kw["bind"].url), connect_args={"check_same_thread": False},
    )
    restarted_factory = sessionmaker(
        restarted_engine, autoflush=False, expire_on_commit=False,
    )
    monkeypatch.setattr(service, "SessionLocal", restarted_factory)
    try:
        assert service.get_session_snapshot(session_id) == first
        recovered = service.evaluate_sentence(
            session_id, None, "重启后这个效果绝对很好", 14_000,
        )
        assert recovered[0]["result"] == "exempted"
        assert recovered[0]["rule_id"] == session_allow["id"]
    finally:
        restarted_engine.dispose()


def test_priority_allow_tie_negation_and_safe_decision_record(rules_db):
    factory, (session_id, _other_id, _streamer_id) = rules_db
    service.create_rule({
        "action": "forbid", "keyword": "最低价", "scope_type": "session",
        "scope_id": str(session_id), "priority": 8,
    })
    allow = service.create_rule({
        "action": "allow", "keyword": "最低价", "scope_type": "session",
        "scope_id": str(session_id), "priority": 8,
    })
    service.create_rule({"action": "forbid", "keyword": "绝对", "scope_type": "all"})
    service.lock_session_snapshot(session_id)

    tied = service.evaluate_sentence(session_id, None, "这是最低价", 100)
    assert tied[0]["result"] == "exempted"
    assert tied[0]["rule_id"] == allow["id"]
    negated = service.evaluate_sentence(session_id, None, "这个并非绝对", 200)
    assert negated[0]["result"] == "context_exempted"
    with factory() as db:
        rows = db.execute(select(ComplianceDecision).where(
            ComplianceDecision.session_id == session_id,
        )).scalars().all()
        assert rows
        assert all("这是最低价" not in row.safe_summary for row in rows)
        assert all(row.safe_summary.startswith("sha256:") for row in rows)


def test_legacy_forbidden_migration_is_idempotent_and_records_source(rules_db):
    factory, _ids = rules_db
    with factory() as db:
        db.add_all([
            KnowledgeItem(kind="forbidden", name="历史违禁词", note="旧接口"),
            KnowledgeItem(kind="forbidden", name="历史违禁词", note="重复旧数据"),
        ])
        db.commit()

    service.migrate_legacy_forbidden_rules()
    service.migrate_legacy_forbidden_rules()
    with factory() as db:
        rows = db.execute(select(ComplianceRule).where(
            ComplianceRule.normalized_keyword == service.normalize_keyword("历史违禁词"),
            ComplianceRule.scope_type == "all",
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].enabled is True
        assert rows[0].source_type == "knowledge_item"
