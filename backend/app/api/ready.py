"""本地运行就绪检查；不发起 ASR/LLM 网络请求。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..config import get_settings
from ..core.asr_stream import real_asr_configuration_error
from ..core.llm import llm_configuration_error
from ..db import engine

router = APIRouter(prefix="/api", tags=["ready"])
_LIVE_PROBE_TTL_SEC = 300
_last_live_probe: dict = {"at": 0.0, "result": None}


def _result(ok: bool, reason: str = "") -> dict:
    return {"ok": ok, "reason": reason if not ok else ""}


def _check_database() -> dict:
    """用连接级临时表验证读写，不修改任何业务表。"""
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TEMPORARY TABLE ready_probe (value INTEGER)"))
            conn.execute(text("INSERT INTO ready_probe(value) VALUES (1)"))
            if conn.execute(text("SELECT value FROM ready_probe")).scalar_one() != 1:
                return _result(False, "数据库写入后读取结果异常")
            conn.execute(text("DROP TABLE ready_probe"))
        return _result(True)
    except Exception:  # noqa: BLE001
        return _result(False, "数据库读写不可用")


def _check_data_directory() -> dict:
    try:
        directory = get_settings().audio_path
        with tempfile.NamedTemporaryFile(prefix=".ready-", dir=directory, delete=True) as probe:
            probe.write(b"ready")
            probe.flush()
        return _result(True)
    except Exception:  # noqa: BLE001
        return _result(False, "业务数据目录不可写")


def _check_ffmpeg() -> dict:
    configured = (get_settings().ffmpeg_executable or "ffmpeg").strip()
    executable = configured if os.path.isfile(configured) else shutil.which(configured)
    if not executable:
        return _result(False, "ffmpeg 未安装或未配置为可执行文件")
    try:
        completed = subprocess.run(
            [executable, "-version"], capture_output=True, timeout=5, check=False,
        )
        return _result(completed.returncode == 0,
                       "ffmpeg 无法执行" if completed.returncode else "")
    except (OSError, subprocess.SubprocessError):
        return _result(False, "ffmpeg 无法执行")


def _check_chromium() -> dict:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            configured = (get_settings().playwright_chromium_executable_path
                          or os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "")).strip()
            if configured:
                options["executable_path"] = configured
            browser = playwright.chromium.launch(**options)
            browser.version
            browser.close()
        return _result(True)
    except Exception:  # noqa: BLE001
        return _result(False, "Chromium 服务无法启动")


def _check_asr() -> dict:
    error = real_asr_configuration_error(get_settings())
    return _result(not error, error)


def _check_llm() -> dict:
    error = llm_configuration_error(get_settings())
    return _result(not error, error)


def run_readiness_checks() -> dict:
    checks = {
        "database": _check_database(),
        "data_directory": _check_data_directory(),
        "ffmpeg": _check_ffmpeg(),
        "chromium": _check_chromium(),
        "asr": _check_asr(),
        "llm": _check_llm(),
    }
    labels = {
        "database": "数据库",
        "data_directory": "业务数据目录",
        "ffmpeg": "FFmpeg",
        "chromium": "Chromium",
        "asr": "ASR",
        "llm": "LLM",
    }
    components = {
        name: {
            "ok": item["ok"],
            "label": labels[name],
            "message": "可用" if item["ok"] else item["reason"],
        }
        for name, item in checks.items()
    }
    blockers = [
        f'{item["label"]}：{item["message"]}'
        for item in components.values() if not item["ok"]
    ]
    return {
        "ready": not blockers,
        "checks": checks,
        "components": components,
        "blockers": blockers,
    }


@router.get("/ready")
def ready() -> JSONResponse:
    payload = run_readiness_checks()
    return JSONResponse(payload, status_code=200 if payload["ready"] else 503)


@router.get("/ready/live")
async def live_ready() -> JSONResponse:
    """Opt-in provider probe; normal readiness remains network-free."""
    payload = run_readiness_checks()
    if not payload["ready"]:
        return JSONResponse(payload, status_code=503)
    now = time.monotonic()
    cached = _last_live_probe.get("result")
    if cached and now - float(_last_live_probe.get("at", 0)) < _LIVE_PROBE_TTL_SEC:
        return JSONResponse({**payload, "live_probe": cached}, status_code=200 if cached["ok"] else 503)
    try:
        from ..core.llm import test_llm_connection
        result = await test_llm_connection()
        probe = {"ok": True, "provider": result.get("provider", ""),
                 "model": result.get("model", "")}
    except Exception as exc:  # noqa: BLE001
        probe = {"ok": False, "reason": "模型接口探测失败"}
    _last_live_probe.update({"at": now, "result": probe})
    return JSONResponse({**payload, "live_probe": probe}, status_code=200 if probe["ok"] else 503)
