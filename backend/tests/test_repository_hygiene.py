from __future__ import annotations

from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SENSITIVE_TRACKED = re.compile(
    r"(?i)(^|/)(?:\.env\.bak[^/]*|\.secret_key|[^/]*\.bak(?:[.-].*)?|"
    r"[^/]*\.(?:db|sqlite|sqlite3)(?:$|[.-])|[^/]*-(?:wal|shm)$)"
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False,
    )


def test_git_tracks_no_secret_backup_or_runtime_database_files() -> None:
    result = _git("ls-files")
    assert result.returncode == 0, result.stderr
    offenders = [path for path in result.stdout.splitlines() if SENSITIVE_TRACKED.search(path)]
    assert offenders == []


def test_gitignore_blocks_representative_sensitive_and_runtime_files() -> None:
    samples = [
        "backend/.env.bak-20990101",
        "backend/data/.secret_key",
        "backend/data/replay.db",
        "backend/data/replay.db-wal",
        "backend/data/replay.db-shm",
        "backend/app/example.py.bak.20990101",
        "backend/data/audio/session_test.m4a",
    ]
    for sample in samples:
        result = _git("check-ignore", "--no-index", "-q", sample)
        assert result.returncode == 0, f"未被 .gitignore 拦截: {sample}"
    assert _git("check-ignore", "--no-index", "-q", ".env.example").returncode == 1


def test_environment_example_contains_no_real_secret_values() -> None:
    secret_name = re.compile(r"(?i)(key|secret|token|password|cookie|secret_id)$")
    assignments = {}
    for line in (ROOT / ".env.example").read_text("utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        assignments[key.strip()] = value.strip()
    populated = {key: value for key, value in assignments.items()
                 if secret_name.search(key) and value}
    assert populated == {}
