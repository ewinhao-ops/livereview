"""业绩系统只读数据面：主播在业绩后台看自己的复盘（业绩后端 BFF 代理调用）。

鉴权：X-Partner-Token 头（与 signal_token 分离的只读凭据，配置 PARTNER_TOKEN）
+ 仅本机回环（同 signal 的回环校验）。复盘登录凭据永不进主播浏览器。
按 ext_id（业绩主播终身 ID）定位花名册主播，花名兜底；所有数据按 streamer_id
过滤——只回该主播本人的班次、点评与整场总结。
时间字段一律 ISO8601 +08:00（库内为本地 naive，服务器 TZ=Asia/Shanghai）。
"""

from __future__ import annotations

import hmac
import json
import math
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from pathlib import Path

from ..models import (SessionStatus, AudioStatus, Critique, CritiqueLevel, DanmakuRefine,
                      LiveRoom, ReplaySession, Severity, Streamer, StreamerTurn, Transcript,
                      ViewActivity)
from ..core import media_store
from ..core.review import focus_store
from ..core.safe_error import safe_error_text, safe_exception_code
from .live import _reject_non_loopback

router = APIRouter(prefix="/api/partner", tags=["partner"])

_SEV_RANK = {Severity.error: 0, Severity.warn: 1, Severity.info: 2}


def _auth(request: Request) -> None:
    st = get_settings()
    token = request.headers.get("x-partner-token", "")
    if not st.partner_token or not hmac.compare_digest(token, st.partner_token):
        raise HTTPException(403, "无效或未配置 partner_token")
    _reject_non_loopback(request)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "+08:00" if dt else None


def _find_streamer(db: Session, ext_id: str, name: str) -> Streamer | None:
    if ext_id:
        s = db.execute(
            select(Streamer).where(Streamer.ext_id == ext_id, Streamer.deleted_at.is_(None))
        ).scalars().first()
        if s:
            return s
    if name:
        rows = db.execute(
            select(Streamer).where(Streamer.name == name, Streamer.deleted_at.is_(None))
        ).scalars().all()
        if len(rows) == 1:
            return rows[0]
    return None


def _my_summary(db: Session, session_id: int, streamer_id: int) -> Critique | None:
    return db.execute(
        select(Critique).where(
            Critique.session_id == session_id,
            Critique.streamer_id == streamer_id,
            Critique.level == CritiqueLevel.summary,
        ).order_by(Critique.id.desc())
    ).scalars().first()


@router.get("/replays")
def list_replays(
    request: Request,
    ext_id: str = Query(""),
    name: str = Query(""),
    limit: int = Query(10),
    offset: int = Query(0),
    db: Session = Depends(get_db),
):
    _auth(request)
    limit = max(1, min(50, limit))
    offset = max(0, offset)
    s = _find_streamer(db, ext_id.strip(), name.strip())
    if s is None:
        raise HTTPException(404, "复盘花名册中未找到该主播（ext_id 未绑定且花名不唯一/不存在）")

    sess_ids = select(StreamerTurn.session_id).where(StreamerTurn.streamer_id == s.id).distinct()
    total = db.execute(
        select(func.count(ReplaySession.id)).where(ReplaySession.id.in_(sess_ids))
    ).scalar() or 0
    rows = db.execute(
        select(ReplaySession).where(ReplaySession.id.in_(sess_ids))
        .order_by(ReplaySession.id.desc()).limit(limit).offset(offset)
    ).scalars().all()

    sessions = []
    for r in rows:
        turns = db.execute(
            select(StreamerTurn).where(
                StreamerTurn.session_id == r.id, StreamerTurn.streamer_id == s.id
            ).order_by(StreamerTurn.seq)
        ).scalars().all()
        summary = _my_summary(db, r.id, s.id)
        alert_count = db.execute(
            select(func.count(Critique.id)).where(
                Critique.session_id == r.id,
                Critique.streamer_id == s.id,
                Critique.level != CritiqueLevel.summary,
                Critique.severity.in_([Severity.warn, Severity.error]),
            )
        ).scalar() or 0
        comment = (summary.comment or "") if summary else ""
        sessions.append({
            "session_id": r.id,
            "title": r.title,
            "status": r.status.value,
            "report_status": getattr(r, "report_status", "pending") or "pending",
            "deep_status": getattr(r, "deep_status", "not_requested") or "not_requested",
            "started_at": _iso(r.started_at),
            "ended_at": _iso(r.ended_at),
            "my_turns": [{"start_ms": t.start_ms, "end_ms": t.end_ms} for t in turns],
            "my_duration_ms": sum(max(0, t.end_ms - t.start_ms) for t in turns),
            "score": summary.score if summary else None,
            "summary_ready": summary is not None,       # off 信号当场就出总结，processing 即可读
            "summary_excerpt": comment[:120],
            "alert_count": alert_count,
        })
    return {"ok": True,
            "streamer": {"id": s.id, "name": s.name, "ext_id": s.ext_id},
            "total": total, "sessions": sessions}


