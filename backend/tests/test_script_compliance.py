from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api import script_compliance as api
from app.core import script_compliance as service
from app.db import Base, _install_script_compliance_guards
from app.models import (
    ReplaySession,
    ScriptComplianceOccurrence,
    ScriptComplianceResult,
    ScriptFrequencyRule,
    SessionScriptCompliancePlan,
    SessionStatus,
    Streamer,
    StreamerTurn,
    Transcript,
)


@pytest.fixture
def compliance_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'script-compliance.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _install_script_compliance_guards(conn)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(service, "SessionLocal", factory)
    try:
        yield factory
    finally:
        engine.dispose()


def _seed_session(factory, *, duration_sec: int = 1200):  # noqa: ANN001
    with factory() as db:
        streamer = Streamer(name="脱敏测试主播")
        db.add(streamer)
        db.flush()
        session = ReplaySession(
            title="脱敏规定话术测试",
            lineup=json.dumps([streamer.id]),
            current_streamer_id=streamer.id,
            status=SessionStatus.done,
            started_at=datetime.now() - timedelta(seconds=duration_sec),
            ended_at=datetime.now(),
        )
        db.add(session)
        db.flush()
        db.add(StreamerTurn(
            session_id=session.id,
            streamer_id=streamer.id,
            seq=0,
            start_ms=0,
            end_ms=duration_sec * 1000,
        ))
        db.commit()
        return session.id, streamer.id


def _rule_values(**overrides) -> dict:  # noqa: ANN003
    values = {
        "name": "欢迎语频次规则",
        "standard_script": "欢迎来到测试直播间今天为大家准备了专属福利",
        "repeat_interval_sec": 600,
        "target_duration_sec": 10,
        "interval_tolerance_sec": 30,
        "duration_tolerance_sec": 2,
        "similarity_threshold": 0.8,
        "effective_scope": {"type": "all"},
        "enabled": True,
    }
    values.update(overrides)
    return values


