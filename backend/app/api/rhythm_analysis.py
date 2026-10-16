"""R3 场次话术节奏提取、模板对比、执行计划与效果验证 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..core.review import rhythm_analysis as service


router = APIRouter(prefix="/api/sessions", tags=["rhythm-analysis"])


class RhythmPlanIn(BaseModel):
    streamer_id: int = Field(gt=0)
    template_id: int = Field(gt=0)


class RhythmAnalyzeIn(BaseModel):
    streamer_id: int = Field(gt=0)
    template_id: int | None = Field(default=None, gt=0)
    # 兼容旧客户端；true 只有在同模板已通过 /plan 前置登记时才会被接受。
    declare_execution: bool | None = None


def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, LookupError):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, service.RhythmTemplateNotEligible):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, (service.RhythmExecutionPlanError, service.RhythmAnalysisSuperseded)):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(400, str(exc)) from exc
    raise exc


@router.get("/{session_id}/rhythm-analysis/recommendations")
def recommendations(
    session_id: int,
    streamer_id: int = Query(..., gt=0),
    limit: int = Query(5, ge=1, le=20),
):
    try:
        return {"recommendations": service.recommend_templates(
            session_id, streamer_id, limit=limit,
        )}
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)


@router.get("/{session_id}/rhythm-analysis")
def get_rhythm_analysis(session_id: int, streamer_id: int = Query(..., gt=0)):
    try:
        result = service.get_analysis(session_id, streamer_id)
        return result or {
            "available": False,
            "session_id": session_id,
            "streamer_id": streamer_id,
            "status": "not_generated",
        }
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)


@router.post("/{session_id}/rhythm-analysis/plan")
def plan_rhythm(session_id: int, payload: RhythmPlanIn):
    try:
        return service.plan_session_rhythm(
            session_id, payload.streamer_id, payload.template_id,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)


@router.post("/{session_id}/rhythm-analysis/analyze")
async def analyze_rhythm(session_id: int, payload: RhythmAnalyzeIn):
    try:
        return await service.analyze_session_rhythm(
            session_id,
            payload.streamer_id,
            payload.template_id,
            declare_execution=payload.declare_execution,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)


@router.post("/{session_id}/rhythm-analysis/verify")
def verify_rhythm(session_id: int, streamer_id: int = Query(..., gt=0)):
    try:
        return service.refresh_validation(session_id, streamer_id)
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc)
