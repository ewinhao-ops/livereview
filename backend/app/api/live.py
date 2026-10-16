"""业绩统计系统对接：主播上/下播打卡信号 → 归属切换 / 录制联动。

业绩系统在主播点『打卡上播/下播』时经本机回环 POST 本接口（Outbox 异步投递）。
身份匹配优先走 ext_id（业绩系统主播终身 ID），花名/登录名兜底并安全回填绑定；
每条信号落 signal_events 审计，event_id 幂等——收到新 event_id 先落占位行
（status=processing）再执行动作，重复投递重放首次结果，杜绝超时重试双执行。

阶段0 约定：行为开关默认全关——on 只做切人（现状语义），off 只记录不动作
（也不自动建档），自动开录与结班/停录在阶段2 由 autopilot/pipeline 实装。
响应契约：除 403（token/回环）与 422（体格式）外一律 200，业务态放 result 字段。
业绩侧重试语义：网络错/超时/5xx 无限退避重试；200+result=failed 有限重试（本接口对
failed 事件接管重执行，不重放旧结果）；4xx 终态不重试。
"""

from __future__ import annotations

import asyncio
import hmac
import json
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core import autopilot, runtime
from ..core.pipeline import get_running, stop_pipeline
from ..core.safe_error import safe_error_text, safe_exception_code
from ..db import get_db
from ..models import LiveRoom, ReplaySession, SessionStatus, SignalEvent, Streamer

router = APIRouter(prefix="/api/live", tags=["live"])

# 本机回环来源（uvicorn 直连）；testclient 是 FastAPI TestClient 的固定 host，仅测试可达
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "testclient"}

# 占位行超过该秒数仍是 processing，视为原请求已死（进程重启），允许重试请求接管执行
_PROCESSING_TAKEOVER_SEC = 90

# 停录宽限定时器（session_id → task，仅 off_mode=stop）。进程重启丢失属已知取舍：
# 停录退化给断流看门狗，最坏行为等于今天
_pending_stop: dict[int, asyncio.Task] = {}


async def _grace_stop(session_id: int) -> None:
    """stop 档宽限：到期先探活——流还有数据就不停（交接班/误触保护），确认死流才收尾。
    停的都是死流，autopilot 对死流 resolve 必失败不会重开，无需冷却机制。"""
    try:
        await asyncio.sleep(max(10, runtime.signal_stop_grace_sec()))
        p = get_running(session_id)
        if p is None:
            return
        if p.recently_receiving(30.0):
            print(f"[signal] 场次#{session_id} 停录宽限到期但流仍在推送——不停录（归属已置空，等断流看门狗收尾）")
            return
        print(f"[signal] 场次#{session_id} 停录宽限到期且流已无数据，停止录制并收尾")
        stop_pipeline(session_id)
    except asyncio.CancelledError:
        pass                                     # 下一位主播上播打卡：取消停录，录制继续
    finally:
        _pending_stop.pop(session_id, None)


def _cancel_grace(session_id: int) -> None:
    t = _pending_stop.pop(session_id, None)
    if t:
        t.cancel()
        print(f"[signal] 场次#{session_id} 的停录宽限已取消（新上播信号到达）")


def _reject_non_loopback(request: Request) -> None:
    """integration_loopback_only 开启时只接受本机回环：经 Caddy 反代的公网请求带
    X-Forwarded-For 直接 403，攻击面归零（业绩系统与复盘同机，回环调用天然满足）。"""
    if not get_settings().integration_loopback_only:
        return
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        raise HTTPException(403, "该接口仅接受本机回环调用（检测到反向代理头）")
    host = request.client.host if request.client else ""
    if host not in _LOOPBACK_HOSTS:
        raise HTTPException(403, "该接口仅接受本机回环调用")