def _add_two_executions(
    factory, session_id: int, streamer_id: int, *, second_start_ms: int = 700_000,
    second_duration_ms: int = 10_000,
) -> None:  # noqa: ANN001
    parts = [
        (100_000, 105_000, "欢迎来到测试直播间"),
        (105_000, 110_000, "今天为大家准备了专属福利"),
        (second_start_ms, second_start_ms + second_duration_ms // 2, "欢迎来到测试直播间"),
        (second_start_ms + second_duration_ms // 2,
         second_start_ms + second_duration_ms, "今天为大家准备了专属福利"),
    ]
    with factory() as db:
        for seq, (start, end, text) in enumerate(parts):
            db.add(Transcript(
                session_id=session_id,
                streamer_id=streamer_id,
                seq=seq,
                start_ms=start,
                end_ms=end,
                raw_text=text,
                corrected_text=text,
            ))
        db.commit()


async def _matching_embeddings(texts: list[str]) -> list[list[float]]:
    return [[1.0, 0.0] for _ in texts]


def _add_single_execution(
    factory, session_id: int, streamer_id: int,  # noqa: ANN001
    *, first_text: str = "欢迎来到测试直播间", second_text: str = "今天为大家准备了专属福利",
    start_ms: int = 100_000, end_ms: int = 110_000,
) -> None:
    middle_ms = start_ms + (end_ms - start_ms) // 2
    with factory() as db:
        db.add_all([
            Transcript(
                session_id=session_id, streamer_id=streamer_id, seq=0,
                start_ms=start_ms, end_ms=middle_ms,
                raw_text=first_text, corrected_text=first_text,
            ),
            Transcript(
                session_id=session_id, streamer_id=streamer_id, seq=1,
                start_ms=middle_ms, end_ms=end_ms,
                raw_text=second_text, corrected_text=second_text,
            ),
        ])
        db.commit()


def test_database_constraints_disabled_assignment_and_used_rule_delete_guard(
    compliance_db,
) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory)
    with factory() as db:
        invalid_values = (
            {"repeat_interval_sec": 0, "target_duration_sec": 1,
             "similarity_threshold": 0.5},
            {"repeat_interval_sec": 1, "target_duration_sec": 0,
             "similarity_threshold": 0.5},
            {"repeat_interval_sec": 1, "target_duration_sec": 1,
             "similarity_threshold": 1.01},
        )
        for values in invalid_values:
            db.add(ScriptFrequencyRule(
                name="invalid", standard_script="text",
                interval_tolerance_sec=0, duration_tolerance_sec=0,
                effective_scope='{"type":"all"}', enabled=True, version=1,
                **values,
            ))
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()

    disabled = service.create_rule(_rule_values(enabled=False))
    with factory() as db:
        db.add(SessionScriptCompliancePlan(
            session_id=session_id,
            streamer_id=streamer_id,
            rule_id=disabled["id"],
            rule_version=disabled["version"],
            rule_snapshot=json.dumps(disabled, ensure_ascii=False),
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    active = service.create_rule(_rule_values(name="已分配规则"))
    service.assign_rules(session_id, streamer_id, [active["id"]])
    with factory() as db:
        with pytest.raises(IntegrityError):
            db.execute(delete(ScriptFrequencyRule).where(
                ScriptFrequencyRule.id == active["id"],
            ))
        db.rollback()
    with pytest.raises(service.ScriptRuleInUse):
        service.delete_rule(active["id"])


def test_plan_is_idempotent_and_snapshot_does_not_change_after_rule_update(
    compliance_db,
) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory)
    rule = service.create_rule(_rule_values())
    first = service.assign_rules(session_id, streamer_id, [rule["id"]])
    second = service.assign_rules(session_id, streamer_id, [rule["id"], rule["id"]])
    assert first[0]["id"] == second[0]["id"]
    assert first[0]["rule_version"] == 1

    updated = service.patch_rule(rule["id"], {
        "standard_script": "修改后的规定话术不会覆盖历史场次",
    })
    assert updated["version"] == 2
    plan = service.list_plans(session_id)[0]
    assert plan["rule_version"] == 1
    assert plan["rule_snapshot"]["standard_script"] == rule["standard_script"]
    with factory() as db:
        assert db.query(SessionScriptCompliancePlan).count() == 1


def test_rule_versions_include_current_and_used_session_snapshots(compliance_db) -> None:
    session_id, streamer_id = _seed_session(compliance_db)
    rule = service.create_rule(_rule_values())
    first = service.assign_rules(session_id, streamer_id, [rule["id"]])[0]
    updated = service.patch_rule(rule["id"], {"name": "欢迎语新版"})

    versions = service.list_rule_versions(rule["id"])

    assert [item["version"] for item in versions] == [updated["version"], first["rule_version"]]
    assert versions[0]["snapshot"]["name"] == "欢迎语新版"
    assert versions[1]["snapshot"]["name"] == rule["name"]


def test_mutable_auto_saved_draft_accepts_plan_before_final_lineup(compliance_db) -> None:
    with compliance_db() as db:
        streamer = Streamer(name="稍后选择的主播")
        session = ReplaySession(title="自动保存草稿", lineup="[]", status=SessionStatus.created)
        db.add_all([streamer, session]); db.commit()
        streamer_id, session_id = streamer.id, session.id
    rule = service.create_rule(_rule_values())

    plans = service.assign_rules(session_id, streamer_id, [rule["id"]])

    assert plans[0]["streamer_id"] == streamer_id


def test_stopped_rule_cannot_be_newly_assigned(compliance_db) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory)
    rule = service.create_rule(_rule_values(enabled=False))
    with pytest.raises(service.ScriptRuleNotEligible):
        service.assign_rules(session_id, streamer_id, [rule["id"]])


