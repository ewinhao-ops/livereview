"""Add the R3 session rhythm analysis table.

Revision ID: 20260724_0001_r3
Revises:
Create Date: 2026-07-24

The project originally created tables with SQLAlchemy ``create_all()`` and
patched a few legacy columns in ``app.db._MIGRATIONS``. This first Alembic
revision therefore also adopts an already-created R3 table: it fills the three
known legacy columns and missing constraints/indexes instead of attempting to
create the table twice.
"""

from collections.abc import Sequence

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260724_0001_r3"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "session_rhythm_analyses"
REQUIRED_PARENT_TABLES = {"sessions", "streamers", "rhythm_templates"}
OWNER_COLUMNS = ("session_id", "streamer_id")
INDEXES = {
    "ix_session_rhythm_analyses_session_id": ("session_id",),
    "ix_session_rhythm_analyses_streamer_id": ("streamer_id",),
    "ix_session_rhythm_analyses_template_id": ("template_id",),
    "ix_session_rhythm_analyses_status": ("status",),
    "ix_session_rhythm_analyses_analysis_run_id": ("analysis_run_id",),
}
FOREIGN_KEYS = (
    (
        "fk_session_rhythm_analyses_session_id_sessions",
        ("session_id",),
        "sessions",
        ("id",),
        "CASCADE",
    ),
    (
        "fk_session_rhythm_analyses_streamer_id_streamers",
        ("streamer_id",),
        "streamers",
        ("id",),
        None,
    ),
    (
        "fk_session_rhythm_analyses_template_id_rhythm_templates",
        ("template_id",),
        "rhythm_templates",
        ("id",),
        "SET NULL",
    ),
)


def _create_table() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("streamer_id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=True),
        sa.Column("selection_mode", sa.String(length=16), nullable=False),
        sa.Column("declared_execution", sa.Boolean(), nullable=False),
        sa.Column("declared_at", sa.DateTime(), nullable=True),
        sa.Column("declaration_start_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("analysis_run_id", sa.String(length=64), nullable=False),
        sa.Column("template_snapshot", sa.Text(), nullable=False),
        sa.Column("extracted", sa.Text(), nullable=False),
        sa.Column("comparison", sa.Text(), nullable=False),
        sa.Column("validation", sa.Text(), nullable=False),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("effect_score", sa.Float(), nullable=True),
        sa.Column("effective", sa.Boolean(), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["streamer_id"], ["streamers.id"]),
        sa.ForeignKeyConstraint(
            ["template_id"], ["rhythm_templates.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "streamer_id",
            name="uq_session_rhythm_analysis_owner",
        ),
    )


def _upgrade_existing_table(bind) -> None:  # noqa: ANN001
    inspector = sa.inspect(bind)
    column_names = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    unique_column_sets = {
        tuple(constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints(TABLE_NAME)
    }
    foreign_keys = {}
    for constraint in inspector.get_foreign_keys(TABLE_NAME):
        target = (
            tuple(constraint.get("constrained_columns") or ()),
            constraint.get("referred_table"),
            tuple(constraint.get("referred_columns") or ()),
        )
        ondelete = (constraint.get("options") or {}).get("ondelete")
        foreign_keys[target] = (ondelete or "NO ACTION").upper()

    with op.batch_alter_table(TABLE_NAME) as batch_op:
        if "declared_at" not in column_names:
            batch_op.add_column(sa.Column("declared_at", sa.DateTime(), nullable=True))
        if "declaration_start_ms" not in column_names:
            batch_op.add_column(
                sa.Column(
                    "declaration_start_ms",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
        if "analysis_run_id" not in column_names:
            batch_op.add_column(
                sa.Column(
                    "analysis_run_id",
                    sa.String(length=64),
                    nullable=False,
                    server_default=sa.text("''"),
                )
            )
        if OWNER_COLUMNS not in unique_column_sets:
            batch_op.create_unique_constraint(
                "uq_session_rhythm_analysis_owner", list(OWNER_COLUMNS)
            )
        for name, local_columns, remote_table, remote_columns, ondelete in FOREIGN_KEYS:
            target = (local_columns, remote_table, remote_columns)
            expected_ondelete = (ondelete or "NO ACTION").upper()
            if target in foreign_keys and foreign_keys[target] != expected_ondelete:
                raise RuntimeError(
                    f"{TABLE_NAME} foreign key {local_columns} has ON DELETE "
                    f"{foreign_keys[target]}, expected {expected_ondelete}; "
                    "repair the legacy constraint before upgrading"
                )
            if target not in foreign_keys:
                batch_op.create_foreign_key(
                    name,
                    remote_table,
                    list(local_columns),
                    list(remote_columns),
                    ondelete=ondelete,
                )

    # Temporary server defaults make ADD COLUMN safe for populated legacy
    # tables. Once old rows are backfilled, remove those defaults and enforce
    # the same nullability as the SQLAlchemy model.
    bind.execute(
        sa.text(
            f"UPDATE {TABLE_NAME} SET declaration_start_ms = 0 "
            "WHERE declaration_start_ms IS NULL"
        )
    )
    bind.execute(
        sa.text(
            f"UPDATE {TABLE_NAME} SET analysis_run_id = '' "
            "WHERE analysis_run_id IS NULL"
        )
    )
    refreshed_columns = {
        column["name"]: column for column in sa.inspect(bind).get_columns(TABLE_NAME)
    }
    needs_normalization = any(
        refreshed_columns[name].get("default") is not None
        or refreshed_columns[name].get("nullable", True)
        for name in ("declaration_start_ms", "analysis_run_id")
    )
    if needs_normalization:
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            batch_op.alter_column(
                "declaration_start_ms",
                existing_type=sa.Integer(),
                nullable=False,
                server_default=None,
            )
            batch_op.alter_column(
                "analysis_run_id",
                existing_type=sa.String(length=64),
                nullable=False,
                server_default=None,
            )


def _create_missing_indexes(bind) -> None:  # noqa: ANN001
    inspector = sa.inspect(bind)
    column_names = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    existing_names = {
        index["name"]
        for index in inspector.get_indexes(TABLE_NAME)
        if index.get("name")
    }
    for name, columns in INDEXES.items():
        if name not in existing_names and set(columns).issubset(column_names):
            op.create_index(name, TABLE_NAME, list(columns), unique=False)


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "R3 migration inspects the existing schema and must run without --sql"
        )
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    missing_parents = sorted(
        table for table in REQUIRED_PARENT_TABLES if not inspector.has_table(table)
    )
    if missing_parents:
        raise RuntimeError(
            "R3 migration requires the existing base schema; missing tables: "
            f"{', '.join(missing_parents)}. For a new database run "
            "`python -c \"from app.db import init_db; init_db()\"` first."
        )
    if inspector.has_table(TABLE_NAME):
        _upgrade_existing_table(bind)
    else:
        _create_table()
    _create_missing_indexes(bind)


def downgrade() -> None:
    raise RuntimeError(
        "R3 downgrade is intentionally blocked because this revision may have "
        "adopted an existing table with historical analysis data. Back up the "
        "database and use a reviewed manual rollback plan."
    )
