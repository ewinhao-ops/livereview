"""无人值守：后台轮询标记了 auto_record 的直播间，检测到开播就自动建会话并录制，
不需要有人打开网页点开始；同时按保留期清理过期录像，控制存储成本。

阶段2 扩展（业绩打卡联动）：live.py 收到上播信号但直播还没开录时，调 request_start()
登记「开录意向」——本循环被立即唤醒，且意向存续期间轮询加速（signal_fast_poll_sec），
开播瞬间即建场并把归属/排班直接定为打卡主播。意向持久化在 signal_events(deferred)，
进程重启后自动重建；TTL 到期作废（打卡了但一直没开播）。

信号只是加速器：意向丢了/过期了，本循环的常规检测照样录（归属走默认排班），
录制能力永远不比没有信号时差。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..db import SessionLocal
from ..models import (
    AudioFile,
    AudioStatus,
    LiveRoom,
    ReplaySession,
    SessionStatus,
    SessionType,
    SignalEvent,
    Streamer,
)
from . import media_store, runtime
from .live_link import normalize_live_link
from .douyin import resolve_by_sec_uid, resolve_stream
from .pipeline import get_running, start_pipeline
from .safe_error import safe_error_text, safe_exception_code

# 正在建/刚建、尚未进入 live 的房间，避免同一房间被并发重复开录
_starting: set[str] = set()

# ---------- 开录意向（上播打卡 → 等开播） ----------


@dataclass
class StartIntent:
    streamer_id: int
    event_id: str | None      # signal_events 幂等键（持久化锚点）
    created_at: datetime


# 按 LiveRoom 主键入键（不按解析出的 rid——web_rid/短链 rid 可能不一致）；
# 每房间单槽位，后到覆盖。协调状态在单进程内存，持久化靠 signal_events(deferred)。
_pending: dict[int, StartIntent] = {}
_wake = asyncio.Event()


def request_start(room_pk: int, streamer_id: int, event_id: str | None) -> None:
    """登记开录意向并立即唤醒检测循环（live.py 上播信号的冷路径调用）。"""
    old = _pending.get(room_pk)
    if old and old.event_id and old.event_id != event_id:
        _mark_event(old.event_id, "expired", note="被同房间更新的上播意向覆盖")
        print(f"[autopilot] 房间#{room_pk} 意向被覆盖：主播{old.streamer_id} → {streamer_id}")
    _pending[room_pk] = StartIntent(streamer_id=streamer_id, event_id=event_id,
                                    created_at=datetime.now())
    _wake.set()


def cancel_intent(streamer_id: int) -> bool:
    """撤销该主播的开录意向（下播信号：上播后还没开播就又下播了）。"""
    for pk, intent in list(_pending.items()):
        if intent.streamer_id == streamer_id:
            _pending.pop(pk, None)
            if intent.event_id:
                _mark_event(intent.event_id, "expired", note="主播在开播前打卡下播，意向撤销")
            return True
    return False


def _mark_event(event_id: str | None, status: str, *, note: str = "",
                replay_session_id: int | None = None) -> None:
    """更新意向对应的 signal_events 审计行（deferred → applied/expired）。"""
    if not event_id:
        return
    with SessionLocal() as db:
        ev = db.execute(
            select(SignalEvent).where(SignalEvent.event_id == event_id)
        ).scalars().first()
        if not ev or ev.status != "deferred":
            return
        ev.status = status
        if note:
            ev.error = note if status == "expired" else ""
        if replay_session_id is not None:
            ev.replay_session_id = replay_session_id
        if status == "applied":
            ev.applied_at = datetime.now()
        db.commit()


def _consume_intent(room_pk: int, session_id: int, intent: StartIntent) -> None:
    # 按对象身份消费：resolve 期间意向可能已被撤销/覆盖，别误删新意向；
    # 已撤销的意向 _mark_event 因状态非 deferred 自动 no-op
    if _pending.get(room_pk) is intent:
        _pending.pop(room_pk, None)
    _mark_event(intent.event_id, "applied", replay_session_id=session_id)


def _expire_intents() -> None:
    ttl = timedelta(seconds=max(60, get_settings().signal_pending_ttl_sec))
    now = datetime.now()
    for pk, intent in list(_pending.items()):
        if now - intent.created_at > ttl:
            _pending.pop(pk, None)
            _mark_event(intent.event_id, "expired", note="打卡后始终未检测到开播，意向过期")
            print(f"[autopilot] ⚠️ 房间#{pk} 的开录意向过期（主播{intent.streamer_id} 打卡后一直未开播）")


def _rebuild_intents() -> None:
    """启动时从 signal_events(deferred) 重建意向（进程重启不丢）。"""
    ttl = timedelta(seconds=max(60, get_settings().signal_pending_ttl_sec))
    cutoff = datetime.now() - ttl
    with SessionLocal() as db:
        rows = db.execute(
            select(SignalEvent).where(SignalEvent.status == "deferred",
                                      SignalEvent.created_at >= cutoff)
            .order_by(SignalEvent.id)
        ).scalars().all()
        for ev in rows:
            room = None
            if ev.room_id:
                room = db.execute(
                    select(LiveRoom).where(
                        LiveRoom.room_id == ev.room_id, LiveRoom.deleted_at.is_(None),
                    )
                ).scalars().first()
            if room is None:
                autos = db.execute(
                    select(LiveRoom).where(
                        LiveRoom.auto_record.is_(True), LiveRoom.deleted_at.is_(None),
                    )
                ).scalars().all()
                room = autos[0] if len(autos) == 1 else None
            if room is None:
                # 整天模式关闭时 auto_record 房间为空：单直播间团队回退唯一房间
                # （与 live._route_room 兜底一致，否则重启后意向永远恢复不了）
                all_rooms = db.execute(select(LiveRoom).where(
                    LiveRoom.deleted_at.is_(None),
                )).scalars().all()
                room = all_rooms[0] if len(all_rooms) == 1 else None
            if room is None or ev.streamer_id is None:
                continue
            _pending[room.id] = StartIntent(streamer_id=ev.streamer_id,
                                            event_id=ev.event_id,
                                            created_at=ev.created_at or datetime.now())
    if _pending:
        print(f"[autopilot] 重启恢复 {len(_pending)} 个开录意向")


async def _try_consume_on_live(room: dict, intent: StartIntent) -> bool:
    """意向第二消费点：该房间已有 live 场次（autopilot 常规路径先开了录 / 恢复场次）
    → 直接把当前主播切成意向主播并消费。返回 True 表示已消费。"""
    with SessionLocal() as db:
        row = db.execute(
            select(ReplaySession).where(
                ReplaySession.room_id == (room.get("room_id") or ""),
                ReplaySession.status == SessionStatus.live,
            ).order_by(ReplaySession.id.desc())
        ).scalars().first()
        if row is None:
            # room_id 体系不统一（session 可能存 web_rid 短号，配置是长号）→
            # 回退唯一 live 场次，否则接班意向永远消费不了、TTL 后 expired（07-06 实发）
            lives = db.execute(select(ReplaySession).where(
                ReplaySession.status == SessionStatus.live)).scalars().all()
            if len(lives) == 1:
                row = lives[0]
        if row is None:
            return False
        sid, cur = row.id, row.current_streamer_id
    if cur == intent.streamer_id:
        _consume_intent(room["id"], sid, intent)
        return True
    p = get_running(sid)
    if p:
        await p.switch_streamer(intent.streamer_id)
    else:
        with SessionLocal() as db:
            db.get(ReplaySession, sid).current_streamer_id = intent.streamer_id
            db.commit()
    _consume_intent(room["id"], sid, intent)
    print(f"[autopilot] 意向消费：场次#{sid} 当前主播切为 {intent.streamer_id}")
    return True


# 开播探测失败的节流日志：曾经解析失败 8 小时全静默（07-07 白天两个班漏录时毫无线索），
# 现在每房间每 10 分钟打一条累计告警，日志里能看到"探测在跑但一直失败"。
_fail_log_at: dict[int, float] = {}
_fail_count: dict[int, int] = {}


def _log_parse_fail(room: dict, err: Exception | None) -> None:
    rid = room["id"]
    _fail_count[rid] = _fail_count.get(rid, 0) + 1
    now = time.monotonic()
    if now - _fail_log_at.get(rid, 0.0) >= 600:
        _fail_log_at[rid] = now
        detail = safe_error_text(err, fallback="解析无流地址") if err else "解析无流地址"
        print(f"[autopilot] ⚠️ 房间#{rid} 开播探测失败已累计 {_fail_count[rid]} 次"
              f"（最近：{detail}）"
              f"——若直播确实在播，可能房间号已变/被风控；可在后台粘当场分享链接兜底")


def _learn_sec_uid(room: dict, res) -> None:
    """自学习回填账号级恒定 ID：解析成功一次即永久固化，此后按账号定位当前直播，
    不再受每场房间号(web_rid)变化影响（07-07 白天漏录的根因）。"""
    sec = getattr(res, "sec_uid", "") or ""
    if not sec or (room.get("sec_user_id") or ""):
        return
    try:
        with SessionLocal() as db:
            row = db.get(LiveRoom, room["id"])
            if row and not (row.sec_user_id or ""):
                row.sec_user_id = sec
                db.commit()
                print(f"[autopilot] ✅ 已学习房间#{room['id']} 的账号ID(sec_uid)，"
                      "此后按账号定位当前直播间，不受每场房间号变化影响")
        room["sec_user_id"] = sec
    except Exception as e:  # noqa: BLE001
        print(
            "[autopilot] sec_uid 回填失败; "
            f"code={safe_exception_code(e, prefix='AUTOPILOT_UID')}; "
            f"error={safe_error_text(e)}"
        )


async def _check_and_start(room: dict, intent: StartIntent | None = None) -> None:
    st = get_settings()
    res = None
    # 优先按账号级恒定 ID 定位当前直播（每场自动拿到新房间号）
    if room.get("sec_user_id"):
        try:
            res = await resolve_by_sec_uid(room["sec_user_id"], cookie=st.douyin_cookie)
        except Exception:  # noqa: BLE001  账号路失败回落 share_link 老路
            res = None
    if res is None or not res.stream_url:
        try:
            res = await resolve_stream(share_link=room["share_link"], cookie=st.douyin_cookie)
        except Exception as e:  # noqa: BLE001  解析失败视作未开播，下轮再试
            _log_parse_fail(room, e)
            return
    if not (res and res.stream_url):
        _log_parse_fail(room, None)
        return                                   # 未开播 / 解析不到有效流（意向保留，快轮询继续）
    _fail_count.pop(room["id"], None)
    _learn_sec_uid(room, res)
    # resolve 是长 await：期间意向可能被下播信号撤销或被新上播覆盖——重新校验对象身份，
    # 已失效则退回常规路径（房间确实在播，照录，归属走默认排班）
    if intent is not None and _pending.get(room["id"]) is not intent:
        intent = None
    rid = res.room_id or room.get("room_id") or ""
    if rid and rid in _starting:
        return
    with SessionLocal() as db:
        # 去重：常规路径挡 live/processing/parsing；意向路径放宽为只挡 live/parsing——
        # processing 的 capture 已停不会双录，不能让"上一场收尾中"挡住下一位主播开录
        blocked = ([SessionStatus.live, SessionStatus.parsing] if intent
                   else [SessionStatus.live, SessionStatus.processing, SessionStatus.parsing])
        active = db.query(ReplaySession).filter(
            ReplaySession.room_id == rid,
            ReplaySession.status.in_(blocked),
        ).first()
        if active:
            return
        # 失败冷却：抖音页面常驻陈旧拉流地址，"解析成功但拉不到流"会每轮建一个 error 场次
        # 刷屏（一晚 600+）。上一场是拉流失败且还在冷却窗内 → 本轮跳过。
        # 意向路径（主播刚打卡，流大概率真在）用短冷却，常规路径用长冷却。
        last = db.query(ReplaySession).filter(
            ReplaySession.room_id == rid,
        ).order_by(ReplaySession.id.desc()).first()
        if (last is not None and last.status == SessionStatus.error
                and (last.error_msg or "").startswith("拉流失败")):
            cooldown = 120 if intent else 900
            age = (datetime.now() - last.created_at).total_seconds() if last.created_at else 1e9
            if age < cooldown:
                return
        # 占位页标题拦截已撤销（2026-07-04）：LiveReview 直播间正式直播也用
        # "敬请期待"常驻标题，按标题拦截会把真直播全拦掉（07-04 晚整场漏录的元凶之一）。
        # 挂机页误录靠拉流失败冷却兜底；HLS 直传后误录成本低、可随手删除。
        now = datetime.now()
        title = f"{room.get('title') or '直播间'} · 自动录制 {now.strftime('%m-%d %H:%M')}"
        if intent is not None and db.get(Streamer, intent.streamer_id):
            ids = [intent.streamer_id]           # 打卡主播直接顶上：排班与归属都是他
        else:
            intent = None                        # 意向主播已被删等异常：退回默认排班
            try:
                ids = [i for i in (json.loads(room.get("auto_streamer_ids") or "[]"))
                       if db.get(Streamer, i)]
            except (ValueError, TypeError):
                ids = []
        normalized_link = normalize_live_link(room["share_link"])
        row = ReplaySession(title=title, share_link=normalized_link or room["share_link"],
                            normalized_share_link=normalized_link, room_id=rid,
                            stream_url=res.stream_url, status=SessionStatus.created,
                            lineup=json.dumps(ids),
                            current_streamer_id=ids[0] if ids else None)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # 与手工建场并发时由数据库唯一约束保留先到的一场。
            db.rollback()
            return
        db.refresh(row)
        sid = row.id
    _starting.add(rid)
    try:
        start_pipeline(sid)
        if intent is not None:
            _consume_intent(room["id"], sid, intent)
            print(f"[autopilot] 打卡意向开录 session #{sid}（房间 {rid}，主播 {ids[0]}）")
        else:
            print(f"[autopilot] 检测到开播，自动开录 session #{sid}（房间 {rid}）")
    finally:
        # pipeline 会很快把状态推进到 live，下一轮 active 查询即可拦住；这里延后移除即可
        _starting.discard(rid)


async def _retention_sweep() -> None:
    """到期录像清理：删 COS + 本地并标 deleted。retention_days<=0 表示永久保留。"""
    days = runtime.retention_days()
    if days <= 0:
        return
    cutoff = datetime.now() - timedelta(days=days)
    with SessionLocal() as db:
        olds = (db.query(AudioFile)
                .join(ReplaySession, AudioFile.session_id == ReplaySession.id)
                .filter(AudioFile.status == AudioStatus.available,
                        ReplaySession.session_type == SessionType.normal,
                        ReplaySession.started_at.isnot(None),
                        ReplaySession.started_at < cutoff)
                .all())
        for af in olds:
            if af.cos_key:
                try:
                    if af.cos_key.endswith(".m3u8"):
                        media_store.delete_prefix(f"replay/session_{af.session_id}/")
                        media_store.delete(f"replay/session_{af.session_id}.m4a")
                    else:
                        media_store.delete(af.cos_key)
                except Exception:  # noqa: BLE001
                    pass
                af.cos_key = ""
            if af.path:
                Path(af.path).unlink(missing_ok=True)
            af.status = AudioStatus.deleted
            af.size_bytes = 0
        if olds:
            db.commit()
            print(f"[autopilot] 保留期清理：删除 {len(olds)} 个过期录像（>{days} 天）")
    # 顺带清理 24h 前的"拉流失败"error 场次：冷却判断只看最近一条，隔天的纯属库存垃圾
    with SessionLocal() as db:
        stale = (db.query(ReplaySession)
                 .filter(ReplaySession.status == SessionStatus.error,
                         ReplaySession.error_msg.like("拉流失败%"),
                         ReplaySession.created_at < datetime.now() - timedelta(hours=24))
                 .all())
        for s in stale:
            db.delete(s)
        if stale:
            db.commit()
            print(f"[autopilot] 清理 {len(stale)} 条过期的拉流失败记录")


async def autopilot_loop() -> None:
    st = get_settings()
    if not st.autopilot_enabled:
        return
    await asyncio.sleep(15)                       # 让服务先就绪
    try:
        _rebuild_intents()
    except Exception as e:  # noqa: BLE001
        print(
            "[autopilot] 意向重建失败; "
            f"code={safe_exception_code(e, prefix='AUTOPILOT_INTENT')}; "
            f"error={safe_error_text(e)}"
        )
    interval = max(30, st.autopilot_interval_sec)
    while True:
        try:
            _expire_intents()
            with SessionLocal() as db:
                rows = db.query(LiveRoom).filter(
                    LiveRoom.auto_record.is_(True), LiveRoom.deleted_at.is_(None),
                ).all()
                # 有开录意向的房间必须一并巡逻：整天模式关闭时，主播打卡自选复盘
                # 全靠意向驱动——否则意向登记了也永远没人探测开播（缺陷修复 2026-07-04）
                if _pending:
                    seen = {r.id for r in rows}
                    rows += [r for r in db.query(LiveRoom)
                             .filter(LiveRoom.id.in_(list(_pending.keys())),
                                     LiveRoom.deleted_at.is_(None)).all()
                             if r.id not in seen]
                rooms = [{"id": r.id, "room_id": r.room_id, "share_link": r.share_link,
                          "title": r.title, "auto_streamer_ids": r.auto_streamer_ids,
                          "sec_user_id": getattr(r, "sec_user_id", "") or ""}
                         for r in rows]
            for room in rooms:
                intent = _pending.get(room["id"])
                if intent is not None and await _try_consume_on_live(room, intent):
                    intent = None
                if room["share_link"] or room.get("sec_user_id"):
                    await _check_and_start(room, intent)
            await _retention_sweep()
        except Exception as e:  # noqa: BLE001
            print(
                "[autopilot] 轮询失败; "
                f"code={safe_exception_code(e, prefix='AUTOPILOT_LOOP')}; "
                f"error={safe_error_text(e)}"
            )
        # 有意向 → 快轮询压低"先打卡后开播"的开场丢失；信号到达 → 立即唤醒
        wait = max(3, st.signal_fast_poll_sec) if _pending else interval
        try:
            await asyncio.wait_for(_wake.wait(), timeout=wait)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        _wake.clear()