def test_session_can_assign_multiple_rules_for_its_streamer(compliance_db) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory)
    first = service.create_rule(_rule_values(name="欢迎语"))
    second = service.create_rule(_rule_values(name="优惠说明", repeat_interval_sec=300))
    plans = service.assign_rules(session_id, streamer_id, [first["id"], second["id"]])
    assert len(plans) == 2
    assert {plan["rule_id"] for plan in plans} == {first["id"], second["id"]}
    assert {plan["streamer_id"] for plan in plans} == {streamer_id}


def test_exact_script_is_recognized_as_matched(compliance_db) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory, duration_sec=600)
    _add_single_execution(factory, session_id, streamer_id)
    with factory() as db:
        transcripts = db.query(Transcript).order_by(Transcript.seq).all()
        turns = db.query(StreamerTurn).all()

    result = asyncio.run(service.scan_transcripts(
        transcripts, _rule_values(repeat_interval_sec=600), turns=turns,
        streamer_id=streamer_id, embedder=_matching_embeddings,
    ))

    assert result["expected_count"] == 1
    assert result["actual_count"] == 1
    assert result["matched_count"] == 1
    assert result["occurrences"][0]["status"] == "matched"
    assert result["occurrences"][0]["similarity_percent"] == 100.0


def test_semantically_similar_paraphrase_is_recognized_from_embedding_similarity(
    compliance_db,
) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory, duration_sec=600)
    paraphrase = "感谢各位来到测试直播间今天给大家安排了专享优惠"
    _add_single_execution(
        factory, session_id, streamer_id,
        first_text="感谢各位来到测试直播间",
        second_text="今天给大家安排了专享优惠",
    )
    embedded_inputs: list[str] = []

    async def semantic_embeddings(texts: list[str]) -> list[list[float]]:
        embedded_inputs.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    with factory() as db:
        transcripts = db.query(Transcript).order_by(Transcript.seq).all()
        turns = db.query(StreamerTurn).all()
    result = asyncio.run(service.scan_transcripts(
        transcripts, _rule_values(repeat_interval_sec=600), turns=turns,
        streamer_id=streamer_id, embedder=semantic_embeddings,
    ))

    assert result["matched_count"] == 1
    assert any(paraphrase in value.replace(" ", "") for value in embedded_inputs[1:])
    assert result["occurrences"][0]["similarity_passed"] is True


def test_cross_segment_normal_frequency_and_adjacent_runs_do_not_reuse_transcripts(
    compliance_db,
) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory)
    _add_two_executions(factory, session_id, streamer_id)
    with factory() as db:
        transcripts = db.query(Transcript).order_by(Transcript.seq).all()
        turns = db.query(StreamerTurn).all()
    result = asyncio.run(service.scan_transcripts(
        transcripts,
        _rule_values(),
        turns=turns,
        streamer_id=streamer_id,
        embedder=_matching_embeddings,
    ))
    actual = [item for item in result["occurrences"] if item["actual_start_ms"] is not None]
    assert result["expected_count"] == 2
    assert result["actual_count"] == 2
    assert result["matched_count"] == 2
    assert result["missing_count"] == 0
    assert [item["transcript_ids"] for item in actual] == [
        [transcripts[0].id, transcripts[1].id],
        [transcripts[2].id, transcripts[3].id],
    ]
    assert not (set(actual[0]["transcript_ids"]) & set(actual[1]["transcript_ids"]))


def test_one_execution_can_report_late_and_too_short_together(compliance_db) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory)
    _add_two_executions(
        factory, session_id, streamer_id, second_start_ms=850_000, second_duration_ms=4_000,
    )
    with factory() as db:
        transcripts = db.query(Transcript).order_by(Transcript.seq).all()
        turns = db.query(StreamerTurn).all()
    result = asyncio.run(service.scan_transcripts(
        transcripts, _rule_values(), turns=turns, streamer_id=streamer_id,
        embedder=_matching_embeddings,
    ))
    actual = [item for item in result["occurrences"] if item["actual_start_ms"] is not None]
    assert {"late", "too_short"}.issubset(set(actual[1]["issues"]))
    assert actual[1]["duration_passed"] is False
    assert actual[1]["frequency_passed"] is False


