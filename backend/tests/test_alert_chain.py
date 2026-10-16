from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import auth
from app.api import anchor
from app.bus import bus
from app.core.danmaku_alert import analyze_danmaku_alert
from app.core.knowledge import load_kb
from app.core.review.sentence import check_sentence
from app.db import Base, get_db
from app.models import (AudioFile, AudioStatus, Critique, CritiqueLevel, LiveAlert,
                        ReplaySession, SessionStatus, Severity, Streamer,
                        StreamerAccount, StreamerTurn, Transcript)


class TestAlertChain:
    def setup_method(self) -> None:
        self.tmp = TemporaryDirectory()
        db_path = Path(self.tmp.name) / "alerts.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        with self.Session() as db:
            first = Streamer(name="测试主播甲")
            second = Streamer(name="测试主播乙")
            db.add_all([first, second])
            db.flush()
            db.add_all([
                StreamerAccount(streamer_id=first.id, username="anchor-a",
                                password_hash="unused-test-hash", enabled=True),
                StreamerAccount(streamer_id=second.id, username="anchor-b",
                                password_hash="unused-test-hash", enabled=True),
            ])
            replay_a = ReplaySession(
                title="甲的脱敏场次", status=SessionStatus.live,
                current_streamer_id=first.id,
            )
            replay_b = ReplaySession(
                title="乙的脱敏场次", status=SessionStatus.live,
                current_streamer_id=second.id,
            )
            db.add_all([replay_a, replay_b])
            db.flush()
            db.add_all([
                StreamerTurn(session_id=replay_a.id, streamer_id=first.id, seq=0),
                StreamerTurn(session_id=replay_b.id, streamer_id=second.id, seq=0),
            ])
            db.commit()
            self.a, self.b = first.id, second.id
            self.replay_a, self.replay_b = replay_a.id, replay_b.id
        self.session_patch = patch.object(anchor, "SessionLocal", self.Session)
        self.session_patch.start()

    def teardown_method(self) -> None:
        self.session_patch.stop()
        bus._streamer_subscribers.clear()
        self.engine.dispose()
        self.tmp.cleanup()

    def _create(self, *, kind: str, at_ms: int, streamer_id: int | None = None,
                session_id: int | None = None, quote: str = "脱敏测试原话"):
        return anchor.create_alert(
            session_id or self.replay_a,
            streamer_id or self.a,
            kind=kind,
            severity="warn",
            title="测试提醒",
            message="检测到测试问题",
            quote=quote,
            suggestion="请使用准确、合规的替代表述",
            at_ms=at_ms,
        )

    def test_non_sensitive_rules_trigger_all_three_required_types(self) -> None:
        kb = load_kb()
        compliance = check_sentence("今天保证全网最低价", kb)
        product = check_sentence("Demo Pro 阻抗只有六欧而且手机很好推", kb)
        danmaku = analyze_danmaku_alert("这个价格太贵了，我还要再看看")
        assert any(flag.category == "违禁词" for flag in compliance)
        assert any(flag.category == "规格" for flag in product)
        assert danmaku and danmaku["kind"] == "danmaku_anomaly"
        assert danmaku["quote"] and danmaku["suggestion"]

    def test_database_event_key_suppresses_concurrent_duplicates(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: self._create(kind="compliance", at_ms=12000), range(2)
            ))
        assert sum(item is not None for item in results) == 1
        with self.Session() as db:
            rows = db.execute(select(LiveAlert)).scalars().all()
            assert len(rows) == 1
            assert rows[0].event_key

    def test_creation_gate_rejects_cross_streamer_alert(self) -> None:
        event = self._create(
            kind="product_error", at_ms=2000,
            streamer_id=self.b, session_id=self.replay_a,
        )
        assert event is None
        with self.Session() as db:
            assert not db.execute(select(LiveAlert)).scalars().all()

    def test_api_websocket_identity_read_and_disconnect_recovery(self) -> None:
        app = FastAPI()
        app.include_router(anchor.router)

        def db_dependency():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = db_dependency

        @app.post("/test/publish/{session_id}/{streamer_id}")
        async def publish(session_id: int, streamer_id: int, kind: str, at_ms: int):
            event = anchor.create_alert(
                session_id, streamer_id, kind=kind, severity="warn",
                title="联动测试", message="非敏感联动数据", quote=f"测试原话-{at_ms}",
                suggestion="测试优化建议", at_ms=at_ms,
            )
            if event:
                await bus.publish_streamer(streamer_id, event)
            return event or {"duplicate": True}

        with patch.object(auth, "get_secret", return_value="unit-test-secret"), TestClient(app) as client:
            client.cookies.set(auth.STREAMER_COOKIE, auth.streamer_token(self.a))
            for kind, at_ms in (("compliance", 1000), ("product_error", 6000),
                                ("danmaku_anomaly", 11000)):
                assert self._create(kind=kind, at_ms=at_ms)

            alerts = client.get("/api/anchor/alerts").json()
            assert {item["kind"] for item in alerts} == {
                "compliance", "product_error", "danmaku_anomaly",
            }
            assert all(item["quote"] and item["suggestion"] and item["occurred_at"]
                       for item in alerts)

            other = TestClient(app)
            other.cookies.set(auth.STREAMER_COOKIE, auth.streamer_token(self.b))
            assert other.get("/api/anchor/alerts").json() == []
            # 修改场次 ID 也不能读取另一主播的复盘或媒体；统一返回 404，
            # 避免泄露“场次存在但无权访问”这一内部信息。
            assert client.get(f"/api/anchor/replays/{self.replay_b}").status_code == 404
            assert client.get(
                f"/api/anchor/replays/{self.replay_b}/media"
            ).status_code == 404
            assert other.get(f"/api/anchor/replays/{self.replay_a}").status_code == 404

            for item in alerts:
                assert client.post(f"/api/anchor/alerts/{item['id']}/read").status_code == 200
            assert client.get("/api/anchor/alerts?unread_only=true").json() == []

            # 实时联动：先发布乙主播事件，再发布甲主播事件；甲的 WS 只能收到甲。
            with client.websocket_connect("/api/anchor/ws") as websocket:
                assert client.post(
                    f"/test/publish/{self.replay_b}/{self.b}?kind=danmaku_anomaly&at_ms=21000"
                ).status_code == 200
                assert client.post(
                    f"/test/publish/{self.replay_a}/{self.a}?kind=compliance&at_ms=26000"
                ).status_code == 200
                pushed = websocket.receive_json()
                assert pushed["type"] == "alert"
                assert pushed["kind"] == "compliance"
                assert pushed["session_id"] == self.replay_a

            # WS 断连期间仍先落库；重连前可由未读接口补查，数据不丢。
            assert client.post(
                f"/test/publish/{self.replay_a}/{self.a}?kind=product_error&at_ms=31000"
            ).status_code == 200
            unread = client.get("/api/anchor/alerts?unread_only=true").json()
            assert {item["at_ms"] for item in unread} == {26000, 31000}

    def test_completed_replay_artifacts_remain_queryable_after_reconnect(self) -> None:
        media_path = Path(self.tmp.name) / "completed-session.m4a"
        media_path.write_bytes(b"non-zero-readable-test-media")
        with self.Session() as db:
            replay = db.get(ReplaySession, self.replay_a)
            replay.status = SessionStatus.done
            replay.ended_at = datetime.now()
            db.add(AudioFile(
                session_id=replay.id, path=str(media_path), filename=media_path.name,
                size_bytes=media_path.stat().st_size, status=AudioStatus.available,
            ))
            db.add(Transcript(
                session_id=replay.id, streamer_id=self.a, seq=1,
                start_ms=0, end_ms=1000, raw_text="脱敏逐字稿", corrected_text="脱敏逐字稿",
            ))
            db.add_all([
                Critique(
                    session_id=replay.id, streamer_id=self.a,
                    level=CritiqueLevel.segment, severity=Severity.info,
                    category="节奏/表现", start_ms=0, end_ms=1000, comment="脱敏点评",
                ),
                Critique(
                    session_id=replay.id, streamer_id=self.a,
                    level=CritiqueLevel.summary, severity=Severity.info,
                    category="整场总结", comment="脱敏整场报告", score=8,
                ),
            ])
            db.add(LiveAlert(
                session_id=replay.id, streamer_id=self.a, kind="compliance",
                severity="warn", title="脱敏提醒", message="测试消息",
            ))
            db.commit()

        app = FastAPI()
        app.include_router(anchor.router)

        def db_dependency():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = db_dependency
        with patch.object(auth, "get_secret", return_value="unit-test-secret"), patch(
            "app.core.attribution.SessionLocal", self.Session,
        ), TestClient(app) as client:
            client.cookies.set(auth.STREAMER_COOKIE, auth.streamer_token(self.a))
            detail = client.get(f"/api/anchor/replays/{self.replay_a}")
            assert detail.status_code == 200
            payload = detail.json()
            assert payload["media"]["available"] is True
            assert payload["transcripts"][0]["text"] == "脱敏逐字稿"
            assert {item["level"] for item in payload["critiques"]} == {"segment", "summary"}
            assert client.get(
                f"/api/anchor/replays/{self.replay_a}/media"
            ).content == media_path.read_bytes()
            assert client.get("/api/anchor/alerts?unread_only=true").json()

            client.cookies.set(auth.STREAMER_COOKIE, auth.streamer_token(self.b))
            assert client.get(f"/api/anchor/replays/{self.replay_a}").status_code == 404
