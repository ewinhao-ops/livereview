"""统一登录核验（2026-10-01 起后台改认 workspace.example.test 平台登录态，替代共用口令）。

流程（各系统共同约定）：
1. 浏览器带平台 cookie studio_token（Domain=.example.test，HS256 JWT，sub=统一账号 UUID）；
2. 本地用 STUDIO_JWT_SECRET 验签名和 exp——不过就是没登录（401，前端整页跳平台登录页）；
3. 再调 studio 的 GET /api/me（本机回环，3 秒超时）读账号实时状态与权限：
   role=admin 或 feature_permissions 含 fupan_access 才放行，否则 403（前端跳平台「申请开通」页）。
   放行结论按人缓存 5 分钟（撤权/停用 5 分钟内生效）；没权限、已停用的结论不缓存——
   刚开通的人马上能进，也不会和平台的申请开通页来回跳；
4. studio 连不上：沿用这个人上一次的结论，30 秒内不再去问；从没核验过的人回 503。
本系统不另建会话：平台退出、换人后浏览器不再带原来的 token，下一次请求随即失效。
应急口令会话（auth.py）只认服务器本机直连，平台故障时管理员经 SSH 隧道用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx
from fastapi.responses import JSONResponse

from .auth import emergency_authed, is_local_direct
from .config import get_settings
from .core.safe_error import safe_exception_code

STUDIO_COOKIE = "studio_token"
SYSTEM_ID = "fupan"                     # 平台 /go/<系统 id>
ACCESS_PERMISSION = "fupan_access"      # = 原共用口令能做的全部后台操作

_ALLOW_TTL = 300.0       # 放行结论最多缓存 5 分钟
_RETRY_AFTER = 30.0      # studio 连不上时，30 秒内不再去问
_ME_TIMEOUT = 3.0
_CACHE_MAX = 500
_clock = time.monotonic  # 测试替换用


@dataclass(frozen=True)
class StudioUser:
    sub: str
    username: str
    display_name: str
    role: str
    allowed: bool            # 有本系统权限（admin 或 fupan_access）
    source: str = "studio"   # studio | emergency

    @property
    def label(self) -> str:
        """日志里的操作人：显示名(用户名)。"""
        if self.display_name and self.username and self.display_name != self.username:
            return f"{self.display_name}({self.username})"
        return self.display_name or self.username or self.sub or "未知账号"

    def public(self) -> dict:
        return {"username": self.username, "display_name": self.display_name or self.username,
                "role": self.role}


@dataclass(frozen=True)
class AuthResult:
    status: str              # ok | login（没登录/已失效）| forbidden（没开通）| unavailable（暂时无法核验）
    user: StudioUser | None = None


EMERGENCY_USER = StudioUser(sub="", username="emergency", display_name="本机应急口令",
                            role="admin", allowed=True, source="emergency")
PASSWORD_USER = StudioUser(sub="local-admin", username="admin", display_name="管理员",
                          role="admin", allowed=True, source="password")

_lock = threading.Lock()
_verdicts: dict[str, tuple[StudioUser | None, float]] = {}   # sub → (上次结论, 核验时刻)；None=令牌被拒/已停用
_studio_down_until = 0.0


def reset_cache() -> None:
    global _studio_down_until
    with _lock:
        _verdicts.clear()
        _studio_down_until = 0.0


def _b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_studio_token(token: str, secret: str, now: float | None = None) -> dict | None:
    """本地验 HS256 签名、exp（及 nbf）；任何一项不合格都返回 None，不区分原因。"""
    if not token or not secret or len(token) > 4096 or token.count(".") != 2:
        return None
    head, body, sig = token.split(".")
    try:
        header = json.loads(_b64url(head))
        payload = json.loads(_b64url(body))
        signature = _b64url(sig)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(header, dict) or header.get("alg") != "HS256" or not isinstance(payload, dict):
        return None
    expected = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None
    now = time.time() if now is None else now
    exp, nbf, sub = payload.get("exp"), payload.get("nbf"), payload.get("sub")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp <= now:
        return None
    if nbf is not None and (isinstance(nbf, bool) or not isinstance(nbf, (int, float)) or nbf > now + 60):
        return None
    if not isinstance(sub, str) or not sub.strip():
        return None
    return payload


async def _fetch_me(token: str) -> tuple[str, dict | None]:
    """调 studio /api/me：('ok', 账号) / ('denied', None) 令牌被拒或账号停用 / ('error', None) 暂时不可用。"""
    try:
        async with httpx.AsyncClient(timeout=_ME_TIMEOUT, follow_redirects=False, trust_env=False) as client:
            resp = await client.get(get_settings().studio_me_url,
                                    headers={"Cookie": f"{STUDIO_COOKIE}={token}",
                                             "Accept": "application/json"})
    except httpx.HTTPError as exc:
        _mark_down(safe_exception_code(exc, prefix="STUDIO_ME"))
        return "error", None
    if resp.status_code in (401, 403):
        return "denied", None
    if resp.status_code != 200:
        _mark_down(f"STUDIO_ME_HTTP_{resp.status_code}")
        return "error", None
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        _mark_down("STUDIO_ME_BAD_JSON")
        return "error", None
    return "ok", data


def _mark_down(code: str) -> None:
    global _studio_down_until
    with _lock:
        first = _clock() >= _studio_down_until
        _studio_down_until = _clock() + _RETRY_AFTER
    if first:
        print(f"[统一登录] studio 暂时不可用; code={code}; 30 秒内沿用各人上次的核验结论")


def _judge(sub: str, data: dict) -> tuple[str, StudioUser | None]:
    """studio 账号 → ('ok', 用户) / ('login', None) 已停用或不是同一人 / ('error', None) 返回格式不对。"""
    uid = str(data.get("id") or "").strip().lower()
    if not uid:
        return "error", None
    if uid != sub or data.get("is_active") is not True or data.get("account_status") != "approved":
        return "login", None
    role = str(data.get("role") or "")
    perms = data.get("feature_permissions")
    allowed = role == "admin" or (isinstance(perms, list) and ACCESS_PERMISSION in perms)
    return "ok", StudioUser(sub=sub, username=str(data.get("username") or "")[:64],
                            display_name=str(data.get("display_name") or "")[:64],
                            role=role[:32], allowed=allowed)


def _from_verdict(verdict: tuple[StudioUser | None, float] | None) -> AuthResult:
    if verdict is None:
        return AuthResult("unavailable")
    user = verdict[0]
    if user is None:
        return AuthResult("login")
    return AuthResult("ok" if user.allowed else "forbidden", user)


async def authenticate_studio(token: str) -> AuthResult:
    secret = get_settings().studio_jwt_secret
    if not secret:
        return AuthResult("unavailable")
    claims = verify_studio_token(token, secret)
    if claims is None:
        return AuthResult("login")
    sub = claims["sub"].strip().lower()
    now = _clock()
    with _lock:
        verdict = _verdicts.get(sub)
        down = now < _studio_down_until
    if verdict and verdict[0] is not None and verdict[0].allowed and now - verdict[1] < _ALLOW_TTL:
        return AuthResult("ok", verdict[0])
    if down:
        return _from_verdict(verdict)
    outcome, data = await _fetch_me(token)
    if outcome == "ok":
        outcome, user = _judge(sub, data or {})
        if outcome == "error":
            _mark_down("STUDIO_ME_BAD_PAYLOAD")
    else:
        user = None
    if outcome == "error":
        return _from_verdict(verdict)
    with _lock:
        _verdicts[sub] = (user, _clock())
        if len(_verdicts) > _CACHE_MAX:
            for key, _ in sorted(_verdicts.items(), key=lambda kv: kv[1][1])[:len(_verdicts) - _CACHE_MAX]:
                _verdicts.pop(key, None)
    return _from_verdict((user, now))


async def authenticate(conn) -> AuthResult:  # noqa: ANN001  Request / WebSocket
    """后台身份：本机应急会话优先（不依赖平台），其次平台统一登录。"""
    if emergency_authed(conn):
        return AuthResult("ok", PASSWORD_USER if get_settings().auth_mode == "password" else EMERGENCY_USER)
    if get_settings().auth_mode == "password":
        return AuthResult("login" if get_settings().access_password else "unavailable")
    token = conn.cookies.get(STUDIO_COOKIE, "")
    if not token:
        return AuthResult("unavailable") if not get_settings().studio_jwt_secret else AuthResult("login")
    return await authenticate_studio(token)


def default_next() -> str:
    """平台登录后默认回到的站内路径：PUBLIC_BASE_URL 的路径部分（/fupan/）。"""
    path = urlsplit(get_settings().public_base_url.strip()).path.rstrip("/")
    return f"{path}/"


def _platform() -> str:
    return get_settings().platform_url.strip().rstrip("/")


def login_url(next_path: str | None = None) -> str:
    if get_settings().auth_mode == "password":
        return default_next()
    return f"{_platform()}/login?next={quote(next_path or default_next(), safe='')}"


def access_url() -> str:
    return f"{_platform()}/go/{SYSTEM_ID}"


def logout_url() -> str:
    if get_settings().auth_mode == "password":
        return default_next()
    return f"{_platform()}/logout"


def failure_response(conn, result: AuthResult) -> JSONResponse:  # noqa: ANN001
    """核验不通过的接口回应：401 带登录地址、403 带申请开通地址、503 暂时无法核验。
    emergency_login=true 只在本机直连且配了应急口令时出现，前端据此显示应急口令框而不跳平台。"""
    if get_settings().auth_mode == "password":
        configured = bool(get_settings().access_password)
        return JSONResponse({
            "detail": "请登录" if configured else "请先运行安装配置脚本，设置管理员密码",
            "code": "need_login" if configured else "auth_unavailable",
            "emergency_login": configured,
        }, status_code=401 if configured else 503)
    emergency = is_local_direct(conn) and bool(get_settings().access_password)
    if result.status == "forbidden":
        return JSONResponse({"detail": "你的统一账号还没有开通直播复盘", "code": "need_access",
                             "go_url": access_url()}, status_code=403)
    if result.status == "unavailable":
        return JSONResponse({"detail": "统一登录暂时无法核验，请稍后刷新重试", "code": "auth_unavailable",
                             "emergency_login": emergency}, status_code=503)
    return JSONResponse({"detail": "未登录", "code": "need_login", "login_url": login_url(),
                         "emergency_login": emergency}, status_code=401)