@router.get("/replays/{session_id}")
def replay_detail(
    session_id: int,
    request: Request,
    ext_id: str = Query(""),
    name: str = Query(""),
    db: Session = Depends(get_db),
):
    _auth(request)
    s = _find_streamer(db, ext_id.strip(), name.strip())
    if s is None:
        raise HTTPException(404, "复盘花名册中未找到该主播")
    row = db.get(ReplaySession, session_id)
    if row is None:
        raise HTTPException(404, "场次不存在")

    turns = db.execute(
        select(StreamerTurn).where(
            StreamerTurn.session_id == row.id, StreamerTurn.streamer_id == s.id
        ).order_by(StreamerTurn.seq)
    ).scalars().all()
    # 该主播未参与本场：返回空数据（不报错——防猜 session_id 探他人数据）
    if not turns:
        return {"ok": True, "session": {"session_id": row.id, "title": row.title,
                                        "status": row.status.value,
                                        "report_status": getattr(row, "report_status", "pending") or "pending",
                                        "deep_status": getattr(row, "deep_status", "not_requested") or "not_requested",
                                        "started_at": _iso(row.started_at),
                                        "ended_at": _iso(row.ended_at)},
                "summary": None, "critiques": [], "turns": []}

    summary = _my_summary(db, row.id, s.id)
    summary_out = None
    if summary:
        try:
            data = json.loads(summary.data) if summary.data else {}
        except json.JSONDecodeError:
            data = {}
        summary_out = {"comment": summary.comment, "score": summary.score, "data": data}

    crits = db.execute(
        select(Critique).where(
            Critique.session_id == row.id,
            Critique.streamer_id == s.id,
            Critique.level != CritiqueLevel.summary,
        )
    ).scalars().all()
    crits.sort(key=lambda c: (_SEV_RANK.get(c.severity, 9), c.start_ms))
    critiques = [{
        "level": c.level.value, "severity": c.severity.value, "category": c.category,
        "start_ms": c.start_ms, "end_ms": c.end_ms,
        "quote": (c.quote or "")[:200], "comment": c.comment, "score": c.score,
    } for c in crits[:50]]

    return {"ok": True,
            "session": {"session_id": row.id, "title": row.title, "status": row.status.value,
                        "report_status": getattr(row, "report_status", "pending") or "pending",
                        "deep_status": getattr(row, "deep_status", "not_requested") or "not_requested",
                        "started_at": _iso(row.started_at), "ended_at": _iso(row.ended_at)},
            "summary": summary_out,
            "critiques": critiques,
            "turns": [{"start_ms": t.start_ms, "end_ms": t.end_ms} for t in turns]}


# ---------- 管理端（业绩 admin 经 BFF 调用；同 partner_token + 回环） ----------

@router.get("/admin/replays")
def admin_list_replays(
    request: Request,
    date: str = Query(""),        # YYYY-MM-DD：列该天的场次；空=最近 limit 场
    ext_id: str = Query(""),      # 只看某主播参与的场次
    limit: int = Query(20),
    db: Session = Depends(get_db),
):
    """全员回放管理列表：每场附各主播的班次时长/评分/告警数（管理员整天复盘视角）。"""
    _auth(request)
    limit = max(1, min(60, limit))
    # 自动轮询失败等 error 场次是系统噪音，不进管理列表
    q = (select(ReplaySession)
         .where(ReplaySession.status != SessionStatus.error)
         .order_by(ReplaySession.id.desc()))
    if date:
        try:
            day = datetime.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, "date 须为 YYYY-MM-DD")
        q = q.where(ReplaySession.started_at >= day,
                    ReplaySession.started_at < day.replace(hour=23, minute=59, second=59))
    target: Streamer | None = None
    if ext_id.strip():
        target = _find_streamer(db, ext_id.strip(), "")
        if target is None:
            return {"ok": True, "sessions": []}
        sess_ids = select(StreamerTurn.session_id).where(StreamerTurn.streamer_id == target.id).distinct()
        q = q.where(ReplaySession.id.in_(sess_ids))
    rows = db.execute(q.limit(limit)).scalars().all()

    out = []
    for r in rows:
        turns = db.execute(
            select(StreamerTurn).where(StreamerTurn.session_id == r.id).order_by(StreamerTurn.seq)
        ).scalars().all()
        by_sid: dict[int, int] = {}
        for t in turns:
            by_sid[t.streamer_id] = by_sid.get(t.streamer_id, 0) + max(0, t.end_ms - t.start_ms)
        participants = []
        for sid, dur in by_sid.items():
            if target is not None and sid != target.id:
                continue
            s = db.get(Streamer, sid)
            summary = _my_summary(db, r.id, sid)
            alert_count = db.execute(
                select(func.count(Critique.id)).where(
                    Critique.session_id == r.id, Critique.streamer_id == sid,
                    Critique.level != CritiqueLevel.summary,
                    Critique.severity.in_([Severity.warn, Severity.error]),
                )
            ).scalar() or 0
            data = {}
            if summary and summary.data:
                try:
                    data = json.loads(summary.data)
                except json.JSONDecodeError:
                    data = {}
            participants.append({
                "streamer_id": sid,
                "name": s.name if s else f"主播{sid}",
                "ext_id": s.ext_id if s else None,
                "duration_ms": dur,
                "score": summary.score if summary else None,
                "summary_ready": summary is not None,
                "summary_excerpt": (summary.comment or "")[:100] if summary else "",
                "alert_count": alert_count,
                "prev_compare": data.get("prev_compare"),
            })
        out.append({
            "session_id": r.id, "title": r.title, "status": r.status.value,
            "report_status": getattr(r, "report_status", "pending") or "pending",
            "deep_status": getattr(r, "deep_status", "not_requested") or "not_requested",
            "started_at": _iso(r.started_at), "ended_at": _iso(r.ended_at),
            "participants": participants,
        })
    return {"ok": True, "sessions": out}


