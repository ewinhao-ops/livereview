"""本系统自己的凭据：本机应急口令会话 + 主播账号 cookie（2026-10-01 起都只认服务器本机直连）。

后台日常登录已改走 workspace.example.test 平台统一登录（见 sso.py），这里只剩两样：
- 应急口令：平台故障时管理员经 SSH 隧道直连 8077 用。cookie 存「到期时间.HMAC 签名」，
  签名密钥 = HMAC(secret_key, access_password)——改了口令，已发出的应急 cookie 立即失效；
  比较一律用 hmac.compare_digest 常量时间，避免计时旁路。
- 主播账号（dl_streamer）：对公网关闭，主播看报告走业绩系统给的签名外链。
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import os
import time

from .config import get_secret, get_settings

EMERGENCY_COOKIE = "fupan_emergency"
EMERGENCY_MAX_AGE = 12 * 3600
STREAMER_COOKIE = "dl_streamer"

# 本机回环来源（uvicorn 直连）；testclient 是 FastAPI TestClient 的固定 host，仅测试可达（同 live.py）
_LOCAL_HOSTS = {"127.0.0.1", "::1", "testclient"}
# 经 Caddy 反代来的请求一定带 X-Forwarded-For；三种转发头任何一个出现都不算本机直连
_FORWARD_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


def is_local_direct(conn) -> bool:  # noqa: ANN001  Request / WebSocket
    """请求是否直接来自服务器本机（SSH 隧道直连 127.0.0.1:8077）：对端是回环地址，且不带任何转发头。
    公网请求都经 Caddy（必带 X-Forwarded-For，uvicorn 还会把对端改写成真实来源 IP），因此进不来。"""
    headers = getattr(conn, "headers", None) or {}
    if any(headers.get(name) for name in _FORWARD_HEADERS):
        return False
    client = getattr(conn, "client", None)
    return bool(client) and getattr(client, "host", "") in _LOCAL_HOSTS


def local_cookie_secure(request) -> bool:  # noqa: ANN001
    """应急/主播 cookie 只在本机直连时发，直连多半是 http://localhost（浏览器不一定收 Secure cookie）。"""
    if get_settings().auth_mode == "password":
        return bool(get_settings().cookie_secure)
    return bool(get_settings().cookie_secure) and request.url.scheme == "https"


def password_login_allowed(conn) -> bool:  # noqa: ANN001
    """Independent installs accept password sessions; Studio keeps its loopback restriction."""
    return get_settings().auth_mode == "password" or is_local_direct(conn)


def _emergency_key() -> bytes:
    return hmac.new(get_secret().encode(), get_settings().access_password.encode(),
                    hashlib.sha256).digest()


def emergency_token(now: float | None = None) -> str:
    exp = int(time.time() if now is None else now) + EMERGENCY_MAX_AGE
    sig = hmac.new(_emergency_key(), f"emergency|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def emergency_authed(conn) -> bool:  # noqa: ANN001
    """本机应急会话：未配置 ACCESS_PASSWORD（=应急入口关闭）或不是本机直连，一律不认。"""
    if not get_settings().access_password or not password_login_allowed(conn):
        return False
    try:
        exp_text, sig = conn.cookies.get(EMERGENCY_COOKIE, "").split(".", 1)
        exp = int(exp_text)
    except ValueError:
        return False
    if exp < time.time() or not sig.isascii():
        return False
    expected = hmac.new(_emergency_key(), f"emergency|{exp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 210_000)
    return f"pbkdf2_sha256$210000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, rounds, salt, expected = encoded.split("$", 3)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def streamer_token(streamer_id: int, max_age: int = 30 * 86400) -> str:
    payload = {"sid": streamer_id, "exp": int(time.time()) + max_age}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(get_secret().encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def streamer_id_from_cookies(cookies) -> int | None:
    try:
        raw, sig = cookies.get(STREAMER_COOKIE, "").split(".", 1)
        expected = hmac.new(get_secret().encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return int(data["sid"]) if int(data["exp"]) >= int(time.time()) else None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def streamer_id_from_request(conn) -> int | None:  # noqa: ANN001
    """主播 cookie 只在本机直连时认：主播账号登录已对公网关闭，旧域名上残留的 cookie 也不再生效。"""
    if not password_login_allowed(conn):
        return None
    return streamer_id_from_cookies(conn.cookies)
