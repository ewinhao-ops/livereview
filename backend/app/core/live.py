"""直播画面 → 浏览器的实时转发（看得到、不落盘）。

设计原则：视频**只过内存、绝不写盘**，满足"服务器端不保留直播画面/视频"。
  - 真实模式：服务器带 cookie/referer 拉抖音 FLV，转成低码率预览流给浏览器
    （同源转发，顺带解决浏览器跨域与防盗链；浏览器用 mpegts.js 走 MSE 播放）。
  - mock 模式：ffmpeg 用 lavfi 合成一路测试 FLV，便于无真实链接时验证播放器。

注意：真实模式下预览与 capture 的 ffmpeg 各自拉一次流（双倍入向带宽）。
后续可优化为"单次入流 tee 给分析与播放"，MVP 先求正确。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx

from ..config import get_settings

_CHUNK = 64 * 1024
_PREVIEW_PRESETS = {
    "smooth": {
        "height": 360,
        "fps": 15,
        "video_bitrate": "650k",
        "maxrate": "760k",
        "bufsize": "1500k",
        "audio_bitrate": "48k",
    },
    "clear": {
        "height": 540,
        "fps": 18,
        "video_bitrate": "1100k",
        "maxrate": "1300k",
        "bufsize": "2600k",
        "audio_bitrate": "64k",
    },
    "high": {
        "height": 720,
        "fps": 18,
        "video_bitrate": "1650k",
        "maxrate": "1900k",
        "bufsize": "3800k",
        "audio_bitrate": "64k",
    },
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.douyin.com/",
}


async def mock_flv_stream() -> AsyncIterator[bytes]:
    """ffmpeg 合成测试 FLV（带动态画面 + 计时器 + 正弦音），无限直到客户端断开。"""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-re",
        "-f", "lavfi", "-i", "testsrc2=size=854x480:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=220",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-g", "50",
        "-c:a", "aac", "-b:a", "64k",
        "-f", "flv", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        assert proc.stdout
        while True:
            chunk = await proc.stdout.read(_CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                proc.kill()


async def proxy_flv_stream(url: str, cookie: str = "") -> AsyncIterator[bytes]:
    """把抖音 FLV 原样转发给浏览器（pass-through，不落盘）。"""
    headers = dict(_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    # 抖音拉流地址会 302 到节点专用地址，必须跟随重定向（ffmpeg 自动跟随，httpx 默认不跟）
    async with httpx.AsyncClient(timeout=None, headers=headers,
                                 follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes(_CHUNK):
                yield chunk


async def transcoded_flv_stream(
    url: str, cookie: str = "", quality: str = "clear"
) -> AsyncIterator[bytes]:
    """把原始直播流压成低码率 FLV 预览，避免公网下行带宽不足导致卡顿。"""
    preset = _PREVIEW_PRESETS.get(quality, _PREVIEW_PRESETS["clear"])
    fps = preset["fps"]
    headers = "Referer: https://live.douyin.com/\r\n"
    if cookie:
        headers += f"Cookie: {cookie}\r\n"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-headers", headers,
        "-user_agent", _HEADERS["User-Agent"],
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-rw_timeout", "15000000",
        "-i", url,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-vf", f"scale=-2:{preset['height']},fps={fps}",
        "-c:v", "libx264",
        "-preset", "superfast",
        "-tune", "zerolatency",
        "-profile:v", "main",
        "-pix_fmt", "yuv420p",
        "-b:v", preset["video_bitrate"],
        "-maxrate", preset["maxrate"],
        "-bufsize", preset["bufsize"],
        "-g", str(fps * 2),
        "-keyint_min", str(fps * 2),
        "-sc_threshold", "0",
        "-c:a", "aac",
        "-b:a", preset["audio_bitrate"],
        "-ar", "44100",
        "-ac", "1",
        "-f", "flv",
        "-flvflags", "no_duration_filesize",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        assert proc.stdout
        while True:
            chunk = await proc.stdout.read(_CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


def open_live_stream(stream_url: str, quality: str = "clear") -> AsyncIterator[bytes]:
    """有真实拉流地址走代理，否则给 mock 测试流。"""
    if stream_url:
        cookie = get_settings().douyin_cookie
        if quality == "source":
            return proxy_flv_stream(stream_url, cookie)
        return transcoded_flv_stream(stream_url, cookie, quality)
    return mock_flv_stream()
