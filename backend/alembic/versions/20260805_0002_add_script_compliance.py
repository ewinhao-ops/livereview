"""Add prescribed-script rules, immutable plans, and compliance results.

Revision ID: 20260805_0002_script_compliance
Revises: 20260724_0001_r3
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260805_0002_script_compliance"
down_revision: str | Sequence[str] | None = "20260724_0001_r3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_tables() -> None:
    op.create_table(
        "script_frequency_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("standard_script", sa.Text(), nullable=False),
        sa.Column("repeat_interval_sec", sa.Integer(), nullable=False),
        sa.Column("target_duration_sec", sa.Integer(), nullable=False),
        sa.Column("interval_tolerance_sec", sa.Integer(), nullable=False),
        sa.Column("duration_tolerance_sec", sa.Integer(), nullable=False),
        sa.Column("similarity_threshold", sa.Float(), nullable=False),
        sa.Column("effective_scope", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "repeat_interval_sec > 0", name="ck_script_rule_repeat_interval_positive",
        ),
        sa.CheckConstraint(
            "target_duration_sec > 0", name="ck_script_rule_target_duration_positive",
        ),
        sa.CheckConstraint(
            "interval_tolerance_sec >= 0",
            name="ck_script_rule_interval_tolerance_nonnegative",
        ),
        sa.CheckConstraint(
            "duration_tolerance_sec >= 0",
            name="ck_script_rule_duration_tolerance_nonnegative",
        ),
        sa.CheckConstraint(
            "similarity_threshold >= 0 AND similarity_threshold <= 1",
            name="ck_script_rule_similarity_range",
        ),
        sa.CheckConstraint("version > 0", name="ck_script_rule_version_positive"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_script_frequency_rules_enabled", "script_frequency_rules", ["enabled"],
    )

    op.create_table(
        "session_script_compliance_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("streamer_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("rule_snapshot", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "rule_version > 0", name="ck_session_script_plan_version_positive",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["streamer_id"], ["streamers.id"]),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["script_frequency_rules.id"], ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "streamer_id", "rule_id",
            name="uq_session_script_plan_assignment",
        ),
    )
    op.create_index(
        "ix_session_script_compliance_plans_session_id",
        "session_script_compliance_plans", ["session_id"],
    )
    op.create_index(
        "ix_session_script_compliance_plans_streamer_id",
        "session_script_compliance_plans", ["streamer_id"],
    )
    op.create_index(
        "ix_session_script_compliance_plans_rule_id",
        "session_script_compliance_plans", ["rule_id"],
    )

    op.create_table(
        "script_compliance_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("streamer_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("overall_status", sa.String(length=24), nullable=False),
        sa.Column("analysis_run_id", sa.String(length=64), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("actual_count", sa.Integer(), nullable=False),
        sa.Column("matched_count", sa.Integer(), nullable=False),
        sa.Column("missing_count", sa.Integer(), nullable=False),
        sa.Column("compliance_rate", sa.Float(), nullable=False),
        sa.Column("first_occurrence_ms", sa.Integer(), nullable=True),
        sa.Column("average_interval_sec", sa.Float(), nullable=True),
        sa.Column("min_interval_sec", sa.Float(), nullable=True),
        sa.Column("max_interval_sec", sa.Float(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=False),
        sa.Column("error_msg", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "expected_count >= 0", name="ck_script_result_expected_nonnegative",
        ),
        sa.CheckConstraint("actual_count >= 0", name="ck_script_result_actual_nonnegative"),
        sa.CheckConstraint("matched_count >= 0", name="ck_script_result_matched_nonnegative"),
        sa.CheckConstraint("missing_count >= 0", name="ck_script_result_missing_nonnegative"),
        sa.CheckConstraint(
            "compliance_rate >= 0 AND compliance_rate <= 1",
            name="ck_script_result_compliance_rate",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["session_script_compliance_plans.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["streamer_id"], ["streamers.id"]),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["script_frequency_rules.id"], ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", name="uq_script_compliance_result_plan"),
        sa.UniqueConstraint(
            "session_id", "streamer_id", "rule_id", "rule_version",
            name="uq_script_compliance_result_version",
        ),
    )
    for column in (
        "plan_id", "session_id", "streamer_id", "rule_id", "status", "analysis_run_id",
    ):
        op.create_index(
            f"ix_script_compliance_results_{column}",
            "script_compliance_results", [column],
        )

    op.create_table(
        "script_compliance_occurrences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("result_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("issues", sa.Text(), nullable=False),
        sa.Column("actual_start_ms", sa.Integer(), nullable=True),
        sa.Column("actual_end_ms", sa.Integer(), nullable=True),
        sa.Column("actual_duration_sec", sa.Float(), nullable=True),
        sa.Column("interval_from_previous_sec", sa.Float(), nullable=True),
        sa.Column("similarity_percent", sa.Float(), nullable=True),
        sa.Column("transcript_ids", sa.Text(), nullable=False),
        sa.Column("raw_transcript", sa.Text(), nullable=False),
        sa.Column("similarity_passed", sa.Boolean(), nullable=False),
        sa.Column("duration_passed", sa.Boolean(), nullable=False),
        sa.Column("frequency_passed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("ordinal > 0", name="ck_script_occurrence_ordinal_positive"),
        sa.CheckConstraint(
            "similarity_percent IS NULL OR "
            "(similarity_percent >= 0 AND similarity_percent <= 100)",
            name="ck_script_occurrence_similarity_percent",
        ),
        sa.ForeignKeyConstraint(
            ["result_id"], ["script_compliance_results.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "result_id", "ordinal", name="uq_script_compliance_occurrence_ordinal",
        ),
    )
    op.create_index(
        "ix_script_compliance_occurrences_result_id",
        "script_compliance_occurrences", ["result_id"],
    )
    op.create_index(
        "ix_script_compliance_occurrences_status",
        "script_compliance_occurrences", ["status"],
    )


def _install_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_script_plan_enabled_insert "
            "BEFORE INSERT ON session_script_compliance_plans "
            "WHEN COALESCE((SELECT enabled FROM script_frequency_rules "
            "WHERE id = NEW.rule_id), 0) <> 1 BEGIN "
            "SELECT RAISE(ABORT, 'script rule is disabled or missing'); END"
        )
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_script_plan_enabled_update "
            "BEFORE UPDATE OF rule_id ON session_script_compliance_plans "
            "WHEN COALESCE((SELECT enabled FROM script_frequency_rules "
            "WHERE id = NEW.rule_id), 0) <> 1 BEGIN "
            "SELECT RAISE(ABORT, 'script rule is disabled or missing'); END"
        )
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_script_rule_delete_used "
            "BEFORE DELETE ON script_frequency_rules "
            "WHEN EXISTS (SELECT 1 FROM session_script_compliance_plans "
            "WHERE rule_id = OLD.id) BEGIN "
            "SELECT RAISE(ABORT, 'script rule is used by a session'); END"
        )
    elif dialect == "postgresql":
        op.execute("""
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
        """)
        op.execute(
            "CREATE TRIGGER trg_script_plan_enabled BEFORE INSERT OR UPDATE OF rule_id "
            "ON session_script_compliance_plans FOR EACH ROW "
            "EXECUTE FUNCTION enforce_script_plan_enabled()"
        )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    table_names = (
        "script_frequency_rules",
        "session_script_compliance_plans",
        "script_compliance_results",
        "script_compliance_occurrences",
    )
    existing = [name for name in table_names if inspector.has_table(name)]
    if existing and len(existing) != len(table_names):
        raise RuntimeError(
            "script compliance migration found a partial schema; back up and repair it first"
        )
    if not existing:
        _create_tables()
    _install_guards()


def downgrade() -> None:
    raise RuntimeError(
        "script compliance downgrade is intentionally blocked because rule snapshots and "
        "historical compliance evidence must not be destroyed"
    )
