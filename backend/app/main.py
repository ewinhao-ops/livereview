"""FastAPI 应用入口。

启动即建表；挂载 sessions / knowledge / streamers 路由；把 frontend/ 作为静态站点
直接服务。/api/* 认 workspace.example.test 平台统一登录（sso.py；没登录前端整页跳平台登录页），
机器接口（罗盘上报、上下播信号、partner）各自校验 token；本机直连另有应急口令入口。
"""

from __future__ import annotations

import asyncio
import hmac
import shutil  # noqa: F401  compatibility: readiness integrations patch this symbol
import time

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .api import (anchor, compass, compliance_rules, experience, knowledge, live, observation, partner, ready as ready_api,
                  rhythm_analysis, rhythm_library, rooms, script_compliance, sessions,
                  settings as settings_api, stats, streamers)
from .auth import (EMERGENCY_COOKIE, EMERGENCY_MAX_AGE, emergency_token, is_local_direct,
                   local_cookie_secure, password_login_allowed)
from .config import BACKEND_ROOT, get_settings
from .db import init_db
from .db import SessionLocal  # noqa: F401  compatibility for readiness integrations
from .core.safe_error import safe_error_text, safe_exception_code
from .sso import authenticate, failure_response, logout_url

app = FastAPI(title="LiveReview 主播直播实时复盘系统")
_service_tasks: set[asyncio.Task] = set()


def _start_service_task(coro) -> asyncio.Task:  # noqa: ANN001
    task = asyncio.create_task(coro)
    _service_tasks.add(task)
    task.add_done_callback(_service_tasks.discard)
    return task

