from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api import sessions as sessions_api
from app.core.douyin import StreamResolution
from app.core.live_link import normalize_live_link
from app.db import Base
from app.models import ReplaySession, SessionStatus, Streamer
from app.schemas import SessionCreate


class TestDuplicateSessions:
    def setup_method(self) -> None:
        self.tmp = TemporaryDirectory()
        db_path = Path(self.tmp.name) / "sessions.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.resolution = StreamResolution(
            room_id="889900", stream_url="https://media.example.test/live.flv",
            title="脱敏直播标题", anchor_nickname="测试主播",
        )
        self.ready_patch = patch(
            "app.main.ready", return_value={"ready": True, "blockers": []}
        )
        self.ready_patch.start()

    def teardown_method(self) -> None:
        self.ready_patch.stop()
        self.engine.dispose()
        self.tmp.cleanup()

    async def _create(self, link: str):
        with self.Session() as db:
            return await sessions_api.create_session(SessionCreate(share_link=link), db)

    def test_serial_duplicate_returns_original_session_in_409(self) -> None:
        link = "https://live.douyin.com/889900?enter_method=copy_link_share"
        with patch.object(
            sessions_api.douyin, "resolve_stream", AsyncMock(return_value=self.resolution)
        ):
            first = asyncio.run(self._create(link))
            try:
                asyncio.run(self._create("https://www.live.douyin.com/889900"))
                raise AssertionError("重复请求应返回 409")
            except HTTPException as exc:
                assert exc.status_code == 409
                assert exc.detail["code"] == "ACTIVE_SESSION_EXISTS"
                assert exc.detail["session"]["id"] == first.id
                assert "stream_url" not in exc.detail["session"]

        with self.Session() as db:
            rows = db.execute(select(ReplaySession)).scalars().all()
            assert len(rows) == 1

    def test_concurrent_duplicate_submissions_keep_one_active_session(self) -> None:
        link = "https://live.douyin.com/778899"

        async def delayed_resolution(**_kwargs):
            await asyncio.sleep(0.05)
            return self.resolution

        async def submit_twice():
            return await asyncio.gather(
                self._create(link),
                self._create(f"{link}?utm_source=test"),
                return_exceptions=True,
            )

        with patch.object(sessions_api.douyin, "resolve_stream", delayed_resolution):
            results = asyncio.run(submit_twice())

        assert sum(not isinstance(item, Exception) for item in results) == 1
        conflicts = [item for item in results if isinstance(item, HTTPException)]
        assert len(conflicts) == 1 and conflicts[0].status_code == 409
        with self.Session() as db:
            normalized = normalize_live_link(link)
            active = db.execute(select(ReplaySession).where(
                ReplaySession.normalized_share_link == normalized,
                ReplaySession.ended_at.is_(None),
                ReplaySession.status.in_([
                    SessionStatus.created, SessionStatus.parsing,
                    SessionStatus.live, SessionStatus.processing,
                ]),
            )).scalars().all()
            assert len(active) == 1

    def test_finished_session_allows_a_new_broadcast(self) -> None:
        link = "https://live.douyin.com/667788"
        with patch.object(
            sessions_api.douyin, "resolve_stream", AsyncMock(return_value=self.resolution)
        ):
            first = asyncio.run(self._create(link))
            with self.Session() as db:
                row = db.get(ReplaySession, first.id)
                row.status = SessionStatus.done
                row.ended_at = datetime.now()
                db.commit()
            second = asyncio.run(self._create(f"{link}?enter_from_merge=share"))

        assert second.id != first.id
        with self.Session() as db:
            assert len(db.execute(select(ReplaySession)).scalars().all()) == 2

    def test_controlled_source_title_falls_back_to_assigned_streamer_name(self) -> None:
        resolution = StreamResolution(
            room_id="", stream_url="http://127.0.0.1:19090/test.wav",
            title="", anchor_nickname="",
        )
        with self.Session() as db:
            streamer = Streamer(name="可控测试主播")
            db.add(streamer)
            db.commit()
            streamer_id = streamer.id
        with self.Session() as db, patch.object(
            sessions_api.douyin, "resolve_stream", AsyncMock(return_value=resolution),
        ):
            created = asyncio.run(sessions_api.create_session(SessionCreate(
                manual_stream_url=resolution.stream_url, streamer_ids=[streamer_id],
            ), db))
        assert created.title.startswith("可控测试主播 · ")
        assert "广告" not in created.title
