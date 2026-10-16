"""长班次音频收尾（2026-10-03）：合并限时按大小放宽、超时放宽再试一次、失败/超时不出半截成品、
原始分片原样保留；切片直传场次合并没成功时不上传伴随音频。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core import capture as capture_module
from app.core import pipeline as pipeline_module
from app.core.capture import FFmpegCapture


class HangingMerge:
    """写了半截就卡住的 ffmpeg：等到被 kill。"""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.returncode = None
        self.killed = 0
        self._exit = asyncio.Event()

    async def communicate(self):
        self.output.write_bytes(b"half-written-without-index")
        await self._exit.wait()
        return b"", b""

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9
        self._exit.set()

    async def wait(self) -> int:
        await self._exit.wait()
        return self.returncode


class QuickMerge:
    def __init__(self, output: Path, returncode: int = 0) -> None:
        self.output = output
        self.returncode = returncode

    async def communicate(self):
        if self.returncode == 0:
            self.output.write_bytes(b"merged-ok")
            return b"", b""
        self.output.write_bytes(b"broken")
        return b"", b"Invalid data found when processing input"


def make_capture(root: Path, parts: int = 1) -> FFmpegCapture:
    cap = FFmpegCapture("https://stream.example/live.flv", root / "session_9.m4a",
                        extract_frames=False, record_video=True, hls=True)
    cap._audio_parts = []
    for index in range(parts):
        part = root / f"session_9.part{index}.m4a"
        part.write_bytes(b"x" * 1000)
        cap._audio_parts.append(part)
    return cap


def test_timeout_grows_with_size_and_is_capped():
    mb = 1024 * 1024
    assert capture_module.finalize_timeout(0) == 60
    assert capture_module.finalize_timeout(25 * mb) == pytest.approx(72.5)       # 1 小时伴随音频
    assert capture_module.finalize_timeout(385_009_700) == pytest.approx(60 + 385_009_700 / (2 * mb))
    assert 240 < capture_module.finalize_timeout(385_009_700) < 250             # 9-29 那场：约 4 分钟
    assert capture_module.finalize_timeout(50 * 1024 * mb) == 1800              # 封顶 30 分钟


def test_timeout_retries_once_with_a_longer_limit_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(capture_module, "FINALIZE_TIMEOUT_SEC", 0.05)
    cap = make_capture(tmp_path, parts=2)
    procs = [HangingMerge(cap.audio_file), QuickMerge(cap.audio_file)]
    create = AsyncMock(side_effect=procs)
    monkeypatch.setattr(capture_module.asyncio, "create_subprocess_exec", create)
    cap._validate_media_file = AsyncMock(return_value=True)
    waits: list[float] = []
    original = asyncio.wait_for

    async def spy(awaitable, timeout):
        if getattr(awaitable, "__name__", "") == "communicate":   # 只记合并本身的限时
            waits.append(timeout)
        return await original(awaitable, timeout)

    monkeypatch.setattr(capture_module.asyncio, "wait_for", spy)
    assert asyncio.run(cap._finalize_audio()) is True
    assert create.await_count == 2 and procs[0].killed == 1
    assert waits[1] == pytest.approx(waits[0] * 2)                     # 第二次放宽一倍
    assert cap.finalize_status == "success" and cap.audio_file.read_bytes() == b"merged-ok"
    assert not any(part.exists() for part in cap._audio_parts)        # 成功才删分片


def test_two_timeouts_keep_parts_and_leave_no_half_written_file(tmp_path, monkeypatch):
    monkeypatch.setattr(capture_module, "FINALIZE_TIMEOUT_SEC", 0.05)
    cap = make_capture(tmp_path)
    procs = [HangingMerge(cap.audio_file), HangingMerge(cap.audio_file)]
    monkeypatch.setattr(capture_module.asyncio, "create_subprocess_exec",
                        AsyncMock(side_effect=procs))
    assert asyncio.run(cap._finalize_audio()) is False
    assert (cap.finalize_status, cap.finalize_error_code) == ("timeout", "AUDIO_FINALIZE_TIMEOUT")
    assert "原始分片保留" in cap.finalize_error_message
    assert all(p.killed == 1 for p in procs)
    assert not cap.audio_file.exists()                                  # 半截成品删掉
    assert all(part.exists() for part in cap._audio_parts)              # 原始分片原样保留


def test_merge_failure_is_not_retried_and_keeps_parts(tmp_path, monkeypatch):
    cap = make_capture(tmp_path)
    create = AsyncMock(return_value=QuickMerge(cap.audio_file, returncode=1))
    monkeypatch.setattr(capture_module.asyncio, "create_subprocess_exec", create)
    assert asyncio.run(cap._finalize_audio()) is False
    assert create.await_count == 1
    assert cap.finalize_error_code == "AUDIO_MERGE_FAILED" and "Invalid data" in cap.finalize_error_message
    assert not cap.audio_file.exists() and cap._audio_parts[0].exists()


def test_unreadable_output_is_removed_and_parts_kept(tmp_path, monkeypatch):
    cap = make_capture(tmp_path)
    monkeypatch.setattr(capture_module.asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=QuickMerge(cap.audio_file)))
    cap._validate_media_file = AsyncMock(return_value=False)
    assert asyncio.run(cap._finalize_audio()) is False
    assert cap.finalize_error_code == "AUDIO_OUTPUT_UNREADABLE"
    assert not cap.audio_file.exists() and cap._audio_parts[0].exists()


def test_outer_guard_scales_with_part_size(tmp_path):
    cap = make_capture(tmp_path)
    small = cap._finalize_budget()
    cap._audio_parts[0].write_bytes(b"x" * 10_000_000)
    assert cap._finalize_budget() > small
    first = capture_module.finalize_timeout(10_000_000)
    assert cap._finalize_budget() >= first * 3                          # 够两次合并（第二次放宽一倍）


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="需要 ffmpeg")
def test_fragmented_companion_audio_merges_into_a_readable_file(tmp_path):
    """切片直传模式的伴随音频是碎片化 mp4（1 秒一片）：合并后要能读、时长不变。"""
    cap = make_capture(tmp_path, parts=0)
    part = tmp_path / "session_9.part0.m4a"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=8", "-ac", "2", "-c:a", "aac", "-b:a", "48k",
        "-movflags", "+empty_moov+default_base_moof", "-frag_duration", "1000000",
        "-f", "mp4", "-y", str(part),
    ], check=True, timeout=30)
    cap._audio_parts = [part]
    assert asyncio.run(cap._finalize_audio()) is True
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(cap.audio_file)],
                           capture_output=True, text=True, check=True, timeout=15)
    assert abs(float(probe.stdout.strip()) - 8) < 0.2
    assert not part.exists()


def _pipeline(tmp_path: Path):
    p = pipeline_module.ReplayPipeline.__new__(pipeline_module.ReplayPipeline)
    p.session_id = 9
    p._capture = FFmpegCapture("x", tmp_path / "session_9.m4a", extract_frames=False)
    return p


def test_companion_audio_is_uploaded_only_after_a_good_merge(tmp_path, monkeypatch):
    uploads = []
    monkeypatch.setattr(pipeline_module.media_store, "upload_file",
                        lambda path, key: uploads.append((Path(path).name, key)) or True)
    audio = tmp_path / "session_9.m4a"
    audio.write_bytes(b"good")
    p = _pipeline(tmp_path)
    p._capture.finalize_status = "success"
    assert asyncio.run(p._publish_companion_audio(audio, 4)) == 4
    assert uploads == [("session_9.m4a", "replay/session_9.m4a")] and not audio.exists()

    for status in ("timeout", "error"):
        uploads.clear()
        audio.write_bytes(b"half")                       # 就算半截文件还在，也不传
        p._capture.finalize_status = status
        p._capture.finalize_error_code = "AUDIO_FINALIZE_TIMEOUT"
        assert asyncio.run(p._publish_companion_audio(audio, 4)) == 1
        assert uploads == []