# ===== 罗盘数据变化归因 + 成功技巧沉淀（管理员复盘中心 BFF，回环+token）=====
@router.get("/admin/compass-review")
def admin_compass_review(request: Request, rid: int = Query(...), db: Session = Depends(get_db)):
    """某场罗盘数据变化归因（收尾算好、存 category=罗盘归因 的整场级 Critique）。"""
    _auth(request)
    row = db.execute(select(Critique).where(
        Critique.session_id == rid,
        Critique.level == CritiqueLevel.summary,
        Critique.category == "罗盘归因")).scalars().first()
    if not row or not row.data:
        return {"ok": True, "available": False}
    try:
        return {"ok": True, "available": True, **json.loads(row.data)}
    except json.JSONDecodeError:
        return {"ok": True, "available": False}


@router.get("/admin/patterns")
def admin_patterns(request: Request, db: Session = Depends(get_db)):
    """标准库（2026-07-10 整改）：数据验证长出来的讲法模式账本。
    validated=已验证标准（带标准卡）；observing=积累中；negative=反面模式。"""
    _auth(request)
    from ..models import PatternLedger
    rows = db.execute(select(PatternLedger).order_by(
        PatternLedger.status, PatternLedger.sample_count.desc())).scalars().all()
    out = []
    for r in rows:
        rate = round(r.positive_count * 100 / max(1, r.sample_count))
        item = {"id": r.id, "scene": r.scene, "rep_text": (r.rep_text or "")[:160],
                "sample_count": r.sample_count, "positive_rate": rate,
                "status": r.status, "updated_at": str(r.updated_at or "")[:16]}
        if r.standard_card:
            try:
                item["card"] = json.loads(r.standard_card)
            except json.JSONDecodeError:
                pass
        out.append(item)
    return {"ok": True, "patterns": out}


@router.get("/admin/experience")
def admin_experience(
    request: Request,
    rid: int | None = Query(None),
    status: str | None = Query(None),
):
    """统计技巧列表；rid 可选，省略时可用 status=pending 拉取跨场待采纳队列。"""
    _auth(request)
    from ..core.review.experience import list_tips
    return {"ok": True, "tips": list_tips(status=status, session_id=rid)}


@router.post("/admin/experience/{tip_id}/adopt")
async def admin_adopt_experience(request: Request, tip_id: int):
    """采纳技巧 → embed 入知识库供点评复用。"""
    _auth(request)
    from ..core.review.experience import adopt_tip
    return await adopt_tip(tip_id)


@router.post("/admin/experience/{tip_id}/reject")
def admin_reject_experience(request: Request, tip_id: int):
    """驳回技巧（不入库）。"""
    _auth(request)
    from ..core.review.experience import reject_tip
    return reject_tip(tip_id)


@router.get("/admin/compass-setup")
def admin_compass_setup(request: Request, db: Session = Depends(get_db)):
    """罗盘全局接入信息（油猴脚本/书签 + 采集状态），供业绩后台管理员复盘中心展示。"""
    _auth(request)
    from .compass import setup_payload
    return setup_payload(db)   # 上报地址取 PUBLIC_BASE_URL（含 /fupan 前缀）


@router.get("/admin/room-mode")
def get_room_mode(request: Request, db: Session = Depends(get_db)):
    """直播间整天录制模式：auto_record=True 表示开播即录（不依赖主播打开复盘）。"""
    _auth(request)
    rooms = db.execute(select(LiveRoom).where(LiveRoom.deleted_at.is_(None))).scalars().all()
    return {"ok": True, "rooms": [
        {"id": r.id, "room_id": r.room_id, "title": r.title, "auto_record": bool(r.auto_record)}
        for r in rooms
    ]}


@router.put("/admin/room-mode")
def set_room_mode(
    request: Request,
    room_pk: int = Query(...),
    auto_record: bool = Query(...),
    db: Session = Depends(get_db),
):
    _auth(request)
    room = db.get(LiveRoom, room_pk)
    if room is None:
        raise HTTPException(404, "直播间不存在")
    room.auto_record = auto_record
    db.commit()
    return {"ok": True, "room_id": room.room_id, "auto_record": bool(room.auto_record)}


# =====================================================================
# 主播免密只读直达页（业绩后台「查看完整复盘」跳转用）
# 安全模型：业绩后端持 partner_token 调 view-url 拿签名链接（校验主播确实参与本场）；
# 主播浏览器带签名参数直接调 /view/{sid}（公网、免 cookie），签名绑定 场次+主播+有效期，
# 改任何参数即失效——只能看自己的、这一场的、限时的数据，写操作一概没有。
# =====================================================================

def _view_sig(session_id: int, streamer_id: int, exp: int) -> str:
    key = (get_settings().partner_token or "").encode()
    return hmac.new(key, f"view|{session_id}|{streamer_id}|{exp}".encode(), "sha256").hexdigest()[:40]


@router.get("/replays/{session_id}/view-url")
def replay_view_url(
    request: Request,
    session_id: int,
    ext_id: str = Query(""),
    name: str = Query(""),
    db: Session = Depends(get_db),
):
    """业绩后端调用：为该主播生成本场的免密只读直达链接（4 小时有效）。"""
    _auth(request)
    streamer = _find_streamer(db, ext_id.strip(), name.strip())
    if streamer is None:
        raise HTTPException(404, "复盘花名册中没有该主播")
    row = db.get(ReplaySession, session_id)
    if row is None:
        raise HTTPException(404, "场次不存在")
    joined = db.execute(select(StreamerTurn.id).where(
        StreamerTurn.session_id == session_id,
        StreamerTurn.streamer_id == streamer.id).limit(1)).first()
    lineup_ids = []
    try:
        lineup_ids = json.loads(row.lineup or "[]")
    except json.JSONDecodeError:
        pass
    if not joined and streamer.id not in lineup_ids:
        raise HTTPException(403, "该主播未参与本场直播")
    exp = int(time.time()) + 4 * 3600
    sig = _view_sig(session_id, streamer.id, exp)
    return {"ok": True, "url": f"/?view={session_id}&s={streamer.id}&exp={exp}&sig={sig}"}


