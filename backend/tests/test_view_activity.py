"""主播直达页阅读行为：beacon 上报（签名校验/防刷/去重/sendBeacon）与业绩系统拉取接口。"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.api import partner
from app.config import BACKEND_ROOT, get_settings
from app.db import Base, get_db
from app.models import (Critique, CritiqueLevel, ReplaySession, Severity, Streamer,
                        StreamerTurn, Transcript, ViewActivity)

TOKEN = "unit-test-partner-token"
H = {"X-Partner-Token": TOKEN}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(get_settings(), "partner_token", TOKEN)
    # /view 顺带算整场归因（走全局库），这里与本测试无关，固定为空保证隔离
    monkeypatch.setattr("app.core.attribution.analyze_attribution", lambda sid: None)
    monkeypatch.setattr("app.core.danmaku_signal.analyze_session_danmaku", lambda sid: None)
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'view.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        me = Streamer(name="小美", ext_id="s-me")
        other = Streamer(name="小红", ext_id="s-other")
        db.add_all([me, other])
        db.flush()
        row = ReplaySession(title="阅读行为测试场", lineup=json.dumps([me.id]))
        db.add(row)
        db.flush()
        db.add(StreamerTurn(session_id=row.id, streamer_id=me.id, seq=0, start_ms=0, end_ms=600000))
        db.add(Transcript(session_id=row.id, streamer_id=me.id, seq=0, start_ms=12000,
                          end_ms=15000, raw_text="宝宝们快下单"))
        db.add_all([
            Critique(session_id=row.id, streamer_id=me.id, level=CritiqueLevel.sentence,
                     severity=Severity.warn, category="逼单", start_ms=12000,
                     quote="宝宝们快下单", comment="逼单没给下单理由"),
            Critique(session_id=row.id, streamer_id=me.id, level=CritiqueLevel.screen,
                     severity=Severity.info, category="", start_ms=30500, comment="画面文字过小"),
            Critique(session_id=row.id, streamer_id=me.id, level=CritiqueLevel.segment,
                     severity=Severity.info, category="讲款", start_ms=60000, comment="段落点评", score=78),
            Critique(session_id=row.id, streamer_id=me.id, level=CritiqueLevel.summary,
                     severity=Severity.info, category="", start_ms=0, comment="整场总结", score=80),
        ])
        db.commit()
        ids = SimpleNamespace(session=row.id, me=me.id, other=other.id)
    app = FastAPI()
    app.include_router(partner.router)

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        yield client, factory, ids
    finally:
        client.close()
        engine.dispose()


def _link(session_id: int, streamer_id: int, exp: int | None = None) -> dict:
    exp = int(time.time()) + 3600 if exp is None else exp
    return {"s": streamer_id, "exp": exp, "sig": partner._view_sig(session_id, streamer_id, exp)}


def _beacon(client: TestClient, session_id: int, params: dict, payload: dict):
    return client.post(f"/api/partner/view/{session_id}/beacon", params=params, json=payload)


def _row(factory, session_id: int, streamer_id: int) -> ViewActivity | None:
    with factory() as db:
        return db.execute(select(ViewActivity).where(
            ViewActivity.session_id == session_id,
            ViewActivity.streamer_id == streamer_id)).scalars().first()


def _count(factory) -> int:
    with factory() as db:
        return db.execute(select(func.count(ViewActivity.id))).scalar()


def _age_last_beat(factory, session_id: int, streamer_id: int, seconds: float) -> None:
    """把"上次上报时间"往前拨，模拟两次上报之间隔了 seconds 秒。"""
    with factory() as db:
        row = db.execute(select(ViewActivity).where(
            ViewActivity.session_id == session_id,
            ViewActivity.streamer_id == streamer_id)).scalars().one()
        row.last_beat_at = datetime.now() - timedelta(seconds=seconds)
        db.commit()


def test_beacon_rejects_bad_or_expired_signature_like_view(env):
    client, factory, ids = env
    sid = ids.session
    good = _link(sid, ids.me)
    expired_link = _link(sid, ids.me, exp=int(time.time()) - 5)   # 签名正确但已过期
    bad_cases = [
        {**good, "sig": "0" * 40},              # 签名不对
        {**good, "s": ids.other},               # 改主播
        {**good, "exp": good["exp"] + 60},      # 改有效期
        expired_link,
    ]
    for params in bad_cases:
        r = _beacon(client, sid, params, {"kind": "open"})
        assert r.status_code == 403, params
        # 与 /view/{sid} 的校验结果一致
        assert client.get(f"/api/partner/view/{sid}", params=params).status_code == 403
    assert _beacon(client, sid + 1, good, {"kind": "open"}).status_code == 403  # 改场次
    # 非 ASCII 签名：按签名无效 403（不让 compare_digest 抛 TypeError 变 500）
    assert _beacon(client, sid, {**good, "sig": "签名"}, {"kind": "open"}).status_code == 403
    assert "过期" in _beacon(client, sid, expired_link, {"kind": "open"}).json()["detail"]
    assert _count(factory) == 0

    # 不需要 X-Partner-Token：签名正确即可上报
    ok = _beacon(client, sid, good, {"kind": "open"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert _count(factory) == 1


def test_first_report_creates_row_then_accumulates(env):
    client, factory, ids = env
    sid, me = ids.session, ids.me
    link = _link(sid, me)
    assert _row(factory, sid, me) is None

    r = _beacon(client, sid, link, {"kind": "open", "active": 0, "video": 0, "clips": 0, "seen": []})
    assert r.status_code == 200
    row = _row(factory, sid, me)
    assert row.opens == 1 and row.active_seconds == 0 and row.video_seconds == 0
    assert row.clips_played == 0 and json.loads(row.seen_keys) == []
    assert row.first_at is not None and row.last_at is not None and row.last_beat_at is not None
    first_at = row.first_at

    _age_last_beat(factory, sid, me, 20)
    r = _beacon(client, sid, link, {"kind": "beat", "active": 15, "video": 12.5, "clips": 2,
                                    "seen": ["sentence|12000|逼单"]})
    assert r.json()["accepted"] == {"active": 15.0, "video": 12.5, "clips": 2, "seen": 1}
    _age_last_beat(factory, sid, me, 20)
    _beacon(client, sid, link, {"kind": "beat", "active": 14, "video": 0, "clips": 1,
                                "seen": ["segment|60000|讲款"]})
    _beacon(client, sid, link, {"kind": "open"})   # 再次打开：opens+1，其余字段缺省按 0

    row = _row(factory, sid, me)
    assert row.opens == 2
    assert row.active_seconds == pytest.approx(29)
    assert row.video_seconds == pytest.approx(12.5)
    assert row.clips_played == 3
    assert json.loads(row.seen_keys) == ["sentence|12000|逼单", "segment|60000|讲款"]
    assert row.first_at == first_at
    assert row.last_at >= first_at
    assert _count(factory) == 1


def test_anti_abuse_limits(env):
    client, factory, ids = env
    sid, me = ids.session, ids.me
    link = _link(sid, me)
    _beacon(client, sid, link, {"kind": "open"})

    # 刚上报过（距上次≈0 秒）：active/video 最多记 0+5 秒
    acc = _beacon(client, sid, link, {"kind": "beat", "active": 25, "video": 25}).json()["accepted"]
    assert acc["active"] == pytest.approx(5, abs=0.5) and acc["video"] == pytest.approx(5, abs=0.5)

    # 距上次 12 秒：最多 12+5=17 秒
    _age_last_beat(factory, sid, me, 12)
    acc = _beacon(client, sid, link, {"kind": "beat", "active": 25, "video": 25}).json()["accepted"]
    assert acc["active"] == pytest.approx(17, abs=0.5) and acc["video"] == pytest.approx(17, abs=0.5)

    # 隔了很久：单次仍最多 30 秒；clips 单次最多 10
    _age_last_beat(factory, sid, me, 3600)
    acc = _beacon(client, sid, link, {"kind": "beat", "active": 999, "video": 1e9, "clips": 50}).json()["accepted"]
    assert acc == {"active": 30.0, "video": 30.0, "clips": 10, "seen": 0}

    # 非法数值（负数/字符串/布尔/NaN/∞/超大整数）一律记 0
    _age_last_beat(factory, sid, me, 3600)
    acc = _beacon(client, sid, link, {"kind": "beat", "active": -5, "video": "20", "clips": True}).json()["accepted"]
    assert acc == {"active": 0.0, "video": 0.0, "clips": 0, "seen": 0}
    r = client.post(f"/api/partner/view/{sid}/beacon", params=link,
                    content=b'{"kind":"beat","active":NaN,"video":Infinity,"clips":' + b"9" * 400 + b"}")
    assert r.status_code == 200
    assert r.json()["accepted"] == {"active": 0.0, "video": 0.0, "clips": 0, "seen": 0}

    # seen：每次只看前 100 个，每个 key ≤200 字符（超长的丢弃，不截断）
    keys = [f"sentence|{i}|类别" for i in range(150)]
    too_long = "segment|1|" + "长" * 191          # 201 字符
    just_fit = "segment|2|" + "长" * 190          # 200 字符
    acc = _beacon(client, sid, link, {"kind": "beat", "seen": [too_long, just_fit] + keys}).json()["accepted"]
    assert acc["seen"] == 99
    stored = json.loads(_row(factory, sid, me).seen_keys)
    assert stored == [just_fit] + keys[:98]
    assert too_long not in stored

    row = _row(factory, sid, me)
    assert row.clips_played == 10
    assert row.active_seconds == pytest.approx(5 + 17 + 30, abs=1)
    assert row.video_seconds == pytest.approx(5 + 17 + 30, abs=1)


def test_seen_keys_are_deduplicated_in_order(env):
    client, factory, ids = env
    sid, me = ids.session, ids.me
    link = _link(sid, me)
    first = _beacon(client, sid, link, {"kind": "open", "seen": ["a|1|x", "a|1|x", "b|2|"]}).json()
    assert first["accepted"]["seen"] == 2
    second = _beacon(client, sid, link, {"kind": "beat", "seen": ["b|2|", "c|3|y", "a|1|x", 7, None, ""]}).json()
    assert second["accepted"]["seen"] == 1
    assert json.loads(_row(factory, sid, me).seen_keys) == ["a|1|x", "b|2|", "c|3|y"]


def test_send_beacon_text_plain_body_is_parsed(env):
    client, factory, ids = env
    sid, me = ids.session, ids.me
    link = _link(sid, me)
    url = f"/api/partner/view/{sid}/beacon"
    # navigator.sendBeacon(url, 字符串) 发出的就是 text/plain;charset=UTF-8
    body = json.dumps({"kind": "open", "active": 0, "video": 0, "clips": 0,
                       "seen": ["sentence|12000|逼单"]}, ensure_ascii=False).encode("utf-8")
    r = client.post(url, params=link, content=body, headers={"Content-Type": "text/plain;charset=UTF-8"})
    assert r.status_code == 200, r.text
    # 没有 Content-Type 也按原始请求体解析
    _age_last_beat(factory, sid, me, 20)
    r = client.post(url, params=link, content=json.dumps({"kind": "beat", "active": 3, "clips": 1}).encode())
    assert r.status_code == 200, r.text
    row = _row(factory, sid, me)
    assert row.opens == 1 and row.clips_played == 1 and row.active_seconds == pytest.approx(3)
    assert json.loads(row.seen_keys) == ["sentence|12000|逼单"]


def test_invalid_payloads_are_rejected_without_side_effects(env):
    client, factory, ids = env
    sid, me = ids.session, ids.me
    link = _link(sid, me)
    url = f"/api/partner/view/{sid}/beacon"
    for content in (b"", b"not json", b"[]", b'"open"', b'{"kind":"close"}', b'{"active":5}', b"[" * 20000):
        r = client.post(url, params=link, content=content, headers={"Content-Type": "text/plain"})
        assert r.status_code == 400, content[:20]
    too_big = json.dumps({"kind": "beat", "pad": "x" * (70 * 1024)}).encode()
    assert client.post(url, params=link, content=too_big).status_code == 413
    # 签名正确但场次/主播已不存在 → 404
    assert _beacon(client, 9999, _link(9999, me), {"kind": "open"}).status_code == 404
    assert _beacon(client, sid, _link(sid, 9999), {"kind": "open"}).status_code == 404
    assert _count(factory) == 0


def test_concurrent_reports_do_not_lose_updates(env):
    client, factory, ids = env
    sid, me = ids.session, ids.me
    link = _link(sid, me)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: _beacon(client, sid, link, {
            "kind": "open", "clips": 1, "seen": [f"sentence|{i}|并发"]}).status_code, range(16)))
    assert results == [200] * 16
    row = _row(factory, sid, me)
    assert row.opens == 16 and row.clips_played == 16
    assert sorted(json.loads(row.seen_keys)) == sorted(f"sentence|{i}|并发" for i in range(16))
    assert _count(factory) == 1


def test_racing_first_insert_falls_back_to_update(env, monkeypatch):
    """另一进程在"查无此行→插入"之间抢先建了同一行：撞唯一约束后回滚、按已有行累加。"""
    client, factory, ids = env
    sid, me = ids.session, ids.me
    real_apply = partner._apply_view_beacon
    calls = {"n": 0}

    def racing_apply(db, session_id, streamer_id, *args):
        calls["n"] += 1
        if calls["n"] == 1:
            real_commit = db.commit

            def commit_after_rival():
                with factory() as rival:
                    rival.add(ViewActivity(session_id=session_id, streamer_id=streamer_id, opens=1))
                    rival.commit()
                db.commit = real_commit
                real_commit()

            db.commit = commit_after_rival
        return real_apply(db, session_id, streamer_id, *args)

    monkeypatch.setattr(partner, "_apply_view_beacon", racing_apply)
    r = _beacon(client, sid, _link(sid, me), {"kind": "open", "clips": 2})
    assert r.status_code == 200, r.text
    assert calls["n"] == 2
    row = _row(factory, sid, me)
    assert row.opens == 2 and row.clips_played == 2
    assert _count(factory) == 1


def test_view_activity_endpoint(env):
    client, factory, ids = env
    sid, me = ids.session, ids.me
    url = f"/api/partner/replays/{sid}/view-activity"
    q = {"ext_id": "s-me"}
    # 鉴权与其他 partner 接口一致：X-Partner-Token + 仅本机回环
    assert client.get(url, params=q).status_code == 403
    assert client.get(url, params=q, headers={"X-Partner-Token": "wrong"}).status_code == 403
    assert client.get(url, params=q, headers={**H, "X-Forwarded-For": "1.2.3.4"}).status_code == 403
    assert client.get(url, params={"ext_id": "s-nobody"}, headers=H).status_code == 404
    assert client.get("/api/partner/replays/9999/view-activity", params=q, headers=H).status_code == 404

    # 没有任何上报 → activity 为 null
    assert client.get(url, params=q, headers=H).json() == {"ok": True, "activity": None}

    link = _link(sid, me)
    _beacon(client, sid, link, {"kind": "open"})
    _age_last_beat(factory, sid, me, 20)
    _beacon(client, sid, link, {"kind": "beat", "active": 15.4, "video": 9.6, "clips": 2,
                                "seen": ["sentence|12000|逼单", "screen|30500|"]})
    data = client.get(url, params=q, headers=H).json()
    assert data["ok"] is True
    act = data["activity"]
    assert set(act) == {"active_seconds", "video_seconds", "clips_played", "seen_keys",
                        "opens", "first_at", "last_at"}
    assert act["active_seconds"] == 15 and act["video_seconds"] == 10
    assert act["clips_played"] == 2 and act["opens"] == 1
    assert act["seen_keys"] == ["sentence|12000|逼单", "screen|30500|"]
    for field in ("first_at", "last_at"):
        assert act[field].endswith("+08:00")
        datetime.fromisoformat(act[field])
    # 花名兜底查到同一主播
    assert client.get(url, params={"name": "小美"}, headers=H).json() == data
    # 记录按主播隔离：别的主播在本场没有记录
    assert client.get(url, params={"ext_id": "s-other"}, headers=H).json() == {"ok": True, "activity": None}


def test_view_payload_critiques_carry_key_fields(env):
    client, _factory, ids = env
    sid, me = ids.session, ids.me
    r = client.get(f"/api/partner/view/{sid}", params=_link(sid, me))
    assert r.status_code == 200, r.text
    crits = r.json()["critiques"]
    assert crits
    levels = {lv.value for lv in CritiqueLevel}
    for c in crits:
        assert c["level"] in levels
        assert isinstance(c["start_ms"], int) and not isinstance(c["start_ms"], bool)
        assert isinstance(c["category"], str)
    keys = {f"{c['level']}|{c['start_ms']}|{c['category'] or ''}" for c in crits if c["level"] != "summary"}
    assert keys == {"sentence|12000|逼单", "screen|30500|", "segment|60000|讲款"}


def test_frontend_uses_same_key_format():
    """前端直达页算 key 的口径必须与业绩系统 / 上面的断言一致。"""
    app_js = BACKEND_ROOT.parent / "frontend" / "app.js"
    if not app_js.exists():
        pytest.skip("仅后端目录，无前端文件")
    source = app_js.read_text(encoding="utf-8")
    assert 'return `${c.level}|${Math.round(Number(c.start_ms) || 0)}|${c.category || ""}`;' in source
    assert source.count('data-ckey="${esc(viewCritKey(c))}"') == 2      # 句级/画面问题卡 + 段级点评卡
    assert "/beacon?s=" in source and "navigator.sendBeacon" in source
    assert "try { startViewTracking(qs, root, pl); } catch (e) { }" in source


def test_deleting_session_removes_view_activity(env):
    client, factory, ids = env
    sid, me = ids.session, ids.me
    _beacon(client, sid, _link(sid, me), {"kind": "open"})
    assert _count(factory) == 1
    r = client.delete(f"/api/partner/admin/replays/{sid}", params={"mode": "all"}, headers=H)
    assert r.status_code == 200, r.text
    assert _count(factory) == 0


def test_main_app_routes_bypass_cookie_login():
    """真实应用里两个新接口都不走 cookie 登录（beacon 靠签名，view-activity 靠 partner token）。"""
    from app.main import app

    client = TestClient(app)
    r = client.post("/api/partner/view/1/beacon", params={"s": 1, "exp": int(time.time()) + 60, "sig": "x"},
                    content=b'{"kind":"open"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 403            # 签名无效，而不是 401 未登录
    r = client.get("/api/partner/replays/1/view-activity", params={"ext_id": "s-me"})
    assert r.status_code == 403            # 缺 X-Partner-Token，而不是 401 未登录
