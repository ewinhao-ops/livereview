"""Add B12 compliance snapshots, observation learning and deletion boundaries.

Revision ID: 20260808_0003_b12
Revises: 20260805_0002_script_compliance
Create Date: 2026-08-08
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260808_0003_b12"
down_revision: str | Sequence[str] | None = "20260805_0002_script_compliance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _add_column(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    tables = _tables()
    if "sessions" in tables:
        _add_column(
            "sessions",
            sa.Column("session_type", sa.String(16), nullable=False, server_default="normal"),
        )
        _add_column(
            "sessions",
            sa.Column("compliance_rule_ids", sa.Text(), nullable=False, server_default="[]"),
        )
        op.execute("UPDATE sessions SET session_type = 'normal' WHERE session_type IS NULL")
    if "live_rooms" in tables:
        _add_column("live_rooms", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    if "experience_corpora" in tables:
        _add_column(
            "experience_corpora",
            sa.Column("origin_type", sa.String(32), nullable=False, server_default=""),
        )
        _add_column("experience_corpora", sa.Column("origin_id", sa.Integer(), nullable=True))
        _add_column(
            "experience_corpora",
            sa.Column("source_metadata", sa.Text(), nullable=False, server_default="{}"),
        )
        corpus_indexes = {
            index["name"] for index in sa.inspect(op.get_bind()).get_indexes("experience_corpora")
        }
        if "uq_experience_corpora_origin" not in corpus_indexes:
            op.create_index(
                "uq_experience_corpora_origin", "experience_corpora",
                ["origin_type", "origin_id"], unique=True,
                sqlite_where=sa.text("origin_type <> '' AND origin_id IS NOT NULL"),
                postgresql_where=sa.text("origin_type <> '' AND origin_id IS NOT NULL"),
            )

    tables = _tables()
    if "compliance_rules" not in tables:
        op.create_table(
            "compliance_rules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("action", sa.String(16), nullable=False),
            sa.Column("keyword", sa.String(500), nullable=False),
            sa.Column("normalized_keyword", sa.String(500), nullable=False),
            sa.Column("scope_type", sa.String(16), nullable=False, server_default="all"),
            sa.Column("scope_id", sa.String(128), nullable=False, server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_type", sa.String(32), nullable=False, server_default=""),
            sa.Column("source_id", sa.String(64), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint("action IN ('allow', 'forbid')", name="ck_compliance_rule_action"),
            sa.CheckConstraint(
                "scope_type IN ('all', 'session', 'streamer', 'room', 'product')",
                name="ck_compliance_rule_scope_type",
            ),
            sa.CheckConstraint("LENGTH(TRIM(keyword)) > 0", name="ck_compliance_rule_keyword"),
            sa.CheckConstraint("version > 0", name="ck_compliance_rule_version"),
            sa.UniqueConstraint(
                "action", "normalized_keyword", "scope_type", "scope_id",
                name="uq_compliance_rule_scope_keyword",
            ),
        )
        op.create_index("ix_compliance_rules_action", "compliance_rules", ["action"])
        op.create_index("ix_compliance_rules_enabled", "compliance_rules", ["enabled"])

    if "session_compliance_snapshots" not in tables:
        op.create_table(
            "session_compliance_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("required_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("forbid_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("allow_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("locked_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("session_id", name="uq_session_compliance_snapshot"),
            sa.CheckConstraint("version > 0", name="ck_session_compliance_snapshot_version"),
        )
        op.create_index(
            "ix_session_compliance_snapshots_session_id",
            "session_compliance_snapshots", ["session_id"],
        )

    if "session_compliance_rule_snapshots" not in tables:
        op.create_table(
            "session_compliance_rule_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("snapshot_id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("rule_id", sa.Integer(), nullable=False),
            sa.Column("rule_version", sa.Integer(), nullable=False),
            sa.Column("action", sa.String(16), nullable=False),
            sa.Column("keyword", sa.String(500), nullable=False),
            sa.Column("scope_type", sa.String(16), nullable=False),
            sa.Column("scope_id", sa.String(128), nullable=False, server_default=""),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rule_snapshot", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["snapshot_id"], ["session_compliance_snapshots.id"], ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["rule_id"], ["compliance_rules.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint("snapshot_id", "rule_id", name="uq_snapshot_compliance_rule"),
            sa.CheckConstraint("action IN ('allow', 'forbid')", name="ck_snapshot_rule_action"),
            sa.CheckConstraint("rule_version > 0", name="ck_snapshot_rule_version"),
        )

    if "compliance_decisions" not in tables:
        op.create_table(
            "compliance_decisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("transcript_id", sa.Integer(), nullable=True),
            sa.Column("keyword", sa.String(500), nullable=False),
            sa.Column("rule_id", sa.Integer(), nullable=False),
            sa.Column("rule_version", sa.Integer(), nullable=False),
            sa.Column("rule_scope", sa.String(160), nullable=False),
            sa.Column("exempted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("result", sa.String(24), nullable=False),
            sa.Column("at_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("safe_summary", sa.String(160), nullable=False, server_default=""),
            sa.Column("alert_type", sa.String(32), nullable=False, server_default="compliance"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["transcript_id"], ["transcripts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["rule_id"], ["compliance_rules.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint(
                "transcript_id", "rule_id", "keyword", "result",
                name="uq_compliance_decision_transcript_rule",
            ),
        )

    if "observation_analyses" not in tables:
        op.create_table(
            "observation_analyses",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("corpus_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("run_id", sa.String(64), nullable=False, server_default=""),
            sa.Column("error_code", sa.String(64), nullable=False, server_default=""),
            sa.Column("error_msg", sa.Text(), nullable=False, server_default=""),
            sa.Column("include_emotion", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("emotion_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("emotion_status", sa.String(24), nullable=False, server_default="disabled"),
            sa.Column("emotion_data", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["corpus_id"], ["experience_corpora.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("session_id", name="uq_observation_analysis_session"),
        )

    if "observation_heat_slices" not in tables:
        op.create_table(
            "observation_heat_slices",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("analysis_id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("start_ms", sa.Integer(), nullable=False),
            sa.Column("end_ms", sa.Integer(), nullable=False),
            sa.Column("danmaku_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unique_user_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("relative_growth", sa.Float(), nullable=False, server_default="0"),
            sa.Column("heat_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("transcript_ids", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("corresponding_script", sa.Text(), nullable=False, server_default=""),
            sa.Column("audio_at_ms", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["analysis_id"], ["observation_analyses.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
            sa.UniqueConstraint(
                "analysis_id", "start_ms", "end_ms",
                name="uq_observation_heat_slice_range",
            ),
        )

    # Idempotent legacy knowledge migration. Application startup additionally imports built-in seeds.
    if "knowledge_items" in _tables():
        op.execute(sa.text(
            "INSERT INTO compliance_rules "
            "(action, keyword, normalized_keyword, scope_type, scope_id, enabled, note, priority, "
            "version, source_type, source_id, created_at, updated_at) "
            "SELECT 'forbid', k.name, LOWER(TRIM(k.name)), 'all', '', TRUE, k.note, 0, 1, "
            "'knowledge_item', CAST(k.id AS VARCHAR(64)), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "FROM knowledge_items k WHERE k.kind = 'forbidden' AND TRIM(k.name) <> '' "
            "AND NOT EXISTS (SELECT 1 FROM compliance_rules r WHERE r.action = 'forbid' "
            "AND r.scope_type = 'all' AND r.scope_id = '' "
            "AND r.normalized_keyword = LOWER(TRIM(k.name)))"
        ))

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_session_type_locked "
            "BEFORE UPDATE OF session_type ON sessions "
            "WHEN OLD.started_at IS NOT NULL AND NEW.session_type <> OLD.session_type "
            "BEGIN SELECT RAISE(ABORT, 'session type is locked after start'); END"
        )
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_compliance_rule_delete_used "
            "BEFORE DELETE ON compliance_rules "
            "WHEN EXISTS (SELECT 1 FROM session_compliance_rule_snapshots "
            "WHERE rule_id = OLD.id) BEGIN "
            "SELECT RAISE(ABORT, 'compliance rule is used by a session'); END"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_compliance_rule_delete_used")
        op.execute("DROP TRIGGER IF EXISTS trg_session_type_locked")
    for table in (
        "observation_heat_slices", "observation_analyses", "compliance_decisions",
        "session_compliance_rule_snapshots", "session_compliance_snapshots", "compliance_rules",
    ):
        if table in _tables():
            op.drop_table(table)
    for table, columns in (
        ("experience_corpora", ("source_metadata", "origin_id", "origin_type")),
        ("live_rooms", ("deleted_at",)),
        ("sessions", ("compliance_rule_ids", "session_type")),
    ):
        if table not in _tables():
            continue
        existing = _columns(table)
        with op.batch_alter_table(table) as batch:
            for column in columns:
                if column in existing:
                    batch.drop_column(column)