def test_execution_longer_than_allowed_is_reported_too_long(compliance_db) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory, duration_sec=600)
    _add_single_execution(
        factory, session_id, streamer_id, start_ms=100_000, end_ms=120_000,
    )
    with factory() as db:
        transcripts = db.query(Transcript).order_by(Transcript.seq).all()
        turns = db.query(StreamerTurn).all()
    result = asyncio.run(service.scan_transcripts(
        transcripts, _rule_values(repeat_interval_sec=600), turns=turns,
        streamer_id=streamer_id, embedder=_matching_embeddings,
    ))

    actual = [item for item in result["occurrences"] if item["actual_start_ms"] is not None]
    assert len(actual) == 1
    assert "too_long" in actual[0]["issues"]
    assert actual[0]["actual_duration_sec"] == 20.0
    assert actual[0]["duration_passed"] is False


def test_whole_required_period_without_script_is_reported_missing(compliance_db) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory, duration_sec=1_200)
    _add_single_execution(
        factory, session_id, streamer_id,
        first_text="今天室外温度比较舒适",
        second_text="稍后继续介绍测试商品",
    )
    embedding_called = False

    async def should_not_embed(_texts: list[str]) -> list[list[float]]:
        nonlocal embedding_called
        embedding_called = True
        return [[1.0, 0.0]]

    with factory() as db:
        transcripts = db.query(Transcript).order_by(Transcript.seq).all()
        turns = db.query(StreamerTurn).all()
    result = asyncio.run(service.scan_transcripts(
        transcripts, _rule_values(), turns=turns,
        streamer_id=streamer_id, embedder=should_not_embed,
    ))

    assert embedding_called is False
    assert result["expected_count"] == 2
    assert result["actual_count"] == 0
    assert result["matched_count"] == 0
    assert result["missing_count"] == 2
    assert [item["status"] for item in result["occurrences"]] == ["missing", "missing"]


def test_low_semantic_similarity_is_not_completed_and_embedding_failure_is_explicit(
    compliance_db,
) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory, duration_sec=600)
    _add_two_executions(factory, session_id, streamer_id)
    with factory() as db:
        transcripts = db.query(Transcript).filter(Transcript.seq < 2).all()
        turns = db.query(StreamerTurn).all()

    async def low_embeddings(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0], *[[0.6, 0.8] for _ in texts[1:]]]

    result = asyncio.run(service.scan_transcripts(
        transcripts, _rule_values(), turns=turns, streamer_id=streamer_id,
        embedder=low_embeddings,
    ))
    assert result["matched_count"] == 0
    assert result["missing_count"] == 1
    assert any(item["status"] == "low_similarity" for item in result["occurrences"])

    async def unavailable(_texts: list[str]) -> list[list[float]]:
        return []

    with pytest.raises(service.EmbeddingUnavailable):
        asyncio.run(service.scan_transcripts(
            transcripts, _rule_values(), turns=turns, streamer_id=streamer_id,
            embedder=unavailable,
        ))

    async def malformed(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0], *[[1.0] for _ in texts[1:]]]

    with pytest.raises(service.EmbeddingUnavailable):
        asyncio.run(service.scan_transcripts(
            transcripts, _rule_values(), turns=turns, streamer_id=streamer_id,
            embedder=malformed,
        ))


