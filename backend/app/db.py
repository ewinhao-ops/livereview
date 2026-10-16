"""SQLAlchemy 引擎与会话工厂。MVP 默认 SQLite，量大改 DATABASE_URL 即可。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import BACKEND_ROOT, get_settings


class Base(DeclarativeBase):
    pass


def _engine_url() -> str:
    url = get_settings().database_url
    # 让相对路径的 sqlite 锚定到 backend 根目录，避免工作目录不同导致建多个库
    if url.startswith("sqlite:///./"):
        abs_path = (BACKEND_ROOT / url.removeprefix("sqlite:///./")).resolve()
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{abs_path}"
    return url


_url = _engine_url()
_connect_args = {"check_same_thread": False} if "sqlite" in _url else {}
engine = create_engine(_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


if "sqlite" in _url:
    # 并发写：WAL 允许读写并行 + busy_timeout 等锁，显著减少 "database is locked"
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


# 轻量加列迁移：Base.metadata.create_all 只建缺失的表，不会给旧表补新列
_MIGRATIONS: dict[str, dict[str, str]] = {
    "knowledge_chunks": {
        "kind": "VARCHAR(16) DEFAULT 'doc'",        # doc 原文 | distilled 提炼
        "category": "VARCHAR(64) DEFAULT ''",        # 提炼分类：规格/话术/问答/品牌
        "scene": "VARCHAR(24) DEFAULT ''",           # 话术场景：开场/讲款/逼单…
        "origin_type": "VARCHAR(32) DEFAULT ''",      # rhythm_template / experience_tip / 空=普通
        "origin_id": "INTEGER",                      # 对应结构化资产主键
    },
    "experience_tips": {
        "version": "INTEGER DEFAULT 0",              # 审核采纳/驳回的乐观锁版本
        "event_key": "VARCHAR(64) DEFAULT ''",       # 归一化的数据变化类别 + 方向
        "centroid": "TEXT DEFAULT ''",               # 相似动作聚类中心向量
        "matched_count": "INTEGER DEFAULT 0",        # 动作类别命中次数（统计分子）
        "change_sample_count": "INTEGER DEFAULT 0",  # 同类变化总次数（统计分母）
        "matched_rate": "FLOAT DEFAULT 0",           # matched/change
        "last_seen_at": "DATETIME",                  # 最近一次匹配证据
    },
    "experience_tip_samples": {
        "source_key": "VARCHAR(160) DEFAULT ''",     # 单次变化的稳定幂等键
        "verdict": "VARCHAR(16) DEFAULT ''",         # 动作驱动/自然波动/流失/分析失败
    },
    "experience_corpora": {
        "processing_run_id": "VARCHAR(64) DEFAULT ''",  # 提炼任务租约/防旧任务覆盖
        "processing_heartbeat_at": "DATETIME",          # 周期回收过期 processing
        "origin_type": "VARCHAR(32) DEFAULT ''",        # observation_session 等结构化来源
        "origin_id": "INTEGER",                         # 来源业务主键
        "source_metadata": "TEXT DEFAULT '{}'",         # 来源时间段/逐字稿/热度审计
    },
    "session_rhythm_analyses": {
        "declared_at": "DATETIME",                      # 前置执行计划登记时间
        "declaration_start_ms": "INTEGER DEFAULT 0",    # 登记时相对开播位置
        "analysis_run_id": "VARCHAR(64) DEFAULT ''",    # 分析租约/防旧 LLM 覆盖
    },
    "audio_files": {
        "cos_key": "TEXT DEFAULT ''",                # COS 对象 key，空=仅本地
        "media_kind": "VARCHAR(16) DEFAULT 'audio'",  # audio | video
    },
    "critiques": {
        "llm_provider": "VARCHAR(64) DEFAULT ''",     # 本次实际执行 provider
        "llm_requested_provider": "VARCHAR(64) DEFAULT ''",  # 本次请求 provider
        "is_fallback": "BOOLEAN DEFAULT FALSE",       # 本次调用是否发生降级
    },
    "sessions": {
        "compass_token": "VARCHAR(48) DEFAULT ''",   # 罗盘书签上报鉴权
        "error_code": "VARCHAR(64) DEFAULT ''",       # 采集/收尾的机器可读错误码
        "report_status": "VARCHAR(24) DEFAULT 'pending'",  # pending/processing/ready/partial/failed
        "deep_status": "VARCHAR(24) DEFAULT 'not_requested'",  # not_requested/queued/running/done/failed
        "normalized_share_link": "VARCHAR(1024) DEFAULT ''",  # 防重复建场幂等键
        "session_type": "VARCHAR(16) NOT NULL DEFAULT 'normal'",  # normal | observation
        "compliance_rule_ids": "TEXT DEFAULT '[]'",     # 开播前选择，开播时快照锁定
        "danmaku_status": "VARCHAR(16) DEFAULT ''",     # 弹幕采集情况：pending/ok/missing/空
    },
    "streamers": {
        "deleted_at": "DATETIME",                    # 软删标记（NULL=在册）
        "voiceprint": "TEXT DEFAULT ''",             # 声纹 embedding（历史遗漏：模型加列时未登记迁移）
        "ext_id": "VARCHAR(64)",                     # 业绩系统主播终身 ID（NULL=未绑定）
    },
    "live_rooms": {
        # 账号级恒定 ID：每场变的房间号(web_rid)导致漏录，解析成功时自动学习回填
        "sec_user_id": "VARCHAR(120) DEFAULT ''",
        "auto_record": "INTEGER DEFAULT 0",          # 开播自动录制开关
        "auto_streamer_ids": "TEXT DEFAULT ''",      # JSON 默认排班
        "deleted_at": "DATETIME",                    # 软删除，保证重复删除结果稳定
    },
    "live_alerts": {
        "event_key": "VARCHAR(64) DEFAULT ''",       # 违规事件幂等键
    },
    "focus_actions": {
        "category": "VARCHAR(32) DEFAULT ''",        # 改进动作类别（2026-10-03 第二版规则）
    },
}


def _install_script_compliance_guards(conn) -> None:  # noqa: ANN001
    """Install cross-table invariants that cannot be represented as CHECKs."""

    inspector = inspect(conn)
    required = {"script_frequency_rules", "session_script_compliance_plans"}
    if not all(inspector.has_table(table) for table in required):
        return
    if conn.dialect.name == "sqlite":
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS trg_script_plan_enabled_insert "
            "BEFORE INSERT ON session_script_compliance_plans "
            "WHEN COALESCE((SELECT enabled FROM script_frequency_rules "
            "WHERE id = NEW.rule_id), 0) <> 1 BEGIN "
            "SELECT RAISE(ABORT, 'script rule is disabled or missing'); END"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS trg_script_plan_enabled_update "
            "BEFORE UPDATE OF rule_id ON session_script_compliance_plans "
            "WHEN COALESCE((SELECT enabled FROM script_frequency_rules "
            "WHERE id = NEW.rule_id), 0) <> 1 BEGIN "
            "SELECT RAISE(ABORT, 'script rule is disabled or missing'); END"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS trg_script_rule_delete_used "
            "BEFORE DELETE ON script_frequency_rules "
            "WHEN EXISTS (SELECT 1 FROM session_script_compliance_plans "
            "WHERE rule_id = OLD.id) BEGIN "
            "SELECT RAISE(ABORT, 'script rule is used by a session'); END"
        ))
        return
    if conn.dialect.name == "postgresql":
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION enforce_script_plan_enabled()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM script_frequency_rules
                    WHERE id = NEW.rule_id AND enabled = TRUE
                ) THEN
                    RAISE EXCEPTION 'script rule is disabled or missing';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """))
        conn.execute(text(
            "DROP TRIGGER IF EXISTS trg_script_plan_enabled ON "
            "session_script_compliance_plans"
        ))
        conn.execute(text(
            "CREATE TRIGGER trg_script_plan_enabled BEFORE INSERT OR UPDATE OF rule_id "
            "ON session_script_compliance_plans FOR EACH ROW "
            "EXECUTE FUNCTION enforce_script_plan_enabled()"
        ))


