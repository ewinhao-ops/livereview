from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


class DatabaseMigrationTests(unittest.TestCase):
    def test_old_knowledge_and_corpus_tables_are_upgraded(self) -> None:
        backend_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "old.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "CREATE TABLE knowledge_chunks ("
                    "id INTEGER PRIMARY KEY, source VARCHAR(255) DEFAULT '', "
                    "text TEXT DEFAULT '', embedding TEXT DEFAULT '', created_at DATETIME)"
                )
                conn.execute(
                    "CREATE TABLE experience_corpora ("
                    "id INTEGER PRIMARY KEY, source VARCHAR(255) DEFAULT '', "
                    "corpus_type VARCHAR(24) DEFAULT 'live_skill', content_hash VARCHAR(64), "
                    "text TEXT DEFAULT '', status VARCHAR(16) DEFAULT 'processing', "
                    "template_count INTEGER DEFAULT 0, llm_provider VARCHAR(64) DEFAULT '', "
                    "prompt_version VARCHAR(32) DEFAULT 'rhythm-v1', error_msg TEXT DEFAULT '', "
                    "created_at DATETIME, processing_started_at DATETIME, processed_at DATETIME)"
                )
                conn.execute(
                    "CREATE TABLE session_rhythm_analyses ("
                    "id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, streamer_id INTEGER NOT NULL, "
                    "template_id INTEGER, selection_mode VARCHAR(16) DEFAULT 'auto', "
                    "declared_execution BOOLEAN DEFAULT FALSE, status VARCHAR(16) DEFAULT 'planned', "
                    "template_snapshot TEXT DEFAULT '{}', extracted TEXT DEFAULT '{}', "
                    "comparison TEXT DEFAULT '{}', validation TEXT DEFAULT '{}', match_score FLOAT, "
                    "effect_score FLOAT, effective BOOLEAN, error_msg TEXT DEFAULT '', "
                    "created_at DATETIME, updated_at DATETIME, UNIQUE(session_id, streamer_id))"
                )
                conn.execute(
                    "CREATE TABLE sessions ("
                    "id INTEGER PRIMARY KEY, status VARCHAR(16), error_msg TEXT DEFAULT '', "
                    "compass_token VARCHAR(48) DEFAULT '')"
                )
                conn.execute(
                    "CREATE TABLE critiques ("
                    "id INTEGER PRIMARY KEY, data TEXT DEFAULT '')"
                )
                conn.execute(
                    "INSERT INTO session_rhythm_analyses "
                    "(id, session_id, streamer_id, status, extracted, comparison) VALUES "
                    "(1, 10, 20, 'analyzing', '{\"blocks\":[{\"order\":1}]}', "
                    "'{\"summary\":\"旧结果\"}'), "
                    "(2, 10, 21, 'analyzing', '{}', '{}')"
                )
                conn.commit()

            env = os.environ.copy()
            env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
            result = subprocess.run(
                [sys.executable, "-c", "from app.db import init_db; init_db()"],
                cwd=backend_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

            with closing(sqlite3.connect(db_path)) as conn:
                chunk_cols = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_chunks)")}
                corpus_cols = {row[1] for row in conn.execute("PRAGMA table_info(experience_corpora)")}
                rhythm_cols = {row[1] for row in conn.execute("PRAGMA table_info(session_rhythm_analyses)")}
                session_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
                critique_cols = {row[1] for row in conn.execute("PRAGMA table_info(critiques)")}
                recovered = conn.execute(
                    "SELECT id, status, error_msg, analysis_run_id "
                    "FROM session_rhythm_analyses ORDER BY id"
                ).fetchall()
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(knowledge_chunks)")}
            self.assertTrue(
                {"kind", "category", "scene", "origin_type", "origin_id"}.issubset(chunk_cols)
            )
            self.assertTrue(
                {"processing_run_id", "processing_heartbeat_at"}.issubset(corpus_cols)
            )
            self.assertTrue(
                {"declared_at", "declaration_start_ms", "analysis_run_id"}.issubset(rhythm_cols)
            )
            self.assertIn("error_code", session_cols)
            self.assertTrue({
                "llm_provider", "llm_requested_provider", "is_fallback",
            }.issubset(critique_cols))
            self.assertEqual(recovered[0][1], "ready")
            self.assertIn("继续显示上次结果", recovered[0][2])
            self.assertEqual(recovered[0][3], "")
            self.assertEqual(recovered[1][1], "failed")
            self.assertIn("请重试", recovered[1][2])
            self.assertIn("uq_knowledge_chunks_origin", indexes)

    def test_fresh_rhythm_analysis_table_has_unique_owner(self) -> None:
        backend_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fresh.db"
            env = os.environ.copy()
            env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
            result = subprocess.run(
                [sys.executable, "-c", "from app.db import init_db; init_db()"],
                cwd=backend_root, env=env, capture_output=True, text=True,
                timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            with closing(sqlite3.connect(db_path)) as conn:
                cols = {row[1] for row in conn.execute(
                    "PRAGMA table_info(session_rhythm_analyses)"
                )}
                unique_indexes = [row[1] for row in conn.execute(
                    "PRAGMA index_list(session_rhythm_analyses)"
                ) if row[2]]
                unique_columns = [
                    [info[2] for info in conn.execute(f"PRAGMA index_info('{name}')")]
                    for name in unique_indexes
                ]
            self.assertTrue({
                "declared_at", "declaration_start_ms", "analysis_run_id",
            }.issubset(cols))
            self.assertIn(["session_id", "streamer_id"], unique_columns)

    def test_single_event_pending_tip_is_moved_back_to_statistical_observation(self) -> None:
        backend_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "old-experience.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "CREATE TABLE experience_tips ("
                    "id INTEGER PRIMARY KEY, session_id INTEGER, streamer_id INTEGER, "
                    "scene VARCHAR(24) DEFAULT '', metric VARCHAR(32) DEFAULT '', "
                    "effect VARCHAR(80) DEFAULT '', action TEXT DEFAULT '', quote TEXT DEFAULT '', "
                    "template TEXT DEFAULT '', at_ms INTEGER DEFAULT 0, "
                    "status VARCHAR(16) DEFAULT 'pending', chunk_id INTEGER, "
                    "version INTEGER DEFAULT 0, created_at DATETIME, adopted_at DATETIME)"
                )
                # 模拟开发期半成品 R5 表：已有样本表，但还没有 source_key/唯一约束。
                conn.execute(
                    "CREATE TABLE experience_tip_samples ("
                    "id INTEGER PRIMARY KEY, session_id INTEGER, streamer_id INTEGER, "
                    "tip_id INTEGER, scene VARCHAR(24) DEFAULT '', "
                    "event_key VARCHAR(64) DEFAULT '', metric VARCHAR(32) DEFAULT '', "
                    "direction VARCHAR(8) DEFAULT 'up', effect VARCHAR(80) DEFAULT '', "
                    "action TEXT DEFAULT '', quote TEXT DEFAULT '', template TEXT DEFAULT '', "
                    "at_ms INTEGER DEFAULT 0, reusable BOOLEAN DEFAULT FALSE, "
                    "created_at DATETIME)"
                )
                conn.execute(
                    "INSERT INTO experience_tips "
                    "(id, session_id, scene, metric, effect, action, quote, template, at_ms, status) "
                    "VALUES (9, 7, 'product', '实时在线人数', '在线+20', "
                    "'介绍产品佩戴卖点', '久戴不夹头', '先讲佩戴，再讲声音', 1234, 'pending')"
                )
                conn.commit()

            env = os.environ.copy()
            env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
            result = subprocess.run(
                [sys.executable, "-c", "from app.db import init_db; init_db()"],
                cwd=backend_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

            with closing(sqlite3.connect(db_path)) as conn:
                tip = conn.execute(
                    "SELECT status, event_key, matched_count, change_sample_count "
                    "FROM experience_tips WHERE id = 9"
                ).fetchone()
                sample = conn.execute(
                    "SELECT source_key, tip_id, event_key, reusable "
                    "FROM experience_tip_samples"
                ).fetchone()
            self.assertEqual(tip, ("observing", "online:up", 1, 1))
            self.assertEqual(sample, ("session:7:online:up:1234", 9, "online:up", 1))


if __name__ == "__main__":
    unittest.main()
