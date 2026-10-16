"""Observation-session offline heat analysis and excellent-script extraction APIs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import observation as service


router = APIRouter(prefix="/api/observation-sessions", tags=["observation-sessions"])


class ObservationExtractIn(BaseModel):
    include_emotion: bool = False


def _raise(exc: Exception, session_id: int) -> None:
    if isinstance(exc, LookupError):
        raise HTTPException(404, detail={
            "code": "OBSERVATION_SESSION_NOT_FOUND", "message": str(exc),
        }) from exc
    if isinstance(exc, service.ObservationStateError):
        raise HTTPException(409, detail={
            "code": "OBSERVATION_STATE_CONFLICT", "message": str(exc),
        }) from exc
    try:
        analysis = service.get_analysis(session_id)
        code = analysis.get("error_code") or "OBSERVATION_EXTRACT_FAILED"
        message = analysis.get("error_message") or "观摩提炼失败，请稍后重试"
    except Exception:  # noqa: BLE001
        code, message = "OBSERVATION_EXTRACT_FAILED", "观摩提炼失败，请稍后重试"
    raise HTTPException(503, detail={"code": code, "message": message}) from exc


@router.post("/{session_id}/extract")
async def extract_observation_session(
    session_id: int, payload: ObservationExtractIn | None = None,
):
    payload = payload or ObservationExtractIn()
    try:
        return await service.extract_session(
            session_id, include_emotion=payload.include_emotion,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc, session_id)


@router.get("/{session_id}/analysis")
def get_observation_analysis(session_id: int):
    try:
        return service.get_analysis(session_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc, session_id)


@router.post("/{session_id}/retry")
async def retry_observation_session(
    session_id: int, payload: ObservationExtractIn | None = None,
):
    payload = payload or ObservationExtractIn()
    try:
        return await service.extract_session(
            session_id, include_emotion=payload.include_emotion, retry=True,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc, session_id)
