from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.core.sqlite_maintenance import (
    CORE_TABLES,
    SQLiteMaintenanceError,
    backup_database,
    restore_database,
    validate_database,
)


def _seed(path: Path, rows: int) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    for table in CORE_TABLES:
        conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT)')
        conn.executemany(
            f'INSERT INTO "{table}" (value) VALUES (?)',
            [(f"脱敏测试-{index}",) for index in range(rows)],
        )
    conn.commit()
    return conn


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_wal_database_backup_and_restore_preserve_all_core_counts(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    writer = _seed(source, 3)
    # Keep a WAL-mode connection open: raw main-file copying is unsafe here,
    # while sqlite3.Connection.backup must still create a complete snapshot.
    writer.execute('INSERT INTO "sessions" (value) VALUES (?)', ("WAL 中的新场次",))
    writer.commit()

    backup = tmp_path / "snapshot.backup.db"
    backed_up = backup_database(source, backup)
    assert backed_up.counts["sessions"] == 4
    assert all(backed_up.counts[table] == 3 for table in CORE_TABLES if table != "sessions")
    writer.close()

    current = tmp_path / "restore-target.db"
    current_writer = _seed(current, 1)
    current_writer.close()
    safety = tmp_path / "restore-target.pre-restore.db"
    restored = restore_database(current, backup, safety_output=safety)

    assert restored.counts == backed_up.counts
    assert validate_database(safety).counts == {table: 1 for table in CORE_TABLES}


def test_failed_restore_keeps_current_database_and_bad_backup(tmp_path: Path) -> None:
    current = tmp_path / "current.db"
    writer = _seed(current, 2)
    writer.close()
    before_hash = _sha256(current)
    bad_backup = tmp_path / "corrupt.backup.db"
    bad_backup.write_bytes(b"not-a-sqlite-database")

    with pytest.raises(SQLiteMaintenanceError):
        restore_database(current, bad_backup)

    assert _sha256(current) == before_hash
    assert bad_backup.read_bytes() == b"not-a-sqlite-database"
    assert validate_database(current).counts == {table: 2 for table in CORE_TABLES}
