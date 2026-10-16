"""运行时设置读写：录制方式 / 情绪精算 / 视频保留期，前端设置面板用。"""

from __future__ import annotations

from fastapi import APIRouter, Body

from ..core import runtime

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def get_settings_api() -> dict:
    return runtime.all_settings()


@router.put("")
def put_settings_api(
    record_video: bool | None = Body(None),
    auto_emotion: bool | None = Body(None),
    media_retention_days: int | None = Body(None),
) -> dict:
    if record_video is not None:
        runtime.set_setting("record_video", "1" if record_video else "0")
    if auto_emotion is not None:
        runtime.set_setting("auto_emotion", "1" if auto_emotion else "0")
    if media_retention_days is not None:
        runtime.set_setting("media_retention_days", str(max(0, int(media_retention_days))))
    return runtime.all_settings()