# 公开放行（无需登录）：应急口令登录（只认本机直连）、退出、健康检查
_PUBLIC = {"/api/login", "/api/logout", "/api/health", "/api/ready"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_public(method: str, path: str) -> bool:
    # 罗盘上报（全局 POST /api/compass 与旧的按场次书签 POST /api/sessions/{id}/compass）：走 token 校验。
    # 旧场控脚本仍打 review.example.test/api/compass，后端只看路径、不看域名
    compass_report = method == "POST" and path.endswith("/compass")
    # 业绩系统上下播信号：走 signal_token 校验 + 回环限制
    signal_report = method == "POST" and path == "/api/live/signal"
    # 业绩系统 BFF（X-Partner-Token + 回环）与主播签名外链（HMAC 签名，4 小时）：自带校验
    partner = path.startswith("/api/partner/")
    # 主播端接口认主播 cookie（只认本机直连）；/api/anchor/admin/* 是后台管理操作，要统一登录
    anchor_api = path.startswith("/api/anchor/") and not path.startswith("/api/anchor/admin/")
    return path in _PUBLIC or compass_report or signal_report or partner or anchor_api


def _query(request: Request) -> str:
    return ("?" + request.url.query)[:200] if request.url.query else ""


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        # Browser writes must originate from this installation. Token-authenticated
        # integration endpoints retain their own origin/token checks.
        if (get_settings().auth_mode == "password" and request.method in _WRITE_METHODS
                and path.startswith("/api/")
                and (not _is_public(request.method, path)
                     or path in {"/api/login", "/api/logout"}
                     or path.startswith("/api/anchor/"))):
            origin = request.headers.get("origin")
            allowed = {f"{request.url.scheme}://{request.url.netloc}",
                       get_settings().public_base_url.rstrip("/")}
            if origin and origin.rstrip("/") not in allowed:
                return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
        if not path.startswith("/api/") or _is_public(request.method, path):
            response = await call_next(request)
            if request.method in _WRITE_METHODS and path.startswith("/api/partner/admin/"):
                print(f"[操作] 业绩系统(partner) {request.method} {path}{_query(request)} → {response.status_code}")
            return response
        result = await authenticate(request)
        if result.status != "ok":
            return failure_response(request, result)
        request.state.operator = result.user      # 接口里要用操作人时取这个
        response = await call_next(request)
        if request.method in _WRITE_METHODS:
            # 操作留痕（journalctl -u livereview）：谁、什么时候、动了哪个接口、结果如何
            print(f"[操作] {result.user.label} {request.method} {path}{_query(request)} → {response.status_code}")
        return response


app.add_middleware(AuthMiddleware)
# CORS：默认不放行任何跨域（前端与 API 同源，无需 CORS）；如需跨域在 CORS_ORIGINS 显式白名单
_cors = [o.strip() for o in (get_settings().cors_origins or "").split(",") if o.strip()]
if _cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# 登录暴力破解防护：同 IP 60 秒内失败 >5 次则限流
_login_fails: dict[str, list[float]] = {}


def _login_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    fails = [t for t in _login_fails.get(ip, []) if now - t < 60]
    _login_fails[ip] = fails
    return len(fails) >= 5


def _record_login_fail(ip: str) -> None:
    _login_fails.setdefault(ip, []).append(time.monotonic())

app.include_router(sessions.router)
app.include_router(streamers.router)
app.include_router(knowledge.router)
app.include_router(rooms.router)
app.include_router(stats.router)
app.include_router(settings_api.router)
app.include_router(live.router)
app.include_router(partner.router)
app.include_router(experience.router)
app.include_router(rhythm_library.router)
app.include_router(rhythm_analysis.router)
app.include_router(script_compliance.router)
app.include_router(compliance_rules.router)
app.include_router(observation.router)
app.include_router(compass.router)
app.include_router(anchor.router)
app.include_router(ready_api.router)


@app.on_event("startup")
async def _startup() -> None:
    settings = get_settings()
    if settings.auth_mode == "password" and len(settings.access_password) < 12:
        raise RuntimeError("ACCESS_PASSWORD must contain at least 12 characters. Run scripts/setup.py first.")
    init_db()
    settings.audio_path
    settings.log_path

    from .core.rhythm_library import recover_stuck_corpora
    recovered = recover_stuck_corpora()
    if recovered:
        print(f"[rhythm-library] 已恢复 {recovered} 条中断的提炼任务，可在知识库中重试")

    async def _rhythm_lease_sweeper() -> None:
        # 只在 startup 扫一次会让“启动后才过期”的任务永久停在 processing。
        while True:
            await asyncio.sleep(60)
            try:
                count = await asyncio.to_thread(recover_stuck_corpora)
                if count:
                    print(f"[rhythm-library] 已回收 {count} 条过期提炼任务")
            except Exception as exc:  # noqa: BLE001
                print(
                    "[rhythm-library] 提炼任务租约清理失败; "
                    f"code={safe_exception_code(exc, prefix='RHYTHM_SWEEP')}; "
                    f"error={safe_error_text(exc)}"
                )

    _start_service_task(_rhythm_lease_sweeper())

    # 上个进程没跑完的 v2 深度复盘不会有人接着跑：把残留的「生成中」复位，页面可重新生成
    try:
        from .core.review.v2_jobs import reset_stale_deep_status
        stale = reset_stale_deep_status()
        if stale:
            print(f"[deep_analysis] 已复位 {stale} 个上次没跑完的深度复盘状态")
    except Exception as exc:  # noqa: BLE001  状态复位失败不能挡住服务启动
        print(
            "[deep_analysis] 深度复盘状态复位失败; "
            f"code={safe_exception_code(exc, prefix='DEEP_STATUS_RESET')}; "
            f"error={safe_error_text(exc)}"
        )

    if settings.auth_mode == "studio" and not settings.studio_jwt_secret:
        print("[统一登录] 未配置 STUDIO_JWT_SECRET：无法核验平台登录态，后台接口将一律返回 503；"
              "请在 .env 配上与平台相同的密钥")
    pw = settings.access_password
    if not pw:
        print("[应急入口] 未配置 ACCESS_PASSWORD：本机应急口令登录已关闭")
    elif len(pw) < 12:
        print("[安全警告] ACCESS_PASSWORD 太短：应急口令建议换成 12 位以上的随机串")

    from .core.recovery import recover_orphans
    _start_service_task(recover_orphans())   # 后台恢复重启/崩溃遗留的孤儿会话

    from .core.autopilot import autopilot_loop
    _start_service_task(autopilot_loop())    # 无人值守：检测开播自动录制 + 保留期清理

    from .core.review.focus import focus_backfill_loop
    _start_service_task(focus_backfill_loop())   # 改进动作补漏：每 10 分钟补处理下播时没跑成的


@app.on_event("shutdown")
async def _shutdown() -> None:
    from .core.pipeline import stop_all_pipelines
    from .core.review.v2_jobs import cancel_all_jobs

    await stop_all_pipelines()
    await cancel_all_jobs()
    tasks = [task for task in _service_tasks if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _service_tasks.clear()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


def ready() -> dict:
    """兼容服务端建场门禁；HTTP 接口由 api.ready 统一提供。"""
    return ready_api.run_readiness_checks()


@app.post("/api/login")
def login(request: Request, password: str = Body(..., embed=True)) -> JSONResponse:
    """本机应急口令登录（平台故障时管理员经 SSH 隧道直连 8077 用）；公网一律走统一登录。
    cookie Path=/：直连时页面和接口都在根路径（没有 /fupan 前缀）。它只落在隧道地址（localhost /
    127.0.0.1）上，且只在本机直连时被认，经 workspace.example.test 的请求既带不上也用不了。"""
    if not password_login_allowed(request):
        raise HTTPException(403, "口令登录已关闭，请从办公平台统一登录")
    ip = request.client.host if request.client else "?"
    if _login_rate_limited(ip):
        raise HTTPException(429, "尝试过于频繁，请稍后再试")
    pw = get_settings().access_password
    if get_settings().auth_mode == "password" and len(pw) < 12:
        raise HTTPException(503, "请先设置至少 12 位的管理员密码")
    if not pw:                        # 未配置口令 = 应急入口关闭（没有代码默认值）
        raise HTTPException(503, "应急入口未开启：服务端未配置 ACCESS_PASSWORD")
    if not hmac.compare_digest(password.encode(), pw.encode()):
        _record_login_fail(ip)
        print("[应急入口] 本机口令登录失败")
        raise HTTPException(401, "口令错误")
    _login_fails.pop(ip, None)
    print("[应急入口] 本机口令登录成功")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        EMERGENCY_COOKIE, emergency_token(), httponly=True, samesite="strict",
        secure=local_cookie_secure(request), max_age=EMERGENCY_MAX_AGE, path="/",
    )
    return resp


@app.post("/api/logout")
def logout() -> JSONResponse:
    """退出：清本系统自己的 cookie（应急会话）；平台登录态由前端整页跳平台 /logout 清掉。"""
    resp = JSONResponse({"ok": True, "logout_url": logout_url()})
    resp.delete_cookie(EMERGENCY_COOKIE, path="/")
    return resp


@app.get("/api/me")
def me(request: Request) -> dict:
    # 能到这里说明已通过中间件鉴权；返回当前账号供页面显示，退出地址由服务端配置决定
    user = request.state.operator
    return {"ok": True, "auth": user.source, "user": user.public(), "logout_url": logout_url()}


# 前端静态站点（放最后，避免吃掉 /api 路由）
_frontend = (BACKEND_ROOT.parent / "frontend").resolve()
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