@router.get("/view/{session_id}")
def replay_view_data(
    session_id: int,
    s: int = Query(...),
    exp: int = Query(...),
    sig: str = Query(...),
    db: Session = Depends(get_db),
):
    """签名直达数据面（公网免 cookie）：该主播本场的全貌——媒体、逐字稿、点评、整场总结。"""
    if exp < int(time.time()):
        raise HTTPException(403, "链接已过期，请回业绩后台重新点击「查看完整复盘」")
    if not hmac.compare_digest(sig, _view_sig(session_id, s, exp)):
        raise HTTPException(403, "签名无效")
    row = db.get(ReplaySession, session_id)
    if row is None:
        raise HTTPException(404, "场次不存在")
    streamer = db.get(Streamer, s)
    if streamer is None:
        raise HTTPException(404, "主播不存在")

    turns = db.execute(select(StreamerTurn).where(
        StreamerTurn.session_id == session_id, StreamerTurn.streamer_id == s)
        .order_by(StreamerTurn.start_ms)).scalars().all()
    trs = db.execute(select(Transcript).where(
        Transcript.session_id == session_id, Transcript.streamer_id == s)
        .order_by(Transcript.start_ms)).scalars().all()
    crits = db.execute(select(Critique).where(
        Critique.session_id == session_id, Critique.streamer_id == s)
        .order_by(Critique.start_ms)).scalars().all()

    media = {"available": False, "kind": "audio", "url": None}
    af = row.audio
    if af and af.status == AudioStatus.available and af.cos_key:
        if af.cos_key.endswith(".m3u8"):
            media = {"available": True, "kind": getattr(af, "media_kind", "video") or "video", "hls": True,
                     "url": f"/api/partner/view-hls/{session_id}.m3u8?s={s}&exp={exp}&sig={sig}"}
        else:
            url = media_store.presigned_url(af.cos_key, expires=4 * 3600)
            if url:
                media = {"available": True, "kind": getattr(af, "media_kind", "audio") or "audio", "url": url}

    # 整场归因（弱信号/成交，整场维度，只读展示无妨）
    from ..core.attribution import analyze_attribution
    from ..core.danmaku_signal import analyze_session_danmaku
    try:
        danmaku = analyze_session_danmaku(session_id)
    except Exception:  # noqa: BLE001
        danmaku = None
    try:
        attribution = analyze_attribution(session_id)
    except Exception:  # noqa: BLE001
        attribution = None

    return {
        "ok": True,
        "session": {"id": row.id, "title": row.title, "status": row.status.value,
                    "report_status": getattr(row, "report_status", "pending") or "pending",
                    "deep_status": getattr(row, "deep_status", "not_requested") or "not_requested",
                    "error_code": row.error_code, "error_msg": row.error_msg,
                    "started_at": _iso(row.started_at), "ended_at": _iso(row.ended_at)},
        "streamer": {"id": streamer.id, "name": streamer.name},
        "turns": [{"start_ms": t.start_ms, "end_ms": t.end_ms} for t in turns],
        "transcripts": [{"start_ms": t.start_ms, "streamer_id": t.streamer_id,
                         "corrected_text": t.corrected_text or t.raw_text} for t in trs],
        "critiques": [{"level": c.level.value, "severity": c.severity.value, "category": c.category,
                       "start_ms": c.start_ms, "quote": c.quote, "comment": c.comment,
                       "score": c.score, "data": c.data, "streamer_id": c.streamer_id,
                       "llm_provider": c.llm_provider,
                       "llm_requested_provider": c.llm_requested_provider,
                       "is_fallback": c.is_fallback} for c in crits],
        "media": media,
        "danmaku": danmaku,
        "attribution": attribution,
    }


@router.delete("/admin/replays/{session_id}")
def admin_delete_replay(
    request: Request,
    session_id: int,
    mode: str = Query("all", pattern="^(all|media)$"),
    db: Session = Depends(get_db),
):
    """管理员删除复盘（业绩后台复盘管理页调用）。
    mode=media：只删录像/录音（本地+COS+分段），保留逐字稿/点评/总结；
    mode=all：整场彻底删除（媒体+全部复盘数据，不可恢复）。
    正在录制/生成中的场次拒绝删除（先等收播完成）。"""
    _auth(request)
    row = db.get(ReplaySession, session_id)
    if row is None:
        raise HTTPException(404, "场次不存在")
    if row.status in (SessionStatus.live, SessionStatus.processing):
        raise HTTPException(409, "该场还在录制/生成中，请等本场结束后再删除")

    af = row.audio
    if af:
        if af.cos_key:
            try:
                if af.cos_key.endswith(".m3u8"):
                    media_store.delete_prefix(f"replay/session_{session_id}/")
                    media_store.delete(f"replay/session_{session_id}.m4a")
                else:
                    media_store.delete(af.cos_key)
            except Exception:  # noqa: BLE001
                pass
        if af.path:
            p = Path(af.path)
            p.unlink(missing_ok=True)
            if p.name:
                for part in p.parent.glob(f"{p.stem}.part*{p.suffix}"):
                    part.unlink(missing_ok=True)
    if mode == "media":
        if af:
            af.status = AudioStatus.deleted
            af.cos_key = ""
            af.size_bytes = 0
        db.commit()
        return {"ok": True, "deleted": "media"}

    # 整场删除：cascade 覆盖 audio/transcripts/critiques/ocr/turns；danmaku_refines 无 relationship 手动清
    db.query(DanmakuRefine).filter(DanmakuRefine.session_id == session_id).delete()
    db.delete(row)
    db.commit()
    return {"ok": True, "deleted": "session"}