def _parse_occurred_at(raw: str) -> datetime | None:
    """ISO8601（带时区）→ 本地 naive（全库时间口径，服务器 TZ=Asia/Shanghai）。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _replay(prior: SignalEvent) -> dict:
    """重放已存事件的首次结果（幂等命中路径）。"""
    try:
        resp = json.loads(prior.result_json) if prior.result_json else {}
    except json.JSONDecodeError:
        resp = {}
    resp.setdefault("ok", True)
    resp.setdefault("result", prior.status or "already")
    resp["replayed"] = True
    return resp


def _match_streamer(
    db: Session, name: str, username: str, ext_id: str | None, allow_create: bool,
) -> tuple[Streamer | None, str]:
    """匹配链：ext_id → 花名(安全回填) → 登录名(安全回填) → 自动建档(仅 allow_create)。

    返回 (主播, 失败原因)；失败原因非空即匹配失败。
    回填三防护：name 唯一命中 / 该行 ext_id 为空 / 该 ext_id 未绑定他人——防重名串人。
    软删行占用的 ext_id 自动释放（软删=离册，占位只会永久堵死该主播的后续信号）。
    """
    if ext_id:
        s = db.execute(
            select(Streamer).where(Streamer.ext_id == ext_id, Streamer.deleted_at.is_(None))
        ).scalars().first()
        if s:
            return s, ""
        stale_holders = db.execute(
            select(Streamer).where(Streamer.ext_id == ext_id, Streamer.deleted_at.is_not(None))
        ).scalars().all()
        if stale_holders:
            for h in stale_holders:
                h.ext_id = None
            db.commit()
            print(f"[signal] 释放软删主播占用的 ext_id={ext_id}（{len(stale_holders)} 行）")

    for key in (name, username):
        if not key:
            continue
        rows = db.execute(
            select(Streamer).where(Streamer.name == key, Streamer.deleted_at.is_(None))
        ).scalars().all()
        if len(rows) > 1:
            return None, f"花名册中「{key}」重名（{len(rows)} 条），无法安全绑定，请人工设置 ext_id"
        if not rows:
            continue
        s = rows[0]
        if not ext_id:
            return s, ""          # 旧调用方不带 ext_id：按名字用，不回填
        if s.ext_id and s.ext_id != ext_id:
            return None, f"主播「{key}」已绑定 ext_id={s.ext_id}，与信号中的 {ext_id} 冲突，拒绝串绑"
        if not s.ext_id:
            other = db.execute(
                select(Streamer).where(Streamer.ext_id == ext_id, Streamer.deleted_at.is_(None))
            ).scalars().first()
            if other:
                return None, f"ext_id={ext_id} 已绑定主播「{other.name}」，与信号中的「{key}」冲突"
            s.ext_id = ext_id
            try:
                db.commit()
            except IntegrityError:   # 并发回填撞唯一索引：极小概率，放弃本次绑定但不失败
                db.rollback()
            print(f"[signal] 花名册主播「{s.name}」自动绑定 ext_id={ext_id}")
        return s, ""

    if allow_create and get_settings().signal_auto_create_streamer and (name or username):
        s = Streamer(name=name or username, ext_id=ext_id, note="业绩系统自动创建")
        db.add(s)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None, f"自动建档失败：ext_id={ext_id} 已被占用（并发冲突），请重试"
        print(f"[signal] 花名册自动建档「{s.name}」ext_id={ext_id}")
        return s, ""
    return None, f"花名册没有匹配主播（花名={name or '-'} 登录名={username or '-'} ext_id={ext_id or '-'}）"


def _route_room(db: Session, room_id: str, s: Streamer | None) -> tuple[str, LiveRoom | None]:
    """房间路由：显式 room_id → 唯一 auto_record 房间 → auto_streamer_ids 含该主播的唯一房间。
    返回 (目标 room_id, LiveRoom 行或 None)——room_id 用于找场次，LiveRoom 用于登记开录意向。
    LiveReview 当前单直播间四班倒，规则②零配置可用。"""
    if room_id:
        room = db.execute(
            select(LiveRoom).where(LiveRoom.room_id == room_id, LiveRoom.deleted_at.is_(None))
        ).scalars().first()
        return room_id, room
    rooms = db.execute(select(LiveRoom).where(
        LiveRoom.auto_record.is_(True), LiveRoom.deleted_at.is_(None),
    )).scalars().all()
    if len(rooms) == 1:
        return rooms[0].room_id, rooms[0]
    if s and len(rooms) > 1:
        def _ids(r: LiveRoom) -> list[int]:
            try:
                return json.loads(r.auto_streamer_ids or "[]")
            except (ValueError, TypeError):
                return []
        matched = [r for r in rooms if s.id in _ids(r)]
        if len(matched) == 1:
            return matched[0].room_id, matched[0]
    # 兜底：没有任何 auto_record 房间（管理员关了整天模式）时，若全库只有唯一直播间，
    # 仍路由到它——否则整天模式一关，主播「我要复盘」的开录意向就无处安放
    all_rooms = db.execute(select(LiveRoom).where(LiveRoom.deleted_at.is_(None))).scalars().all()
    if len(all_rooms) == 1:
        return all_rooms[0].room_id, all_rooms[0]
    return "", None


def _finish(db: Session, ev: SignalEvent, resp: dict) -> dict:
    """落审计并返回：瞬态 ev 插入，占位/接管的持久 ev 原地更新。
    event_id 唯一索引兜底残余并发：撞索引则重放已存结果。"""
    ev.status = resp.get("result", "")
    ev.error = resp.get("note", "") if resp.get("result") == "failed" else ""
    ev.result_json = json.dumps(resp, ensure_ascii=False)
    if resp.get("result") == "applied":
        ev.applied_at = datetime.now()
    db.add(ev)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if ev.event_id:
            prior = db.execute(
                select(SignalEvent).where(SignalEvent.event_id == ev.event_id)
            ).scalars().first()
            if prior and prior.result_json:
                return _replay(prior)
    return resp


@router.post("/signal")
async def streamer_signal(
    request: Request,
    token: str = Body(""),
    room_id: str = Body(""),
    session_id: int | None = Body(None),
    streamer: str = Body(""),          # 花名（业绩 display_name；兜底匹配键）
    action: str = Body("on"),          # on=上播 / off=下播
    ext_id: str = Body(""),            # 业绩 streamers.id（s-<uuid>），首选匹配键
    username: str = Body(""),          # 业绩登录名，第二兜底键
    perf_session_id: str = Body(""),   # 业绩 live_sessions.id：审计锚点 + 乱序防护键
    event_id: str = Body(""),          # 幂等键（业绩 outbox 行 uuid）
    occurred_at: str = Body(""),       # 打卡真实时刻 ISO8601 带时区
    want_record: bool = Body(False),   # 主播已开启"我要复盘"→未开播时登记开录意向；关=只切归属不开录
    db: Session = Depends(get_db),
):
    st = get_settings()
    if not st.signal_token or not hmac.compare_digest(token, st.signal_token):
        raise HTTPException(403, "签名无效或未配置 signal_token")
    _reject_non_loopback(request)

    # 归一化 + 按列宽截断（SQLite 不强制 VARCHAR(n)，不截断会被超大字段撑库）
    action = (action or "on").strip().lower()
    name = (streamer or "").strip()[:128]
    username = (username or "").strip()[:128]
    ext = (ext_id or "").strip()[:64] or None
    evt_id = (event_id or "").strip()[:64] or None
    perf_sid = (perf_session_id or "").strip()[:64]
    room_id = (room_id or "").strip()[:64]
    when = _parse_occurred_at((occurred_at or "").strip())

    # 幂等（先查）：已完结的重放首次结果；processing 且未超时说明并发在途；
    # processing 超时视为原请求已死（进程重启），本次接管占位行继续执行；
    # failed 也接管重执行——业绩侧对 result=failed 会退避重试，重试必须真正重跑
    # （匹配失败重跑可在花名册修正后自愈，切换异常重跑可在故障恢复后成功），
    # 不能重放首次的 failed 结果（否则两侧互踢皮球谁都不重试）
    ev: SignalEvent | None = None
    if evt_id:
        prior = db.execute(
            select(SignalEvent).where(SignalEvent.event_id == evt_id)
        ).scalars().first()
        if prior:
            if prior.status == "processing":
                age = (datetime.now() - prior.created_at).total_seconds() if prior.created_at else 1e9
                if age < _PROCESSING_TAKEOVER_SEC:
                    return {"ok": True, "result": "processing", "event_id": evt_id,
                            "replayed": True, "note": "同 event_id 正在处理中"}
                ev = prior
            elif prior.status == "failed":
                ev = prior
            else:
                return _replay(prior)

    if ev is None:
        ev = SignalEvent(
            event_id=evt_id, perf_session_id=perf_sid, ext_id=ext or "",
            streamer_name=name, username=username, action=action,
            room_id=room_id, occurred_at=when,
        )
        if evt_id:
            # 占位行先落库（check-then-act 竞态防护）：动作执行前 event_id 已占住唯一索引，
            # 超时重试的同 event_id 请求只会拿到 processing，不会双执行 switch
            ev.status = "processing"
            db.add(ev)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                prior = db.execute(
                    select(SignalEvent).where(SignalEvent.event_id == evt_id)
                ).scalars().first()
                if prior and prior.status != "processing":
                    return _replay(prior)
                return {"ok": True, "result": "processing", "event_id": evt_id,
                        "replayed": True, "note": "同 event_id 正在处理中"}

    # 乱序防护：同班次已有更晚的 OFF 打卡 → 迟到/重试的 ON 不得复活死场次
    # （off 在阶段0 落库 status=ignored，同样是真实下播打卡；failed 的 off 身份不明不作依据）
    if action == "on" and perf_sid and when:
        offs = db.execute(
            select(SignalEvent).where(
                SignalEvent.perf_session_id == perf_sid,
                SignalEvent.action == "off",
                SignalEvent.status.in_(("applied", "ignored")),
            )
        ).scalars().all()
        if any(o.occurred_at and o.occurred_at > when for o in offs):
            return _finish(db, ev, {
                "ok": True, "result": "stale", "event_id": evt_id,
                "note": "同班次已有更晚的下播信号，过期上播信号不执行",
            })

    # ---- 下播：off_mode 三档——ignore 只记录；close_turn 结班+出总结（录制继续）；
    # stop 结班后进入宽限，到期探活确认流已死才真停。
    # 匹配失败不算错（旧调用方 off 可不带主播名）、不自动建档 ----
    if action != "on":
        s, err = _match_streamer(db, name, username, ext, allow_create=False)
        if s:
            ev.streamer_id = s.id
        mode = runtime.signal_off_mode()
        if err:
            return _finish(db, ev, {"ok": True, "result": "ignored", "event_id": evt_id,
                                    "note": f"下播信号已记录，未匹配到花名册主播（{err}）"})
        if mode == "ignore":
            return _finish(db, ev, {"ok": True, "result": "ignored", "event_id": evt_id,
                                    "note": "下播信号已记录（off_mode=ignore：不结班不停录）"})

        # 上播后还没开播就下播：撤销开录意向
        if autopilot.cancel_intent(s.id):
            return _finish(db, ev, {"ok": True, "result": "applied", "event_id": evt_id,
                                    "note": "开播前下播，已撤销开录意向"})

        # 定位该主播所在的 live 场次
        row = db.get(ReplaySession, session_id) if session_id else None
        if row is None:
            target_room, _ = _route_room(db, room_id, s)
            if target_room:
                row = db.execute(
                    select(ReplaySession).where(
                        ReplaySession.room_id == target_room,
                        ReplaySession.status == SessionStatus.live,
                    ).order_by(ReplaySession.id.desc())
                ).scalars().first()
        if row is None:
            # room_id 体系不统一（session 存的可能是 web_rid 短号，配置是长号）——
            # 精确匹配失败时回退「唯一 live 场次」。单直播间团队同时最多一场在录，
            # 这个回退是安全的（07-06 小陈下播被 ignored 的根因就是这里匹配不上）。
            lives = db.execute(select(ReplaySession).where(
                ReplaySession.status == SessionStatus.live)).scalars().all()
            if len(lives) == 1:
                row = lives[0]
        if row is None:
            return _finish(db, ev, {"ok": True, "result": "ignored", "event_id": evt_id,
                                    "note": "没有正在采集的场次，下播无需动作"})
        ev.replay_session_id = row.id
        # 只有当前主播是本人才结班：防乱序/残留信号误停他人班次
        if row.current_streamer_id != s.id:
            return _finish(db, ev, {"ok": True, "result": "ignored", "event_id": evt_id,
                                    "session_id": row.id,
                                    "note": "当前主播非本人（已交接或从未上播），下播无需动作"})
        try:
            p = get_running(row.id)
            if p:
                await p.close_current_turn()   # 结班+出总结，录制继续
            else:
                row.current_streamer_id = None
                db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            print(
                f"[signal] action=close_turn event_id={evt_id}; "
                f"code={safe_exception_code(e, prefix='SIGNAL')}; "
                f"error={safe_error_text(e)}"
            )
            return _finish(db, ev, {"ok": False, "result": "failed", "event_id": evt_id,
                                    "session_id": row.id, "note": "结班执行失败，请稍后重试"})
        note = "已结班并生成整场总结（录制继续，收播由断流看门狗负责）"
        if mode == "stop":
            _cancel_grace(row.id)
            _pending_stop[row.id] = asyncio.create_task(_grace_stop(row.id))
            note = f"已结班；{runtime.signal_stop_grace_sec()}s 宽限后探活停录"
        return _finish(db, ev, {"ok": True, "result": "applied", "event_id": evt_id,
                                "session_id": row.id, "note": note})

    # ---- 上播 ----
    s, err = _match_streamer(db, name, username, ext, allow_create=True)
    if not s:
        return _finish(db, ev, {"ok": False, "result": "failed", "event_id": evt_id, "note": err})
    ev.streamer_id = s.id

    # 定位正在采集的场次（优先显式 session_id，其次房间路由）
    row = db.get(ReplaySession, session_id) if session_id else None
    target_room, room_row = "", None
    if row is None:
        target_room, room_row = _route_room(db, room_id, s)
        if target_room:
            row = db.execute(
                select(ReplaySession).where(
                    ReplaySession.room_id == target_room,
                    ReplaySession.status == SessionStatus.live,
                ).order_by(ReplaySession.id.desc())
            ).scalars().first()
    if row is None:
        # room_id 体系不统一时回退「唯一 live 场次」（与 off 同理）——否则轮班接班的
        # 上播打卡匹配不到正在录的场次，会误走登记意向路径且意向永远消费不了
        # （07-06 杨柳青 19:54 接班 expired 的根因）。
        lives = db.execute(select(ReplaySession).where(
            ReplaySession.status == SessionStatus.live)).scalars().all()
        if len(lives) == 1:
            row = lives[0]
    if row is None:
        # 手动复盘模式（2026-07-09，用户改回，省大模型资源）：只有主播点了「我要复盘」
        # （业绩侧 replay_opt_in → 信号 want_record=True）才登记开录意向、开播即自动建场并
        # 归属打卡主播；没点则不录。配合 off_mode=stop：主播下播即停录，一场=一个人一班，
        # 不再出现「一条连续大场次横跨多人、时长/主播识别全错」（07-09 场次12 6小时全算杨柳青的根因）。
        # 2026-09-29 起用户要求每个班次都录：打开「全部班次录制」时不再看「我要复盘」。
        record_all = runtime.signal_record_all()
        if not want_record and not record_all:
            return _finish(db, ev, {
                "ok": True, "result": "ignored", "event_id": evt_id,
                "note": "本场主播未开启「我要复盘」，按手动复盘模式不录制",
            })
        if runtime.signal_autostart_enabled() and room_row is not None:
            # 先落 deferred 审计行再登记意向：意向持久化重建依赖 signal_events(deferred)
            reason = "我要复盘" if want_record else "全部班次录制"
            resp = _finish(db, ev, {
                "ok": True, "result": "deferred", "event_id": evt_id,
                "note": f"已登记开录意向（{reason}），等待开播检测，开播后自动开录并归属本主播",
            })
            autopilot.request_start(room_row.id, s.id, evt_id)
            return resp
        return _finish(db, ev, {
            "ok": True, "result": "ignored", "event_id": evt_id,
            "note": "未找到正在采集的场次且无法定位直播间（请检查该主播是否已绑定直播间）",
        })

    # 铁序：任何 ON 到达，第一步取消该场次的停录宽限（必须在幂等 already 早退之前）——
    # 误触下播马上重新上播、交接班迟到，都不能让宽限定时器把录制掐了
    _cancel_grace(row.id)

    ev.replay_session_id = row.id
    if row.current_streamer_id == s.id:
        return _finish(db, ev, {
            "ok": True, "result": "already", "event_id": evt_id,
            "already": s.name, "session_id": row.id,
        })

    # 切换执行：任何异常都不外抛（保持 200 契约 + 审计必落），失败交给业绩侧幂等重试
    try:
        # 不在排班里就补进去（自动录制/中途加入的主播）
        try:
            lineup = json.loads(row.lineup) if row.lineup else []
        except json.JSONDecodeError:
            lineup = []
        if s.id not in lineup:
            lineup.append(s.id)
            row.lineup = json.dumps(lineup)
            db.commit()

        p = get_running(row.id)
        if p:
            await p.switch_streamer(s.id)   # 直播中：结算上一位 + 切当前 + 推送前端
        else:
            row.current_streamer_id = s.id
            db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(
            f"[signal] action=switch event_id={evt_id}; "
            f"code={safe_exception_code(e, prefix='SIGNAL')}; "
            f"error={safe_error_text(e)}"
        )
        return _finish(db, ev, {
            "ok": False, "result": "failed", "event_id": evt_id,
            "session_id": row.id, "note": "主播切换失败，请稍后重试",
        })
    return _finish(db, ev, {
        "ok": True, "result": "applied", "event_id": evt_id,
        "switched_to": s.name, "session_id": row.id,
    })