def test_analyze_is_idempotent_concurrent_safe_and_failed_run_can_retry(
    compliance_db, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory, duration_sec=600)
    _add_two_executions(factory, session_id, streamer_id)
    rule = service.create_rule(_rule_values(repeat_interval_sec=600))
    plan = service.assign_rules(session_id, streamer_id, [rule["id"]])[0]
    calls = 0
    gate = asyncio.Event()

    async def gated_embeddings(texts: list[str]) -> list[list[float]]:
        nonlocal calls
        calls += 1
        await gate.wait()
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(service, "embed_texts", gated_embeddings)

    async def concurrent_scenario():
        first = asyncio.create_task(service.analyze_session(session_id))
        await asyncio.sleep(0)
        with pytest.raises(service.ScriptAnalysisInProgress):
            await service.analyze_session(session_id)
        gate.set()
        completed = await first
        repeated = await service.analyze_session(session_id)
        return completed, repeated

    completed, repeated = asyncio.run(concurrent_scenario())
    assert calls == 1
    assert completed["results"][0]["id"] == repeated["results"][0]["id"]
    assert completed["results"][0]["plan_id"] == plan["id"]
    with factory() as db:
        assert db.query(ScriptComplianceResult).count() == 1

    # A failed result retains a safe reason and is reused on retry rather than duplicated.
    with factory() as db:
        result = db.query(ScriptComplianceResult).one()
        result.status = "failed"
        result.analysis_run_id = ""
        result.error_code = "EMBEDDING_UNAVAILABLE"
        result.error_msg = "Embedding 服务不可用"
        db.commit()
        existing_id = result.id
    monkeypatch.setattr(service, "embed_texts", _matching_embeddings)
    retried = asyncio.run(service.analyze_session(session_id))
    assert retried["results"][0]["id"] == existing_id
    assert retried["results"][0]["status"] == "ready"


def test_multiple_rules_are_analyzed_in_one_session_without_result_collisions(
    compliance_db, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory, duration_sec=600)
    _add_single_execution(factory, session_id, streamer_id)
    first = service.create_rule(_rule_values(name="完整欢迎语", repeat_interval_sec=600))
    second = service.create_rule(_rule_values(
        name="开场欢迎短句",
        standard_script="欢迎来到测试直播间",
        repeat_interval_sec=600,
        target_duration_sec=5,
        duration_tolerance_sec=1,
    ))
    plans = service.assign_rules(
        session_id, streamer_id, [first["id"], second["id"]],
    )
    monkeypatch.setattr(service, "embed_texts", _matching_embeddings)

    analyzed = asyncio.run(service.analyze_session(session_id))

    assert len(plans) == 2
    assert len(analyzed["results"]) == 2
    assert {result["plan_id"] for result in analyzed["results"]} == {
        plan["id"] for plan in plans
    }
    assert all(result["status"] == "ready" for result in analyzed["results"])
    assert all(result["matched_count"] == 1 for result in analyzed["results"])
    with factory() as db:
        assert db.query(ScriptComplianceResult).count() == 2


def test_no_transcript_returns_not_analyzable_without_creating_result(compliance_db) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory)
    rule = service.create_rule(_rule_values())
    service.assign_rules(session_id, streamer_id, [rule["id"]])
    with pytest.raises(service.ScriptNotAnalyzable):
        asyncio.run(service.analyze_session(session_id))
    with factory() as db:
        assert db.query(ScriptComplianceResult).count() == 0


