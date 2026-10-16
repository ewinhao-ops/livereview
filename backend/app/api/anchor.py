"""R6/R7 主播端：同一主系统内登录、完整复盘和可靠实时提醒。

2026-10-01 起主播账号登录对公网关闭（统一登录改造），只留服务器本机直连的应急入口；
主播日常看报告走业绩系统给的签名外链（partner.py /view）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import (STREAMER_COOKIE, hash_password, is_local_direct, local_cookie_secure,
                    streamer_id_from_request, streamer_token, verify_password, password_login_allowed)
from ..bus import bus
from ..db import SessionLocal, get_db
from ..core import media_store
from ..core.safe_error import safe_error_text
from ..models import (AudioStatus, CompassPoint, Critique, CritiqueLevel, KnowledgeChunk,
                      KnowledgeItem, LiveAlert, ReplaySession, Streamer, StreamerAccount,
                      StreamerTurn, Transcript)

router = APIRouter(prefix="/api/anchor", tags=["anchor"])
_login_fails: dict[str, list[float]] = {}


def _public_reason(value: str, fallback: str = "媒体处理失败") -> str:
    text = (value or "").splitlines()[0]
    if re.search(r"moov atom|impossible to open|error opening input", text, re.I):
        return "音频合并失败：录音分片损坏或不完整"
    if re.search(r"traceback|stack trace|file \".*\", line \d+", text, re.I):
        return fallback
    return safe_error_text(text, fallback=fallback, limit=180)


def _sid(request: Request) -> int:
    sid = streamer_id_from_request(request)
    if sid is None:
        raise HTTPException(401, "主播未登录")
    with SessionLocal() as db:
        account = db.execute(select(StreamerAccount).where(
            StreamerAccount.streamer_id == sid)).scalars().first()
        if account is None or not account.enabled:
            raise HTTPException(401, "主播账号已停用")
    return sid


@router.post("/login")
def login(request: Request, username: str = Body(...), password: str = Body(...),
          db: Session = Depends(get_db)):
    if not password_login_allowed(request):
        raise HTTPException(403, "主播账号登录已关闭：请在业绩系统里点「查看完整复盘」打开自己的复盘")
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    failures = [x for x in _login_fails.get(ip, []) if now - x < 60]
    _login_fails[ip] = failures
    if len(failures) >= 5:
        raise HTTPException(429, "尝试过于频繁，请稍后再试")
    account = db.execute(select(StreamerAccount).where(
        StreamerAccount.username == username.strip()
    )).scalars().first()
    if account is not None and not account.enabled:
        raise HTTPException(403, "账号已禁用，请联系管理员")
    if account is None or not verify_password(password, account.password_hash):
        failures.append(now)
        raise HTTPException(401, "账号或密码错误")
    _login_fails.pop(ip, None)
    response = JSONResponse({"ok": True})
    response.set_cookie(STREAMER_COOKIE, streamer_token(account.streamer_id), httponly=True,
                        samesite="strict", secure=local_cookie_secure(request),
                        max_age=30 * 86400, path="/")
    return response


@router.post("/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(STREAMER_COOKIE)
    return response


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    sid = _sid(request)
    account = db.execute(select(StreamerAccount).where(
        StreamerAccount.streamer_id == sid)).scalars().first()
    if account is None or not account.enabled:
        raise HTTPException(401, "主播账号已停用")
    s = db.get(Streamer, sid)
    if s is None or s.deleted_at is not None:
        raise HTTPException(401, "主播账号已停用")
    return {"id": s.id, "name": s.name, "ext_id": s.ext_id}


def _session_ids(sid: int):
    return select(StreamerTurn.session_id).where(StreamerTurn.streamer_id == sid).distinct()


def _allowed_replay(db: Session, session_id: int, sid: int) -> ReplaySession:
    row = db.get(ReplaySession, session_id)
    allowed = db.execute(select(StreamerTurn.id).where(
        StreamerTurn.session_id == session_id, StreamerTurn.streamer_id == sid)).first()
    if row is None or not allowed:
        raise HTTPException(404, "复盘不存在")
    return row


@router.get("/replays")
def replays(request: Request, db: Session = Depends(get_db)):
    sid = _sid(request)
    rows = db.execute(select(ReplaySession).where(ReplaySession.id.in_(_session_ids(sid)))
                      .order_by(ReplaySession.id.desc())).scalars().all()
    out = []
    for row in rows:
        summary = db.execute(select(Critique).where(
            Critique.session_id == row.id, Critique.streamer_id == sid,
            Critique.level == CritiqueLevel.summary).order_by(Critique.id.desc())).scalars().first()
        out.append({"id": row.id, "title": row.title, "status": row.status.value,
                    "started_at": row.started_at, "ended_at": row.ended_at,
                    "score": summary.score if summary else None,
                    "summary": summary.comment if summary else ""})
    return out


@router.get("/replays/{session_id}")
def replay(session_id: int, request: Request, db: Session = Depends(get_db)):
    sid = _sid(request)
    row = _allowed_replay(db, session_id, sid)
    transcripts = db.execute(select(Transcript).where(
        Transcript.session_id == session_id, Transcript.streamer_id == sid)
        .order_by(Transcript.start_ms)).scalars().all()
    critiques = db.execute(select(Critique).where(
        Critique.session_id == session_id, Critique.streamer_id == sid)
        .order_by(Critique.start_ms)).scalars().all()
    from ..core.attribution import analyze_attribution
    af = row.audio
    local_exists = bool(af and af.path and Path(af.path).is_file())
    remote_exists = bool(af and af.cos_key)
    media_available = bool(af and af.status == AudioStatus.available and af.size_bytes > 0
                           and (local_exists or remote_exists))
    if media_available:
        media_reason = ""
    elif row.error_msg:
        media_reason = _public_reason(row.error_msg, "音频处理失败")
    elif af is None:
        media_reason = "本场尚未生成录音"
    elif af.status == AudioStatus.recording:
        media_reason = "录音仍在处理中"
    elif af.status == AudioStatus.deleted:
        media_reason = "录音已按保留策略清理"
    elif af.size_bytes <= 0:
        media_reason = "音频合并失败：未生成有效文件"
    else:
        media_reason = "录音文件不可用"
    media = {"available": media_available,
             "kind": getattr(af, "media_kind", "audio") if af else "",
             "url": f"/api/anchor/replays/{session_id}/media" if media_available else "",
             "reason": media_reason}
    compass_count = db.scalar(select(func.count(CompassPoint.id)).where(
        CompassPoint.session_id == session_id)) or 0
    knowledge_count = (db.scalar(select(func.count(KnowledgeItem.id))) or 0) + (
        db.scalar(select(func.count(KnowledgeChunk.id))) or 0)
    return {"session": {"id": row.id, "title": row.title, "status": row.status.value,
                         "started_at": row.started_at, "ended_at": row.ended_at},
            "media": media,
            "attribution": analyze_attribution(session_id, sid),
            "compass": {"status": "connected" if compass_count else "empty",
                        "count": compass_count},
            "knowledge": {"status": "available" if knowledge_count else "empty",
                          "count": knowledge_count},
            "transcripts": [{"start_ms": x.start_ms, "end_ms": x.end_ms,
                              "text": x.corrected_text} for x in transcripts],
            "critiques": [{"level": x.level.value, "severity": x.severity.value,
                            "category": x.category, "start_ms": x.start_ms,
                            "quote": x.quote, "comment": x.comment, "score": x.score,
                            "data": _json(x.data), "llm_provider": x.llm_provider,
                            "llm_requested_provider": x.llm_requested_provider,
                            "is_fallback": x.is_fallback} for x in critiques]}


@router.get("/replays/{session_id}/media")
def replay_media(session_id: int, request: Request, db: Session = Depends(get_db)):
    row = _allowed_replay(db, session_id, _sid(request))
    af = row.audio
    if af is None or af.status != AudioStatus.available or af.size_bytes <= 0:
        raise HTTPException(404, "媒体不存在")
    if af.cos_key:
        if af.cos_key.endswith(".m3u8"):
            playlist = media_store.rewrite_hls_playlist(af.cos_key)
            if playlist:
                return Response(playlist, media_type="application/vnd.apple.mpegurl")
        url = media_store.presigned_url(af.cos_key, expires=7200)
        if url:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url)
    from pathlib import Path
    path = Path(af.path or "")
    if path.is_file():
        return FileResponse(path, media_type="video/mp4" if af.media_kind == "video" else "audio/mp4")
    raise HTTPException(404, "媒体文件不可用")


def _json(raw: str) -> dict:
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {"value": value}
    except json.JSONDecodeError:
        return {}


def _alert_payload(row: LiveAlert) -> dict:
    occurred_at = row.created_at.isoformat(timespec="seconds") if row.created_at else None
    return {"type": "alert", "id": row.id, "session_id": row.session_id,
            "kind": row.kind, "severity": row.severity, "title": row.title,
            "message": row.message, "quote": row.quote, "suggestion": row.suggestion,
            "at_ms": row.at_ms, "read": row.read_at is not None,
            "created_at": occurred_at, "occurred_at": occurred_at}


@router.get("/alerts")
def alerts(request: Request, unread_only: bool = False, db: Session = Depends(get_db)):
    sid = _sid(request)
    q = select(LiveAlert).where(LiveAlert.streamer_id == sid)
    if unread_only:
        q = q.where(LiveAlert.read_at.is_(None))
    rows = db.execute(q.order_by(LiveAlert.id.desc()).limit(100)).scalars().all()
    return [_alert_payload(x) for x in rows]


@router.post("/alerts/{alert_id}/read")
def mark_read(alert_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(LiveAlert, alert_id)
    if row is None or row.streamer_id != _sid(request):
        raise HTTPException(404, "提醒不存在")
    row.read_at = datetime.now()
    db.commit()
    return {"ok": True}


@router.websocket("/ws")
async def ws(websocket: WebSocket):
    sid = streamer_id_from_request(websocket)
    if sid is None:
        await websocket.close(code=1008)
        return
    with SessionLocal() as db:
        account = db.execute(select(StreamerAccount).where(
            StreamerAccount.streamer_id == sid)).scalars().first()
        if account is None or not account.enabled:
            await websocket.close(code=1008)
            return
    await websocket.accept()
    q = bus.subscribe_streamer(sid)
    try:
        with SessionLocal() as db:
            unread = db.execute(select(LiveAlert).where(
                LiveAlert.streamer_id == sid, LiveAlert.read_at.is_(None))
                .order_by(LiveAlert.id).limit(100)).scalars().all()
            for x in unread:
                await websocket.send_json(_alert_payload(x))
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=20)
            except TimeoutError:
                event = {"type": "ping"}
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe_streamer(sid, q)


def create_alert(session_id: int, streamer_id: int | None, *, kind: str, severity: str,
                 title: str, message: str, quote: str = "", suggestion: str = "",
                 at_ms: int = 0) -> dict | None:
    if streamer_id is None:
        return None
    with SessionLocal() as db:
        session = db.get(ReplaySession, session_id)
        assigned = db.execute(select(StreamerTurn.id).where(
            StreamerTurn.session_id == session_id,
            StreamerTurn.streamer_id == streamer_id,
        )).first()
        # 创建侧再做一次归属门禁，调用方传错主播也不会落库/推送造成跨主播泄露。
        if session is None or (session.current_streamer_id != streamer_id and not assigned):
            return None
        normalized_quote = "".join((quote or message).lower().split())[:240]
        recent = db.execute(select(LiveAlert).where(
            LiveAlert.session_id == session_id,
            LiveAlert.streamer_id == streamer_id,
            LiveAlert.kind == kind,
            LiveAlert.created_at >= datetime.now() - timedelta(seconds=45),
        ).order_by(LiveAlert.id.desc()).limit(10)).scalars().all()
        if normalized_quote and any(
            "".join((x.quote or x.message).lower().split())[:240] == normalized_quote
            for x in recent
        ):
            return None
        bucket = max(0, int(at_ms)) // 5000
        event_key = hashlib.sha256(
            f"{session_id}:{streamer_id}:{kind}:{bucket}:{normalized_quote}".encode("utf-8")
        ).hexdigest()
        row = LiveAlert(session_id=session_id, streamer_id=streamer_id,
                        event_key=event_key, kind=kind,
                        severity=severity, title=title, message=message, quote=quote,
                        suggestion=suggestion, at_ms=at_ms)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
        db.refresh(row)
        return _alert_payload(row)


@router.put("/admin/account/{streamer_id}")
def set_account(streamer_id: int, request: Request, username: str = Body(...),
                password: str = Body(...), db: Session = Depends(get_db)):
    # 后台身份（统一登录 / 本机应急口令）已由全局中间件校验并挂在 request.state；主播 cookie 不能调用此接口。
    operator = getattr(request.state, "operator", None)
    if operator is None:
        raise HTTPException(403, "仅管理员可设置主播账号")
    if db.get(Streamer, streamer_id) is None:
        raise HTTPException(404, "主播不存在")
    if len(password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    row = db.execute(select(StreamerAccount).where(
        StreamerAccount.streamer_id == streamer_id)).scalars().first()
    if row is None:
        row = StreamerAccount(streamer_id=streamer_id, username=username.strip(),
                              password_hash=hash_password(password)); db.add(row)
    else:
        row.username = username.strip(); row.password_hash = hash_password(password)
        row.enabled = True; row.updated_at = datetime.now()
    db.commit()
    return {"ok": True, "username": row.username}
