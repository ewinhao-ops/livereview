from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import sessionmaker

from app.api import knowledge as knowledge_api
from app.api import rhythm_library as rhythm_api
from app.core import embed, rhythm_library
from app.db import Base
from app.models import ExperienceCorpus, KnowledgeChunk, RhythmTemplate


class FakeRhythmLLM:
    name = "fake-rhythm"

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        assert "话术节奏模板" in system
        assert "<corpus>" in user
        return json.dumps({
            "templates": [{
                "title": "新客进场留人节奏",
                "scene": "开场留人",
                "summary": "先给停留理由，再引导互动",
                "applicable_when": "新客集中进入且停留不稳定时",
                "audience": "新客占比较高的直播间",
                "goal": "完成首轮留人和互动",
                "tags": ["留人", "互动"],
                "source_excerpt": "欢迎新来的朋友",
                "steps": [
                    {
                        "phase": "欢迎定位",
                        "duration_sec": 10,
                        "duration_basis": "source",
                        "content": "欢迎新客并说明本场主题",
                        "trigger": "看到新客进入",
                        "action": "快速点明直播间价值",
                        "example_script": "欢迎新来的朋友，今天专门解决选耳机的问题。",
                    },
                    {
                        "phase": "福利预告",
                        "duration_sec": 20,
                        "content": "预告后续福利并引导扣1",
                        "trigger": "在线人数开始波动",
                        "action": "给出明确停留理由和互动指令",
                        "example_script": "想看对比的先扣1，稍后给大家现场讲。",
                    },
                ],
            }]
        }, ensure_ascii=False)


class EmptyLLM:
    name = "empty"

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        return "{}"


class RhythmLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "rhythm.db"
        self.engine = create_engine(f"sqlite:///{db_path}", future=True)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False,
        )
        self.original_session = rhythm_library.SessionLocal
        self.original_get_llm = rhythm_library.get_llm
        self.original_embed_one = rhythm_library.embed_one
        self.original_embed_session = embed.SessionLocal
        self.original_embed_one_for_retrieve = embed.embed_one
        rhythm_library.SessionLocal = self.sessions
        rhythm_library.get_llm = lambda _purpose="deep": FakeRhythmLLM()

        async def fake_embedding(_text: str):
            return [0.1, 0.2, 0.3]

        rhythm_library.embed_one = fake_embedding
        embed.SessionLocal = self.sessions

    async def asyncTearDown(self) -> None:
        rhythm_library.SessionLocal = self.original_session
        rhythm_library.get_llm = self.original_get_llm
        rhythm_library.embed_one = self.original_embed_one
        embed.SessionLocal = self.original_embed_session
        embed.embed_one = self.original_embed_one_for_retrieve
        self.engine.dispose()
        self.tmp.cleanup()

    async def test_ingest_review_adopt_and_retract(self) -> None:
        source_text = (
            "欢迎新来的朋友。前十秒说明今天讲什么；看到在线人数波动时，"
            "马上预告福利，并请想看型号对比的观众扣1。"
        )
        result = await rhythm_library.ingest_corpus(
            "优秀主播开场课",
            "live_skill",
            source_text,
        )
        self.assertEqual(result["corpus"]["status"], "ready")
        self.assertEqual(result["corpus"]["template_count"], 1)
        template = result["templates"][0]
        self.assertEqual(template["status"], "pending")
        self.assertEqual(template["scene"], "opening")
        self.assertEqual(template["total_duration_sec"], 30)
        self.assertEqual(
            [(step["start_sec"], step["end_sec"]) for step in template["steps"]],
            [(0, 10), (10, 30)],
        )
        self.assertEqual(template["steps"][1]["duration_basis"], "estimated")
        duplicate = await rhythm_library.ingest_corpus("同内容重复提交", "live_skill", source_text)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["corpus"]["id"], result["corpus"]["id"])

        with self.sessions() as db:
            chunks = db.scalar(select(func.count()).select_from(KnowledgeChunk))
            self.assertEqual(chunks, 0, "待审核模板不得提前污染正式检索库")

        adopted = await rhythm_library.adopt_template(template["id"])
        self.assertTrue(adopted["ok"])
        self.assertEqual(adopted["template"]["status"], "adopted")
        self.assertTrue(adopted["template"]["indexed"])
        with self.sessions() as db:
            chunk = db.scalar(select(KnowledgeChunk))
            self.assertEqual(chunk.category, "优秀节奏")
            self.assertIn("外部精选参考", chunk.text)
            self.assertIn("0-10秒", chunk.text)

        rejected = rhythm_library.reject_template(template["id"])
        self.assertEqual(rejected["template"]["status"], "rejected")
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(KnowledgeChunk)), 0)

    async def test_operations_type_failure_is_preserved_and_retryable(self) -> None:
        rhythm_library.get_llm = lambda _purpose="deep": EmptyLLM()
        result = await rhythm_library.ingest_corpus(
            "运营课程逐字稿",
            "operations",
            "当在线人数下降时先检查讲解是否过长，再根据评论区问题切换到互动答疑，"
            "不要没有依据地承诺优惠。",
        )
        self.assertEqual(result["corpus"]["status"], "failed")
        self.assertIn("未返回", result["corpus"]["error_msg"])
        with self.sessions() as db:
            corpus = db.scalar(select(ExperienceCorpus))
            self.assertEqual(corpus.corpus_type, "operations")
            self.assertTrue(corpus.text)
            self.assertEqual(db.scalar(select(func.count()).select_from(RhythmTemplate)), 0)

        rhythm_library.get_llm = lambda _purpose="deep": FakeRhythmLLM()
        retried = await rhythm_library.retry_corpus(result["corpus"]["id"])
        self.assertEqual(retried["corpus"]["status"], "ready")
        self.assertEqual(retried["templates"][0]["corpus_type_label"], "运营经验")

    async def test_delete_corpus_removes_templates_and_adopted_chunk(self) -> None:
        result = await rhythm_library.ingest_corpus(
            "可删除样本",
            "live_skill",
            "欢迎新来的朋友，先讲直播间能解决什么问题，再给出互动指令和后续内容预告。",
        )
        template_id = result["templates"][0]["id"]
        await rhythm_library.adopt_template(template_id)
        self.assertTrue(rhythm_library.delete_corpus(result["corpus"]["id"]))
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ExperienceCorpus)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(RhythmTemplate)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(KnowledgeChunk)), 0)

    async def test_external_chunk_delete_cannot_rebind_or_delete_product_chunk(self) -> None:
        result = await rhythm_library.ingest_corpus(
            "ID 复用样本", "live_skill",
            "欢迎新来的朋友，先点明直播价值，再根据在线变化预告福利并引导互动。",
        )
        template_id = result["templates"][0]["id"]
        await rhythm_library.adopt_template(template_id)
        with self.sessions() as db:
            template = db.get(RhythmTemplate, template_id)
            old_chunk_id = template.chunk_id
            knowledge_api.delete_chunk(old_chunk_id, db)
        with self.sessions() as db:
            template = db.get(RhythmTemplate, template_id)
            self.assertEqual(template.status, "rejected")
            self.assertIsNone(template.chunk_id)
            product = KnowledgeChunk(
                source="产品事实", kind="doc", category="产品规格", scene="product",
                text="产品阻抗32欧姆", embedding="",
            )
            db.add(product)
            db.commit()
            product_id = product.id
        # SQLite 可以复用刚删除的 id；撤回模板仍只能按 provenance 操作，不能删该产品片段。
        self.assertEqual(product_id, old_chunk_id)
        rhythm_library.reject_template(template_id)
        with self.sessions() as db:
            self.assertIsNotNone(db.get(KnowledgeChunk, product_id))

    async def test_reject_during_embedding_cancels_stale_adoption(self) -> None:
        result = await rhythm_library.ingest_corpus(
            "并发审核样本", "live_skill",
            "欢迎新来的朋友，先给出停留理由；在线波动时预告福利，再明确要求观众互动。",
        )
        template_id = result["templates"][0]["id"]
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed_embedding(_text: str):
            started.set()
            await release.wait()
            return [0.1, 0.2]

        rhythm_library.embed_one = delayed_embedding
        task = asyncio.create_task(rhythm_library.adopt_template(template_id))
        await started.wait()
        rhythm_library.reject_template(template_id)
        release.set()
        with self.assertRaises(rhythm_library.TemplateStateError):
            await task
        with self.sessions() as db:
            template = db.get(RhythmTemplate, template_id)
            self.assertEqual(template.status, "rejected")
            self.assertEqual(db.scalar(select(func.count()).select_from(KnowledgeChunk)), 0)

    async def test_concurrent_adopt_is_idempotent(self) -> None:
        result = await rhythm_library.ingest_corpus(
            "并发采纳样本", "live_skill",
            "欢迎新来的朋友，先说明直播价值；在线变化时预告后续内容，并要求观众互动。",
        )
        template_id = result["templates"][0]["id"]
        ready, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def barrier_embedding(_text: str):
            nonlocal calls
            calls += 1
            if calls == 2:
                ready.set()
            await release.wait()
            return [0.3, 0.4]

        rhythm_library.embed_one = barrier_embedding
        first = asyncio.create_task(rhythm_library.adopt_template(template_id))
        second = asyncio.create_task(rhythm_library.adopt_template(template_id))
        await ready.wait()
        release.set()
        results = await asyncio.gather(first, second)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(sum(bool(result.get("already")) for result in results), 1)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(KnowledgeChunk)), 1)

    async def test_processing_lease_discards_stale_result_and_recovers(self) -> None:
        started, release = asyncio.Event(), asyncio.Event()

        class BlockingLLM(FakeRhythmLLM):
            async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
                started.set()
                await release.wait()
                return await super().complete(system, user, json_mode=json_mode)

        rhythm_library.get_llm = lambda _purpose="deep": BlockingLLM()
        with self.sessions() as db:
            corpus = ExperienceCorpus(
                source="租约样本", corpus_type="live_skill",
                text="欢迎新来的朋友，先说明直播价值，再预告福利并引导观众互动。",
                status="processing", processing_run_id="old-run",
                processing_started_at=datetime.now(), processing_heartbeat_at=datetime.now(),
            )
            db.add(corpus)
            db.commit()
            corpus_id = corpus.id

        old_task = asyncio.create_task(rhythm_library._process_corpus(corpus_id, "old-run"))
        await started.wait()
        with self.sessions() as db:
            corpus = db.get(ExperienceCorpus, corpus_id)
            corpus.processing_run_id = "new-run"
            corpus.processing_heartbeat_at = datetime.now()
            db.commit()
        release.set()
        with self.assertRaises(rhythm_library.CorpusProcessingStateError):
            await old_task
        with self.sessions() as db:
            corpus = db.get(ExperienceCorpus, corpus_id)
            self.assertEqual(corpus.status, "processing")
            self.assertEqual(corpus.processing_run_id, "new-run")
            self.assertEqual(db.scalar(select(func.count()).select_from(RhythmTemplate)), 0)
            corpus.processing_heartbeat_at = datetime.now() - timedelta(minutes=31)
            db.commit()

        self.assertEqual(rhythm_library.recover_stuck_corpora(), 1)
        with self.sessions() as db:
            corpus = db.get(ExperienceCorpus, corpus_id)
            self.assertEqual(corpus.status, "failed")
            self.assertEqual(corpus.processing_run_id, "")

    async def test_retry_claim_is_atomic(self) -> None:
        started, release = asyncio.Event(), asyncio.Event()

        class BlockingLLM(FakeRhythmLLM):
            async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
                started.set()
                await release.wait()
                return await super().complete(system, user, json_mode=json_mode)

        with self.sessions() as db:
            corpus = ExperienceCorpus(
                source="重试竞态样本", corpus_type="operations",
                text="欢迎新来的朋友，先给停留理由，再根据在线波动预告福利并引导互动。",
                status="failed", error_msg="上次失败",
            )
            db.add(corpus)
            db.commit()
            corpus_id = corpus.id
        rhythm_library.get_llm = lambda _purpose="deep": BlockingLLM()
        first = asyncio.create_task(rhythm_library.retry_corpus(corpus_id))
        await started.wait()
        with self.assertRaises(ValueError):
            await rhythm_library.retry_corpus(corpus_id)
        release.set()
        result = await first
        self.assertEqual(result["corpus"]["status"], "ready")

    async def test_rag_category_filters_keep_experience_out_of_fact_recall(self) -> None:
        with self.sessions() as db:
            db.add(KnowledgeChunk(
                source="产品表", kind="doc", category="产品规格", scene="product",
                text="这款耳机阻抗为32欧姆", embedding="",
            ))
            corpus = ExperienceCorpus(
                source="外部课程", corpus_type="live_skill", text="审核过的来源文本", status="ready",
            )
            db.add(corpus)
            db.flush()
            template = RhythmTemplate(
                corpus_id=corpus.id, corpus_type="live_skill", title="留人", scene="opening",
                steps="[]", status="adopted",
            )
            db.add(template)
            db.flush()
            rhythm = KnowledgeChunk(
                source="外部课程", kind="distilled", category="优秀节奏", scene="opening",
                origin_type="rhythm_template", origin_id=template.id,
                text="欢迎新客后用福利预告留人", embedding="",
            )
            # 即使普通入口伪造同名 category，没有审核 provenance 也不能进入优秀节奏召回。
            rogue = KnowledgeChunk(
                source="伪造分类", kind="distilled", category="优秀节奏", scene="opening",
                text="欢迎新客后执行未经审核的动作", embedding="",
            )
            db.add_all([rhythm, rogue])
            db.flush()
            template.chunk_id = rhythm.id
            db.commit()

        async def no_vector(_text: str):
            return None

        embed.embed_one = no_vector
        facts = await embed.retrieve_detailed("这款耳机阻抗是多少", exclude_category="优秀节奏")
        refs = await embed.retrieve_detailed("欢迎新客", category="优秀节奏")
        self.assertEqual([row["source"] for row in facts], ["产品表"])
        self.assertEqual([row["source"] for row in refs], ["外部课程"])

        async def query_vector(_text: str):
            return [1.0, 0.0]

        embed.embed_one = query_vector
        unrelated = await embed.retrieve_detailed("今天天气怎么样")
        self.assertEqual(unrelated, [], "有 query 向量时也不能让无索引切片靠固定加权随机补位")

    def test_validation_rejects_non_textual_or_invalid_corpus(self) -> None:
        tables = set(inspect(self.engine).get_table_names())
        self.assertTrue({"experience_corpora", "rhythm_templates"}.issubset(tables))
        with self.assertRaises(ValueError):
            rhythm_library.validate_corpus("product", "这是一段足够长但类型错误的测试语料内容。")
        with self.assertRaises(ValueError):
            rhythm_library.validate_corpus("live_skill", "太短")
        incomplete = rhythm_library._normalise_template(
            {"applicable_when": "有人进场", "steps": [{"content": "只讲内容"}]},
            "有人进场时讲欢迎语。",
        )
        self.assertIsNone(incomplete)
        duration = rhythm_library._normalise_template({
            "applicable_when": "有人进场",
            "steps": [{
                "content": "欢迎新客", "action": "说明直播价值", "duration_sec": 15,
                "duration_basis": "source",
            }],
        }, "有人进场时先欢迎，再说明直播价值。")
        self.assertEqual(duration["steps"][0]["duration_basis"], "estimated")

    async def test_dedicated_upload_rejects_audio_and_accepts_text(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await rhythm_api.upload_text_document(
                file=UploadFile(filename="course.mp4", file=BytesIO(b"video")),
                corpus_type="live_skill",
                source="",
            )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("不接收音视频", raised.exception.detail)

        with self.assertRaises(HTTPException) as malformed:
            await rhythm_api.upload_text_document(
                file=UploadFile(filename="broken.docx", file=BytesIO(b"not-a-zip")),
                corpus_type="live_skill",
                source="",
            )
        self.assertEqual(malformed.exception.status_code, 400)

        with self.assertRaises(HTTPException) as renamed_media:
            await rhythm_api.upload_text_document(
                file=UploadFile(
                    filename="renamed-video.txt",
                    file=BytesIO(b"\x00\x00\x00\x18ftypmp42\x00\x01\x02\x03"),
                ),
                corpus_type="live_skill",
                source="",
            )
        self.assertEqual(renamed_media.exception.status_code, 400)

        result = await rhythm_api.upload_text_document(
            file=UploadFile(
                filename="course.txt",
                file=BytesIO("欢迎新来的朋友，先给停留理由，再预告福利并引导观众互动。".encode()),
            ),
            corpus_type="operations",
            source="运营课",
        )
        self.assertEqual(result["corpus"]["corpus_type"], "operations")


if __name__ == "__main__":
    unittest.main()
