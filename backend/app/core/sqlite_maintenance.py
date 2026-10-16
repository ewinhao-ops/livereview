"""Safe local SQLite backup, validation and restore helpers.

The sqlite online-backup API snapshots a WAL database consistently while it is
being written.  Restore is intentionally conservative and must be run while the
service is stopped; a verified pre-restore snapshot is always retained.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4


CORE_TABLES = (
    "streamers",
    "sessions",
    "live_alerts",
    "transcripts",
    "critiques",
    "audio_files",
)


class SQLiteMaintenanceError(RuntimeError):
    """A safe maintenance operation was refused or could not be completed."""


@dataclass(frozen=True)
class DatabaseValidation:
    quick_check: str
    counts: dict[str, int]


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def validate_database(
    path: str | Path, *, require_core_tables: bool = True,
) -> DatabaseValidation:
    database = _resolved(path)
    if not database.is_file():
        raise SQLiteMaintenanceError("数据库文件不存在")
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(database, timeout=5)
        try:
            quick_rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
            if quick_rows != ["ok"]:
                raise SQLiteMaintenanceError("数据库完整性校验失败")
            present = {
                str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing = sorted(set(CORE_TABLES) - present)
            if require_core_tables and missing:
                raise SQLiteMaintenanceError("备份缺少核心业务表：" + ", ".join(missing))
            counts = {
                table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in CORE_TABLES if table in present
            }
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise SQLiteMaintenanceError("数据库无法读取或格式无效") from exc
    return DatabaseValidation(quick_check="ok", counts=counts)


def _snapshot(source: Path, destination: Path) -> None:
    """Create one online SQLite snapshot; caller owns destination lifecycle."""
    source_conn: sqlite3.Connection | None = None
    destination_conn: sqlite3.Connection | None = None
    try:
        source_conn = sqlite3.connect(source, timeout=30)
        destination_conn = sqlite3.connect(destination, timeout=30)
        source_conn.backup(destination_conn, pages=256, sleep=0.05)
        destination_conn.commit()
    except sqlite3.Error as exc:
        raise SQLiteMaintenanceError("SQLite 在线快照失败") from exc
    finally:
        if destination_conn is not None:
            destination_conn.close()
        if source_conn is not None:
            source_conn.close()


def _failed_path(parent: Path, stem: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return parent / f"{stem}.failed-{stamp}-{uuid4().hex[:8]}.db"


def backup_database(source: str | Path, output: str | Path) -> DatabaseValidation:
    """Atomically create a verified backup without copying a live WAL file raw."""
    source_path = _resolved(source)
    output_path = _resolved(output)
    if source_path == output_path:
        raise SQLiteMaintenanceError("备份目标不能是当前数据库")
    if not source_path.is_file():
        raise SQLiteMaintenanceError("当前数据库不存在")
    if output_path.exists():
        raise SQLiteMaintenanceError("备份文件已存在，拒绝覆盖")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.parent / f".{output_path.name}.partial-{uuid4().hex}.db"
    try:
        _snapshot(source_path, partial)
        validation = validate_database(partial)
        os.replace(partial, output_path)
        return validation
    except Exception:
        if partial.exists():
            os.replace(partial, _failed_path(output_path.parent, output_path.stem))
        raise


def _preserve_sidecars(database: Path, safety: Path) -> list[tuple[Path, Path]]:
    preserved: list[tuple[Path, Path]] = []
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            saved = Path(str(safety) + suffix)
            os.replace(sidecar, saved)
            preserved.append((sidecar, saved))
    return preserved


def restore_database(
    current: str | Path, backup: str | Path, *, safety_output: str | Path | None = None,
) -> DatabaseValidation:
    """Restore a verified backup, retaining a verified snapshot of current data.

    The service must be stopped.  An exclusive lock check rejects an actively
    written database, and the current database is not replaced until both the
    backup and restore candidate have passed integrity/count validation.
    """
    current_path = _resolved(current)
    backup_path = _resolved(backup)
    if current_path == backup_path:
        raise SQLiteMaintenanceError("恢复源不能是当前数据库")
    if not current_path.is_file():
        raise SQLiteMaintenanceError("当前数据库不存在，拒绝无保护恢复")
    backup_validation = validate_database(backup_path)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safety = (
        _resolved(safety_output) if safety_output else
        current_path.parent / f"{current_path.stem}.pre-restore-{stamp}.db"
    )
    if safety.exists():
        raise SQLiteMaintenanceError("恢复前安全备份已存在，拒绝覆盖")
    safety_validation = backup_database(current_path, safety)
    if safety_validation.quick_check != "ok":  # defensive; backup_database already checks
        raise SQLiteMaintenanceError("恢复前安全备份校验失败")

    candidate = current_path.parent / f".{current_path.name}.restore-{uuid4().hex}.db"
    preserved_sidecars: list[tuple[Path, Path]] = []
    replaced = False
    try:
        _snapshot(backup_path, candidate)
        candidate_validation = validate_database(candidate)
        if candidate_validation.counts != backup_validation.counts:
            raise SQLiteMaintenanceError("恢复候选的核心记录数量与备份不一致")

        # A successful BEGIN EXCLUSIVE proves no service/writer currently owns
        # the database.  Checkpoint first so stale WAL cannot be replayed later.
        guard: sqlite3.Connection | None = None
        try:
            guard = sqlite3.connect(current_path, timeout=1, isolation_level=None)
            guard.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            guard.execute("BEGIN EXCLUSIVE")
            guard.execute("ROLLBACK")
            guard.close()
        except sqlite3.Error as exc:
            if guard is not None:
                guard.close()
            raise SQLiteMaintenanceError("数据库仍在使用，请先停止系统再恢复") from exc

        preserved_sidecars = _preserve_sidecars(current_path, safety)
        os.replace(candidate, current_path)
        replaced = True
        restored = validate_database(current_path)
        if restored.counts != backup_validation.counts:
            raise SQLiteMaintenanceError("恢复后核心记录数量校验失败")
        return restored
    except Exception:
        if replaced and current_path.exists():
            # Keep the failed restored DB for diagnosis, then rebuild the
            # original from the already verified pre-restore snapshot.
            os.replace(current_path, _failed_path(current_path.parent, current_path.stem))
            rollback = current_path.parent / f".{current_path.name}.rollback-{uuid4().hex}.db"
            _snapshot(safety, rollback)
            validate_database(rollback)
            os.replace(rollback, current_path)
        elif candidate.exists():
            os.replace(candidate, _failed_path(current_path.parent, current_path.stem))
        # Before replacement, the old main DB still matches its sidecars, so put
        # them back.  After replacement/rollback, never attach an old WAL to a
        # different main file; it remains beside the safety snapshot.
        if not replaced:
            for original, saved in preserved_sidecars:
                if saved.exists() and not original.exists():
                    os.replace(saved, original)
        raise