def test_real_analysis_failure_is_saved_safely_and_same_result_can_retry(
    compliance_db, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory, duration_sec=600)
    _add_two_executions(factory, session_id, streamer_id)
    sensitive_script = "欢迎来到测试直播间今天为大家准备了专属福利"
    sensitive_provider_error = "sk-sensitive-test-value 完整逐字稿正文"
    rule = service.create_rule(_rule_values(standard_script=sensitive_script))
    plan = service.assign_rules(session_id, streamer_id, [rule["id"]])[0]

    async def failed_embeddings(_texts: list[str]) -> list[list[float]]:
        raise RuntimeError(sensitive_provider_error)

    monkeypatch.setattr(service, "embed_texts", failed_embeddings)
    with caplog.at_level(logging.WARNING, logger=service.__name__):
        with pytest.raises(RuntimeError):
            asyncio.run(service.analyze_session(session_id))

    with factory() as db:
        failed = db.query(ScriptComplianceResult).one()
        result_id = failed.id
        assert failed.plan_id == plan["id"]
        assert failed.status == "failed"
        assert failed.analysis_run_id == ""
        assert failed.error_code.startswith("SCRIPT_ANALYSIS_")
        assert failed.error_msg == "规定话术分析失败，请稍后重试"
    log_output = caplog.text
    assert sensitive_script not in log_output
    assert sensitive_provider_error not in log_output
    assert "sk-sensitive-test-value" not in log_output

    monkeypatch.setattr(service, "embed_texts", _matching_embeddings)
    retried = asyncio.run(service.analyze_session(session_id))
    assert retried["results"][0]["id"] == result_id
    assert retried["results"][0]["status"] == "ready"
    with factory() as db:
        assert db.query(ScriptComplianceResult).count() == 1


def test_embedding_abnormal_response_never_writes_fabricated_success(
    compliance_db, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory, duration_sec=600)
    _add_single_execution(factory, session_id, streamer_id)
    rule = service.create_rule(_rule_values(repeat_interval_sec=600))
    service.assign_rules(session_id, streamer_id, [rule["id"]])

    async def abnormal_embedding_response(_texts: list[str]) -> list[list[float]]:
        return []

    monkeypatch.setattr(service, "embed_texts", abnormal_embedding_response)
    with pytest.raises(service.EmbeddingUnavailable):
        asyncio.run(service.analyze_session(session_id))

    with factory() as db:
        result = db.query(ScriptComplianceResult).one()
        assert result.status == "failed"
        assert result.overall_status == "not_analyzable"
        assert result.error_code == "EMBEDDING_UNAVAILABLE"
        assert result.expected_count == 0
        assert result.actual_count == 0
        assert result.matched_count == 0
        assert result.compliance_rate == 0
        assert db.query(ScriptComplianceOccurrence).count() == 0


def test_stale_analysis_lease_can_be_recovered_without_duplicate_result(
    compliance_db, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory, duration_sec=600)
    _add_two_executions(factory, session_id, streamer_id)
    rule = service.create_rule(_rule_values())
    plan = service.assign_rules(session_id, streamer_id, [rule["id"]])[0]
    with factory() as db:
        stale = ScriptComplianceResult(
            plan_id=plan["id"], session_id=session_id, streamer_id=streamer_id,
            rule_id=rule["id"], rule_version=rule["version"], status="analyzing",
            overall_status="not_analyzable", analysis_run_id="abandoned-run",
            created_at=datetime.now() - timedelta(hours=2),
            updated_at=datetime.now() - timedelta(hours=2),
        )
        db.add(stale)
        db.commit()
        stale_id = stale.id

    monkeypatch.setattr(service, "embed_texts", _matching_embeddings)
    recovered = asyncio.run(service.analyze_session(session_id))
    assert recovered["results"][0]["id"] == stale_id
    assert recovered["results"][0]["status"] == "ready"
    with factory() as db:
        assert db.query(ScriptComplianceResult).count() == 1


