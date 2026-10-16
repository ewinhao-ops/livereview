"""主播花名册 CRUD。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Streamer
from ..schemas import StreamerIn, StreamerOut

router = APIRouter(prefix="/api/streamers", tags=["streamers"])

# 给新主播轮流分配的看板颜色
_PALETTE = ["#5b8def", "#34d399", "#e8b339", "#b07cf0", "#f87171", "#22d3ee", "#f472b6"]


@router.get("", response_model=list[StreamerOut])
def list_streamers(db: Session = Depends(get_db)):
    # 只列在册主播（软删的隐藏），但历史场次仍可按 streamer_id 查到已删主播的名字
    return db.execute(
        select(Streamer).where(Streamer.deleted_at.is_(None)).order_by(Streamer.id)
    ).scalars().all()


@router.post("", response_model=StreamerOut)
def create_streamer(payload: StreamerIn, db: Session = Depends(get_db)):
    data = payload.model_dump()
    if not data.get("color"):
        n = db.query(Streamer).count()
        data["color"] = _PALETTE[n % len(_PALETTE)]
    s = Streamer(**data)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@router.put("/{streamer_id}", response_model=StreamerOut)
def update_streamer(streamer_id: int, payload: StreamerIn, db: Session = Depends(get_db)):
    s = db.get(Streamer, streamer_id)
    if not s:
        raise HTTPException(404, "主播不存在")
    for k, v in payload.model_dump().items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)
    return s


@router.delete("/{streamer_id}")
def delete_streamer(streamer_id: int, db: Session = Depends(get_db)):
    s = db.get(Streamer, streamer_id)
    if not s:
        raise HTTPException(404, "主播不存在")
    # 软删：保留行，历史场次/统计仍能按 streamer_id 显示主播名，避免孤儿外键与跨库迁移问题
    s.deleted_at = datetime.now()
    # 同时释放 ext_id（部分唯一索引不区分软删行，不清会永久堵死该主播后续信号的重绑/建档；
    # 历史归属由 signal_events.streamer_id / ext_id 审计保留）
    s.ext_id = None
    db.commit()
    return {"deleted": True}
