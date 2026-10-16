"""API 请求/响应模型。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator


# ---------- 主播 ----------
class StreamerIn(BaseModel):
    name: str
    note: str = ""
    color: str = ""

    @field_validator("color")
    @classmethod
    def _valid_color(cls, v: str) -> str:
        # 只允许 #RRGGBB 或空，防止任意字符串拼进前端 innerHTML 的 style 造成注入
        if v and not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError("color 必须是 #RRGGBB 格式或留空")
        return v


class StreamerOut(StreamerIn):
    id: int

    class Config:
        from_attributes = True


# ---------- 会话 ----------
class SessionCreate(BaseModel):
    title: str = ""
    share_link: str = ""
    manual_stream_url: str = ""
    streamer_ids: list[int] = []  # 排班顺序（首位为开播主播）
    allow_unassigned: bool = False
    session_type: Literal["normal", "observation"] = "normal"
    compliance_rule_ids: list[int] = []


class SwitchIn(BaseModel):
    streamer_id: int | None = None  # 指定切到某主播
    next: bool = False              # 或按排班切到下一位


class SummaryRegenerateIn(BaseModel):
    streamer_id: int | None = None


class AudioOut(BaseModel):
    id: int
    filename: str
    size_bytes: int
    status: str

    class Config:
        from_attributes = True


class SessionOut(BaseModel):
    id: int
    title: str
    share_link: str
    room_id: str
    stream_url: str
    status: str
    report_status: str = "pending"
    deep_status: str = "not_requested"
    error_code: str = ""
    error_msg: str
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    audio: AudioOut | None = None
    current_streamer_id: int | None = None
    current_streamer_name: str = ""
    lineup: list[StreamerOut] = []
    session_type: Literal["normal", "observation"] = "normal"
    compliance_snapshot: dict | None = None


class TranscriptOut(BaseModel):
    id: int
    seq: int
    start_ms: int
    end_ms: int
    raw_text: str
    corrected_text: str
    streamer_id: int | None = None

    class Config:
        from_attributes = True


class CritiqueOut(BaseModel):
    id: int
    level: str
    severity: str
    category: str
    start_ms: int
    end_ms: int
    quote: str
    comment: str
    score: float | None
    data: str = ""          # 结构化点评 JSON 字符串（维度/Top问题/行动项/issues）
    llm_provider: str = ""
    llm_requested_provider: str = ""
    is_fallback: bool = False
    streamer_id: int | None = None

    class Config:
        from_attributes = True


class StreamerReviewOut(BaseModel):
    """某主播在某场的复盘聚合。"""

    streamer_id: int
    name: str
    color: str = ""
    sentence_count: int = 0
    flag_count: int = 0
    turns: list[dict] = []          # [{start_ms,end_ms}]
    summary: CritiqueOut | None = None
    overall_score: float | None = None
    # 该主播的 v2 深度复盘正在生成时：{status:"running", started_at, elapsed_sec, only_failed}；否则为空
    v2_job: dict | None = None


# ---------- 知识库 ----------
class KnowledgeItemIn(BaseModel):
    kind: str  # product | script | forbidden
    name: str
    aliases: str = ""
    attrs: str = ""
    note: str = ""


class KnowledgeItemOut(KnowledgeItemIn):
    id: int

    class Config:
        from_attributes = True
