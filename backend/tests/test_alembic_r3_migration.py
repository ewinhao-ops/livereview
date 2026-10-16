from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


class R3AlembicMigrationTests(unittest.TestCase):
    backend_root = Path(__file__).resolve().parents[1]

    def _run_alembic(self, db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
            cwd=self.backend_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def _initialize_current_schema(self, db_path: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
        return subprocess.run(
            [sys.executable, "-c", "from app.db import init_db; init_db()"],
            cwd=self.backend_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    @staticmethod
    def _create_r3_parents(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE streamers (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE rhythm_templates (id INTEGER PRIMARY KEY)")

    def test_upgrade_creates_r3_table_and_blocks_destructive_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pre-r3.db"
            with closing(sqlite3.connect(db_path)) as conn:
                self._create_r3_parents(conn)
                conn.commit()

            upgraded = self._run_alembic(db_path, "upgrade", "head")
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr or upgraded.stdout)

            with closing(sqlite3.connect(db_path)) as conn:
                column_rows = {
                    row[1]: row
                    for row in conn.execute(
                        "PRAGMA table_info(session_rhythm_analyses)"
                    )
                }
                columns = set(column_rows)
                indexes = {
                    row[1] for row in conn.execute(
                        "PRAGMA index_list(session_rhythm_analyses)"
                    )
                }
                unique_columns = [
                    tuple(
                        item[2]
                        for item in conn.execute(f"PRAGMA index_info('{row[1]}')")
                    )
                    for row in conn.execute(
                        "PRAGMA index_list(session_rhythm_analyses)"
                    )
                    if row[2]
                ]
                foreign_keys = {
                    (row[3], row[2], row[4], row[6])
                    for row in conn.execute(
                        "PRAGMA foreign_key_list(session_rhythm_analyses)"
                    )
                }
                revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()

            self.assertTrue(
                {
                    "session_id",
                    "streamer_id",
                    "template_id",
                    "declared_at",
                    "declaration_start_ms",
                    "analysis_run_id",
                    "comparison",
                    "validation",
                    "effective",
                }.issubset(columns)
            )
            self.assertTrue(set(INDEX_NAMES).issubset(indexes))
            self.assertIn(("session_id", "streamer_id"), unique_columns)
            self.assertEqual(foreign_keys, EXPECTED_FOREIGN_KEYS)
            self.assertEqual(column_rows["declaration_start_ms"][3:5], (1, None))
            self.assertEqual(column_rows["analysis_run_id"][3:5], (1, None))
            self.assertEqual(revision, (HEAD_REVISION,))

            repeated = self._run_alembic(db_path, "upgrade", "head")
            self.assertEqual(repeated.returncode, 0, repeated.stderr or repeated.stdout)

            downgraded = self._run_alembic(db_path, "downgrade", "base")
            self.assertNotEqual(downgraded.returncode, 0)
            self.assertIn("downgrade is intentionally blocked", downgraded.stderr)
            with closing(sqlite3.connect(db_path)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertIn("session_rhythm_analyses", tables)
            self.assertIn("sessions", tables)

    def test_upgrade_rejects_database_without_base_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "empty.db"
            failed = self._run_alembic(db_path, "upgrade", "head")
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("requires the existing base schema", failed.stderr)

            with closing(sqlite3.connect(db_path)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertNotIn("session_rhythm_analyses", tables)

    def test_upgrade_adopts_existing_r3_table_and_preserves_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy-r3.db"
            with closing(sqlite3.connect(db_path)) as conn:
                self._create_r3_parents(conn)
                conn.execute("INSERT INTO sessions (id) VALUES (10)")
                conn.execute("INSERT INTO streamers (id) VALUES (20)")
                conn.execute("INSERT INTO rhythm_templates (id) VALUES (30)")
                conn.execute(
                    "CREATE TABLE session_rhythm_analyses ("
                    "id INTEGER PRIMARY KEY, "
                    "session_id INTEGER NOT NULL, streamer_id INTEGER NOT NULL, "
                    "template_id INTEGER, selection_mode VARCHAR(16) NOT NULL, "
                    "declared_execution BOOLEAN NOT NULL, status VARCHAR(16) NOT NULL, "
                    "template_snapshot TEXT NOT NULL, extracted TEXT NOT NULL, "
                    "comparison TEXT NOT NULL, validation TEXT NOT NULL, "
                    "match_score FLOAT, effect_score FLOAT, effective BOOLEAN, "
                    "error_msg TEXT NOT NULL, created_at DATETIME NOT NULL, "
                    "updated_at DATETIME NOT NULL, "
                    "UNIQUE(session_id, streamer_id))"
                )
                conn.execute(
                    "INSERT INTO session_rhythm_analyses ("
                    "id, session_id, streamer_id, template_id, selection_mode, "
                    "declared_execution, status, template_snapshot, extracted, comparison, "
                    "validation, error_msg, created_at, updated_at"
                    ") VALUES (1, 10, 20, 30, 'manual', 1, 'ready', '{}', '{}', '{}', "
                    "'{}', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
                conn.commit()

            upgraded = self._run_alembic(db_path, "upgrade", "head")
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr or upgraded.stdout)

            with closing(sqlite3.connect(db_path)) as conn:
                column_rows = {
                    row[1]: row
                    for row in conn.execute(
                        "PRAGMA table_info(session_rhythm_analyses)"
                    )
                }
                columns = set(column_rows)
                row = conn.execute(
                    "SELECT id, declaration_start_ms, analysis_run_id "
                    "FROM session_rhythm_analyses"
                ).fetchone()
                foreign_keys = {
                    (item[3], item[2], item[4], item[6])
                    for item in conn.execute(
                        "PRAGMA foreign_key_list(session_rhythm_analyses)"
                    )
                }
            self.assertTrue(
                {"declared_at", "declaration_start_ms", "analysis_run_id"}.issubset(columns)
            )
            self.assertEqual(row, (1, 0, ""))
            self.assertEqual(foreign_keys, EXPECTED_FOREIGN_KEYS)
            self.assertEqual(column_rows["declaration_start_ms"][3:5], (1, None))
            self.assertEqual(column_rows["analysis_run_id"][3:5], (1, None))

    def test_upgrade_adopts_schema_created_by_current_init_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "current-create-all.db"
            initialized = self._initialize_current_schema(db_path)
            self.assertEqual(
                initialized.returncode, 0, initialized.stderr or initialized.stdout
            )

            upgraded = self._run_alembic(db_path, "upgrade", "head")
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr or upgraded.stdout)

            checked = self._run_alembic(db_path, "check")
            self.assertEqual(checked.returncode, 0, checked.stderr or checked.stdout)

            with closing(sqlite3.connect(db_path)) as conn:
                revision = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                row_count = conn.execute(
                    "SELECT COUNT(*) FROM session_rhythm_analyses"
                ).fetchone()
            self.assertEqual(revision, (HEAD_REVISION,))
            self.assertEqual(row_count, (0,))


INDEX_NAMES = (
    "ix_session_rhythm_analyses_session_id",
    "ix_session_rhythm_analyses_streamer_id",
    "ix_session_rhythm_analyses_template_id",
    "ix_session_rhythm_analyses_status",
    "ix_session_rhythm_analyses_analysis_run_id",
)
EXPECTED_FOREIGN_KEYS = {
    ("session_id", "sessions", "id", "CASCADE"),
    ("streamer_id", "streamers", "id", "NO ACTION"),
    ("template_id", "rhythm_templates", "id", "SET NULL"),
}
HEAD_REVISION = "20260808_0003_b12"


if __name__ == "__main__":
    unittest.main()
