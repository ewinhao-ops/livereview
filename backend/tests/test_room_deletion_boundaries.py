from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import rooms
from app.db import Base, get_db
from app.models import LiveRoom, ReplaySession, SessionStatus


@pytest.fixture
def room_client(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'rooms.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    app = FastAPI()
    app.include_router(rooms.router)

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory
    finally:
        engine.dispose()


def test_missing_active_and_repeated_room_delete_are_stable(room_client):
    client, factory = room_client
    missing = client.delete("/api/rooms/999")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "ROOM_NOT_FOUND"

    with factory() as db:
        room = LiveRoom(room_id="active-room", title="活动房间")
        db.add(room)
        db.flush()
        session = ReplaySession(
            title="活动场次", room_id=room.room_id,
            status=SessionStatus.live, started_at=datetime.now(),
        )
        db.add(session)
        db.commit()
        room_id, session_id = room.id, session.id
    conflict = client.delete(f"/api/rooms/{room_id}")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "ROOM_IN_USE",
        "message": "直播间正被活动场次使用，不能删除",
        "session_id": session_id,
    }

    with factory() as db:
        session = db.get(ReplaySession, session_id)
        session.status = SessionStatus.done
        session.ended_at = datetime.now()
        db.commit()
    first = client.delete(f"/api/rooms/{room_id}")
    second = client.delete(f"/api/rooms/{room_id}")
    assert first.json()["already_deleted"] is False
    assert second.json()["already_deleted"] is True
    with factory() as db:
        # Soft delete keeps user configuration and audit data instead of cascading cleanup.
        row = db.get(LiveRoom, room_id)
        assert row is not None and row.deleted_at is not None
        assert db.get(ReplaySession, session_id) is not None


def test_room_list_has_stable_order_and_warns_over_fifty_without_deleting(room_client):
    client, factory = room_client
    now = datetime.now()
    with factory() as db:
        for index in range(51):
            db.add(LiveRoom(
                room_id=f"room-{index}", title=f"房间{index}",
                created_at=now + timedelta(seconds=index),
                last_used_at=now if index in {49, 50} else now - timedelta(minutes=index + 1),
            ))
        db.commit()

    first = client.get("/api/rooms")
    second = client.get("/api/rooms")
    assert first.status_code == 200
    assert len(first.json()) == 51
    assert [row["id"] for row in first.json()] == [row["id"] for row in second.json()]
    # Same last_used_at is resolved by created_at then id, so newest room is stable first.
    assert first.json()[0]["room_id"] == "room-50"
    assert first.headers["x-room-count-warning"] == "OVER_50"
    with factory() as db:
        assert db.query(LiveRoom).count() == 51
