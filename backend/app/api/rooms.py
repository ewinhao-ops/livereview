"""常用直播间列表：粘贴链接建场时自动收录，之后可复选，不用重复粘链接。"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import LiveRoom, ReplaySession, SessionStatus

router = APIRouter(prefix="/api/rooms", tags=["rooms"])


def _auto_ids(r: LiveRoom) -> list[int]:
    try:
        return json.loads(r.auto_streamer_ids or "[]")
    except (ValueError, TypeError):
        return []


@router.get("")
def list_rooms(response: Response, db: Session = Depends(get_db)):
    rows = db.execute(
        select(LiveRoom).where(LiveRoom.deleted_at.is_(None)).order_by(
            LiveRoom.last_used_at.desc().nullslast(),
            LiveRoom.created_at.desc(),
            LiveRoom.id.desc(),
        )
    ).scalars().all()
    if len(rows) > 50:
        response.headers["Warning"] = '199 - "Room count exceeds 50; manual cleanup recommended"'
        response.headers["X-Room-Count-Warning"] = "OVER_50"
    return [{"id": r.id, "room_id": r.room_id, "title": r.title,
             "share_link": r.share_link, "last_used_at": r.last_used_at,
             "auto_record": bool(r.auto_record), "auto_streamer_ids": _auto_ids(r)} for r in rows]


@router.put("/{room_id}/auto")
def set_auto_record(room_id: int, auto_record: bool = Body(..., embed=True),
                    streamer_ids: list[int] | None = Body(None, embed=True),
                    db: Session = Depends(get_db)):
    r = db.get(LiveRoom, room_id)
    if not r or r.deleted_at is not None:
        raise HTTPException(404, "直播间不存在")
    r.auto_record = auto_record
    if streamer_ids is not None:
        r.auto_streamer_ids = json.dumps(streamer_ids)
    db.commit()
    return {"ok": True, "auto_record": bool(r.auto_record), "auto_streamer_ids": _auto_ids(r)}


@router.delete("/{room_id}")
def delete_room(room_id: int, db: Session = Depends(get_db)):
    r = db.get(LiveRoom, room_id)
    if r is None:
        raise HTTPException(404, detail={
            "code": "ROOM_NOT_FOUND", "message": "直播间不存在",
        })
    if r.deleted_at is not None:
        return {"deleted": True, "already_deleted": True, "id": room_id}
    active = db.execute(select(ReplaySession.id).where(
        ReplaySession.room_id == r.room_id,
        ReplaySession.status.in_((
            SessionStatus.created, SessionStatus.parsing,
            SessionStatus.live, SessionStatus.processing,
        )),
        ReplaySession.ended_at.is_(None),
    ).order_by(ReplaySession.id).limit(1)).scalar_one_or_none()
    if active is not None:
        raise HTTPException(409, detail={
            "code": "ROOM_IN_USE",
            "message": "直播间正被活动场次使用，不能删除",
            "session_id": active,
        })
    r.deleted_at = datetime.now()
    db.commit()
    return {"deleted": True, "already_deleted": False, "id": room_id}