def test_api_contract_create_patch_plan_analyze_and_get(
    compliance_db, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory, duration_sec=600)
    _add_two_executions(factory, session_id, streamer_id)
    monkeypatch.setattr(service, "embed_texts", _matching_embeddings)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        created = client.post("/api/script-frequency-rules", json=_rule_values())
        assert created.status_code == 201
        rule = created.json()
        assert client.get("/api/script-frequency-rules").json()["rules"][0]["id"] == rule["id"]
        patched = client.patch(
            f"/api/script-frequency-rules/{rule['id']}",
            json={"name": "更新后的规则名"},
        )
        assert patched.status_code == 200
        assert patched.json()["version"] == 2
        planned = client.post(
            f"/api/sessions/{session_id}/script-compliance/plan",
            json={"streamer_id": streamer_id, "rule_ids": [rule["id"], rule["id"]]},
        )
        assert planned.status_code == 201
        assert len(planned.json()["assignments"]) == 1
        analyzed = client.post(
            f"/api/sessions/{session_id}/script-compliance/analyze", json={},
        )
        assert analyzed.status_code == 200
        result = analyzed.json()["results"][0]
        listed = client.get(f"/api/sessions/{session_id}/script-compliance")
        assert listed.status_code == 200
        assert listed.json()["plans"][0]["result"]["id"] == result["id"]
        detailed = client.get(
            f"/api/sessions/{session_id}/script-compliance/{result['id']}",
        )
        assert detailed.status_code == 200
        assert detailed.json()["occurrences"]
        cannot_delete = client.delete(f"/api/script-frequency-rules/{rule['id']}")
        assert cannot_delete.status_code == 409


