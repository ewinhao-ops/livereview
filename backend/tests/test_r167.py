from __future__ import annotations

import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api import anchor
from app.core.realtime_review import analyze_realtime_sentence
from app.db import Base
from app.models import LiveAlert, ReplaySession, SessionStatus, Streamer


class RealtimeReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_existing_rag_and_fast_llm(self) -> None:
        provider = SimpleNamespace(complete=AsyncMock(return_value=json.dumps({
            "alert": {"kind": "product_error", "severity": "error", "title": "参数错误",
                      "message": "阻抗与知识库不符", "quote": "阻抗是 20 欧",
                      "suggestion": "正确参数请以商品页为准"}
        }, ensure_ascii=False)))
        with patch("app.core.realtime_review.retrieve_detailed", AsyncMock(return_value=[
                {"text": "该型号阻抗为 32 欧"}
        ])) as retrieve, patch("app.core.realtime_review.get_llm", return_value=provider) as llm:
            result = await analyze_realtime_sentence("阻抗是 20 欧", ["现在介绍这款耳机"])
        self.assertEqual(result["kind"], "product_error")
        llm.assert_called_once_with("fast")
        retrieve.assert_awaited_once()

    async def test_invalid_or_non_alert_model_output_is_ignored(self) -> None:
        provider = SimpleNamespace(complete=AsyncMock(return_value='{"alert": null}'))
        with patch("app.core.realtime_review.retrieve_detailed", AsyncMock(return_value=[])), \
                patch("app.core.realtime_review.get_llm", return_value=provider):
            self.assertIsNone(await analyze_realtime_sentence("大家晚上好", []))


class AlertPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.session_patch = patch.object(anchor, "SessionLocal", self.Session)
        self.session_patch.start()
        with self.Session() as db:
            streamer = Streamer(name="测试主播")
            db.add(streamer); db.flush()
            replay = ReplaySession(title="测试", status=SessionStatus.live,
                                   current_streamer_id=streamer.id, started_at=datetime.now())
            db.add(replay); db.commit()
            self.streamer_id, self.replay_id = streamer.id, replay.id

    def tearDown(self) -> None:
        self.session_patch.stop()
        self.engine.dispose()

    def test_duplicate_alert_within_window_is_suppressed(self) -> None:
        first = anchor.create_alert(self.replay_id, self.streamer_id, kind="compliance",
                                    severity="error", title="提醒", message="违规",
                                    quote="全网最低", at_ms=1)
        second = anchor.create_alert(self.replay_id, self.streamer_id, kind="compliance",
                                     severity="error", title="提醒", message="违规",
                                     quote="全网最低", at_ms=2)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        with self.Session() as db:
            self.assertEqual(len(db.execute(select(LiveAlert)).scalars().all()), 1)


if __name__ == "__main__":
    unittest.main()
