"""可选付费集成测试。

手工设置 RUN_REAL_ASR_TEST=1、ALIYUN_DASHSCOPE_API_KEY 与
REAL_ASR_AUDIO_FILE（真实中文音频路径）后执行本文件。默认跳过，CI/就绪接口都不会付费。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from app.core.asr_stream import DashScopeASR, REAL_ASR_MODEL
from app.config import get_settings


@pytest.mark.skipif(os.getenv("RUN_REAL_ASR_TEST") != "1", reason="未启用真实 ASR 付费测试")
def test_real_chinese_audio_transcription_end_to_end() -> None:
    settings = get_settings()
    key = (settings.aliyun_dashscope_api_key or "").strip()
    audio_path = Path(os.getenv("REAL_ASR_AUDIO_FILE", ""))
    configured = (settings.ffmpeg_executable or "ffmpeg").strip()
    ffmpeg = configured if Path(configured).is_file() else shutil.which(configured)
    if not key or not audio_path.is_file() or not ffmpeg:
        pytest.fail("需配置 DashScope Key、真实中文音频文件和 ffmpeg")
    pcm = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(audio_path),
         "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
        check=True, capture_output=True,
    ).stdout

    async def transcribe() -> list[str]:
        asr = DashScopeASR(key, REAL_ASR_MODEL)
        finals: list[str] = []

        async def consume() -> None:
            async for event in asr:
                if event.kind == "final" and event.text.strip():
                    finals.append(event.text.strip())

        consumer = asyncio.create_task(consume())
        for offset in range(0, len(pcm), 3200):
            await asr.feed(pcm[offset:offset + 3200], offset * 1000 // 32000)
            await asyncio.sleep(0.08)
        await asr.aclose()
        await asyncio.wait_for(consumer, 10)
        return finals

    results = asyncio.run(transcribe())
    assert results
    assert any("\u4e00" <= char <= "\u9fff" for text in results for char in text)
