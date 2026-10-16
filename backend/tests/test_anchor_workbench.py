from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import anchor
from app.auth import hash_password
from app.db import Base
from app.models import (AudioFile, AudioStatus, CompassPoint, Critique, CritiqueLevel,
                        ReplaySession, SessionStatus, Severity, Streamer, StreamerAccount,
                        StreamerTurn, Transcript)
from app.schemas import SessionCreate


class AnchorWorkbenchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        with self.Session() as db:
            streamer = Streamer(name="测试主播")
            db.add(streamer); db.flush()
            db.add(StreamerAccount(streamer_id=streamer.id, username="anchor",
                                   password_hash=hash_password("password1"), enabled=False))
            replay = ReplaySession(title="空数据场次", status=SessionStatus.error,
                                   error_msg="音频合并失败：没有有效分片",
                                   started_at=datetime.now(), ended_at=datetime.now())
            db.add(replay); db.flush()
            db.add(StreamerTurn(session_id=replay.id, streamer_id=streamer.id,
                                seq=1, start_ms=0, end_ms=3000))
            db.add(Transcript(session_id=replay.id, streamer_id=streamer.id, seq=1,
                              start_ms=0, end_ms=1000, corrected_text="测试逐字稿"))
            db.add(Critique(session_id=replay.id, streamer_id=streamer.id,
                            level=CritiqueLevel.segment, severity=Severity.warn,
                            category="节奏", start_ms=0, end_ms=1000,
                            comment="需要优化", data=json.dumps({"dimensions": {"表达": 8}}),
                            is_fallback=True))
            db.add(AudioFile(session_id=replay.id, path="", filename="bad.m4a",
                             size_bytes=0, status=AudioStatus.available))
            db.commit(); self.streamer_id, self.replay_id = streamer.id, replay.id

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_disabled_account_has_friendly_error(self) -> None:
        # 2026-10-01 起主播账号登录只认服务器本机直连（公网关闭见 test_oa_sso.py）
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
        with self.Session() as db, self.assertRaises(HTTPException) as caught:
            anchor.login(request, "anchor", "password1", db)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("账号已禁用", caught.exception.detail)

    def test_public_reason_redacts_sensitive_details(self) -> None:
        reason = anchor._public_reason("合并失败 api_key=secret C:\\srv\\audio\\part.m4a")
        self.assertNotIn("secret", reason)
        self.assertNotIn("srv", reason)

    def test_public_reason_redacts_forward_slash_windows_path(self) -> None:
        reason = anchor._public_reason("merge failed C:/Users/test/private/session.part0.m4a")
        self.assertNotIn("Users", reason)
        self.assertNotIn("session.part0", reason)
        media = anchor._public_reason("ffmpeg: moov atom not found C:/private/part.m4a")
        self.assertEqual(media, "音频合并失败：录音分片损坏或不完整")

    def test_replay_returns_real_empty_states_and_audio_reason(self) -> None:
        request = SimpleNamespace(cookies={})
        with self.Session() as db, patch.object(anchor, "_sid", return_value=self.streamer_id), \
                patch("app.core.attribution.SessionLocal", self.Session):
            result = anchor.replay(self.replay_id, request, db)
        self.assertFalse(result["media"]["available"])
        self.assertIn("音频合并失败", result["media"]["reason"])
        self.assertEqual(result["compass"], {"status": "empty", "count": 0})
        self.assertEqual(result["knowledge"], {"status": "empty", "count": 0})
        self.assertTrue(result["critiques"][0]["is_fallback"])


class ReadyEndpointTests(unittest.TestCase):
    def test_ready_payload_is_public_and_blocks_missing_services(self) -> None:
        from app import main
        settings = SimpleNamespace(asr_provider="mock", llm_provider="mock",
                                   qwen_api_key="secret", aliyun_dashscope_api_key="secret",
                                   doubao_base_url="", llm_api_key="secret", ark_api_key="secret",
                                   hunyuan_api_key="secret", openai_api_key="secret")
        fake_chromium = SimpleNamespace(executable_path=str(Path("missing-chromium")))
        fake_playwright = SimpleNamespace(chromium=fake_chromium)
        context = unittest.mock.MagicMock()
        context.__enter__.return_value = fake_playwright
        with patch.object(main, "get_settings", return_value=settings), \
                patch.object(main, "SessionLocal", self.SessionFactory()), \
                patch.object(main.shutil, "which", return_value=None), \
                patch("playwright.sync_api.sync_playwright", return_value=context):
            payload = main.ready()
        self.assertFalse(payload["ready"])
        self.assertIn("components", payload)
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("missing-chromium", rendered)

    @staticmethod
    def SessionFactory():
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        return sessionmaker(engine)


class ReadyCreationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_check_runs_outside_asyncio_event_loop(self) -> None:
        from app.api import sessions
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionFactory = sessionmaker(engine)
        observed = {"event_loop_running": None}

        def readiness_probe():
            try:
                asyncio.get_running_loop()
                observed["event_loop_running"] = True
            except RuntimeError:
                observed["event_loop_running"] = False
            return {"ready": False, "blockers": ["测试门禁"]}

        with SessionFactory() as db, patch("app.main.ready", side_effect=readiness_probe):
            with self.assertRaises(HTTPException):
                await sessions.create_session(SessionCreate(share_link="https://example.com"), db)
        self.assertFalse(observed["event_loop_running"])
        engine.dispose()

    async def test_unready_system_rejects_new_session(self) -> None:
        from app.api import sessions
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionFactory = sessionmaker(engine)
        with SessionFactory() as db, patch("app.main.ready", return_value={
                "ready": False, "blockers": ["ASR：未配置"]}):
            with self.assertRaises(HTTPException) as caught:
                await sessions.create_session(SessionCreate(share_link="https://example.com"), db)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("ASR", caught.exception.detail)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
