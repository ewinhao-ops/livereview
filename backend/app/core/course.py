"""上传资料（录制课程音视频 / 教程文档）→ 转写 + 提炼利于复盘的要点。

目标④：知识库不只是堆原文，而是【整理提炼】。流程：
  音视频 → ffmpeg 抽音轨 16k 单声道 PCM → 复用实时 ASR 转写
  文档/转写文本 → LLM 按类别（产品规格/销售话术/常见问答/品牌故事/合规）提炼要点
提炼版入库时优先被检索命中（embed.retrieve_detailed 对 kind=distilled 加权）。
无 ASR key（mock）时音视频无法转写，返回空；文档/文本仍可提炼（走 LLM）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .asr_stream import MockASR, get_asr_session
from .llm import get_llm
from .scene import detect_scene, key_from_label
from .safe_error import safe_error_text, safe_exception_code

AUDIO_VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".webm",
                   ".m4a", ".mp3", ".wav", ".aac", ".ogg")

# 最多转写约 30 分钟，避免超长课程拖垮/烧钱（16k*2byte*1800s）
_MAX_PCM_BYTES = 16000 * 2 * 1800
_FEED_CHUNK = 16000 * 2 // 5   # 200ms

_DISTILL_SYS = (
    "你是带货培训资料整理员。把给定的课程转写/教程文档，提炼成【利于直播复盘】的要点。"
    "每条要点标注两个维度："
    "category（内容类型：产品规格|销售话术|常见问答|品牌故事|合规）；"
    "scene（话术场景：开场留人|产品讲款|信任证据|异议答疑|逼单促单|互动宠粉|过款承接|合规风险|通用）。"
    "每条要点一句话、具体可直接对照直播使用，去掉寒暄与无关内容。"
    "只输出 JSON：{\"items\":[{\"category\":\"...\",\"scene\":\"...\",\"point\":\"要点\"}]}。"
)
_ALLOWED_CATEGORIES = {"产品规格", "销售话术", "常见问答", "品牌故事", "合规"}


def is_media(filename: str) -> bool:
    return (filename or "").lower().endswith(AUDIO_VIDEO_EXT)


async def _extract_pcm(src: Path) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out[:_MAX_PCM_BYTES]


async def transcribe_media(src: Path, hotwords: list[str] | None = None) -> str:
    """音视频 → 文本。无真实 ASR（mock）时返回 ""。"""
    asr = get_asr_session(hotwords or [])
    if isinstance(asr, MockASR):
        return ""
    pcm = await _extract_pcm(src)
    if not pcm:
        return ""
    finals: list[str] = []

    async def _consume():
        async for ev in asr:
            if ev.kind == "final" and ev.text:
                finals.append(ev.text)

    asr.start()
    ctask = asyncio.create_task(_consume())
    try:
        for i in range(0, len(pcm), _FEED_CHUNK):
            await asyncio.wait_for(asr.feed(pcm[i:i + _FEED_CHUNK], 0), timeout=10)
            await asyncio.sleep(0.003)   # 轻微节流，别打爆 WS
        await asr.aclose()
        await asyncio.wait_for(ctask, timeout=120)
    except Exception as e:  # noqa: BLE001
        print(
            "[course] transcription status=failed; "
            f"code={safe_exception_code(e, prefix='COURSE_ASR')}; "
            f"error={safe_error_text(e)}"
        )
        ctask.cancel()
    return "\n".join(finals)


async def distill_points(text: str) -> list[dict]:
    """文本 → 复盘要点列表 [{category, scene, point}]。失败/为空返回 []（调用方回退存原文）。

    scene 优先取 LLM 输出并映射为内部 key，识别不了则按要点文本关键词兜底。
    """
    text = (text or "").strip()
    if not text:
        return []
    out = await get_llm("deep").complete(_DISTILL_SYS, text[:6000], json_mode=True)
    try:
        items = json.loads(out).get("items", [])
    except (json.JSONDecodeError, AttributeError):
        items = []
    result = []
    for it in items:
        if not (isinstance(it, dict) and (it.get("point") or "").strip()):
            continue
        point = it["point"].strip()
        scene = key_from_label(it.get("scene") or "") or detect_scene(point)[0]
        category = (it.get("category") or "").strip()
        if category not in _ALLOWED_CATEGORIES:
            category = "销售话术"
        result.append({"category": category,
                       "scene": scene, "point": point})
    return result
