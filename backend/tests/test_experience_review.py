from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.api import knowledge as knowledge_api
from app import db as db_module
from app.core.review import experience
from app.db import Base
from app.models import ExperienceTip, KnowledgeChunk


class ExperienceReviewConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "experience.db"
        self.engine = create_engine(f"sqlite:///{db_path}", future=True)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False,
        )
        self.original_session = experience.SessionLocal
        self.original_embed_one = experience.embed_one
        experience.SessionLocal = self.sessions

        async def fake_embedding(_text: str):
            return [0.2, 0.4]

        experience.embed_one = fake_embedding

    async def asyncTearDown(self) -> None:
        experience.SessionLocal = self.original_session
        experience.embed_one = self.original_embed_one
        self.engine.dispose()
        self.tmp.cleanup()

    def _new_tip(self) -> int:
        with self.sessions() as db:
            tip = ExperienceTip(
                scene="opening",
                action="先说明直播价值，再引导新客互动",
                template="新来的朋友先扣 1，我马上讲清选款方法。",
                effect="新客停留提升",
                event_key="retention:up",
                matched_count=5,
                change_sample_count=5,
                matched_rate=1.0,
                status="pending",
            )
            db.add(tip)
            db.commit()
            return tip.id

    async def test_reject_while_embedding_cancels_stale_adoption(self) -> None:
        tip_id = self._new_tip()
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed_embedding(_text: str):
            started.set()
            await release.wait()
            return [0.1, 0.3]

        experience.embed_one = delayed_embedding
        adopting = asyncio.create_task(experience.adopt_tip(tip_id))
        await started.wait()
        rejected = experience.reject_tip(tip_id)
        self.assertTrue(rejected["ok"])
        release.set()
        stale_result = await adopting
        self.assertFalse(stale_result["ok"])

        with self.sessions() as db:
            tip = db.get(ExperienceTip, tip_id)
            self.assertEqual(tip.status, "rejected")
            self.assertEqual(tip.version, 1)
            self.assertIsNone(tip.chunk_id)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(KnowledgeChunk)), 0,
            )

        # 已驳回技巧重新采纳时，重复点击驳回也要推进 version，取消在途 embed。
        started.clear()
        release.clear()
        adopting_again = asyncio.create_task(experience.adopt_tip(tip_id))
        await started.wait()
        rejected_again = experience.reject_tip(tip_id)
        self.assertTrue(rejected_again["already"])
        release.set()
        stale_readoption = await adopting_again
        self.assertFalse(stale_readoption["ok"])
        with self.sessions() as db:
            tip = db.get(ExperienceTip, tip_id)
            self.assertEqual(tip.status, "rejected")
            self.assertEqual(tip.version, 2)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(KnowledgeChunk)), 0,
            )

    async def test_concurrent_adopt_and_reject_then_readopt_are_idempotent(self) -> None:
        tip_id = self._new_tip()
        both_started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def barrier_embedding(_text: str):
            nonlocal calls
            calls += 1
            if calls == 2:
                both_started.set()
            await release.wait()
            return [0.5, 0.7]

        experience.embed_one = barrier_embedding
        first = asyncio.create_task(experience.adopt_tip(tip_id))
        second = asyncio.create_task(experience.adopt_tip(tip_id))
        await both_started.wait()
        release.set()
        adopted = await asyncio.gather(first, second)
        self.assertTrue(all(result["ok"] for result in adopted))
        self.assertEqual(sum(bool(result.get("already")) for result in adopted), 1)
        with self.sessions() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(KnowledgeChunk)), 1,
            )

        rejected = experience.reject_tip(tip_id)
        self.assertTrue(rejected["ok"])
        with self.sessions() as db:
            tip = db.get(ExperienceTip, tip_id)
            self.assertEqual(tip.status, "rejected")
            self.assertIsNone(tip.chunk_id)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(KnowledgeChunk)), 0,
            )

        async def fresh_embedding(_text: str):
            return [0.8, 0.9]

        experience.embed_one = fresh_embedding
        readopted = await experience.adopt_tip(tip_id)
        self.assertTrue(readopted["ok"])
        again = await experience.adopt_tip(tip_id)
        self.assertTrue(again["already"])
        with self.sessions() as db:
            tip = db.get(ExperienceTip, tip_id)
            chunks = db.scalars(select(KnowledgeChunk)).all()
            self.assertEqual(tip.status, "adopted")
            self.assertEqual(len(chunks), 1)
            self.assertEqual(chunks[0].origin_type, "experience_tip")
            self.assertEqual(chunks[0].origin_id, tip_id)

    async def test_generic_delete_uses_provenance_not_reused_chunk_id(self) -> None:
        tip_id = self._new_tip()
        adopted = await experience.adopt_tip(tip_id)
        owned_id = adopted["chunk_id"]
        with self.sessions() as db:
            before = db.get(ExperienceTip, tip_id).version
            knowledge_api.delete_chunk(owned_id, db)
        with self.sessions() as db:
            tip = db.get(ExperienceTip, tip_id)
            self.assertEqual(tip.status, "rejected")
            self.assertEqual(tip.version, before + 1)
            self.assertIsNone(tip.chunk_id)

            product = KnowledgeChunk(
                source="产品事实", kind="doc", category="产品规格", scene="product",
                text="产品阻抗 32 欧姆", embedding="",
            )
            db.add(product)
            db.commit()
            product_id = product.id
            # 模拟旧数据遗留的悬空 chunk_id；SQLite 也可能复用刚删除的 id。
            tip.status = "adopted"
            tip.chunk_id = product_id
            db.commit()
            stale_version = tip.version

        with self.sessions() as db:
            knowledge_api.delete_chunk(product_id, db)
        with self.sessions() as db:
            tip = db.get(ExperienceTip, tip_id)
            self.assertEqual(tip.status, "adopted")
            self.assertEqual(tip.version, stale_version)

        # adopted 但 provenance 已丢失的旧数据可安全重建，不触发唯一约束。
        rebuilt = await experience.adopt_tip(tip_id)
        self.assertTrue(rebuilt["ok"])
        with self.sessions() as db:
            tip = db.get(ExperienceTip, tip_id)
            chunk = db.get(KnowledgeChunk, tip.chunk_id)
            self.assertEqual(chunk.origin_type, "experience_tip")
            self.assertEqual(chunk.origin_id, tip_id)

    async def test_old_schema_migration_backfills_version_and_deduplicates_origin(self) -> None:
        legacy_path = Path(self.tmp.name) / "legacy.db"
        legacy_engine = create_engine(f"sqlite:///{legacy_path}", future=True)
        with legacy_engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE knowledge_chunks ("
                "id INTEGER PRIMARY KEY, category VARCHAR(64) DEFAULT '', "
                "origin_type VARCHAR(32) DEFAULT '', origin_id INTEGER)"
            ))
            conn.execute(text(
                "CREATE TABLE experience_tips ("
                "id INTEGER PRIMARY KEY, status VARCHAR(16), chunk_id INTEGER)"
            ))
            conn.execute(text(
                "CREATE TABLE streamers (id INTEGER PRIMARY KEY, ext_id VARCHAR(64))"
            ))
            conn.execute(text(
                "CREATE TABLE streamer_turns (id INTEGER PRIMARY KEY, streamer_id INTEGER)"
            ))
            # tip 7 的重复 provenance 应优先保留其 chunk_id=11；tip 8 的旧切片
            # 没有 provenance，应依据“本店经验”分类安全回填。
            conn.execute(text(
                "INSERT INTO knowledge_chunks(id, category, origin_type, origin_id) VALUES "
                "(10, '本店经验', 'experience_tip', 7), "
                "(11, '本店经验', 'experience_tip', 7), "
                "(12, '本店经验', '', NULL)"
            ))
            conn.execute(text(
                "INSERT INTO experience_tips(id, status, chunk_id) VALUES "
                "(7, 'adopted', 11), (8, 'adopted', 12)"
            ))

        original_engine = db_module.engine
        db_module.engine = legacy_engine
        try:
            db_module._migrate()
        finally:
            db_module.engine = original_engine

        columns = {column["name"] for column in inspect(legacy_engine).get_columns("experience_tips")}
        self.assertIn("version", columns)
        with legacy_engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, origin_type, origin_id FROM knowledge_chunks ORDER BY id"
            )).all()
        self.assertEqual(rows[0], (10, "", None))
        self.assertEqual(rows[1], (11, "experience_tip", 7))
        self.assertEqual(rows[2], (12, "experience_tip", 8))
        legacy_engine.dispose()


if __name__ == "__main__":
    unittest.main()