def _install_b12_guards(conn) -> None:  # noqa: ANN001
    """Install session-type immutability and compliance-rule reference guards."""
    inspector = inspect(conn)
    if inspector.has_table("sessions"):
        if conn.dialect.name == "sqlite":
            conn.execute(text(
                "CREATE TRIGGER IF NOT EXISTS trg_session_type_locked "
                "BEFORE UPDATE OF session_type ON sessions "
                "WHEN OLD.started_at IS NOT NULL AND NEW.session_type <> OLD.session_type "
                "BEGIN SELECT RAISE(ABORT, 'session type is locked after start'); END"
            ))
        elif conn.dialect.name == "postgresql":
            conn.execute(text("""
                CREATE OR REPLACE FUNCTION enforce_session_type_locked()
                RETURNS TRIGGER AS $$
                BEGIN
                    IF OLD.started_at IS NOT NULL AND NEW.session_type <> OLD.session_type THEN
                        RAISE EXCEPTION 'session type is locked after start';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
            """))
            conn.execute(text("DROP TRIGGER IF EXISTS trg_session_type_locked ON sessions"))
            conn.execute(text(
                "CREATE TRIGGER trg_session_type_locked BEFORE UPDATE OF session_type "
                "ON sessions FOR EACH ROW EXECUTE FUNCTION enforce_session_type_locked()"
            ))
    if all(inspector.has_table(name) for name in (
        "compliance_rules", "session_compliance_rule_snapshots",
    )) and conn.dialect.name == "sqlite":
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS trg_compliance_rule_delete_used "
            "BEFORE DELETE ON compliance_rules "
            "WHEN EXISTS (SELECT 1 FROM session_compliance_rule_snapshots "
            "WHERE rule_id = OLD.id) BEGIN "
            "SELECT RAISE(ABORT, 'compliance rule is used by a session'); END"
        ))


