"""运行时可调配置：SystemSetting 表覆盖 .env 默认，带内存缓存，前端设置面板可改。

覆盖项：record_video / auto_emotion / media_retention_days（其余仍走 .env）。
未在表里设置的项自动回落到 get_settings() 的 .env 值。
"""

from __future__ import annotations

from ..config import get_settings
from ..db import SessionLocal
from ..models import SystemSetting

_cache: dict[str, str] = {}
_loaded = False


def _load() -> None:
    global _loaded
    _cache.clear()
    with SessionLocal() as db:
        for s in db.query(SystemSetting).all():
            _cache[s.key] = s.value
    _loaded = True


def _get(key: str) -> str | None:
    if not _loaded:
        _load()
    return _cache.get(key)


def set_setting(key: str, value: str) -> None:
    with SessionLocal() as db:
        s = db.get(SystemSetting, key)
        if s:
            s.value = value
        else:
            db.add(SystemSetting(key=key, value=value))
        db.commit()
    _cache[key] = value


def record_video() -> bool:
    v = _get("record_video")
    return (v == "1") if v is not None else get_settings().record_video


def auto_emotion() -> bool:
    v = _get("auto_emotion")
    return (v == "1") if v is not None else get_settings().auto_emotion


def retention_days() -> int:
    v = _get("media_retention_days")
    try:
        return int(v) if v is not None else get_settings().media_retention_days
    except (TypeError, ValueError):
        return get_settings().media_retention_days


# --- 业绩系统信号行为开关（灰度热切，不用重启；阶段2 实装消费方） ---

_OFF_MODES = {"ignore", "close_turn", "stop"}


def signal_autostart_enabled() -> bool:
    v = _get("signal_autostart_enabled")
    return (v == "1") if v is not None else get_settings().signal_autostart_enabled


def signal_off_mode() -> str:
    v = _get("signal_off_mode")
    mode = v if v is not None else get_settings().signal_off_mode
    return mode if mode in _OFF_MODES else "ignore"   # 配错档位按最安全的 ignore


def signal_record_all() -> bool:
    """全部班次录制（2026-09-29 用户要求每个班次都录）：开=任何上播信号都登记开录意向，
    不再看主播是否打开「我要复盘」；关=回到手动复盘模式。"""
    v = _get("signal_record_all")
    return (v == "1") if v is not None else bool(getattr(get_settings(), "signal_record_all", False))


def signal_stop_grace_sec() -> int:
    v = _get("signal_stop_grace_sec")
    try:
        return int(v) if v is not None else get_settings().signal_stop_grace_sec
    except (TypeError, ValueError):
        return get_settings().signal_stop_grace_sec


def all_settings() -> dict:
    return {
        "record_video": record_video(),
        "auto_emotion": auto_emotion(),
        "media_retention_days": retention_days(),
    }
