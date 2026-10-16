from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path


def test_script_compliance_migration_creates_constraints_and_cross_table_guards(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    database = tmp_path / "script-compliance-migration.db"
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE streamers (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE rhythm_templates (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO sessions (id) VALUES (1)")
        conn.execute("INSERT INTO streamers (id) VALUES (1)")
        conn.commit()
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=backend_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert migrated.returncode == 0, migrated.stderr or migrated.stdout
    with closing(sqlite3.connect(database)) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert {
            "script_frequency_rules",
            "session_script_compliance_plans",
            "script_compliance_results",
            "script_compliance_occurrences",
        }.issubset(tables)
        assert {
            "trg_script_plan_enabled_insert",
            "trg_script_plan_enabled_update",
            "trg_script_rule_delete_used",
        }.issubset(triggers)
        with conn:
            try:
                conn.execute(
                    "INSERT INTO script_frequency_rules "
                    "(id,name,standard_script,repeat_interval_sec,target_duration_sec,"
                    "interval_tolerance_sec,duration_tolerance_sec,similarity_threshold,"
                    "effective_scope,enabled,version,created_at,updated_at) VALUES "
                    "(1,'rule','script',600,10,30,2,0.8,'{\"type\":\"all\"}',0,1,"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                )
                conn.execute(
                    "INSERT INTO session_script_compliance_plans "
                    "(session_id,streamer_id,rule_id,rule_version,rule_snapshot,created_at) "
                    "VALUES (1,1,1,1,'{}',CURRENT_TIMESTAMP)"
                )
            except sqlite3.IntegrityError as exc:
                assert "disabled" in str(exc)
            else:
                raise AssertionError("disabled rule assignment was not blocked")
