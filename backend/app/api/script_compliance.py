"""Prescribed-script rule, planning, and whole-session compliance APIs."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from ..core import script_compliance as service


router = APIRouter(tags=["script-compliance"])


class RuleCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    standard_script: str = Field(min_length=1, max_length=20_000)
    repeat_interval_sec: int = Field(gt=0, le=86_400)
    target_duration_sec: int = Field(gt=0, le=3_600)
    interval_tolerance_sec: int = Field(default=0, ge=0, le=86_400)
    duration_tolerance_sec: int = Field(default=0, ge=0, le=3_600)
    similarity_threshold: float = Field(ge=0, le=1)
    effective_scope: dict[str, Any] = Field(default_factory=lambda: {"type": "all"})
    enabled: bool = True

    @field_validator("name", "standard_script")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("内容不能为空")
        return value

    @field_validator("effective_scope")
    @classmethod
    def validate_scope(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False)) > 4_000:
            raise ValueError("生效范围配置过大")
        scope_type = str(value.get("type") or "all").lower()
        if scope_type not in {"all", "streamers", "sessions", "rooms"}:
            raise ValueError("生效范围类型必须是 all、streamers、sessions 或 rooms")
        if scope_type == "streamers" and not value.get("streamer_ids"):
            raise ValueError("streamers 生效范围必须包含 streamer_ids")
        if scope_type == "sessions" and not value.get("session_ids"):
            raise ValueError("sessions 生效范围必须包含 session_ids")
        if scope_type == "rooms" and not value.get("room_ids"):
            raise ValueError("rooms 生效范围必须包含 room_ids")
        return {**value, "type": scope_type}


class RulePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    standard_script: str | None = Field(default=None, min_length=1, max_length=20_000)
    repeat_interval_sec: int | None = Field(default=None, gt=0, le=86_400)
    target_duration_sec: int | None = Field(default=None, gt=0, le=3_600)
    interval_tolerance_sec: int | None = Field(default=None, ge=0, le=86_400)
    duration_tolerance_sec: int | None = Field(default=None, ge=0, le=3_600)
    similarity_threshold: float | None = Field(default=None, ge=0, le=1)
    effective_scope: dict[str, Any] | None = None
    enabled: bool | None = None

    @field_validator("name", "standard_script")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("内容不能为空")
        return value

    @field_validator("effective_scope")
    @classmethod
    def validate_optional_scope(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return RuleCreateIn.validate_scope(value) if value is not None else None

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("至少提供一个需要修改的字段")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("修改字段不能为 null")
        return self


class ScriptPlanIn(BaseModel):
    streamer_id: int = Field(gt=0)
    rule_ids: list[int] = Field(min_length=1, max_length=100)

    @field_validator("rule_ids")
    @classmethod
    def positive_rule_ids(cls, value: list[int]) -> list[int]:
        if any(rule_id <= 0 for rule_id in value):
            raise ValueError("规则ID必须大于0")
        return list(dict.fromkeys(value))


class ScriptAnalyzeIn(BaseModel):
    streamer_id: int | None = Field(default=None, gt=0)
    plan_ids: list[int] | None = Field(default=None, max_length=100)

    @field_validator("plan_ids")
    @classmethod
    def positive_plan_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and any(plan_id <= 0 for plan_id in value):
            raise ValueError("计划ID必须大于0")
        return list(dict.fromkeys(value)) if value else None


def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, LookupError):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, service.ScriptNotAnalyzable):
        raise HTTPException(
            409,
            detail={"status": "not_analyzable", "reason": str(exc)},
        ) from exc
    if isinstance(exc, service.EmbeddingUnavailable):
        raise HTTPException(
            503,
            detail={
                "status": "failed",
                "error_code": "EMBEDDING_UNAVAILABLE",
                "reason": "Embedding 服务不可用，请检查配置或云端额度后重试",
            },
        ) from exc
    if isinstance(exc, (
        service.ScriptRuleInUse,
        service.ScriptRuleNotEligible,
        service.ScriptPlanConflict,
        service.ScriptAnalysisInProgress,
        service.ScriptAnalysisSuperseded,
    )):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(400, str(exc)) from exc
    raise exc


@router.post("/api/script-frequency-rules", status_code=201)
def create_script_frequency_rule(payload: RuleCreateIn):
    try:
        return service.create_rule(payload.model_dump())
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)


@router.get("/api/script-frequency-rules")
def get_script_frequency_rules(enabled: bool | None = Query(default=None)):
    return {"rules": service.list_rules(enabled=enabled)}


@router.get("/api/script-frequency-rules/{rule_id}/versions")
def get_script_frequency_rule_versions(rule_id: int):
    try:
        return {"versions": service.list_rule_versions(rule_id)}
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)


@router.patch("/api/script-frequency-rules/{rule_id}")
def update_script_frequency_rule(rule_id: int, payload: RulePatchIn):
    try:
        return service.patch_rule(rule_id, payload.model_dump(exclude_unset=True))
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)


@router.delete("/api/script-frequency-rules/{rule_id}")
def remove_script_frequency_rule(rule_id: int):
    try:
        service.delete_rule(rule_id)
        return {"deleted": True}
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)


@router.post("/api/sessions/{session_id}/script-compliance/plan", status_code=201)
def plan_script_compliance(session_id: int, payload: ScriptPlanIn):
    try:
        return {
            "session_id": session_id,
            "assignments": service.assign_rules(
                session_id, payload.streamer_id, payload.rule_ids,
            ),
        }
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)


@router.get("/api/sessions/{session_id}/script-compliance/plan")
def get_script_compliance_plan(
    session_id: int,
    streamer_id: int | None = Query(default=None, gt=0),
):
    try:
        return {
            "session_id": session_id,
            "assignments": service.list_plans(session_id, streamer_id=streamer_id),
        }
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)


@router.post("/api/sessions/{session_id}/script-compliance/analyze")
async def analyze_script_compliance(session_id: int, payload: ScriptAnalyzeIn | None = None):
    payload = payload or ScriptAnalyzeIn()
    try:
        return await service.analyze_session(
            session_id,
            streamer_id=payload.streamer_id,
            plan_ids=payload.plan_ids,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)


@router.get("/api/sessions/{session_id}/script-compliance")
def get_script_compliance(
    session_id: int,
    streamer_id: int | None = Query(default=None, gt=0),
):
    try:
        return service.list_session_compliance(session_id, streamer_id=streamer_id)
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)


@router.get("/api/sessions/{session_id}/script-compliance/{result_id}")
def get_script_compliance_result(session_id: int, result_id: int):
    try:
        return service.get_result(session_id, result_id)
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)
