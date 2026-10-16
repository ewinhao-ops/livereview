"""Unified forbid/allow compliance rule management and session snapshot APIs."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from ..core import compliance_rules as service


router = APIRouter(tags=["compliance-rules"])


class ComplianceRuleCreateIn(BaseModel):
    action: Literal["allow", "forbid"]
    keyword: str = Field(min_length=1, max_length=500)
    scope_type: Literal["all", "session", "streamer", "room"] = "all"
    scope_id: str = Field(default="", max_length=128)
    enabled: bool = True
    note: str = Field(default="", max_length=4_000)
    priority: int = Field(default=0, ge=-1_000_000, le=1_000_000)

    @field_validator("keyword")
    @classmethod
    def strip_keyword(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("关键词或短语不能为空")
        return value

    @model_validator(mode="after")
    def valid_scope(self):
        service.validate_scope(self.scope_type, self.scope_id)
        return self


class ComplianceRulePatchIn(BaseModel):
    action: Literal["allow", "forbid"] | None = None
    keyword: str | None = Field(default=None, min_length=1, max_length=500)
    scope_type: Literal["all", "session", "streamer", "room"] | None = None
    scope_id: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None
    note: str | None = Field(default=None, max_length=4_000)
    priority: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("至少提供一个需要修改的字段")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("修改字段不能为 null")
        return self


def _raise(exc: Exception) -> None:
    if isinstance(exc, LookupError):
        raise HTTPException(404, detail={"code": "COMPLIANCE_RULE_NOT_FOUND", "message": str(exc)}) from exc
    if isinstance(exc, service.ComplianceRuleInUse):
        raise HTTPException(409, detail={"code": "COMPLIANCE_RULE_IN_USE", "message": str(exc)}) from exc
    if isinstance(exc, service.ComplianceRuleConflict):
        raise HTTPException(409, detail={"code": "COMPLIANCE_RULE_DUPLICATE", "message": str(exc)}) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(422, detail={"code": "COMPLIANCE_RULE_INVALID", "message": str(exc)}) from exc
    raise exc


@router.post("/api/compliance-rules", status_code=201)
def create_compliance_rule(payload: ComplianceRuleCreateIn):
    try:
        return service.create_rule(payload.model_dump())
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/api/compliance-rules")
def get_compliance_rules(
    action: Literal["allow", "forbid"] | None = None,
    enabled: bool | None = None,
    scope_type: Literal["all", "session", "streamer", "room", "product"] | None = None,
    scope_id: str | None = Query(default=None, max_length=128),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    try:
        return service.list_rules(
            action=action, enabled=enabled, scope_type=scope_type, scope_id=scope_id,
            page=page, page_size=page_size,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/api/compliance-rules/{rule_id}")
def get_compliance_rule(rule_id: int):
    row = service.get_rule(rule_id)
    if row is None:
        raise HTTPException(404, detail={
            "code": "COMPLIANCE_RULE_NOT_FOUND", "message": "合规规则不存在",
        })
    return row


@router.patch("/api/compliance-rules/{rule_id}")
def update_compliance_rule(rule_id: int, payload: ComplianceRulePatchIn):
    try:
        return service.patch_rule(rule_id, payload.model_dump(exclude_unset=True))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.delete("/api/compliance-rules/{rule_id}")
def remove_compliance_rule(rule_id: int):
    try:
        return service.delete_rule(rule_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/api/sessions/{session_id}/compliance-snapshot")
def get_compliance_snapshot(session_id: int):
    snapshot = service.get_session_snapshot(session_id)
    if snapshot is None:
        return {
            "session_id": session_id,
            "required_script_count": 0,
            "forbid_rule_count": 0,
            "allow_rule_count": 0,
            "snapshot_version": None,
            "snapshot_created_at": None,
            "locked": False,
        }
    return snapshot
