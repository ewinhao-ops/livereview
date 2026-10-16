"""成功经验技巧库 API（D4）。

  GET  /api/experience?status=observing|pending|adopted|rejected  跨场统计技巧列表
  GET  /api/experience/session/{sid}                     本场参与验证的技巧类别
  POST /api/experience/{id}/adopt                        采纳 → embed 入库供复用
  POST /api/experience/{id}/reject                       驳回
pending 只包含频次/占比统计达标的类别；采纳/驳回是人工把关，只有人工采纳后
才进向量库被点评召回。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..core.review import experience

router = APIRouter(prefix="/api/experience", tags=["experience"])


@router.get("")
def list_experience(status: str | None = None):
    return {"tips": experience.list_tips(status=status)}


@router.get("/session/{session_id}")
def session_experience(session_id: int):
    return {"tips": experience.list_tips(session_id=session_id)}


@router.post("/{tip_id}/adopt")
async def adopt(tip_id: int):
    return await experience.adopt_tip(tip_id)


@router.post("/{tip_id}/reject")
def reject(tip_id: int):
    return experience.reject_tip(tip_id)
