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

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class SessionStatus(str, enum.Enum):
    created = "created"
    parsing = "parsing"
    live = "live"
    processing = "processing"
    done = "done"
    error = "error"


class SessionType(str, enum.Enum):
    normal = "normal"
    observation = "observation"


class AudioStatus(str, enum.Enum):
    recording = "recording"
    available = "available"
    error = "error"
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
    # 软删：置删除时间而非真删行，历史场次的 streamer_id 仍能查到主播名（与前端"保留归属"提示一致）
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voiceprint: Mapped[str] = mapped_column(Text, default="")   # 声纹 embedding（JSON 192维，空=未注册）
    # 业绩统计系统的主播终身 ID（streamers.id，s-<uuid>）；未绑定=NULL 不写空串，
    # 唯一性由 db.init_db 的部分唯一索引保证（NULL 可重复）
    ext_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class StreamerAccount(Base):
    """主播在主系统内的登录凭据；与花名册严格一对一。"""

    __tablename__ = "streamer_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    streamer_id: Mapped[int] = mapped_column(
        ForeignKey("streamers.id", ondelete="CASCADE"), unique=True, index=True
    )
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class LiveAlert(Base):
    """R7 主播提醒。持久化保证主播重连后不漏消息。"""

    __tablename__ = "live_alerts"
    __table_args__ = (
        Index(
            "uq_live_alerts_event_key",
            "event_key",
            unique=True,
            sqlite_where=text("event_key <> ''"),
            postgresql_where=text("event_key <> ''"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warn")
    title: Mapped[str] = mapped_column(String(160), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    quote: Mapped[str] = mapped_column(Text, default="")
    suggestion: Mapped[str] = mapped_column(Text, default="")
    at_ms: Mapped[int] = mapped_column(Integer, default=0)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    auto_record: Mapped[bool] = mapped_column(default=False)          # 开播自动录制（无人值守）
    auto_streamer_ids: Mapped[str] = mapped_column(Text, default="")  # JSON 默认排班顺序
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # 账号级恒定 ID（sec_uid）：share_link/房间号(web_rid)每场直播都可能变（07-07 白天两个班
    # 因旧房间号 8 小时探测不到开播全部漏录），sec_uid 永不变——有值时优先按账号定位当前直播。
    # 由解析成功时自动学习回填（douyin.StreamResolution.sec_uid），无需手工配置。
    sec_user_id: Mapped[str] = mapped_column(String(120), default="")


class SystemSetting(Base):
    """运行时可调配置（键值），覆盖 .env 默认；前端设置面板读写。"""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class ReplaySession(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index(
            "uq_sessions_active_live_link",
            "normalized_share_link",
            unique=True,
            sqlite_where=text(
                "normalized_share_link <> '' AND ended_at IS NULL "
                "AND status IN ('created', 'parsing', 'live', 'processing')"
            ),
            postgresql_where=text(
                "normalized_share_link <> '' AND ended_at IS NULL "
                "AND status IN ('created', 'parsing', 'live', 'processing')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(160), default="")
    share_link: Mapped[str] = mapped_column(Text, default="")
    normalized_share_link: Mapped[str] = mapped_column(String(1024), default="")
    room_id: Mapped[str] = mapped_column(String(64), default="")
    stream_url: Mapped[str] = mapped_column(Text, default="")
    lineup: Mapped[str] = mapped_column(Text, default="")  # JSON: [streamer_id,...] 排班顺序
    current_streamer_id: Mapped[int | None] = mapped_column(
        ForeignKey("streamers.id"), nullable=True
    )
    session_type: Mapped[SessionType] = mapped_column(
        Enum(SessionType), nullable=False, default=SessionType.normal, index=True,
    )
    # 建场时选择的追加禁止/豁免规则；开播时固化进统一快照，之后不再读取这里。
    compliance_rule_ids: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus), default=SessionStatus.created
    )
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_msg: Mapped[str] = mapped_column(Text, default="")
    # 采集状态和报告状态分开，避免后台分析未完成时把场次误报为完整 done。
    report_status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    deep_status: Mapped[str] = mapped_column(String(24), default="not_requested", index=True)
    compass_token: Mapped[str] = mapped_column(String(48), default="")  # 罗盘书签上报鉴权
    # 弹幕采集情况（2026-10-03）：pending 抓取中还没采到 / ok 采到了（挂上过弹幕列表）/
    # missing 整场都没采到 / 空 = 没开抓取或老场次。没采到时整场总结不评价公屏互动。
    danmaku_status: Mapped[str] = mapped_column(String(16), default="")
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
    rhythm_analyses: Mapped[list["SessionRhythmAnalysis"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    # 主播直达页阅读记录随场次一并删除（SQLite 未开外键约束，且删掉最大 id 后会复用该 id，
    # 不级联的话旧记录会串到新场次上）
    view_activities: Mapped[list["ViewActivity"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    # 改进动作的核对记录/处理记录同理随场次删除（防 id 复用后串到新场次）；
    # 动作本身不删，只把「从哪一场定的」置空
    focus_checks: Mapped[list["FocusCheck"]] = relationship(cascade="all, delete-orphan")
    focus_runs: Mapped[list["FocusRun"]] = relationship(cascade="all, delete-orphan")
    focus_origin_actions: Mapped[list["FocusAction"]] = relationship()


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
    # 单次调用的真实执行结果；不能用系统当前配置推断历史调用是否发生过降级。
    llm_provider: Mapped[str] = mapped_column(String(64), default="")
    llm_requested_provider: Mapped[str] = mapped_column(String(64), default="")
    is_fallback: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    session: Mapped[ReplaySession] = relationship(back_populates="critiques")


class LLMUsage(Base):
    """One provider request, including token usage when the provider returns it."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stage: Mapped[str] = mapped_column(String(64), default="unknown", index=True)
    provider: Mapped[str] = mapped_column(String(64), default="")
    requested_provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(160), default="")
    request_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(default=True)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


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
    __table_args__ = (
        UniqueConstraint("origin_type", "origin_id", name="uq_knowledge_chunks_origin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(255), default="")  # 文件名/来源
    kind: Mapped[str] = mapped_column(String(16), default="doc")  # doc 原文 | distilled 提炼
    category: Mapped[str] = mapped_column(String(64), default="")  # 提炼分类：规格/话术/问答/品牌
    scene: Mapped[str] = mapped_column(String(24), default="", index=True)  # 话术场景：开场/讲款/逼单…
    # 结构化资产来源。空=普通知识；rhythm_template/experience_tip 用于审核门禁与安全关联。
    origin_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    origin_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[str] = mapped_column(Text, default="")      # JSON 向量，空=未建索引
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ExperienceCorpus(Base):
    """外部直播技巧/运营经验原始语料。

    原文与提炼结果分开持久化：原文用于审核和重新提炼，RhythmTemplate 用于
    结构化展示/后续节奏对比。只接收已经转成文字的资料，不承担音视频转写。
    """

    __tablename__ = "experience_corpora"
    __table_args__ = (
        UniqueConstraint("corpus_type", "content_hash", name="uq_experience_corpus_content"),
        Index(
            "uq_experience_corpora_origin",
            "origin_type", "origin_id", unique=True,
            sqlite_where=text("origin_type <> '' AND origin_id IS NOT NULL"),
            postgresql_where=text("origin_type <> '' AND origin_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(255), default="")
    # live_skill 直播技巧 | operations 运营经验
    corpus_type: Mapped[str] = mapped_column(String(24), index=True, default="live_skill")
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    # processing 提炼中 | ready 已产出候选 | failed 提炼失败
    status: Mapped[str] = mapped_column(String(16), index=True, default="processing")
    template_count: Mapped[int] = mapped_column(Integer, default=0)
    llm_provider: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="rhythm-v1")
    error_msg: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 每轮提炼的租约标识和心跳；旧任务只能提交与自身 run_id 匹配的结果。
    processing_run_id: Mapped[str] = mapped_column(String(64), default="")
    processing_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # observation_session 等结构化来源；普通手工/文档语料保持空。
    origin_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    origin_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_metadata: Mapped[str] = mapped_column(Text, default="{}")


class RhythmTemplate(Base):
    """由外部经验语料提炼出的可复用话术节奏模板。

    steps 保存确定性 JSON，颗粒度固定为“时间段/内容/时长/条件动作”，供 R3
    做逐段对比。候选需人工采纳后才写入 KnowledgeChunk，避免未经审核的模型
    产物污染正式检索与推荐。
    """

    __tablename__ = "rhythm_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    corpus_id: Mapped[int] = mapped_column(
        ForeignKey("experience_corpora.id", ondelete="CASCADE"), index=True)
    corpus_type: Mapped[str] = mapped_column(String(24), index=True, default="live_skill")
    title: Mapped[str] = mapped_column(String(160), default="")
    scene: Mapped[str] = mapped_column(String(24), index=True, default="general")
    summary: Mapped[str] = mapped_column(Text, default="")
    applicable_when: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(String(160), default="")
    goal: Mapped[str] = mapped_column(Text, default="")
    total_duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    steps: Mapped[str] = mapped_column(Text, default="[]")
    tags: Mapped[str] = mapped_column(Text, default="[]")
    source_excerpt: Mapped[str] = mapped_column(Text, default="")
    # pending 待审核 | adopted 已进入优秀节奏库/RAG | rejected 已驳回
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    chunk_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_chunks.id", ondelete="SET NULL"), unique=True, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    analyses: Mapped[list["SessionRhythmAnalysis"]] = relationship(
        back_populates="template"
    )


class SessionRhythmAnalysis(Base):
    """一场直播中某位主播与一个优秀节奏模板的提取、对比和验证结果。

    模板的审核状态（RhythmTemplate.status）与实战效果必须分开：模板被采纳只代表
    可以参与对比；只有 declared_execution=true 且罗盘证据充分时，effective 才会
    被写成真/假。template_snapshot 保证来源模板后来被删除时历史报告仍可追溯。
    """

    __tablename__ = "session_rhythm_analyses"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "streamer_id", name="uq_session_rhythm_analysis_owner",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True,
    )
    streamer_id: Mapped[int] = mapped_column(
        ForeignKey("streamers.id"), index=True,
    )
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("rhythm_templates.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # auto 自动推荐 | manual 运营手选
    selection_mode: Mapped[str] = mapped_column(String(16), default="auto")
    # 只有明确声明本场按该模板执行，才允许把罗盘表现记为模板的实战验证。
    declared_execution: Mapped[bool] = mapped_column(default=False)
    # 执行计划必须在直播执行前登记；时间戳和相对开播毫秒用于排除事后挑模板归因。
    declared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    declaration_start_ms: Mapped[int] = mapped_column(Integer, default=0)
    # planned 待执行/待分析 | analyzing | ready | failed
    status: Mapped[str] = mapped_column(String(16), default="planned", index=True)
    # 每次分析/计划的租约。LLM 慢请求只能提交自己 claim 的 run，防止旧结果覆盖重选模板。
    analysis_run_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    template_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    extracted: Mapped[str] = mapped_column(Text, default="{}")
    comparison: Mapped[str] = mapped_column(Text, default="{}")
    validation: Mapped[str] = mapped_column(Text, default="{}")
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    effect_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    effective: Mapped[bool | None] = mapped_column(nullable=True)
    error_msg: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    session: Mapped[ReplaySession] = relationship(back_populates="rhythm_analyses")
    template: Mapped[RhythmTemplate | None] = relationship(back_populates="analyses")


class ScriptFrequencyRule(Base):
    """Versioned required-script execution rule managed before a live session."""

    __tablename__ = "script_frequency_rules"
    __table_args__ = (
        CheckConstraint(
            "repeat_interval_sec > 0", name="ck_script_rule_repeat_interval_positive",
        ),
        CheckConstraint(
            "target_duration_sec > 0", name="ck_script_rule_target_duration_positive",
        ),
        CheckConstraint(
            "interval_tolerance_sec >= 0", name="ck_script_rule_interval_tolerance_nonnegative",
        ),
        CheckConstraint(
            "duration_tolerance_sec >= 0", name="ck_script_rule_duration_tolerance_nonnegative",
        ),
        CheckConstraint(
            "similarity_threshold >= 0 AND similarity_threshold <= 1",
            name="ck_script_rule_similarity_range",
        ),
        CheckConstraint("version > 0", name="ck_script_rule_version_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    standard_script: Mapped[str] = mapped_column(Text)
    repeat_interval_sec: Mapped[int] = mapped_column(Integer)
    target_duration_sec: Mapped[int] = mapped_column(Integer)
    interval_tolerance_sec: Mapped[int] = mapped_column(Integer, default=0)
    duration_tolerance_sec: Mapped[int] = mapped_column(Integer, default=0)
    similarity_threshold: Mapped[float] = mapped_column(Float)
    # JSON object. Examples: {"type":"all"} or
    # {"type":"streamers","streamer_ids":[1,2]}.
    effective_scope: Mapped[str] = mapped_column(Text, default='{"type":"all"}')
    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SessionScriptCompliancePlan(Base):
    """Immutable rule snapshot assigned to one streamer in one session."""

    __tablename__ = "session_script_compliance_plans"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "streamer_id", "rule_id",
            name="uq_session_script_plan_assignment",
        ),
        CheckConstraint("rule_version > 0", name="ck_session_script_plan_version_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True,
    )
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"), index=True)
    # RESTRICT plus database triggers protects every rule used by a historical session.
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("script_frequency_rules.id", ondelete="RESTRICT"), index=True,
    )
    rule_version: Mapped[int] = mapped_column(Integer)
    rule_snapshot: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ComplianceRule(Base):
    """统一禁止/豁免规则；历史场次永远读取场次快照。"""

    __tablename__ = "compliance_rules"
    __table_args__ = (
        CheckConstraint("action IN ('allow', 'forbid')", name="ck_compliance_rule_action"),
        CheckConstraint(
            "scope_type IN ('all', 'session', 'streamer', 'room', 'product')",
            name="ck_compliance_rule_scope_type",
        ),
        CheckConstraint("LENGTH(TRIM(keyword)) > 0", name="ck_compliance_rule_keyword"),
        CheckConstraint("version > 0", name="ck_compliance_rule_version"),
        UniqueConstraint(
            "action", "normalized_keyword", "scope_type", "scope_id",
            name="uq_compliance_rule_scope_keyword",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(16), index=True)
    keyword: Mapped[str] = mapped_column(String(500))
    normalized_keyword: Mapped[str] = mapped_column(String(500))
    scope_type: Mapped[str] = mapped_column(String(16), default="all", index=True)
    scope_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    source_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class SessionComplianceSnapshot(Base):
    """开播时一次性锁定的规定话术+禁止+豁免统一快照头。"""

    __tablename__ = "session_compliance_snapshots"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_session_compliance_snapshot"),
        CheckConstraint("version > 0", name="ck_session_compliance_snapshot_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    required_count: Mapped[int] = mapped_column(Integer, default=0)
    forbid_count: Mapped[int] = mapped_column(Integer, default=0)
    allow_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    locked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SessionComplianceRuleSnapshot(Base):
    """单条禁止/豁免规则的不可变场次快照，同时构成规则使用引用。"""

    __tablename__ = "session_compliance_rule_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "rule_id", name="uq_snapshot_compliance_rule"),
        CheckConstraint("action IN ('allow', 'forbid')", name="ck_snapshot_rule_action"),
        CheckConstraint("rule_version > 0", name="ck_snapshot_rule_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("session_compliance_snapshots.id", ondelete="CASCADE"), index=True,
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True,
    )
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("compliance_rules.id", ondelete="RESTRICT"), index=True,
    )
    rule_version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(16), index=True)
    keyword: Mapped[str] = mapped_column(String(500))
    scope_type: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[str] = mapped_column(String(128), default="")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    rule_snapshot: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ComplianceDecision(Base):
    """实时逐句合规判断审计；只存安全摘要，不存完整敏感原话。"""

    __tablename__ = "compliance_decisions"
    __table_args__ = (
        UniqueConstraint(
            "transcript_id", "rule_id", "keyword", "result",
            name="uq_compliance_decision_transcript_rule",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True,
    )
    transcript_id: Mapped[int | None] = mapped_column(
        ForeignKey("transcripts.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    keyword: Mapped[str] = mapped_column(String(500))
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("compliance_rules.id", ondelete="RESTRICT"), index=True,
    )
    rule_version: Mapped[int] = mapped_column(Integer)
    rule_scope: Mapped[str] = mapped_column(String(160))
    exempted: Mapped[bool] = mapped_column(default=False)
    result: Mapped[str] = mapped_column(String(24), index=True)
    at_ms: Mapped[int] = mapped_column(Integer, default=0)
    safe_summary: Mapped[str] = mapped_column(String(160), default="")
    alert_type: Mapped[str] = mapped_column(String(32), default="compliance")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ScriptComplianceResult(Base):
    """Idempotent whole-session compliance analysis for one planned rule snapshot."""

    __tablename__ = "script_compliance_results"
    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_script_compliance_result_plan"),
        UniqueConstraint(
            "session_id", "streamer_id", "rule_id", "rule_version",
            name="uq_script_compliance_result_version",
        ),
        CheckConstraint("expected_count >= 0", name="ck_script_result_expected_nonnegative"),
        CheckConstraint("actual_count >= 0", name="ck_script_result_actual_nonnegative"),
        CheckConstraint("matched_count >= 0", name="ck_script_result_matched_nonnegative"),
        CheckConstraint("missing_count >= 0", name="ck_script_result_missing_nonnegative"),
        CheckConstraint(
            "compliance_rate >= 0 AND compliance_rate <= 1",
            name="ck_script_result_compliance_rate",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("session_script_compliance_plans.id", ondelete="CASCADE"), index=True,
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True,
    )
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"), index=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("script_frequency_rules.id", ondelete="RESTRICT"), index=True,
    )
    rule_version: Mapped[int] = mapped_column(Integer)
    # analyzing | ready | failed. Public compliance verdict is stored in overall_status.
    status: Mapped[str] = mapped_column(String(20), default="analyzing", index=True)
    overall_status: Mapped[str] = mapped_column(String(24), default="not_analyzable")
    analysis_run_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    expected_count: Mapped[int] = mapped_column(Integer, default=0)
    actual_count: Mapped[int] = mapped_column(Integer, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    compliance_rate: Mapped[float] = mapped_column(Float, default=0.0)
    first_occurrence_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    average_interval_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_interval_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_interval_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="{}")
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_msg: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ScriptComplianceOccurrence(Base):
    """One detected execution or one missing expected execution."""

    __tablename__ = "script_compliance_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "result_id", "ordinal", name="uq_script_compliance_occurrence_ordinal",
        ),
        CheckConstraint("ordinal > 0", name="ck_script_occurrence_ordinal_positive"),
        CheckConstraint(
            "similarity_percent IS NULL OR "
            "(similarity_percent >= 0 AND similarity_percent <= 100)",
            name="ck_script_occurrence_similarity_percent",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    result_id: Mapped[int] = mapped_column(
        ForeignKey("script_compliance_results.id", ondelete="CASCADE"), index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), index=True)
    issues: Mapped[str] = mapped_column(Text, default="[]")
    actual_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    interval_from_previous_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    similarity_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    transcript_ids: Mapped[str] = mapped_column(Text, default="[]")
    raw_transcript: Mapped[str] = mapped_column(Text, default="")
    similarity_passed: Mapped[bool] = mapped_column(default=False)
    duration_passed: Mapped[bool] = mapped_column(default=False)
    frequency_passed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SignalEvent(Base):
    """业绩系统打卡信号的幂等与审计记录。

    event_id 唯一（重复投递直接重放首次结果，跨重启有效）；
    同时是业绩打卡班次 ↔ 复盘场次的 n:m 关联审计（一场录制跨多个打卡班次）。
    """

    __tablename__ = "signal_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)  # 幂等键；旧调用方不传=NULL（可重复）
    perf_session_id: Mapped[str] = mapped_column(String(64), default="", index=True)  # 业绩 live_sessions.id
    ext_id: Mapped[str] = mapped_column(String(64), default="")          # 业绩主播终身 ID
    streamer_name: Mapped[str] = mapped_column(String(128), default="")  # 信号里的花名
    username: Mapped[str] = mapped_column(String(128), default="")       # 业绩登录名
    action: Mapped[str] = mapped_column(String(8), default="on")         # on | off
    room_id: Mapped[str] = mapped_column(String(64), default="")
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 打卡真实时刻（本地 naive）
    # applied=已执行 / deferred=等开播 / ignored=无需动作 / stale=乱序拒绝 / failed=匹配失败 / expired=意向过期
    status: Mapped[str] = mapped_column(String(16), default="", index=True)
    replay_session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    streamer_id: Mapped[int | None] = mapped_column(ForeignKey("streamers.id"), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="")           # 首次响应快照（幂等重放原样返回）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DanmakuRefine(Base):
    """公屏购买意向 AI 精析结果（每场一条，覆盖式）。data: JSON 数组，
    每条 {at, text, verdict, reason, context}；verdict ∈ 真实意向/疑似玩笑/咨询了解。"""

    __tablename__ = "danmaku_refines"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), unique=True, index=True)
    data: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ObservationAnalysis(Base):
    """观摩学习离线分析状态；一场一条，重试覆盖状态但不制造重复模板。"""

    __tablename__ = "observation_analyses"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_observation_analysis_session"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), unique=True, index=True,
    )
    corpus_id: Mapped[int | None] = mapped_column(
        ForeignKey("experience_corpora.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # pending | processing | ready | no_data | failed
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    run_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_msg: Mapped[str] = mapped_column(Text, default="")
    include_emotion: Mapped[bool] = mapped_column(default=False)
    emotion_generated: Mapped[bool] = mapped_column(default=False)
    emotion_status: Mapped[str] = mapped_column(String(24), default="disabled")
    emotion_data: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ObservationHeatSlice(Base):
    """合并后的观摩弹幕高互动片段；窗口算法可重复执行并覆盖同一分析的切片。"""

    __tablename__ = "observation_heat_slices"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id", "start_ms", "end_ms", name="uq_observation_heat_slice_range",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("observation_analyses.id", ondelete="CASCADE"), index=True,
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True,
    )
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    danmaku_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_user_count: Mapped[int] = mapped_column(Integer, default=0)
    relative_growth: Mapped[float] = mapped_column(Float, default=0.0)
    heat_score: Mapped[float] = mapped_column(Float, default=0.0)
    transcript_ids: Mapped[str] = mapped_column(Text, default="[]")
    corresponding_script: Mapped[str] = mapped_column(Text, default="")
    audio_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class PatternLedger(Base):
    """模式账本（2026-07-10 整改·标准自生长）：同场景相似话术片段聚成"模式"，
    每次出现按罗盘数据反应记账（正向/负向）。样本够且大部分有效 → 晋升「验证有效」
    并由 LLM 总结成标准卡；大部分负向 → 沉淀为反面模式。标准从数据长出来，不靠人喂。"""

    __tablename__ = "pattern_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    scene: Mapped[str] = mapped_column(String(24), index=True, default="")   # 场景 key(product/urge…)
    centroid: Mapped[str] = mapped_column(Text, default="")                  # 中心向量 JSON(移动平均)
    rep_text: Mapped[str] = mapped_column(Text, default="")                  # 代表话术摘录
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    positive_count: Mapped[int] = mapped_column(Integer, default=0)
    # observing 观察中 | validated 验证有效 | negative 反面模式 | retired 淘汰
    status: Mapped[str] = mapped_column(String(16), index=True, default="observing")
    evidence: Mapped[str] = mapped_column(Text, default="[]")                # 验证账本 JSON
    standard_card: Mapped[str] = mapped_column(Text, default="")             # 晋升后 LLM 标准卡 JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class CompassPoint(Base):
    """抖音罗盘实时数据点（书签工具上报）：在线/成交/GMV 等，供实时看板与成交归因。"""

    __tablename__ = "compass_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    start_ms: Mapped[int] = mapped_column(Integer, default=0)   # 相对开播毫秒（用于对齐话术段）
    data: Mapped[str] = mapped_column(Text, default="")         # JSON: 抓到的罗盘指标
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ExperienceTip(Base):
    """成功经验技巧类别：同类罗盘变化前后的相似话术/动作聚类。

    闭环：持续采样 → 相似动作归类 → 频次/占比达标后 status=pending → 人工采纳
    → adopted 后 embed 入 KnowledgeChunk。observing 阶段不得入库，避免单次随机波动
    污染知识库。session_id 仅保留首个代表场次；完整证据在 ExperienceTipSample。
    """

    __tablename__ = "experience_tips"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    streamer_id: Mapped[int | None] = mapped_column(ForeignKey("streamers.id"), nullable=True)
    scene: Mapped[str] = mapped_column(String(24), default="", index=True)   # 话术场景 key
    metric: Mapped[str] = mapped_column(String(32), default="")   # 触发的罗盘指标（在线/转化率/成交…）
    effect: Mapped[str] = mapped_column(String(80), default="")   # 效果描述（在线+150、转化+3%）
    action: Mapped[str] = mapped_column(Text, default="")         # 动作概括
    quote: Mapped[str] = mapped_column(Text, default="")          # 原话摘录
    template: Mapped[str] = mapped_column(Text, default="")       # 可复用模板话术
    at_ms: Mapped[int] = mapped_column(Integer, default=0)        # 变化点时刻（相对开播 ms）
    event_key: Mapped[str] = mapped_column(String(64), default="", index=True)  # online:up / sale:up…
    centroid: Mapped[str] = mapped_column(Text, default="")       # 动作类别中心向量 JSON
    matched_count: Mapped[int] = mapped_column(Integer, default=0)  # 该动作类别命中次数（分子）
    change_sample_count: Mapped[int] = mapped_column(Integer, default=0)  # 同类变化总次数（分母）
    matched_rate: Mapped[float] = mapped_column(Float, default=0.0)  # matched/change
    # observing 统计中 | pending 达标待采纳 | adopted 已入库 | rejected 已驳回
    status: Mapped[str] = mapped_column(String(16), default="observing", index=True)
    chunk_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 采纳后对应 KnowledgeChunk.id
    # 审核操作的乐观锁版本；防止 embed 期间发生的驳回被旧采纳结果覆盖。
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExperienceTipSample(Base):
    """一次罗盘数据变化样本；所有变化都计入分母，可复用动作才关联技巧类别。

    source_key 唯一（正常样本由 session_id/统计类别与方向/时刻组成），保证同一场
    复盘或历史回溯重复执行时不重复记账。tip_id 为空表示自然波动、无法归因或
    尚无可复用动作。
    """

    __tablename__ = "experience_tip_samples"
    __table_args__ = (
        UniqueConstraint(
            "source_key", name="uq_experience_tip_sample_source",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(160))
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    streamer_id: Mapped[int | None] = mapped_column(
        ForeignKey("streamers.id", ondelete="SET NULL"), nullable=True)
    tip_id: Mapped[int | None] = mapped_column(
        ForeignKey("experience_tips.id", ondelete="SET NULL"), nullable=True, index=True)
    scene: Mapped[str] = mapped_column(String(24), default="")
    event_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    metric: Mapped[str] = mapped_column(String(32), default="")
    direction: Mapped[str] = mapped_column(String(8), default="up")
    verdict: Mapped[str] = mapped_column(String(16), default="")
    effect: Mapped[str] = mapped_column(String(80), default="")
    action: Mapped[str] = mapped_column(Text, default="")
    quote: Mapped[str] = mapped_column(Text, default="")
    template: Mapped[str] = mapped_column(Text, default="")
    at_ms: Mapped[int] = mapped_column(Integer, default=0)
    reusable: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ViewActivity(Base):
    """主播在签名直达页（业绩后台「查看完整复盘」）上的阅读行为，每（场次, 主播）一行。

    页面上报走 POST /api/partner/view/{sid}/beacon（签名校验同直达页，链接只发给主播本人，
    行为一律记在签名里的 streamer_id 名下），首次上报建行，之后按防刷上限累加；
    业绩系统经 GET /api/partner/replays/{sid}/view-activity 拉取。
    seen_keys：看过的问题 key（"{level}|{start_ms}|{category}"，与业绩系统同口径）JSON 数组，去重保序。
    表由启动时 init_db 的 create_all 自动建（与近期新增表同惯例，不走 alembic）。
    """

    __tablename__ = "view_activity"
    __table_args__ = (
        UniqueConstraint("session_id", "streamer_id", name="uq_view_activity_session_streamer"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"))
    active_seconds: Mapped[float] = mapped_column(Float, default=0.0)   # 有效阅读秒数（前台且有操作/在播）
    video_seconds: Mapped[float] = mapped_column(Float, default=0.0)    # 录像实际播放秒数
    clips_played: Mapped[int] = mapped_column(Integer, default=0)       # 点问题/逐字稿跳转播放次数
    seen_keys: Mapped[str] = mapped_column(Text, default="[]")          # 看过的问题 key（JSON 数组）
    opens: Mapped[int] = mapped_column(Integer, default=0)              # 打开次数
    first_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)  # 首次上报
    last_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)   # 最近一次有实际行为的上报
    last_beat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 最近一次上报（防刷计时）

    session: Mapped[ReplaySession] = relationship(back_populates="view_activities")


# ========== 改进动作闭环（第一期，2026-10）==========
# 每场下播出完整场总结后自动核对上一批动作、记账，再把本场能检查的问题都变成动作（app/core/review/focus.py）。
# 三张表都由启动时 init_db 的 create_all 自动建（与 ViewActivity 同惯例，不走 alembic）。
# 时间一律本地 naive（服务器 TZ=Asia/Shanghai），接口输出时补 +08:00。


class FocusAction(Base):
    """一条改进动作：主播能做到、能从逐字稿判断做没做的具体行为。条数不设上限，按 sort_order 排重要程度。

    status：active 在练 / passed 过关（确认后连续 3 场做到）/ replaced 被换掉 / removed 被删掉。
    kind：llm 由快速模型对照逐字稿判；silence 纯算法看相邻两句的空白（threshold_sec 秒起算）。
    acked_at 为空或晚于某场班次开始 → 那场的核对只算基线，不动 streak。
    reopened_from：过关后又犯、重新加的那条，指向原来过关的动作。
    category：动作的类别；同一类只留一条在练的（「其他」类除外），一个问题的多个要点合成一条。
    """

    __tablename__ = "focus_actions"
    __table_args__ = (
        Index("ix_focus_actions_streamer_status", "streamer_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"), index=True)
    text: Mapped[str] = mapped_column(String(64))                 # 动作本身（≤30 字）
    trigger: Mapped[str] = mapped_column(Text, default="")        # 什么时候要做
    criterion: Mapped[str] = mapped_column(Text, default="")      # 怎样算做到
    example: Mapped[str] = mapped_column(Text, default="")        # 一句好例句
    example_source: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(16), default="llm")  # llm | silence
    threshold_sec: Mapped[int] = mapped_column(Integer, default=10)   # silence 用
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    streak: Mapped[int] = mapped_column(Integer, default=0)       # 确认后连续做到的场数
    created_by: Mapped[str] = mapped_column(String(64), default="ai")  # ai 或操作人名字
    origin_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acked_by: Mapped[str] = mapped_column(String(64), default="")
    passed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 不再在练的时间
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 最近一次人工改动
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    reopened_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 类别（第二版规则，见 focus_store.CATEGORIES）：同一位主播同一类只留一条在练的；空 = 第一版生成的老动作
    category: Mapped[str] = mapped_column(String(32), default="")


class FocusCheck(Base):
    """某场对某条动作的核对结果；(动作, 场次) 唯一，重跑只覆盖不重复。

    verdict：pass / fail / na（本场没机会做，或核对失败，原因写 reason）。
    silence 类 done/total 为空，看 metric（最长空场、次数）。
    """

    __tablename__ = "focus_checks"
    __table_args__ = (
        UniqueConstraint("action_id", "session_id", name="uq_focus_check_action_session"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    action_id: Mapped[int] = mapped_column(
        ForeignKey("focus_actions.id", ondelete="CASCADE"), index=True,
    )
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"), index=True)
    verdict: Mapped[str] = mapped_column(String(8), default="na")
    baseline: Mapped[bool] = mapped_column(default=True)
    done: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metric: Mapped[str] = mapped_column(Text, default="{}")
    evidence: Mapped[str] = mapped_column(Text, default="[]")     # ≤4 条 {at_ms, wall_time, ok, quote}
    reason: Mapped[str] = mapped_column(Text, default="")
    llm_provider: Mapped[str] = mapped_column(String(64), default="")
    # 这位主播本场班次开始时间：基线判断、连续场数按它排先后
    turn_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class FocusRun(Base):
    """每个（场次, 主播）处理过没有：done / partial / skipped / failed，一行一份。"""

    __tablename__ = "focus_runs"
    __table_args__ = (
        UniqueConstraint("session_id", "streamer_id", name="uq_focus_run_session_streamer"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="done", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    # 值得做、但因和在练的重复或没法从逐字稿判断而没写成动作的几条，供人工参考
    candidates: Mapped[str] = mapped_column(Text, default="[]")
    new_count: Mapped[int] = mapped_column(Integer, default=0)    # 这场新加了几条动作
    note: Mapped[str] = mapped_column(Text, default="")           # 备注（比如新加超过 8 条）
    turn_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