def test_api_rule_lifecycle_and_plan_snapshot_version_isolation(
    compliance_db,
) -> None:
    session_id, streamer_id = _seed_session(compliance_db, duration_sec=600)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        created = client.post("/api/script-frequency-rules", json=_rule_values())
        assert created.status_code == 201
        rule_id = created.json()["id"]

        modified = client.patch(
            f"/api/script-frequency-rules/{rule_id}",
            json={"standard_script": "版本二规定话术内容"},
        )
        assert modified.status_code == 200
        assert modified.json()["version"] == 2

        disabled = client.patch(
            f"/api/script-frequency-rules/{rule_id}", json={"enabled": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert disabled.json()["version"] == 3
        disabled_rules = client.get(
            "/api/script-frequency-rules", params={"enabled": "false"},
        )
        assert [item["id"] for item in disabled_rules.json()["rules"]] == [rule_id]
        blocked = client.post(
            f"/api/sessions/{session_id}/script-compliance/plan",
            json={"streamer_id": streamer_id, "rule_ids": [rule_id]},
        )
        assert blocked.status_code == 409

        enabled = client.patch(
            f"/api/script-frequency-rules/{rule_id}", json={"enabled": True},
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert enabled.json()["version"] == 4
        planned = client.post(
            f"/api/sessions/{session_id}/script-compliance/plan",
            json={"streamer_id": streamer_id, "rule_ids": [rule_id]},
        )
        assert planned.status_code == 201
        snapshot = planned.json()["assignments"][0]
        assert snapshot["rule_version"] == 4
        assert snapshot["rule_snapshot"]["standard_script"] == "版本二规定话术内容"

        current = client.patch(
            f"/api/script-frequency-rules/{rule_id}",
            json={"standard_script": "版本五规定话术内容"},
        )
        assert current.status_code == 200
        assert current.json()["version"] == 5
        historical = client.get(
            f"/api/sessions/{session_id}/script-compliance/plan",
        ).json()["assignments"][0]
        assert historical["rule_version"] == 4
        assert historical["rule_snapshot"]["standard_script"] == "版本二规定话术内容"
        versions = client.get(
            f"/api/script-frequency-rules/{rule_id}/versions",
        )
        assert versions.status_code == 200
        assert [item["version"] for item in versions.json()["versions"]] == [5, 4]


def test_requested_40_minute_client_integration_scenario(
    compliance_db, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = compliance_db
    session_id, streamer_id = _seed_session(factory, duration_sec=2_400)
    standard_script = (
        "欢迎新来的朋友，今天给大家介绍这款产品的核心功能和使用方法。"
    )
    transcript_rows = [
        (120_000, 150_000,
         "欢迎刚进直播间的朋友，今天给大家介绍这款产品的主要功能与使用方式。"),
        (900_000, 915_000, standard_script),
        (1_500_000, 1_530_000,
         "欢迎新朋友，下面给大家换个说法，讲清楚这款产品最重要的功能以及具体怎么使用。"),
        (1_800_000, 1_830_000,
         "欢迎新来的朋友，今天不给大家介绍这款产品的核心功能和使用方法，只聊广告投放。"),
    ]
    with factory() as db:
        db.add_all([
            Transcript(
                session_id=session_id, streamer_id=streamer_id, seq=seq,
                start_ms=start_ms, end_ms=end_ms, raw_text=text, corrected_text=text,
            )
            for seq, (start_ms, end_ms, text) in enumerate(transcript_rows)
        ])
        db.commit()

    async def scenario_embeddings(texts: list[str]) -> list[list[float]]:
        vectors = [[1.0, 0.0]]
        for text in texts[1:]:
            if "只聊广告投放" in text:
                score = 0.65
            elif "换个说法" in text:
                score = 0.86
            elif text == standard_script:
                score = 1.0
            else:
                score = 0.92
            vectors.append([score, (1 - score * score) ** 0.5])
        return vectors

    monkeypatch.setattr(service, "embed_texts", scenario_embeddings)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        created = client.post("/api/script-frequency-rules", json={
            "name": "核心功能与使用方法",
            "standard_script": standard_script,
            "repeat_interval_sec": 600,
            "target_duration_sec": 30,
            "interval_tolerance_sec": 120,
            "duration_tolerance_sec": 10,
            "similarity_threshold": 0.75,
            "effective_scope": {"type": "all"},
            "enabled": True,
        })
        assert created.status_code == 201
        rule = created.json()
        planned = client.post(
            f"/api/sessions/{session_id}/script-compliance/plan",
            json={"streamer_id": streamer_id, "rule_ids": [rule["id"]]},
        )
        assert planned.status_code == 201
        snapshot = planned.json()["assignments"][0]["rule_snapshot"]
        assert snapshot["repeat_interval_sec"] == 600
        assert snapshot["target_duration_sec"] == 30
        assert snapshot["interval_tolerance_sec"] == 120
        assert snapshot["duration_tolerance_sec"] == 10
        assert snapshot["similarity_threshold"] == 0.75

        analyzed = client.post(
            f"/api/sessions/{session_id}/script-compliance/analyze", json={},
        )
        assert analyzed.status_code == 200
        result = analyzed.json()["results"][0]
        assert result["status"] == "ready"
        assert result["expected_count"] == 4
        assert result["actual_count"] == 4
        assert result["matched_count"] == 2
        assert result["missing_count"] == 1
        assert result["compliance_rate"] == 0.5

        actual = [
            item for item in result["occurrences"]
            if item["actual_start_ms"] is not None
        ]
        assert [item["actual_start_ms"] for item in actual] == [
            120_000, 900_000, 1_500_000, 1_800_000,
        ]
        assert [item["interval_from_previous_sec"] for item in actual] == [
            None, 780.0, 600.0, 300.0,
        ]
        assert [item["actual_duration_sec"] for item in actual] == [
            30.0, 15.0, 30.0, 30.0,
        ]
        assert [item["similarity_percent"] for item in actual] == [
            92.0, 100.0, 86.0, 65.0,
        ]
        assert actual[0]["issues"] == []
        assert set(actual[1]["issues"]) == {"late", "too_short"}
        assert actual[2]["issues"] == []
        assert actual[3]["issues"] == ["low_similarity"]
        missing = result["occurrences"][-1]
        assert missing["status"] == "missing"
        assert missing["issues"] == ["missing"]
        assert missing["actual_start_ms"] is None
        assert result["average_interval_sec"] == 690.0
        assert result["min_interval_sec"] == 600.0
        assert result["max_interval_sec"] == 780.0

        queried = client.get(
            f"/api/sessions/{session_id}/script-compliance/{result['id']}",
        )
        assert queried.status_code == 200
        assert queried.json()["occurrences"] == result["occurrences"]