def _migrate() -> None:
    insp = inspect(engine)
    experience_cols_before = (
        {c["name"] for c in insp.get_columns("experience_tips")}
        if insp.has_table("experience_tips") else set()
    )
    # 该标记只在 R5 首次升级旧库时为真。旧 pending 是单次结论，必须退回观察，
    # 否则即使新代码有门禁，它们仍可绕过统计验证直接被采纳。
    upgrading_experience_stats = bool(
        experience_cols_before and "event_key" not in experience_cols_before
    )
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            # create_all() does not append members to an existing native PostgreSQL enum.
            conn.execute(text("ALTER TYPE audiostatus ADD VALUE IF NOT EXISTS 'error'"))
        for table, cols in _MIGRATIONS.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
        if (insp.has_table("sessions") and insp.has_table("critiques")
                and {"session_id", "level"}.issubset({c["name"] for c in insp.get_columns("critiques")})):
            # 旧库新增报告状态时按已落盘产物回填，避免历史 done 场次全部显示 pending。
            conn.execute(text(
                "UPDATE sessions SET report_status = CASE "
                "WHEN status = 'error' THEN 'failed' "
                "WHEN status = 'done' AND session_type = 'observation' THEN 'ready' "
                "WHEN status = 'done' AND EXISTS ("
                "  SELECT 1 FROM transcripts t WHERE t.session_id = sessions.id"
                ") AND EXISTS ("
                "  SELECT 1 FROM critiques c WHERE c.session_id = sessions.id "
                "  AND c.level = 'summary'"
                ") THEN 'ready' "
                "WHEN status = 'done' THEN 'partial' "
                "WHEN status IN ('live', 'processing', 'parsing') THEN 'processing' "
                "ELSE 'pending' END "
                "WHERE COALESCE(TRIM(report_status), '') IN ('', 'pending')"
            ))
        if insp.has_table("session_rhythm_analyses"):
            # 进程启动时不可能还有上一进程的 LLM 任务存活；释放遗留 analyzing
            # 租约。有旧 payload 的重跑继续展示上次结果，首次分析则明确失败可重试。
            conn.execute(text(
                "UPDATE session_rhythm_analyses SET "
                "status = CASE WHEN "
                "COALESCE(TRIM(comparison), '') NOT IN ('', '{}', 'null') OR "
                "COALESCE(TRIM(extracted), '') NOT IN ('', '{}', 'null') "
                "THEN 'ready' ELSE 'failed' END, "
                "error_msg = CASE WHEN "
                "COALESCE(TRIM(comparison), '') NOT IN ('', '{}', 'null') OR "
                "COALESCE(TRIM(extracted), '') NOT IN ('', '{}', 'null') "
                "THEN '服务重启中断了本轮重跑，继续显示上次结果' "
                "ELSE '服务重启中断了节奏分析，请重试' END, "
                "analysis_run_id = '', updated_at = CURRENT_TIMESTAMP "
                "WHERE status = 'analyzing'"
            ))
        if insp.has_table("experience_tip_samples"):
            # 唯一索引必须先于下方 legacy INSERT ... ON CONFLICT 创建。兼容开发期
            # 已存在但缺 source_key/唯一约束的半成品表，并保留重复行为独立证据。
            blank_ids = conn.execute(text(
                "SELECT id FROM experience_tip_samples "
                "WHERE COALESCE(source_key, '') = ''"
            )).scalars().all()
            for sample_id in blank_ids:
                conn.execute(text(
                    "UPDATE experience_tip_samples SET source_key = :source_key "
                    "WHERE id = :sample_id"
                ), {"source_key": f"sample:{sample_id}", "sample_id": sample_id})
            duplicate_sources = conn.execute(text(
                "SELECT source_key FROM experience_tip_samples "
                "GROUP BY source_key HAVING COUNT(*) > 1"
            )).scalars().all()
            for source_key in duplicate_sources:
                duplicate_ids = conn.execute(text(
                    "SELECT id FROM experience_tip_samples "
                    "WHERE source_key = :source_key ORDER BY id"
                ), {"source_key": source_key}).scalars().all()
                for sample_id in duplicate_ids[1:]:
                    conn.execute(text(
                        "UPDATE experience_tip_samples SET source_key = :replacement "
                        "WHERE id = :sample_id"
                    ), {"replacement": f"sample:{sample_id}", "sample_id": sample_id})
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_experience_tip_sample_source "
                "ON experience_tip_samples(source_key)"
            ))
        # 幂等索引（SQLite/PG 语法兼容）：ext_id 部分唯一索引（NULL 可重复）防串绑；
        # streamer_turns.streamer_id 供按主播反查参与场次（partner 接口）
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_streamers_ext_id "
            "ON streamers(ext_id) WHERE ext_id IS NOT NULL"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_streamer_turns_streamer "
            "ON streamer_turns(streamer_id)"
        ))
        # 为旧版 ExperienceTip 已采纳切片补 provenance。附加分类条件可避免
        # SQLite rowid 被产品切片复用时把无关数据误认成经验资产。
        if insp.has_table("experience_tips"):
            conn.execute(text(
                "UPDATE knowledge_chunks SET origin_type = 'experience_tip', "
                "origin_id = (SELECT experience_tips.id FROM experience_tips "
                "WHERE experience_tips.chunk_id = knowledge_chunks.id LIMIT 1) "
                "WHERE COALESCE(origin_type, '') = '' AND category = '本店经验' "
                "AND EXISTS (SELECT 1 FROM experience_tips "
                "WHERE experience_tips.chunk_id = knowledge_chunks.id)"
            ))
            if upgrading_experience_stats:
                conn.execute(text(
                    "UPDATE experience_tips SET status = 'observing' "
                    "WHERE status = 'pending'"
                ))

                # 完整旧表可把既有单次候选作为第一条历史样本保留下来；极早期残缺表
                # （只含 id/status/chunk_id）跳过回填，仍能安全完成轻量迁移。
                needed = {
                    "session_id", "streamer_id", "scene", "metric", "effect",
                    "action", "quote", "template", "at_ms",
                }
                if needed.issubset(experience_cols_before):
                    event_case = (
                        "CASE "
                        "WHEN metric LIKE '%停留%' OR metric LIKE '%留存%' "
                        "OR metric LIKE '%观看时长%' THEN :retention_up "
                        "WHEN metric LIKE '%转化%' OR metric LIKE '%点击率%' "
                        "OR metric LIKE '%进入率%' THEN :conversion_up "
                        "WHEN metric LIKE '%成交%' OR metric LIKE '%支付%' "
                        "OR metric LIKE '%订单%' OR metric LIKE '%销量%' "
                        "OR LOWER(metric) LIKE '%gmv%' THEN :sale_up "
                        "WHEN metric LIKE '%在线%' THEN :online_up "
                        "WHEN metric LIKE '%互动%' OR metric LIKE '%点赞%' "
                        "OR metric LIKE '%评论%' OR metric LIKE '%涨粉%' "
                        "THEN :engagement_up "
                        "WHEN metric LIKE '%进房%' OR metric LIKE '%曝光%' "
                        "OR metric LIKE '%观看%' THEN :traffic_up "
                        "ELSE 'metric:' || LOWER(REPLACE(COALESCE(metric, ''), ' ', '')) "
                        "|| :up_suffix END"
                    )
                    source_case = (
                        "CASE WHEN t.session_id IS NOT NULL THEN 'session:' || "
                        "CAST(t.session_id AS VARCHAR) || ':' || COALESCE(t.event_key, '') || "
                        "':' || CAST(COALESCE(t.at_ms, 0) AS VARCHAR) "
                        "ELSE 'legacy:' || CAST(t.id AS VARCHAR) END"
                    )
                    conn.execute(text(
                        f"UPDATE experience_tips SET event_key = {event_case} "
                        "WHERE COALESCE(event_key, '') = ''"
                    ), {
                        "sale_up": "sale:up",
                        "online_up": "online:up",
                        "retention_up": "retention:up",
                        "conversion_up": "conversion:up",
                        "traffic_up": "traffic:up",
                        "engagement_up": "engagement:up",
                        "up_suffix": ":up",
                    })
                    if insp.has_table("experience_tip_samples"):
                        conn.execute(text(
                            "INSERT INTO experience_tip_samples "
                            "(source_key, session_id, streamer_id, tip_id, scene, event_key, metric, "
                            "direction, verdict, effect, action, quote, template, at_ms, reusable, "
                            f"created_at) SELECT {source_case}, t.session_id, t.streamer_id, t.id, "
                            "t.scene, t.event_key, t.metric, 'up', '历史候选', t.effect, t.action, "
                            "t.quote, t.template, t.at_ms, TRUE, CURRENT_TIMESTAMP "
                            "FROM experience_tips t WHERE t.status <> 'rejected' "
                            "AND NOT EXISTS (SELECT 1 FROM experience_tip_samples s "
                            f"WHERE s.source_key = {source_case}) "
                            "ON CONFLICT(source_key) DO NOTHING"
                        ))
                        conn.execute(text(
                            "UPDATE experience_tips SET matched_count = "
                            "(SELECT COUNT(*) FROM experience_tip_samples s "
                            "WHERE s.tip_id = experience_tips.id), change_sample_count = "
                            "(SELECT COUNT(*) FROM experience_tip_samples s "
                            "WHERE s.event_key = experience_tips.event_key)"
                        ))
                        conn.execute(text(
                            "UPDATE experience_tips SET matched_rate = CASE "
                            "WHEN change_sample_count > 0 "
                            "THEN 1.0 * matched_count / change_sample_count ELSE 0 END"
                        ))
        # 早期版本可能已重复写入同一结构化资产的检索切片。建唯一索引前保留
        # 资产当前 chunk_id 指向的一条（否则保留最早一条），其余切片只解除
        # provenance，不删除历史文本，兼容旧数据且避免启动迁移失败。
        duplicate_origins = conn.execute(text(
            "SELECT origin_type, origin_id FROM knowledge_chunks "
            "WHERE origin_type <> '' AND origin_id IS NOT NULL "
            "GROUP BY origin_type, origin_id HAVING COUNT(*) > 1"
        )).all()
        owner_tables = {
            "experience_tip": "experience_tips",
            "rhythm_template": "rhythm_templates",
        }
        for origin_type, origin_id in duplicate_origins:
            chunk_ids = list(conn.execute(text(
                "SELECT id FROM knowledge_chunks "
                "WHERE origin_type = :origin_type AND origin_id = :origin_id "
                "ORDER BY id"
            ), {"origin_type": origin_type, "origin_id": origin_id}).scalars())
            preferred_id = None
            owner_table = owner_tables.get(origin_type)
            if owner_table and insp.has_table(owner_table):
                preferred_id = conn.execute(text(
                    f"SELECT chunk_id FROM {owner_table} WHERE id = :origin_id"
                ), {"origin_id": origin_id}).scalar()
            keep_id = preferred_id if preferred_id in chunk_ids else chunk_ids[0]
            for duplicate_id in chunk_ids:
                if duplicate_id != keep_id:
                    conn.execute(text(
                        "UPDATE knowledge_chunks SET origin_type = '', origin_id = NULL "
                        "WHERE id = :chunk_id"
                    ), {"chunk_id": duplicate_id})
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_chunks_origin "
            "ON knowledge_chunks(origin_type, origin_id) "
            "WHERE origin_type <> '' AND origin_id IS NOT NULL"
        ))
        if insp.has_table("experience_tips"):
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_experience_tips_event_key "
                "ON experience_tips(event_key)"
            ))
        if insp.has_table("experience_tip_samples"):
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_experience_tip_samples_event_key "
                "ON experience_tip_samples(event_key)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_experience_tip_samples_tip_id "
                "ON experience_tip_samples(tip_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_experience_tip_samples_session_id "
                "ON experience_tip_samples(session_id)"
            ))

        if insp.has_table("experience_corpora"):
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_experience_corpora_origin "
                "ON experience_corpora(origin_type, origin_id) "
                "WHERE origin_type <> '' AND origin_id IS NOT NULL"
            ))

        if insp.has_table("sessions"):
            conn.execute(text(
                "UPDATE sessions SET session_type = 'normal' "
                "WHERE session_type IS NULL OR TRIM(session_type) = ''"
            ))
            # 老库补规范化键。若历史遗留了多个未结束场次，保留业务进度最深、
            # 其次最早的一场，其余只标记为重复错误，绝不删除历史业务数据。
            from .core.live_link import normalize_live_link

            session_columns = {c["name"] for c in inspect(conn).get_columns("sessions")}
            required = {"id", "share_link", "status", "ended_at", "normalized_share_link"}
            if not required.issubset(session_columns):
                session_rows = []
            else:
                session_rows = conn.execute(text(
                    "SELECT id, share_link, status, ended_at FROM sessions ORDER BY id"
                )).mappings().all()
            active: dict[str, list[dict]] = {}
            active_statuses = {"created", "parsing", "live", "processing"}
            for row in session_rows:
                normalized = normalize_live_link(row["share_link"])
                conn.execute(text(
                    "UPDATE sessions SET normalized_share_link = :normalized WHERE id = :id"
                ), {"normalized": normalized, "id": row["id"]})
                if normalized and row["status"] in active_statuses and row["ended_at"] is None:
                    active.setdefault(normalized, []).append(dict(row))
            priority = {"live": 0, "processing": 1, "parsing": 2, "created": 3}
            for duplicates in active.values():
                if len(duplicates) < 2:
                    continue
                duplicates.sort(key=lambda item: (priority.get(item["status"], 9), item["id"]))
                for duplicate in duplicates[1:]:
                    conn.execute(text(
                        "UPDATE sessions SET status = 'error', error_code = 'DUPLICATE_SESSION', "
                        "error_msg = '升级时合并了同一直播链接的重复未结束场次', "
                        "ended_at = CURRENT_TIMESTAMP WHERE id = :id"
                    ), {"id": duplicate["id"]})
            if required.issubset(session_columns):
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_active_live_link "
                    "ON sessions(normalized_share_link) WHERE normalized_share_link <> '' "
                    "AND ended_at IS NULL "
                    "AND status IN ('created', 'parsing', 'live', 'processing')"
                ))
        if insp.has_table("live_alerts"):
            conn.execute(text(
                "UPDATE live_alerts SET event_key = 'legacy:' || CAST(id AS VARCHAR) "
                "WHERE COALESCE(event_key, '') = ''"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_live_alerts_event_key "
                "ON live_alerts(event_key) WHERE event_key <> ''"
            ))
        _install_script_compliance_guards(conn)
        _install_b12_guards(conn)


def init_db() -> None:
    from . import models  # noqa: F401  确保模型注册到 Base.metadata

    Base.metadata.create_all(engine)
    _migrate()
    # 新规则为实时合规权威；启动迁移同时导入内置和旧知识库违禁词，幂等且不删旧数据。
    from .core.compliance_rules import migrate_legacy_forbidden_rules

    migrate_legacy_forbidden_rules()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
