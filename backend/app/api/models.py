"""数据模型。

落盘原则：媒体只有"音频文件"一项持久化（AudioFile 指向磁盘文件）；
画面/视频从不入库；逐字稿、OCR 文本、点评都是文本，存库成本低。

多主播：一场直播可由 N 个主播轮班。Streamer 是花名册，StreamerTurn 记录
每个主播的上播时段；每条转写/OCR/点评都带 streamer_id，按"谁在播"归属，
便于按主播分别复盘。
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class SessionStatus(str, enum.Enum):
    created = "created"
    parsing = "parsing"
    live = "live"
    processing = "processing"
    done = "done"
    error = "error"


class AudioStatus(str, enum.Enum):
    recording = "recording"
    available = "available"
    deleted = "deleted"


class CritiqueLevel(str, enum.Enum):
    sentence = "sentence"
    screen = "screen"        # 画面文字点评
    segment = "segment"
    summary = "summary"


class Severity(str, enum.Enum):
    info = "info"
    warn = "warn"
    error = "error"


class Streamer(Base):
    """主播花名册（后台可增删改）。"""

    __tablename__ = "streamers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    note: Mapped[str] = mapped_column(Text, default="")
    color: Mapped[str] = mapped_column(String(16), default="")  # 看板上区分颜色
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class LiveRoom(Base):
    """常用直播间：粘贴链接建场时自动收录，之后可从列表复选，不用重复粘链接。"""

    __tablename__ = "live_rooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[str] = mapped_column(String(64), index=True, default="")  # 去重键
    title: Mapped[str] = mapped_column(String(160), default="")               # 直播间名/昵称
    share_link: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ReplaySession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(160), default="")
    share_link: Mapped[str] = mapped_column(Text, default="")
    room_id: Mapped[str] = mapped_column(String(64), default="")
    stream_url: Mapped[str] = mapped_column(Text, default="")
    lineup: Mapped[str] = mapped_column(Text, default="")  # JSON: [streamer_id,...] 排班顺序
    current_streamer_id: Mapped[int | None] = mapped_column(
        ForeignKey("streamers.id"), nullable=True
    )
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus), default=SessionStatus.created
    )
    error_msg: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    audio: Mapped["AudioFile | None"] = relationship(
        back_populates="session", uselist=False, cascade="all, delete-orphan"
    )
    transcripts: Mapped[list["Transcript"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    ocr_records: Mapped[list["OcrRecord"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    critiques: Mapped[list["Critique"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    turns: Mapped[list["StreamerTurn"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class StreamerTurn(Base):
    """一个主播的上播时段（一场直播里换人就新开一段）。"""

    __tablename__ = "streamer_turns"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    start_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_ms: Mapped[int] = mapped_column(Integer, default=0)

    session: Mapped[ReplaySession] = relationship(back_populates="turns")


class AudioFile(Base):
    __tablename__ = "audio_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    path: Mapped[str] = mapped_column(Text, default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    cos_key: Mapped[str] = mapped_column(Text, default="")       # COS 对象 key，空=仅本地
    media_kind: Mapped[str] = mapped_column(String(16), default="audio")  # audio | video
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[AudioStatus] = mapped_column(
        Enum(AudioStatus), default=AudioStatus.recording
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    session: Mapped[ReplaySession] = relationship(back_populates="audio")


class Transcript(Base):
    """一条定句（final）。partial 字幕只走 WebSocket，不入库。"""

    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    streamer_id: Mapped[int | None] = mapped_column(
        ForeignKey("streamers.id"), nullable=True, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    start_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_ms: Mapped[int] = mapped_column(Integer, default=0)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    corrected_text: Mapped[str] = mapped_column(Text, default="")

    session: Mapped[ReplaySession] = relationship(back_populates="transcripts")


class OcrRecord(Base):
    """一帧画面文字（去重稳定化后）。图片本身用完即弃，绝不入库。"""

    __tablename__ = "ocr_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    streamer_id: Mapped[int | None] = mapped_column(
        ForeignKey("streamers.id"), nullable=True, index=True
    )
    start_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_ms: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")

    session: Mapped[ReplaySession] = relationship(back_populates="ocr_records")


class Critique(Base):
    """四类点评统一表，用 level 区分；按 streamer_id 归属到主播。"""

    __tablename__ = "critiques"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    streamer_id: Mapped[int | None] = mapped_column(
        ForeignKey("streamers.id"), nullable=True, index=True
    )
    level: Mapped[CritiqueLevel] = mapped_column(Enum(CritiqueLevel))
    severity: Mapped[Severity] = mapped_column(Enum(Severity), default=Severity.info)
    category: Mapped[str] = mapped_column(String(64), default="")
    start_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_ms: Mapped[int] = mapped_column(Integer, default=0)
    quote: Mapped[str] = mapped_column(Text, default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    data: Mapped[str] = mapped_column(Text, default="")  # 结构化点评(JSON)：维度/Top问题/行动项/issues
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    session: Mapped[ReplaySession] = relationship(back_populates="critiques")


class KnowledgeItem(Base):
    """知识库条目：产品规格 / 话术规范 / 违禁词。统一表，用 kind 区分。"""

    __tablename__ = "knowledge_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # product | script | forbidden
    name: Mapped[str] = mapped_column(String(128), default="")
    aliases: Mapped[str] = mapped_column(Text, default="")
    attrs: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class KnowledgeChunk(Base):
    """知识库文档切片（供 RAG 检索）：文件/文本录入后切片 + 向量。"""

    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(255), default="")  # 文件名/来源
    kind: Mapped[str] = mapped_column(String(16), default="doc")  # doc 原文 | distilled 提炼
    category: Mapped[str] = mapped_column(String(64), default="")  # 提炼分类：规格/话术/问答/品牌
    text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[str] = mapped_column(Text, default="")      # JSON 向量，空=未建索引
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