@router.get("/view-hls/{session_id}.m3u8")
def replay_view_hls(
    session_id: int,
    s: int = Query(...),
    exp: int = Query(...),
    sig: str = Query(...),
    db: Session = Depends(get_db),
):
    """免密直达页的 HLS 播放清单（同 view 签名校验，逐切片重写签名 URL）。"""
    if exp < int(time.time()):
        raise HTTPException(403, "链接已过期")
    if not hmac.compare_digest(sig, _view_sig(session_id, s, exp)):
        raise HTTPException(403, "签名无效")
    row = db.get(ReplaySession, session_id)
    af = row.audio if row else None
    if not af or af.status != AudioStatus.available or not (af.cos_key or "").endswith(".m3u8"):
        raise HTTPException(404, "本场无 HLS 媒体")
    from .sessions import _legacy_reset_ts
    text = media_store.rewrite_hls_playlist(
        af.cos_key, expires=4 * 3600,
        per_segment_discontinuity=_legacy_reset_ts(db, session_id))
    if text is None:
        raise HTTPException(502, "云端播放清单读取失败")
    return Response(content=text, media_type="application/vnd.apple.mpegurl")


# ========== 直达页阅读行为：页面上报（beacon）+ 业绩系统拉取（2026-09-29） ==========
# 页面加载成功发一次 kind=open，之后每 15 秒报一次本周期增量（kind=beat），页面隐藏/离开时用
# navigator.sendBeacon 冲掉余量——其 Content-Type 常是 text/plain，所以按原始请求体解析 JSON。
# 校验与 /view/{sid} 完全相同（过期或签名不符 403），不需要 X-Partner-Token；签名链接只发给
# 主播本人，行为一律记在 s 名下。防刷：active/video 每次 ≤30 秒且 ≤ 距上次上报秒数+5；
# clips 每次 ≤10；seen 每次 ≤100 个、每个 ≤200 字符。

_BEACON_MAX_BODY = 64 * 1024        # 与浏览器 sendBeacon 的上限一致
_BEACON_MAX_SECONDS = 30.0
_BEACON_SLACK_SECONDS = 5.0
_BEACON_MAX_CLIPS = 10
_BEACON_MAX_SEEN = 100
_BEACON_MAX_KEY_LEN = 200
_SEEN_KEYS_CAP = 2000               # 单行累计上限（正常一场的问题卡远少于此），防被刷爆
_beacon_lock = threading.Lock()     # 单进程内串行化"读-改-写"，防并发上报互相覆盖增量


def _check_view_link(session_id: int, s: int, exp: int, sig: str) -> None:
    """签名直达校验，与 /view/{sid} 相同：过期或签名不符 → 403。"""
    if exp < int(time.time()):
        raise HTTPException(403, "链接已过期，请回业绩后台重新点击「查看完整复盘」")
    # 非 ASCII 的 sig 会让 compare_digest 抛 TypeError（变 500），直接按签名无效处理
    if not sig.isascii() or not hmac.compare_digest(sig, _view_sig(session_id, s, exp)):
        raise HTTPException(403, "签名无效")


async def _beacon_raw_body(request: Request) -> bytes:
    """读原始请求体（≤64KB），不看 Content-Type（sendBeacon 发字符串时是 text/plain）。"""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _BEACON_MAX_BODY:
        raise HTTPException(413, "上报数据过大")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > _BEACON_MAX_BODY:
            raise HTTPException(413, "上报数据过大")
    return bytes(buf)


