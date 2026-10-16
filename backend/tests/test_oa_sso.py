"""2026-10-01 统一登录改造：后台认 workspace.example.test 平台登录态（studio_token），共用口令只剩本机应急入口。

用自己签的测试 JWT（测试密钥）+ 本机起的假 studio（/api/me）模拟平台；
公网请求一律经 Caddy 转发，所以用带 X-Forwarded-For 的请求代表「公网」。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from app import auth, sso
from app.config import get_settings
from app.db import Base, get_db
from app.main import app
from app.models import (CompassPoint, ReplaySession, SessionStatus, Streamer, StreamerAccount)

SECRET = "unit-test-studio-secret"
EMERGENCY_PW = "unit-test-emergency-pass"
PARTNER = "unit-test-partner-token"
ADMIN = "11111111-1111-4111-8111-111111111111"
MEMBER = "22222222-2222-4222-8222-222222222222"     # 有 fupan_access
STRANGER = "33333333-3333-4333-8333-333333333333"   # 只开了业绩
DISABLED = "44444444-4444-4444-8444-444444444444"   # studio 里已停用
NEWBIE = "55555555-5555-4555-8555-555555555555"     # studio 里查得到，但本系统从没核验过
PROXIED = {"X-Forwarded-For": "203.0.113.7"}         # 经 Caddy 来的公网请求
LOCAL = ("127.0.0.1", 50000)                          # SSH 隧道直连 8077


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_token(sub: str, *, secret: str = SECRET, exp_delta: int = 3600, alg: str = "HS256",
               **extra) -> str:
    header = {"alg": alg, "typ": "JWT"}
    payload = {"sub": sub, "role": "member", "iat": int(time.time()),
               "exp": int(time.time()) + exp_delta, **extra}
    signing = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    sig = hmac.new(secret.encode(), signing.encode(), hashlib.sha256).digest()
    return f"{signing}.{_b64(sig)}"


class FakeStudio:
    """假 studio：GET /api/me 按 cookie 里 studio_token 的 sub 找人；down=True 时模拟故障（502）。"""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.down = False
        self.calls: list[str] = []
        studio = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                cookie = self.headers.get("Cookie", "")
                studio.calls.append(cookie)
                if studio.down:
                    return self._send(502, {"message": "bad gateway"})
                token = cookie.split("studio_token=", 1)[-1].split(";")[0] if "studio_token=" in cookie else ""
                try:
                    body = token.split(".")[1]
                    sub = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["sub"]
                except (IndexError, ValueError, KeyError):
                    return self._send(401, {"code": "http_401", "message": "未登录"})
                user = studio.users.get(sub)
                if self.path != "/api/me" or not user or not user["is_active"]:
                    return self._send(401, {"code": "http_401", "message": "账号不存在或已停用"})
                return self._send(200, user)

            def _send(self, code: int, body: dict) -> None:
                raw = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args) -> None:  # noqa: ANN002
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/me"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def add(self, sub: str, *, username: str, name: str, role: str = "member",
            perms: tuple[str, ...] = (), active: bool = True) -> None:
        self.users[sub] = {"id": sub, "username": username, "display_name": name, "role": role,
                           "auth_source": "local", "account_status": "approved", "is_active": active,
                           "feature_permissions": list(perms), "created_at": "2026-10-01T00:00:00Z"}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def studio(monkeypatch):
    fake = FakeStudio()
    settings = get_settings()
    for key, value in {"auth_mode": "studio", "studio_jwt_secret": SECRET, "studio_me_url": fake.url,
                       "platform_url": "https://workspace.example.test",
                       "public_base_url": "https://workspace.example.test/fupan",
                       "access_password": EMERGENCY_PW, "cookie_secure": True,
                       "partner_token": PARTNER, "signal_token": ""}.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(auth, "get_secret", lambda: "unit-test-secret")
    sso.reset_cache()
    fake.add(ADMIN, username="demo-admin", name="示例管理员", role="admin")
    fake.add(MEMBER, username="xiaowang", name="运营小王", perms=("fupan_access",))
    fake.add(STRANGER, username="luren", name="路人", perms=("other_app_access",))
    fake.add(DISABLED, username="tingyong", name="已停用", perms=("fupan_access",), active=False)
    yield fake
    fake.close()
    sso.reset_cache()


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    def override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override
    yield factory
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def public(token: str | None = None, **headers) -> TestClient:
    """公网浏览器（经 Caddy）：带 X-Forwarded-For。"""
    client = TestClient(app, headers={**PROXIED, **headers})
    if token:
        client.cookies.set("studio_token", token)
    return client


def local() -> TestClient:
    """SSH 隧道直连 8077：对端 127.0.0.1、没有任何转发头。"""
    return TestClient(app, client=LOCAL)


# ---------- 本地验签 ----------

def test_verify_studio_token_checks_signature_alg_and_expiry():
    good = make_token(ADMIN)
    assert sso.verify_studio_token(good, SECRET)["sub"] == ADMIN
    assert sso.verify_studio_token(good, "another-secret") is None
    assert sso.verify_studio_token(make_token(ADMIN, exp_delta=-1), SECRET) is None
    assert sso.verify_studio_token(make_token(ADMIN, alg="HS512"), SECRET) is None
    assert sso.verify_studio_token(make_token(ADMIN, alg="none"), SECRET) is None
    assert sso.verify_studio_token(make_token(""), SECRET) is None
    head, body, sig = good.split(".")
    forged = _b64(json.dumps({"sub": ADMIN, "role": "admin", "exp": int(time.time()) + 99}).encode())
    assert sso.verify_studio_token(f"{head}.{forged}.{sig}", SECRET) is None
    for junk in ("", "abc", "a.b.c", "四.五.六", "..", good + ".x"):
        assert sso.verify_studio_token(junk, SECRET) is None


# ---------- 没登录 / 没权限 / 放行 ----------

def test_no_login_returns_401_with_platform_login_url(studio):
    r = public().get("/api/sessions")
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == "need_login"
    assert body["login_url"] == "https://workspace.example.test/login?next=%2Ffupan%2F"
    assert body["emergency_login"] is False      # 公网请求永远不提示应急口令
    assert studio.calls == []


def test_bad_or_expired_token_is_401_without_asking_studio(studio):
    for token in (make_token(ADMIN, exp_delta=-5), make_token(ADMIN, secret="wrong"), "garbage"):
        r = public(token).get("/api/me")
        assert r.status_code == 401 and r.json()["code"] == "need_login"
    assert studio.calls == []


def test_admin_and_fupan_access_are_allowed_and_cached(studio):
    admin_token = make_token(ADMIN)
    admin = public(admin_token)
    r = admin.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "auth": "studio",
                        "user": {"username": "demo-admin", "display_name": "示例管理员", "role": "admin"},
                        "logout_url": "https://workspace.example.test/logout"}
    assert studio.calls == [f"studio_token={admin_token}"]   # 原样带平台 cookie 去问 studio
    member = public(make_token(MEMBER))
    for _ in range(3):
        assert member.get("/api/me").status_code == 200
        assert admin.get("/api/me").status_code == 200
    assert len(studio.calls) == 2                              # 5 分钟内每人只问一次


def test_without_permission_gets_403_and_new_grant_works_at_once(studio):
    client = public(make_token(STRANGER))
    for _ in range(2):
        r = client.get("/api/sessions")
        assert r.status_code == 403
        assert r.json() == {"detail": "你的统一账号还没有开通直播复盘", "code": "need_access",
                            "go_url": "https://workspace.example.test/go/fupan"}
    assert len(studio.calls) == 2                               # 没权限的结论不缓存，每次都重新核验
    studio.add(STRANGER, username="luren", name="路人", perms=("other_app_access", "fupan_access"))
    assert client.get("/api/me").status_code == 200           # 刚开通马上能进


def test_studio_answer_for_another_account_is_not_trusted(studio):
    studio.users[MEMBER] = {**studio.users[MEMBER], "id": ADMIN}   # 返回的不是令牌里那个人
    assert public(make_token(MEMBER)).get("/api/me").status_code == 401
    studio.add(NEWBIE, username="xinren", name="新人", perms=("fupan_access",))
    del studio.users[NEWBIE]["id"]
    assert public(make_token(NEWBIE)).get("/api/me").status_code == 503   # 返回格式不对 = 暂时无法核验


def test_disabled_account_is_sent_back_to_login(studio):
    r = public(make_token(DISABLED)).get("/api/me")
    assert r.status_code == 401 and r.json()["code"] == "need_login"


def test_revoke_takes_effect_after_cache_window(studio, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(sso, "_clock", lambda: now[0])
    client = public(make_token(MEMBER))
    assert client.get("/api/me").status_code == 200
    studio.add(MEMBER, username="xiaowang", name="运营小王", perms=())   # 管理员撤权
    now[0] += 299
    assert client.get("/api/me").status_code == 200            # 5 分钟内沿用
    now[0] += 2
    assert client.get("/api/me").status_code == 403            # 过了 5 分钟重新核验


def test_studio_down_keeps_last_verdict_and_retries_after_30s(studio, monkeypatch, capsys):
    now = [1000.0]
    monkeypatch.setattr(sso, "_clock", lambda: now[0])
    member, stranger = public(make_token(MEMBER)), public(make_token(STRANGER))
    assert member.get("/api/me").status_code == 200
    assert stranger.get("/api/me").status_code == 403
    studio.down = True
    now[0] += 400                                               # 放行缓存已过期
    assert member.get("/api/me").status_code == 200             # studio 挂了：沿用上次结论
    assert stranger.get("/api/me").status_code == 403
    calls = len(studio.calls)
    now[0] += 20
    assert member.get("/api/me").status_code == 200
    assert len(studio.calls) == calls                           # 30 秒内不再去问
    assert public(make_token(NEWBIE)).get("/api/me").status_code == 503   # 从没核验过的人
    assert "studio 暂时不可用" in capsys.readouterr().out
    now[0] += 11
    studio.down = False
    assert member.get("/api/me").status_code == 200
    assert len(studio.calls) == calls + 1                       # 30 秒后重新核验


def test_studio_down_without_previous_verdict_is_503_not_redirect(studio):
    studio.down = True
    r = public(make_token(MEMBER)).get("/api/sessions")
    assert r.status_code == 503
    assert r.json()["code"] == "auth_unavailable"


def test_missing_jwt_secret_is_503_to_avoid_login_loop(studio, monkeypatch):
    monkeypatch.setattr(get_settings(), "studio_jwt_secret", "")
    assert public(make_token(ADMIN)).get("/api/me").status_code == 503
    assert public().get("/api/me").status_code == 503


# ---------- WebSocket ----------

def test_session_websocket_requires_unified_login(studio):
    for client in (public(), public(make_token(STRANGER)), public(make_token(ADMIN, secret="wrong"))):
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect("/api/sessions/1/ws"):
                pass
        assert caught.value.code == 1008
    with public(make_token(MEMBER)).websocket_connect("/api/sessions/1/ws") as ws:
        assert ws is not None                                   # 握手通过


# ---------- 机器接口照旧免登录 ----------

def test_machine_interfaces_stay_open_without_login(studio, db_factory):
    client = public()
    assert client.get("/api/health").json() == {"ok": True}
    assert client.post("/api/compass", content=b'{"token":"wrong","data":{"x":1}}').status_code == 403
    assert client.post("/api/sessions/999/compass", content=b"{}").status_code == 404
    assert client.post("/api/live/signal", json={"token": "x"}).status_code == 403
    assert client.get("/api/partner/admin/replays").status_code == 403
    exp = int(time.time()) + 60
    assert client.get(f"/api/partner/view/1?s=1&exp={exp}&sig=bad").status_code == 403
    assert client.post(f"/api/partner/view/1/beacon?s=1&exp={exp}&sig=bad",
                       content=b'{"kind":"open"}').status_code == 403
    assert client.post("/api/logout").status_code == 200
    assert studio.calls == []


def test_old_domain_compass_report_still_attaches(studio, db_factory):
    from app.api.compass import get_global_token
    with db_factory() as db:
        token = get_global_token(db)
        row = ReplaySession(title="在播", status=SessionStatus.live, started_at=datetime.now())
        db.add(row)
        db.commit()
        sid = row.id
    body = json.dumps({"token": token, "data": {"成交金额": "1,234"}}).encode()
    for host in ("review.example.test", "workspace.example.test"):          # 旧脚本打旧域名，新脚本打新地址
        r = public(Host=host).post("/api/compass", content=body,
                                   headers={"Content-Type": "text/plain;charset=UTF-8"})
        assert r.status_code == 200 and r.json() == {"ok": True, "attached": True, "session_id": sid}
    with db_factory() as db:
        assert db.query(CompassPoint).filter(CompassPoint.session_id == sid).count() == 2


# ---------- 应急入口：只认本机直连 ----------

def test_emergency_password_login_only_from_local_direct(studio):
    body = {"password": EMERGENCY_PW}
    assert public().post("/api/login", json=body).status_code == 403
    for headers in ({"X-Real-IP": "203.0.113.7"}, {"Forwarded": "for=203.0.113.7"},
                    {"X-Forwarded-For": "127.0.0.1"}):
        assert TestClient(app, client=LOCAL, headers=headers).post("/api/login", json=body).status_code == 403
    assert TestClient(app, client=("10.0.0.8", 40000)).post("/api/login", json=body).status_code == 403

    client = local()
    r = client.get("/api/me")
    assert r.status_code == 401 and r.json()["emergency_login"] is True
    assert client.post("/api/login", json={"password": "wrong-pass"}).status_code == 401
    r = client.post("/api/login", json=body)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{auth.EMERGENCY_COOKIE}=")
    assert "Path=/" in cookie and "HttpOnly" in cookie and "Secure" not in cookie
    me = client.get("/api/me").json()
    assert me["auth"] == "emergency" and me["user"]["display_name"] == "本机应急口令"

    leaked = public()
    leaked.cookies.set(auth.EMERGENCY_COOKIE, client.cookies.get(auth.EMERGENCY_COOKIE))
    assert leaked.get("/api/me").status_code == 401             # 应急 cookie 经公网用不了
    legacy = public()
    legacy.cookies.set("dl_auth", "0" * 64)                     # 旧共用口令 cookie 不再认
    assert legacy.get("/api/me").status_code == 401


def test_emergency_entry_closed_without_password(studio, monkeypatch):
    client = local()
    client.cookies.set(auth.EMERGENCY_COOKIE, auth.emergency_token())
    monkeypatch.setattr(get_settings(), "access_password", "")
    assert client.post("/api/login", json={"password": "anything"}).status_code == 503
    r = client.get("/api/me")
    assert r.status_code == 401 and r.json()["emergency_login"] is False


def test_emergency_cookie_expires_and_follows_password_change(studio, monkeypatch):
    client = local()
    client.cookies.set(auth.EMERGENCY_COOKIE, auth.emergency_token(now=time.time() - auth.EMERGENCY_MAX_AGE - 5))
    assert client.get("/api/me").status_code == 401             # 过期
    client.cookies.set(auth.EMERGENCY_COOKIE, auth.emergency_token())
    assert client.get("/api/me").status_code == 200
    monkeypatch.setattr(get_settings(), "access_password", EMERGENCY_PW + "-changed")
    assert client.get("/api/me").status_code == 401             # 换了口令，旧 cookie 作废


def test_logout_clears_emergency_cookie(studio):
    r = public().post("/api/logout")
    assert r.json() == {"ok": True, "logout_url": "https://workspace.example.test/logout"}
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{auth.EMERGENCY_COOKIE}=") and "Max-Age=0" in cookie and "Path=/" in cookie


# ---------- 主播账号：对公网关闭 ----------

def test_streamer_login_and_cookie_closed_to_public(studio, db_factory, monkeypatch):
    from app.api import anchor
    monkeypatch.setattr(anchor, "SessionLocal", db_factory)    # _sid 直接开库查账号
    with db_factory() as db:
        streamer = Streamer(name="测试主播")
        db.add(streamer)
        db.flush()
        db.add(StreamerAccount(streamer_id=streamer.id, username="anchor",
                               password_hash=auth.hash_password("password1"), enabled=True))
        db.commit()
        sid = streamer.id
    body = {"username": "anchor", "password": "password1"}
    r = public().post("/api/anchor/login", json=body)
    assert r.status_code == 403 and "已关闭" in r.json()["detail"]
    streamer_cookie = auth.streamer_token(sid)
    outsider = public()
    outsider.cookies.set(auth.STREAMER_COOKIE, streamer_cookie)
    assert outsider.get("/api/anchor/me").status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with outsider.websocket_connect("/api/anchor/ws"):
            pass
    client = local()
    r = client.post("/api/anchor/login", json=body)
    assert r.status_code == 200 and "Path=/" in r.headers["set-cookie"]
    assert client.get("/api/anchor/me").json()["name"] == "测试主播"


def test_streamer_account_admin_needs_unified_login(studio, db_factory):
    with db_factory() as db:
        streamer = Streamer(name="测试主播")
        db.add(streamer)
        db.commit()
        sid = streamer.id
    body = {"username": "anchor2", "password": "password12"}
    assert public().put(f"/api/anchor/admin/account/{sid}", json=body).status_code == 401
    assert public(make_token(STRANGER)).put(f"/api/anchor/admin/account/{sid}", json=body).status_code == 403
    r = public(make_token(ADMIN)).put(f"/api/anchor/admin/account/{sid}", json=body)
    assert r.status_code == 200 and r.json()["username"] == "anchor2"


# ---------- 对外地址带前缀 ----------

def test_compass_addresses_use_public_base_url(studio, db_factory, monkeypatch):
    with db_factory() as db:
        row = ReplaySession(title="场次", status=SessionStatus.live, started_at=datetime.now())
        db.add(row)
        db.commit()
        sid = row.id
    admin = public(make_token(ADMIN), Host="workspace.example.test")
    setup = admin.get("/api/compass/setup").json()
    assert setup["report_url"] == "https://workspace.example.test/fupan/api/compass"
    assert '"https://workspace.example.test/fupan/api/compass"' in setup["userscript"]
    assert "https://workspace.example.test/fupan/api/compass" in setup["bookmarklet"]
    per_session = admin.get(f"/api/sessions/{sid}/compass/setup").json()
    assert per_session["url"] == f"https://workspace.example.test/fupan/api/sessions/{sid}/compass"
    partner = TestClient(app).get("/api/partner/admin/compass-setup", headers={"X-Partner-Token": PARTNER})
    assert partner.status_code == 200
    assert partner.json()["report_url"] == "https://workspace.example.test/fupan/api/compass"
    monkeypatch.setattr(get_settings(), "public_base_url", "https://new.example.test/replay/")
    assert admin.get("/api/compass/setup").json()["report_url"] == "https://new.example.test/replay/api/compass"


def test_partner_view_link_stays_relative(studio, db_factory):
    """业绩系统拼 REPLAY_PUBLIC_URL + url，所以 url 仍是以 / 开头的相对地址。"""
    with db_factory() as db:
        streamer = Streamer(name="主播甲", ext_id="s-a")
        db.add(streamer)
        db.flush()
        row = ReplaySession(title="场次", status=SessionStatus.done, started_at=datetime.now(),
                            lineup=json.dumps([streamer.id]))
        db.add(row)
        db.commit()
        sid = row.id
    r = TestClient(app).get(f"/api/partner/replays/{sid}/view-url?ext_id=s-a",
                            headers={"X-Partner-Token": PARTNER})
    url = r.json()["url"]
    assert url.startswith(f"/?view={sid}&s=")
    query = url.split("?", 1)[1]
    assert public().get(f"/api/partner/view/{sid}?{query.split('&', 1)[1]}").status_code == 200


# ---------- 操作留痕 ----------

def test_write_operations_log_operator(studio, db_factory, capsys):
    r = public(make_token(ADMIN)).post("/api/streamers", json={"name": "新主播"})
    assert r.status_code == 200
    r = public(make_token(MEMBER)).delete(f"/api/streamers/{r.json()['id']}")
    assert r.status_code == 200
    TestClient(app).delete("/api/partner/admin/replays/999", headers={"X-Partner-Token": PARTNER})
    out = capsys.readouterr().out
    assert "[操作] 示例管理员(demo-admin) POST /api/streamers → 200" in out
    assert "[操作] 运营小王(xiaowang) DELETE /api/streamers/" in out
    assert "[操作] 业绩系统(partner) DELETE /api/partner/admin/replays/999 → 404" in out
    assert "studio_token" not in out and "eyJ" not in out     # 日志里不出现平台令牌
