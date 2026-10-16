from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import capture as capture_module
from app.core import pipeline as pipeline_module
from app.core.capture import FFmpegCapture
from app.db import Base
from app.models import (
    AudioFile, AudioStatus, Critique, CritiqueLevel, ReplaySession, SessionStatus, Severity,
)
from app.schemas import CritiqueOut


class _StubbornProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminate_count = 0
        self.kill_count = 0
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        self.terminate_count += 1

    def kill(self) -> None:
        self.kill_count += 1
        self.returncode = -9
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        return int(self.returncode)


class _LineReader:
    def __init__(self, *lines: bytes) -> None:
        self.lines = list(lines)

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""


class _StderrProcess:
    def __init__(self, line: bytes) -> None:
        self.returncode = None
        self.stderr = _LineReader(line)
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _MergeProcess:
    def __init__(self, output: Path, content: bytes, returncode: int = 0) -> None:
        self.output = output
        self.content = content
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.returncode == 0:
            self.output.write_bytes(self.content)
            return b"", b""
        return b"", b"invalid media"


class CaptureLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_capture(self, name: str = "session.m4a") -> FFmpegCapture:
        return FFmpegCapture(
            "https://stream.example/live.flv?token=secret",
            self.root / name,
            extract_frames=False,
        )

    def test_audio_only_recording_flushes_fragments_without_video_keyframes(self) -> None:
        capture = self.make_capture()
        capture._cur_audio = capture._part_path(0)
        command = capture._ffmpeg_cmd()
        self.assertEqual(capture._cur_audio.suffix, ".aac")
        self.assertIn("adts", command)
        self.assertIn("-flush_packets", command)
        self.assertIn("direct", command)
        self.assertNotIn("+frag_keyframe+empty_moov+default_base_moof", command)

    async def test_preflight_404_stops_without_ffmpeg_or_refresh(self) -> None:
        refreshed = 0

        async def provider() -> str:
            nonlocal refreshed
            refreshed += 1
            return "https://stream.example/new.flv"

        cap = self.make_capture()
        cap.url_provider = provider

        async def gone():
            return "gone", 404, "HTTP 404"

        cap._probe_stream_url = gone
        await cap.run()
        self.assertEqual(cap.error_code, "STREAM_HTTP_404")
        self.assertEqual(cap.end_reason, "live_ended_or_stream_expired")
        self.assertEqual(cap._part_idx, 0)
        self.assertEqual(refreshed, 0)
        self.assertEqual(cap.finalize_status, "error")

    async def test_preflight_403_refreshes_before_a_later_terminal_response(self) -> None:
        refreshed = 0
        probes = iter((
            ("retry", 403, "HTTP 403"),
            ("gone", 410, "HTTP 410"),
        ))

        async def provider() -> str:
            nonlocal refreshed
            refreshed += 1
            return "https://stream.example/refreshed.flv?token=new-secret"

        async def probe():
            return next(probes)

        cap = self.make_capture()
        cap.url_provider = provider
        cap._probe_stream_url = probe
        with patch.object(capture_module.asyncio, "sleep", return_value=None):
            await cap.run()
        self.assertEqual(refreshed, 1)
        self.assertEqual(cap.error_code, "STREAM_HTTP_410")
        self.assertEqual(cap._part_idx, 0)

    async def test_ffmpeg_explicit_404_kills_internal_reconnect(self) -> None:
        cap = self.make_capture()
        proc = _StderrProcess(b"Server returned 404 Not Found\n")
        cap._proc = proc
        with self.assertLogs("app.core.capture", level="WARNING"):
            await cap._drain_stderr()
        self.assertTrue(proc.killed)
        self.assertTrue(cap._stop.is_set())
        self.assertEqual(cap.error_code, "STREAM_HTTP_404")

    async def test_terminate_timeout_kills_and_repeated_stop_is_idempotent(self) -> None:
        cap = self.make_capture()
        cap._loop = asyncio.get_running_loop()
        proc = _StubbornProcess()
        cap._proc = proc
        with patch.object(capture_module, "PROCESS_STOP_TIMEOUT_SEC", 0.01):
            cap.stop()
            cap.stop()
            await cap._termination_task
        self.assertEqual(proc.terminate_count, 1)
        self.assertEqual(proc.kill_count, 1)

    async def test_no_valid_or_zero_byte_parts_fail_audio_finalize(self) -> None:
        cap = self.make_capture()
        zero = self.root / "zero.m4a"
        zero.touch()
        cap._audio_parts = [self.root / "missing.m4a", zero]
        self.assertFalse(await cap._finalize_audio())
        self.assertEqual(cap.finalize_status, "error")
        self.assertEqual(cap.finalize_error_code, "AUDIO_NO_VALID_PARTS")
        self.assertFalse(cap.audio_file.exists())

    async def test_valid_part_generates_nonzero_output_and_finalize_runs_once(self) -> None:
        cap = self.make_capture()
        zero = self.root / "zero.m4a"
        valid = self.root / "valid.m4a"
        zero.touch()
        valid.write_bytes(b"recoverable-valid-part")
        cap._audio_parts = [zero, valid]
        merge = _MergeProcess(cap.audio_file, valid.read_bytes())
        create = AsyncMock(return_value=merge)
        cap._validate_media_file = AsyncMock(return_value=True)
        with patch.object(capture_module.asyncio, "create_subprocess_exec", create):
            self.assertTrue(await cap._finalize_audio())
            self.assertTrue(await cap._finalize_audio())
        self.assertGreater(cap.audio_file.stat().st_size, 0)
        self.assertEqual(create.await_count, 1)
        self.assertTrue(zero.exists())
        self.assertFalse(valid.exists())

    async def test_merge_failure_preserves_valid_parts(self) -> None:
        cap = self.make_capture()
        valid = self.root / "valid.m4a"
        valid.write_bytes(b"recoverable-valid-part")
        cap._audio_parts = [valid]
        merge = _MergeProcess(cap.audio_file, b"", returncode=1)
        with patch.object(
            capture_module.asyncio, "create_subprocess_exec", AsyncMock(return_value=merge),
        ):
            self.assertFalse(await cap._finalize_audio())
        self.assertEqual(cap.finalize_error_code, "AUDIO_MERGE_FAILED")
        self.assertTrue(valid.exists())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    async def test_valid_part_produces_nonzero_readable_audio_and_finalize_is_once(self) -> None:
        cap = self.make_capture()
        zero = self.root / "zero.m4a"
        valid = self.root / "valid.m4a"
        zero.touch()
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=0.4", "-c:a", "aac", "-y", str(valid)],
            check=True, timeout=15,
        )
        cap._audio_parts = [zero, valid]
        self.assertTrue(await cap._finalize_audio())
        first_size = cap.audio_file.stat().st_size
        self.assertGreater(first_size, 0)
        self.assertTrue(await cap._validate_media_file(cap.audio_file))
        self.assertTrue(zero.exists())
        self.assertFalse(valid.exists())
        self.assertTrue(await cap._finalize_audio())
        self.assertEqual(cap.audio_file.stat().st_size, first_size)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    async def test_audio_only_capture_end_to_end_produces_readable_m4a(self) -> None:
        cap = FFmpegCapture(
            "mock://audio", self.root / "captured.m4a", mock=True,
            mock_duration_sec=1, extract_frames=False, record_video=False,
        )
        await cap.run()
        self.assertEqual(cap.finalize_status, "success")
        self.assertGreater(cap.audio_file.stat().st_size, 0)
        self.assertTrue(await cap._validate_media_file(cap.audio_file))

    async def test_finalize_timeout_is_bounded(self) -> None:
        cap = self.make_capture()

        async def no_capture_loop() -> None:
            return None

        async def stuck_finalize() -> bool:
            await asyncio.Event().wait()
            return True

        cap._run_loop = no_capture_loop
        cap._finalize_audio = stuck_finalize
        with patch.object(capture_module, "FINALIZE_TIMEOUT_SEC", 0.01):
            await cap.run()
        self.assertEqual(cap.finalize_status, "timeout")
        self.assertEqual(cap.finalize_error_code, "AUDIO_FINALIZE_TIMEOUT")


class PipelineTerminalStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "pipeline.db"
        self.engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.original_sessions = pipeline_module.SessionLocal
        pipeline_module.SessionLocal = self.sessions
        with self.sessions() as db:
            row = ReplaySession(status=SessionStatus.live)
            db.add(row)
            db.flush()
            db.add(AudioFile(
                session_id=row.id, filename="session.m4a", path="session.m4a",
                status=AudioStatus.recording,
            ))
            db.commit()
            self.session_id = row.id

    def tearDown(self) -> None:
        pipeline_module.SessionLocal = self.original_sessions
        self.engine.dispose()
        self.tmp.cleanup()

    def test_no_valid_parts_marks_audio_error_and_session_terminal(self) -> None:
        pipeline = pipeline_module.ReplayPipeline(self.session_id)
        pipeline._mark_media_error("AUDIO_NO_VALID_PARTS", "没有有效音频分片")
        # Repeating the same terminal write remains safe and does not create new rows/reports.
        pipeline._mark_media_error("AUDIO_NO_VALID_PARTS", "没有有效音频分片")
        with self.sessions() as db:
            row = db.get(ReplaySession, self.session_id)
            self.assertEqual(row.status, SessionStatus.error)
            self.assertIsNotNone(row.ended_at)
            self.assertEqual(row.error_code, "AUDIO_NO_VALID_PARTS")
            self.assertEqual(row.audio.status, AudioStatus.error)
            self.assertEqual(row.audio.size_bytes, 0)

    def test_finalize_timeout_still_marks_session_terminal(self) -> None:
        pipeline = pipeline_module.ReplayPipeline(self.session_id)
        pipeline._mark_media_error("AUDIO_FINALIZE_TIMEOUT", "音频收尾超过 30 秒")
        with self.sessions() as db:
            row = db.get(ReplaySession, self.session_id)
            self.assertEqual(row.status, SessionStatus.error)
            self.assertIsNotNone(row.ended_at)
            self.assertEqual(row.audio.status, AudioStatus.error)

    def test_repeated_pipeline_stop_calls_dependencies_once(self) -> None:
        class StopCounter:
            def __init__(self) -> None:
                self.count = 0

            def stop(self) -> None:
                self.count += 1

        pipeline = pipeline_module.ReplayPipeline(self.session_id)
        pipeline._capture = StopCounter()
        pipeline._danmaku = StopCounter()
        pipeline.stop()
        pipeline.stop()
        self.assertEqual(pipeline._capture.count, 1)
        self.assertEqual(pipeline._danmaku.count, 1)

    def test_fallback_metadata_is_persisted_and_returned_by_history_schema(self) -> None:
        pipeline = pipeline_module.ReplayPipeline(self.session_id)
        pipeline._save_critique(
            CritiqueLevel.segment, Severity.info, "节奏/表现", 0, 1000,
            comment="降级点评", data='{"is_fallback": true}',
            llm_provider="mock", llm_requested_provider="qwen", is_fallback=True,
        )
        with self.sessions() as db:
            row = db.query(Critique).filter_by(session_id=self.session_id).one()
            payload = CritiqueOut.model_validate(row)
        self.assertTrue(payload.is_fallback)
        self.assertEqual(payload.llm_provider, "mock")
        self.assertEqual(payload.llm_requested_provider, "qwen")


if __name__ == "__main__":
    unittest.main()