def _beacon_num(value, hi: float) -> float:  # noqa: ANN001
    """上报数值归一：非数字 / 布尔 / ≤0 / NaN / ∞ / 溢出一律记 0，封顶 hi。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        v = float(value)
    except OverflowError:
        return 0.0
    if not math.isfinite(v) or v <= 0:
        return 0.0
    return min(v, hi)


def _load_seen(raw: str | None) -> list[str]:
    try:
        keys = json.loads(raw or "[]")
    except (ValueError, RecursionError):
        return []
    return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


def _apply_view_beacon(db: Session, session_id: int, streamer_id: int, kind: str,
                       active: float, video: float, clips: int, seen: list[str]) -> dict:
    now = datetime.now()
    row = db.execute(select(ViewActivity).where(
        ViewActivity.session_id == session_id,
        ViewActivity.streamer_id == streamer_id)).scalars().first()
    cap = _BEACON_MAX_SECONDS
    if row is None:
        row = ViewActivity(session_id=session_id, streamer_id=streamer_id,
                           active_seconds=0.0, video_seconds=0.0, clips_played=0,
                           seen_keys="[]", opens=0, first_at=now, last_at=now)
        db.add(row)
    elif row.last_beat_at is not None:
        elapsed = max(0.0, (now - row.last_beat_at).total_seconds())
        cap = min(cap, elapsed + _BEACON_SLACK_SECONDS)
    add_active = min(active, cap)
    add_video = min(video, cap)
    keys = _load_seen(row.seen_keys)
    have = set(keys)
    new_keys = [k for k in seen if k not in have][:max(0, _SEEN_KEYS_CAP - len(keys))]

    row.active_seconds = (row.active_seconds or 0.0) + add_active
    row.video_seconds = (row.video_seconds or 0.0) + add_video
    row.clips_played = (row.clips_played or 0) + clips
    if new_keys:
        row.seen_keys = json.dumps(keys + new_keys, ensure_ascii=False)
    if kind == "open":
        row.opens = (row.opens or 0) + 1
    if kind == "open" or add_active > 0 or add_video > 0 or clips or new_keys:
        row.last_at = now          # last_at = 最近一次有实际行为的上报
    row.last_beat_at = now         # 防刷计时：距上次上报（任何一次）的秒数
    db.commit()
    return {"ok": True, "accepted": {"active": round(add_active, 2), "video": round(add_video, 2),
                                      "clips": clips, "seen": len(new_keys)}}


@router.post("/view/{session_id}/beacon")
def replay_view_beacon(
    session_id: int,
    s: int = Query(...),
    exp: int = Query(...),
    sig: str = Query(...),
    raw: bytes = Depends(_beacon_raw_body),
    db: Session = Depends(get_db),
):
    """直达页阅读行为上报（公网免 cookie，签名校验同 /view）。首次上报建行，之后按防刷上限累加。"""
    _check_view_link(session_id, s, exp, sig)
    try:
        body = json.loads(raw.decode("utf-8-sig")) if raw.strip() else None
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体须为 JSON 对象")
    kind = body.get("kind")
    if kind not in ("open", "beat"):
        raise HTTPException(400, "kind 须为 open 或 beat")
    if db.get(ReplaySession, session_id) is None:
        raise HTTPException(404, "场次不存在")
    if db.get(Streamer, s) is None:
        raise HTTPException(404, "主播不存在")

    active = _beacon_num(body.get("active"), _BEACON_MAX_SECONDS)
    video = _beacon_num(body.get("video"), _BEACON_MAX_SECONDS)
    clips = int(_beacon_num(body.get("clips"), _BEACON_MAX_CLIPS))
    seen: list[str] = []
    raw_seen = body.get("seen")
    if isinstance(raw_seen, list):
        for k in raw_seen[:_BEACON_MAX_SEEN]:
            if isinstance(k, str) and 0 < len(k) <= _BEACON_MAX_KEY_LEN and k not in seen:
                seen.append(k)

    with _beacon_lock:
        try:
            return _apply_view_beacon(db, session_id, s, kind, active, video, clips, seen)
        except IntegrityError:
            # 另一进程抢先建了同一行（唯一约束）：回滚后按已有行再算一次
            db.rollback()
            return _apply_view_beacon(db, session_id, s, kind, active, video, clips, seen)


@router.get("/replays/{session_id}/view-activity")
def replay_view_activity(
    session_id: int,
    request: Request,
    ext_id: str = Query(""),
    name: str = Query(""),
    db: Session = Depends(get_db),
):
    """业绩后端调用：该主播在本场直达页上的阅读行为累计；从未上报过时 activity 为 null。"""
    _auth(request)
    s = _find_streamer(db, ext_id.strip(), name.strip())
    if s is None:
        raise HTTPException(404, "复盘花名册中未找到该主播")
    if db.get(ReplaySession, session_id) is None:
        raise HTTPException(404, "场次不存在")
    row = db.execute(select(ViewActivity).where(
        ViewActivity.session_id == session_id,
        ViewActivity.streamer_id == s.id)).scalars().first()
    if row is None:
        return {"ok": True, "activity": None}
    return {"ok": True, "activity": {
        "active_seconds": int(round(row.active_seconds or 0)),
        "video_seconds": int(round(row.video_seconds or 0)),
        "clips_played": int(row.clips_played or 0),
        "seen_keys": _load_seen(row.seen_keys),
        "opens": int(row.opens or 0),
        "first_at": _iso(row.first_at),
        "last_at": _iso(row.last_at),
    }}


# ========== 视频复盘报告：AI 出题 / 视频上传 / 播放签名（2026-07-10） ==========
# 业绩系统无对象存储 → 视频复盘报告整块放这里：按某场问题 AI 出 5-10 题，主播每题录一段
# 短视频（业绩后端回环转发字节到这里）存 COS，管理员看时给播放签名 URL。

_REPORT_Q_SYS = (
    "你是直播复盘教练。根据一场直播里发现的问题，给主播出复盘自省题，"
    "让主播对着镜头逐题口述回答。要求："
    "①每题只针对一个具体问题，直接、犀利、可回答；"
    "②题目数量按问题多少定：问题少出 5 题，问题多最多 10 题；"
    "③题干里点明是哪个问题（如“这场有 3 次逼单没给下单理由”），再问“你打算下次怎么改”；"
    "④只输出 JSON：{\"questions\":[\"...\",\"...\"]}，不要多余文字。"
)


@router.post("/replay-report/questions")
async def replay_report_questions(request: Request, db: Session = Depends(get_db)):
    """按某复盘场次的问题，用 LLM 出 5-10 道复盘自省题。业绩后端回环调用。"""
    _auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        sid = int(body.get("session_id") or 0)
    except (TypeError, ValueError):
        sid = 0
    ext_id = str(body.get("ext_id") or "")
    if not sid:
        raise HTTPException(400, "缺少 session_id")

    q = select(Critique).where(
        Critique.session_id == sid, Critique.level != CritiqueLevel.summary)
    if ext_id:
        s = db.execute(select(Streamer).where(Streamer.ext_id == ext_id)).scalars().first()
        if s:
            q = q.where(Critique.streamer_id == s.id)
    crits = db.execute(q).scalars().all()
    crits.sort(key=lambda c: (_SEV_RANK.get(c.severity, 9), c.start_ms))
    problems = [f"[{c.category}] {c.comment}（原话：{(c.quote or '')[:60]}）"
                for c in crits[:20] if (c.comment or "").strip()]

    if not problems:
        # 没抓到明确问题：出通用复盘题（也要 5 题）
        return {"ok": True, "questions": [
            "这一场你自己最满意的一个环节是什么？为什么？",
            "这一场哪个环节转化最差？你觉得问题出在哪？",
            "有没有哪一款产品你觉得没讲透？下次怎么讲？",
            "这场的节奏（快/慢/停顿）你怎么评价？哪里要调整？",
            "下一场你打算具体改进的一件事是什么？怎么做？",
        ]}

    from ..core.llm import get_llm
    user = "本场发现的问题如下，请出题：\n" + "\n".join(problems)
    try:
        raw = await get_llm("fast").complete(_REPORT_Q_SYS, user, json_mode=True)
        data = json.loads(raw)
        questions = [str(x).strip() for x in (data.get("questions") or []) if str(x).strip()]
        questions = questions[:10]
        if len(questions) < 3:
            raise ValueError("题目过少")
    except Exception as e:  # noqa: BLE001
        print(
            "[replay-report] question_generation status=failed; "
            f"code={safe_exception_code(e, prefix='REPORT_QUESTION')}; "
            f"error={safe_error_text(e)}"
        )
        questions = [f"针对「{p[:40]}」，你打算下次怎么改？" for p in problems[:6]]
    return {"ok": True, "questions": questions}


@router.post("/replay-report/upload")
async def replay_report_upload(request: Request):
    """接收一段答题视频（base64，业绩后端回环转发），存 COS，返回 key。"""
    _auth(request)
    import base64
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    b64 = str(body.get("video_base64") or "")
    key = str(body.get("key") or "")
    if not b64 or not key or not key.startswith("replay-reports/"):
        raise HTTPException(400, "缺少 video_base64 或 key 非法")
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "video_base64 解码失败")
    if len(raw) > 60 * 1024 * 1024:
        raise HTTPException(413, "单段视频过大（>60MB）")
    if not media_store.enabled():
        raise HTTPException(503, "COS 未启用，无法存储视频")
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        f.write(raw)
        tmp = f.name
    try:
        ok = media_store.upload_file(tmp, key)
    finally:
        os.unlink(tmp)
    if not ok:
        raise HTTPException(502, "视频上传 COS 失败")
    return {"ok": True, "key": key, "bytes": len(raw)}


@router.get("/replay-report/video-url")
def replay_report_video_url(request: Request, key: str = Query(...)):
    """给答题视频生成播放签名 URL（管理员回放）。业绩后端回环调用。"""
    _auth(request)
    if not key.startswith("replay-reports/"):
        raise HTTPException(400, "key 非法")
    url = media_store.presigned_url(key, expires=4 * 3600)
    return {"ok": True, "available": bool(url), "url": url}


# ========== 改进动作闭环（第一期，2026-10）：业绩系统读写主播的「改进动作」 ==========
# 鉴权同上（X-Partner-Token + 只认本机回环）。主播两种传法都认：
#   - 复盘自己的 streamer_id（/replays 返回的 streamer.id、/admin/replays 的 participants[].streamer_id）；
#   - 业绩系统的主播 id（= 复盘 streamers.ext_id）：路径用 by-ext/{ext_id}，或 query / body 里的 ext_id，
#     查法与 /replays 相同（ext_id 优先；另给 name 且花名唯一时兜底）。
# 找不到对应主播（比如业绩的测试账号）时，读接口返回空结果（found=false），不报错。
# 写操作 body 里带 actor（操作人）；「记住了」必须带 ext_id 或 streamer_id，动作不是这位主播的返回 403。
# 返回的动作都带 needs_ack（在练且还没点「记住了」）；带主播的地方都给 {id, name, ext_id}。


_FOCUS_MAX_BODY = 16 * 1024


async def _focus_body(request: Request) -> dict:
    """写接口的请求体：先验 token 和回环，再读 ≤16KB 的 JSON 对象。"""
    _auth(request)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _FOCUS_MAX_BODY:
        raise HTTPException(413, "请求体太大")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > _FOCUS_MAX_BODY:
            raise HTTPException(413, "请求体太大")
    if not bytes(buf).strip():
        return {}
    try:
        body = json.loads(bytes(buf).decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, "请求体须为 JSON 对象") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体须为 JSON 对象")
    return body


def _focus_do(fn, *args):  # noqa: ANN001
    try:
        return fn(*args)
    except focus_store.FocusError as exc:
        raise HTTPException(exc.status, exc.message) from None


def _focus_lookup(db: Session, key: str = "", ext_id: str = "", name: str = "") -> Streamer | None:
    """路径里的 key：纯数字 = 复盘 streamer_id，否则当业绩的主播 id（ext_id）。找不到返回 None。"""
    key = (key or "").strip()
    if key.isdigit():
        return db.get(Streamer, int(key))
    ext_id = key or (ext_id or "").strip()
    name = (name or "").strip()
    if not ext_id and not name:
        return None
    return _find_streamer(db, ext_id, name)


def _focus_owner(db: Session, body: dict, *, required: bool) -> Streamer | None:
    """body 里的 streamer_id / ext_id → 主播；给了却对不上任何主播，就不可能是他的动作 → 403。"""
    raw_id = body.get("streamer_id")
    ext_id = body.get("ext_id")
    if raw_id is None and not ext_id:
        if required:
            raise HTTPException(400, "缺少 ext_id（或 streamer_id），用来核对这条动作是不是这位主播的")
        return None
    if raw_id is not None:
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise HTTPException(400, "streamer_id 要填整数")
        streamer = db.get(Streamer, raw_id)
    else:
        if not isinstance(ext_id, str):
            raise HTTPException(400, "ext_id 要填字符串")
        streamer = _find_streamer(db, ext_id.strip(), "")
    if streamer is None:
        raise HTTPException(403, "这条动作不是这位主播的")
    return streamer


def _focus_log(what: str, action_id: int | str, actor: str, result: dict) -> None:
    target = (result.get("action") or {}).get("id")
    extra = f" new={target}" if target and target != action_id else ""
    print(f"[操作] 业绩系统(partner) 改进动作 {what} action={action_id}{extra} by={actor}")


@router.get("/focus/streamers/by-ext/{ext_id}")
def focus_streamer_by_ext(ext_id: str, request: Request, name: str = Query(""),
                          db: Session = Depends(get_db)):
    """按业绩主播 id 看这位主播：在练的动作、最近过关的、最近 10 场核对历史。"""
    _auth(request)
    return focus_store.streamer_view(db, _focus_lookup(db, ext_id=ext_id, name=name))


@router.get("/focus/streamers/{streamer_key}")
def focus_streamer(streamer_key: str, request: Request, db: Session = Depends(get_db)):
    """streamer_key：复盘 streamer_id（纯数字）或业绩主播 id。找不到主播返回空结果。"""
    _auth(request)
    return focus_store.streamer_view(db, _focus_lookup(db, streamer_key))


@router.get("/focus/sessions/{session_id}")
def focus_session(
    session_id: int,
    request: Request,
    streamer_id: int | None = Query(None),
    ext_id: str = Query(""),
    name: str = Query(""),
    db: Session = Depends(get_db),
):
    """这场这位主播报告里的 focus 块（核对结果、接下来要练的全部动作、候选）。"""
    _auth(request)
    streamer = (db.get(Streamer, streamer_id) if streamer_id is not None
                else _focus_lookup(db, ext_id=ext_id, name=name))
    return focus_store.session_view(db, session_id, streamer)


@router.post("/focus/actions/{action_id}/ack")
def focus_ack(action_id: int, body: dict = Depends(_focus_body), db: Session = Depends(get_db)):
    """主播点「记住了」：body {ext_id 或 streamer_id, actor?}。只能确认自己的动作；重复点不变。"""
    owner = _focus_owner(db, body, required=True)
    actor = _focus_do(lambda: focus_store.clean_actor(body.get("actor"), default=owner.name or ""))
    result = _focus_do(focus_store.ack_action, action_id, owner.id, actor)
    if result.get("changed"):
        _focus_log("确认", action_id, actor, result)
    return result


@router.put("/focus/actions/{action_id}")
def focus_edit(action_id: int, body: dict = Depends(_focus_body), db: Session = Depends(get_db)):
    """改一条在练动作：body {text?, trigger?, criterion?, example?, actor}。改后要主播重新确认。"""
    owner = _focus_owner(db, body, required=False)
    actor = _focus_do(focus_store.clean_actor, body.get("actor"))
    fields = {key: body[key] for key in ("text", "trigger", "criterion", "example", "threshold_sec")
              if key in body}
    result = _focus_do(focus_store.edit_action, action_id, fields, actor,
                       owner.id if owner else None)
    if result.get("changed"):
        _focus_log("修改", action_id, actor, result)
    return result


@router.post("/focus/actions/{action_id}/replace")
def focus_replace(action_id: int, body: dict = Depends(_focus_body), db: Session = Depends(get_db)):
    """换掉一条在练动作：body {candidate_index, session_id?} 或 {text, trigger, criterion, example,
    kind, threshold_sec}，加 actor。旧的标「已换掉」，新的另起 id、等主播确认。"""
    owner = _focus_owner(db, body, required=False)
    actor = _focus_do(focus_store.clean_actor, body.get("actor"))
    result = _focus_do(focus_store.replace_action, action_id, body, actor,
                       owner.id if owner else None)
    _focus_log("替换", action_id, actor, result)
    return result


@router.post("/focus/actions/{action_id}/remove")
def focus_remove(action_id: int, body: dict = Depends(_focus_body), db: Session = Depends(get_db)):
    """删掉一条在练动作（不补新的；同一个问题以后也不再自动加回来）：body {actor}。"""
    owner = _focus_owner(db, body, required=False)
    actor = _focus_do(focus_store.clean_actor, body.get("actor"))
    result = _focus_do(focus_store.remove_action, action_id, actor, owner.id if owner else None)
    _focus_log("删除", action_id, actor, result)
    return result


def _focus_add(streamer: Streamer | None, body: dict) -> dict:
    if streamer is None:
        raise HTTPException(404, "复盘花名册中未找到该主播")
    actor = _focus_do(focus_store.clean_actor, body.get("actor"))
    result = _focus_do(focus_store.add_action, streamer.id, body, actor)
    _focus_log("新增", "-", actor, result)
    return result


@router.post("/focus/streamers/by-ext/{ext_id}/actions")
def focus_add_by_ext(ext_id: str, body: dict = Depends(_focus_body), db: Session = Depends(get_db)):
    """手动加一条在练动作（按业绩主播 id）；不限条数，同一个问题不能重复加。"""
    return _focus_add(_focus_lookup(db, ext_id=ext_id), body)


@router.post("/focus/streamers/{streamer_key}/actions")
def focus_add(streamer_key: str, body: dict = Depends(_focus_body), db: Session = Depends(get_db)):
    """手动加一条在练动作：body {candidate_index, session_id?} 或手填字段，加 actor；不限条数。"""
    return _focus_add(_focus_lookup(db, streamer_key), body)


@router.get("/admin/focus/overview")
def focus_overview(request: Request, db: Session = Depends(get_db)):
    """看板/完成情况页：每位主播在练的动作 + 最近 5 场的核对情况。"""
    _auth(request)
    return focus_store.overview(db)
