from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core import rhythm_library
from app.core.review import report_data, rhythm_analysis
from app.db import Base
from app.models import (
    CompassPoint,
    Critique,
    CritiqueLevel,
    ExperienceCorpus,
    ReplaySession,
    RhythmTemplate,
    SessionRhythmAnalysis,
    Streamer,
    StreamerTurn,
    Transcript,
)


class FakeRhythmComparisonLLM:
    name = "fake-r3-comparison"

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        self.last_system = system
        self.last_payload = json.loads(user)
        return json.dumps({
            "match_score": 82,
            "summary": "开场定位基本匹配，但福利预告被闲聊拉长。",
            "differences": [
                {
                    "step_order": 1,
                    "actual_block_orders": [1],
                    "actual_duration_sec": 25,
                    "status": "matched",
                    "difference": "欢迎和定位完整。",
                    "action": "保持前 25 秒完成欢迎与定位。",
                },
                {
                    "step_order": 2,
                    # 不存在的块必须被服务端丢弃，时长也不能超过真实证据。
                    "actual_block_orders": [2, 999],
                    "actual_duration_sec": 999,
                    "status": "too_long",
                    "difference": "福利预告拖沓，通用闲聊挤占了目标内容。",
                    "action": "把福利预告控制在 20 秒内并立即引导互动。",
                },
            ],
            "extra_issues": [{
                "type": "pacing",
                "diagnosis": "核心动作出现得太晚。",
                "action": "开场一分钟内给出明确互动指令。",
            }],
            "action_items": ["把福利预告控制在 20 秒内并立即引导互动。"],
        }, ensure_ascii=False)


class RhythmAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "r3.db"
        self.engine = create_engine(f"sqlite:///{db_path}", future=True)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False,
        )

        self.original_rhythm_session = rhythm_analysis.SessionLocal
        self.original_report_session = report_data.SessionLocal
        self.original_library_session = rhythm_library.SessionLocal
        self.original_get_llm = rhythm_analysis.get_llm
        rhythm_analysis.SessionLocal = self.sessions
        report_data.SessionLocal = self.sessions
        rhythm_library.SessionLocal = self.sessions
        self.fake_llm = FakeRhythmComparisonLLM()
        rhythm_analysis.get_llm = lambda _purpose="deep": self.fake_llm

        with self.sessions() as db:
            streamer = Streamer(name="主播甲")
            other_streamer = Streamer(name="主播乙")
            db.add_all([streamer, other_streamer])
            db.flush()
            self.streamer_id = streamer.id
            self.other_streamer_id = other_streamer.id

            session = ReplaySession(
                title="R3 测试场次",
                lineup=json.dumps([self.streamer_id, self.other_streamer_id]),
                current_streamer_id=self.streamer_id,
            )
            db.add(session)
            db.flush()
            self.session_id = session.id

            corpus = ExperienceCorpus(
                source="优秀主播开场课",
                corpus_type="live_skill",
                text="先欢迎定位，再预告福利并引导互动。",
                status="ready",
            )
            db.add(corpus)
            db.flush()
            steps = [
                {
                    "order": 1,
                    "phase": "欢迎定位",
                    "duration_sec": 25,
                    "content": "欢迎新客并说明本场价值",
                    "action": "在 25 秒内说清直播间定位",
                },
                {
                    "order": 2,
                    "phase": "福利预告",
                    "duration_sec": 20,
                    "content": "预告福利并引导互动",
                    "action": "预告福利后让观众扣 1",
                },
            ]
            adopted = RhythmTemplate(
                corpus_id=corpus.id,
                corpus_type="live_skill",
                title="新客开场留人节奏",
                scene="opening",
                summary="欢迎定位后紧接福利钩子",
                applicable_when="新客集中进入",
                goal="提高停留与互动",
                total_duration_sec=45,
                steps=json.dumps(steps, ensure_ascii=False),
                tags=json.dumps(["留人", "互动"], ensure_ascii=False),
                status="adopted",
            )
            pending = RhythmTemplate(
                corpus_id=corpus.id,
                corpus_type="live_skill",
                title="尚未审核的节奏",
                scene="opening",
                total_duration_sec=45,
                steps=json.dumps(steps, ensure_ascii=False),
                status="pending",
            )
            db.add_all([adopted, pending])
            db.flush()
            self.adopted_template_id = adopted.id
            self.pending_template_id = pending.id

            db.add_all([
                Transcript(
                    session_id=self.session_id,
                    streamer_id=self.streamer_id,
                    seq=1,
                    start_ms=0,
                    end_ms=10_000,
                    raw_text="欢迎新来的朋友。",
                    corrected_text="欢迎新来的朋友，今天教大家选耳机。",
                ),
                Transcript(
                    session_id=self.session_id,
                    streamer_id=self.streamer_id,
                    seq=2,
                    start_ms=11_000,
                    end_ms=25_000,
                    raw_text="本场先讲选购方法。",
                ),
                Transcript(
                    session_id=self.session_id,
                    streamer_id=self.streamer_id,
                    seq=3,
                    start_ms=26_000,
                    end_ms=50_000,
                    raw_text="先聊聊我今天的状态。",
                ),
                Transcript(
                    session_id=self.session_id,
                    streamer_id=self.streamer_id,
                    seq=4,
                    start_ms=51_000,
                    end_ms=90_000,
                    raw_text="刚才还有朋友问我吃饭没有。",
                ),
                Transcript(
                    session_id=self.session_id,
                    streamer_id=self.streamer_id,
                    seq=5,
                    start_ms=100_000,
                    end_ms=120_000,
                    raw_text="这款耳机久戴不夹头。",
                ),
                Transcript(
                    session_id=self.session_id,
                    streamer_id=self.other_streamer_id,
                    seq=1,
                    start_ms=30_000,
                    end_ms=40_000,
                    raw_text="另一位主播的内容不得混入。",
                ),
                Critique(
                    session_id=self.session_id,
                    streamer_id=self.streamer_id,
                    level=CritiqueLevel.segment,
                    category="分段点评",
                    start_ms=0,
                    end_ms=25_000,
                    quote="欢迎新来的朋友",
                    data=json.dumps({"scene": "opening", "metrics": {"wpm": 180}}),
                ),
                Critique(
                    session_id=self.session_id,
                    streamer_id=self.streamer_id,
                    level=CritiqueLevel.segment,
                    category="分段点评",
                    start_ms=26_000,
                    end_ms=90_000,
                    quote="先聊聊",
                    data=json.dumps({"scene": "general", "metrics": {"wpm": 120}}),
                ),
                Critique(
                    session_id=self.session_id,
                    streamer_id=self.streamer_id,
                    level=CritiqueLevel.segment,
                    category="分段点评",
                    start_ms=100_000,
                    end_ms=120_000,
                    quote="久戴不夹头",
                    data=json.dumps({"scene": "product", "metrics": {"wpm": 160}}),
                ),
                Critique(
                    session_id=self.session_id,
                    streamer_id=self.streamer_id,
                    level=CritiqueLevel.summary,
                    category="整场总结",
                    data=json.dumps({
                        "existing_section": {"keep": True},
                        "action_items": ["保留原有行动项"],
                        "top_issues": [{"issue": "保留原有问题"}],
                    }, ensure_ascii=False),
                ),
            ])
            db.commit()

    async def asyncTearDown(self) -> None:
        rhythm_analysis.SessionLocal = self.original_rhythm_session
        report_data.SessionLocal = self.original_report_session
        rhythm_library.SessionLocal = self.original_library_session
        rhythm_analysis.get_llm = self.original_get_llm
        self.engine.dispose()
        self.tmp.cleanup()

    def _take_transcripts(self) -> list[dict]:
        """暂存逐字稿，让测试能真实地先登记计划、再产生直播证据。"""
        with self.sessions() as db:
            rows = db.scalars(select(Transcript).where(
                Transcript.session_id == self.session_id,
            ).order_by(Transcript.id)).all()
            snapshots = [{
                "session_id": row.session_id,
                "streamer_id": row.streamer_id,
                "seq": row.seq,
                "start_ms": row.start_ms,
                "end_ms": row.end_ms,
                "raw_text": row.raw_text,
                "corrected_text": row.corrected_text,
            } for row in rows]
            for row in rows:
                db.delete(row)
            db.commit()
        return snapshots

    def _restore_transcripts(self, snapshots: list[dict]) -> None:
        with self.sessions() as db:
            db.add_all(Transcript(**snapshot) for snapshot in snapshots)
            db.commit()

    def test_extracts_ordered_blocks_for_only_the_selected_streamer(self) -> None:
        result = rhythm_analysis.extract_actual_structure(
            self.session_id, self.streamer_id,
        )

        blocks = result["blocks"]
        self.assertEqual([block["order"] for block in blocks], [1, 2, 3])
        self.assertEqual(
            [block["scene"] for block in blocks],
            ["opening", "general", "product"],
        )
        self.assertEqual(
            [(block["start_ms"], block["end_ms"]) for block in blocks],
            [(0, 25_000), (26_000, 90_000), (100_000, 120_000)],
        )
        self.assertEqual([block["duration_sec"] for block in blocks], [25, 64, 20])
        self.assertIn("今天教大家选耳机", blocks[0]["content"])
        self.assertNotIn("另一位主播", " ".join(block["content"] for block in blocks))
        self.assertEqual(result["transcript_count"], 5)
        self.assertGreater(result["general_share_pct"], 50)

    def test_returning_streamer_same_scene_is_split_at_turn_boundary(self) -> None:
        with self.sessions() as db:
            product_segment = db.scalar(select(Critique).where(
                Critique.session_id == self.session_id,
                Critique.streamer_id == self.streamer_id,
                Critique.level == CritiqueLevel.segment,
                Critique.start_ms == 100_000,
            ))
            payload = json.loads(product_segment.data)
            payload["scene"] = "general"
            product_segment.data = json.dumps(payload, ensure_ascii=False)
            db.add_all([
                StreamerTurn(
                    session_id=self.session_id, streamer_id=self.streamer_id,
                    seq=1, start_ms=0, end_ms=95_000,
                ),
                StreamerTurn(
                    session_id=self.session_id, streamer_id=self.other_streamer_id,
                    seq=2, start_ms=95_000, end_ms=100_000,
                ),
                StreamerTurn(
                    session_id=self.session_id, streamer_id=self.streamer_id,
                    seq=3, start_ms=100_000, end_ms=130_000,
                ),
            ])
            db.commit()

        result = rhythm_analysis.extract_actual_structure(
            self.session_id, self.streamer_id,
        )

        self.assertEqual(
            [(block["scene"], block["start_ms"], block["end_ms"])
             for block in result["blocks"]],
            [
                ("opening", 0, 25_000),
                ("general", 26_000, 90_000),
                ("general", 100_000, 120_000),
            ],
        )
        self.assertEqual(result["spoken_duration_sec"], 109)

    async def test_only_adopted_templates_can_be_planned_or_analyzed(self) -> None:
        self._take_transcripts()
        with self.assertRaisesRegex(
            rhythm_analysis.RhythmTemplateNotEligible, "已人工采纳",
        ):
            rhythm_analysis.plan_session_rhythm(
                self.session_id, self.streamer_id, self.pending_template_id,
            )
        with self.assertRaisesRegex(
            rhythm_analysis.RhythmTemplateNotEligible, "已人工采纳",
        ):
            await rhythm_analysis.analyze_session_rhythm(
                self.session_id, self.streamer_id, self.pending_template_id,
            )
        with self.sessions() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(SessionRhythmAnalysis)), 0,
            )

        planned = rhythm_analysis.plan_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        )
        self.assertEqual(planned["status"], "planned")
        self.assertTrue(planned["declared_execution"])
        self.assertEqual(planned["template"]["title"], "新客开场留人节奏")

    def test_cross_streamer_effect_requires_verified_positive_streamers(self) -> None:
        with self.sessions() as db:
            for index in range(4):
                owner = self.streamer_id if index < 3 else self.other_streamer_id
                session = ReplaySession(
                    title=f"验证样本 {index}", lineup=json.dumps([owner]),
                    current_streamer_id=owner,
                )
                db.add(session)
                db.flush()
                db.add(SessionRhythmAnalysis(
                    session_id=session.id,
                    streamer_id=owner,
                    template_id=self.adopted_template_id,
                    selection_mode="manual",
                    declared_execution=True,
                    declared_at=datetime.now(),
                    status="ready",
                    effective=True if index < 3 else None,
                ))
            db.commit()
            service_stats = rhythm_analysis._template_stats_map(
                db, [self.adopted_template_id],
            )[self.adopted_template_id]
            library_stats = rhythm_library._template_validation_map(
                db, [self.adopted_template_id],
            )[self.adopted_template_id]

        self.assertEqual(service_stats["streamer_count"], 2)
        self.assertEqual(service_stats["verified_streamer_count"], 1)
        self.assertEqual(service_stats["effective_streamer_count"], 1)
        self.assertEqual(library_stats["verified_count"], 3)
        self.assertEqual(library_stats["verified_streamer_count"], 1)
        self.assertEqual(library_stats["status"], "observing")

    async def test_analysis_cannot_retroactively_declare_execution(self) -> None:
        with self.assertRaisesRegex(
            rhythm_analysis.RhythmExecutionPlanError, "不能在复盘分析时追认执行",
        ):
            await rhythm_analysis.analyze_session_rhythm(
                self.session_id,
                self.streamer_id,
                self.adopted_template_id,
                declare_execution=True,
            )

        with self.sessions() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(SessionRhythmAnalysis)), 0,
            )

    async def test_comparison_is_persisted_and_merged_into_the_main_report(self) -> None:
        result = await rhythm_analysis.analyze_session_rhythm(
            self.session_id,
            self.streamer_id,
            self.adopted_template_id,
        )

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["selection_mode"], "manual")
        # LLM 声称 82 分不可信；服务端按 100/62 两个确定性状态取均值，
        # 再扣除通用内容 58.7% 的超额占比，最终固定为 67.5。
        self.assertEqual(result["match_score"], 67.5)
        self.assertEqual(result["validation"]["status"], "unverified")
        self.assertIsNone(result["effective"])
        comparison = result["comparison"]
        self.assertEqual(len(comparison["differences"]), 2)
        second = comparison["differences"][1]
        self.assertEqual(second["actual_block_orders"], [2])
        self.assertEqual(second["actual_duration_sec"], 64)
        self.assertEqual(second["evidence"][0]["time_range"], "00:26-01:30")
        self.assertTrue(any(
            issue["type"] == "generic_overload"
            for issue in comparison["extra_issues"]
        ))

        with self.sessions() as db:
            row = db.scalar(select(SessionRhythmAnalysis))
            self.assertEqual(row.status, "ready")
            self.assertEqual(json.loads(row.comparison)["match_score"], 67.5)
            summary = db.scalar(select(Critique).where(
                Critique.level == CritiqueLevel.summary,
                Critique.category == "整场总结",
            ))
            data = json.loads(summary.data)
        self.assertEqual(data["existing_section"], {"keep": True})
        self.assertEqual(data["rhythm_analysis"]["id"], result["id"])
        self.assertIn("保留原有行动项", data["action_items"])
        self.assertIn(
            {
                "action": "把福利预告控制在 20 秒内并立即引导互动。",
                "source": "rhythm_analysis",
            },
            data["action_items"],
        )
        self.assertTrue(any(
            isinstance(issue, dict)
            and issue.get("source") == "rhythm_analysis"
            and issue.get("issue", "").startswith("节奏对比：")
            for issue in data["top_issues"]
        ))

    async def test_declared_execution_uses_compass_data_to_mark_template_effective(self) -> None:
        transcripts = self._take_transcripts()
        planned = rhythm_analysis.plan_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        )
        self.assertTrue(planned["declared_execution"])
        self.assertIsNotNone(planned["declared_at"])
        self._restore_transcripts(transcripts)

        with self.sessions() as db:
            db.add_all([
                CompassPoint(
                    session_id=self.session_id,
                    start_ms=0,
                    data=json.dumps({"成交件数": 1}, ensure_ascii=False),
                ),
                CompassPoint(
                    session_id=self.session_id,
                    start_ms=60_000,
                    data=json.dumps({"成交件数": 2}, ensure_ascii=False),
                ),
                CompassPoint(
                    session_id=self.session_id,
                    start_ms=120_000,
                    data=json.dumps({"成交件数": 3}, ensure_ascii=False),
                ),
            ])
            db.commit()

        result = await rhythm_analysis.analyze_session_rhythm(
            self.session_id,
            self.streamer_id,
            self.adopted_template_id,
        )

        validation = result["validation"]
        self.assertEqual(validation["status"], "effective")
        self.assertEqual(validation["verdict"], "本场验证有效")
        self.assertEqual(validation["positive_count"], 2)
        self.assertEqual(validation["negative_count"], 0)
        self.assertEqual(validation["effect_score"], 100)
        self.assertTrue(result["effective"])
        self.assertTrue(all(item["beneficial"] for item in validation["evidence"]))
        self.assertEqual(validation["template_stats"]["trial_count"], 1)
        self.assertEqual(validation["template_stats"]["verified_count"], 1)
        self.assertEqual(validation["template_stats"]["effective_count"], 1)
        self.assertEqual(validation["template_stats"]["effective_rate"], 100)

    async def test_rejecting_template_immediately_invalidates_declared_effect(self) -> None:
        transcripts = self._take_transcripts()
        rhythm_analysis.plan_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        )
        self._restore_transcripts(transcripts)
        with self.sessions() as db:
            db.add_all([
                CompassPoint(
                    session_id=self.session_id, start_ms=0,
                    data=json.dumps({"成交件数": 1}, ensure_ascii=False),
                ),
                CompassPoint(
                    session_id=self.session_id, start_ms=60_000,
                    data=json.dumps({"成交件数": 2}, ensure_ascii=False),
                ),
                CompassPoint(
                    session_id=self.session_id, start_ms=120_000,
                    data=json.dumps({"成交件数": 3}, ensure_ascii=False),
                ),
            ])
            db.commit()
        before = await rhythm_analysis.analyze_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        )
        self.assertTrue(before["effective"])

        rhythm_library.reject_template(self.adopted_template_id)

        with self.sessions() as db:
            template = db.get(RhythmTemplate, self.adopted_template_id)
            row = db.scalar(select(SessionRhythmAnalysis))
            summary = db.scalar(select(Critique).where(
                Critique.session_id == self.session_id,
                Critique.streamer_id == self.streamer_id,
                Critique.level == CritiqueLevel.summary,
                Critique.category == "整场总结",
            ))
            report = json.loads(summary.data)
        self.assertEqual(template.status, "rejected")
        self.assertEqual(row.status, "failed")
        self.assertIsNone(row.effective)
        self.assertIsNone(row.effect_score)
        self.assertIn("停止效果归因", row.error_msg)
        self.assertEqual(json.loads(row.validation)["status"], "unverified")
        self.assertEqual(json.loads(row.template_snapshot), before["template"])
        self.assertEqual(report["rhythm_analysis"]["status"], "failed")
        self.assertIsNone(report["rhythm_analysis"]["effective"])
        self.assertEqual(
            report["rhythm_analysis"]["validation"]["status"], "unverified",
        )
        self.assertFalse(any(
            isinstance(item, dict) and item.get("source") == "rhythm_analysis"
            for item in report.get("action_items", []) + report.get("top_issues", [])
        ))
        with self.assertRaises(rhythm_analysis.RhythmExecutionPlanError):
            rhythm_analysis.refresh_validation(self.session_id, self.streamer_id)

    async def test_blank_transcripts_and_fake_llm_score_cannot_validate_execution(self) -> None:
        self._take_transcripts()
        rhythm_analysis.plan_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        )
        with self.sessions() as db:
            db.add_all([
                Transcript(
                    session_id=self.session_id,
                    streamer_id=self.streamer_id,
                    seq=1,
                    start_ms=0,
                    end_ms=30_000,
                    raw_text="   ",
                    corrected_text="\n",
                ),
                CompassPoint(
                    session_id=self.session_id,
                    start_ms=0,
                    data=json.dumps({"成交件数": 1}, ensure_ascii=False),
                ),
                CompassPoint(
                    session_id=self.session_id,
                    start_ms=60_000,
                    data=json.dumps({"成交件数": 2}, ensure_ascii=False),
                ),
                CompassPoint(
                    session_id=self.session_id,
                    start_ms=120_000,
                    data=json.dumps({"成交件数": 3}, ensure_ascii=False),
                ),
            ])
            db.commit()

        extracted = rhythm_analysis.extract_actual_structure(
            self.session_id, self.streamer_id,
        )
        self.assertEqual(extracted["blocks"], [])
        self.assertEqual(extracted["transcript_count"], 0)
        self.assertEqual(extracted["span_sec"], 0)

        result = await rhythm_analysis.analyze_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        )

        # Fake LLM 声称 82 分且两个步骤达标/超时，但没有任何真实块号可作证。
        self.assertEqual(self.fake_llm.last_payload["actual"]["blocks"], [])
        self.assertEqual(result["match_score"], 0)
        self.assertTrue(all(
            difference["status"] == "missing"
            and difference["evidence"] == []
            for difference in result["comparison"]["differences"]
        ))
        self.assertEqual(result["validation"]["status"], "not_followed")
        self.assertEqual(result["validation"]["verdict"], "缺少真实执行证据")
        self.assertIsNone(result["effective"])

    async def test_concurrent_plan_supersedes_a_stale_analysis_result(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingComparisonLLM(FakeRhythmComparisonLLM):
            async def complete(
                self, system: str, user: str, *, json_mode: bool = False,
            ) -> str:
                started.set()
                await release.wait()
                return await super().complete(system, user, json_mode=json_mode)

        blocking_llm = BlockingComparisonLLM()
        rhythm_analysis.get_llm = lambda _purpose="deep": blocking_llm
        with self.sessions() as db:
            session = db.get(ReplaySession, self.session_id)
            session.started_at = datetime.now()
            db.commit()
        stale_task = asyncio.create_task(rhythm_analysis.analyze_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        ))
        await asyncio.wait_for(started.wait(), timeout=2)

        try:
            planned = rhythm_analysis.plan_session_rhythm(
                self.session_id, self.streamer_id, self.adopted_template_id,
            )
        finally:
            release.set()
        self.assertEqual(planned["status"], "planned")
        self.assertTrue(planned["declared_execution"])

        with self.assertRaises(rhythm_analysis.RhythmAnalysisSuperseded):
            await asyncio.wait_for(stale_task, timeout=2)
        with self.sessions() as db:
            row = db.scalar(select(SessionRhythmAnalysis))
            self.assertEqual(row.status, "planned")
            self.assertTrue(row.declared_execution)
            self.assertEqual(json.loads(row.comparison), {})
            self.assertIsNone(row.match_score)

    async def test_deleted_declared_template_preserves_snapshot_and_stops_attribution(self) -> None:
        transcripts = self._take_transcripts()
        planned = rhythm_analysis.plan_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        )
        original_snapshot = planned["template"]
        with self.sessions() as db:
            template = db.get(RhythmTemplate, self.adopted_template_id)
            db.delete(template)
            db.commit()
        self._restore_transcripts(transcripts)

        with self.assertRaisesRegex(
            rhythm_analysis.RhythmTemplateNotEligible, "保留登记时的模板快照",
        ):
            await rhythm_analysis.analyze_session_rhythm(
                self.session_id, self.streamer_id,
            )

        with self.sessions() as db:
            row = db.scalar(select(SessionRhythmAnalysis))
            summary = db.scalar(select(Critique).where(
                Critique.session_id == self.session_id,
                Critique.streamer_id == self.streamer_id,
                Critique.level == CritiqueLevel.summary,
                Critique.category == "整场总结",
            ))
            report_r3 = json.loads(summary.data)["rhythm_analysis"]
        self.assertIsNone(row.template_id)
        self.assertTrue(row.declared_execution)
        self.assertEqual(row.status, "failed")
        self.assertEqual(json.loads(row.template_snapshot), original_snapshot)
        self.assertEqual(json.loads(row.validation)["status"], "unverified")
        self.assertIsNone(row.effective)
        self.assertEqual(report_r3["template"], original_snapshot)
        self.assertEqual(report_r3["status"], "failed")

    async def test_failed_rerun_keeps_the_previous_ready_payload(self) -> None:
        first = await rhythm_analysis.analyze_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        )
        with mock.patch.object(
            rhythm_analysis,
            "extract_actual_structure",
            side_effect=RuntimeError("forced extraction failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced extraction failure"):
                await rhythm_analysis.analyze_session_rhythm(
                    self.session_id, self.streamer_id, self.adopted_template_id,
                )

        with self.sessions() as db:
            row = db.scalar(select(SessionRhythmAnalysis))
        self.assertEqual(row.status, "ready")
        self.assertEqual(json.loads(row.extracted), first["actual"])
        self.assertEqual(json.loads(row.comparison), first["comparison"])
        self.assertEqual(json.loads(row.template_snapshot), first["template"])
        self.assertEqual(row.match_score, first["match_score"])
        self.assertIn("继续显示上次结果", row.error_msg)

    async def test_rerun_replaces_only_prior_r3_report_items(self) -> None:
        first = await rhythm_analysis.analyze_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        )
        self.assertEqual(first["status"], "ready")

        class SecondPassLLM:
            name = "fake-r3-second-pass"

            async def complete(
                self, _system: str, _user: str, *, json_mode: bool = False,
            ) -> str:
                return json.dumps({
                    "match_score": 100,
                    "summary": "第二轮重新对齐。",
                    "differences": [
                        {
                            "step_order": 1,
                            "actual_block_orders": [1],
                            "actual_duration_sec": 25,
                            "status": "matched",
                            "difference": "第二轮开场达标。",
                            "action": "保持开场。",
                        },
                        {
                            "step_order": 2,
                            "actual_block_orders": [3],
                            "actual_duration_sec": 20,
                            "status": "wrong_content",
                            "difference": "第二轮发现内容偏题。",
                            "action": "第二轮新动作。",
                        },
                    ],
                    "extra_issues": [],
                    "action_items": ["第二轮新动作。"],
                }, ensure_ascii=False)

        rhythm_analysis.get_llm = lambda _purpose="deep": SecondPassLLM()
        second = await rhythm_analysis.analyze_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        )
        self.assertEqual(second["comparison"]["summary"], "第二轮重新对齐。")

        with self.sessions() as db:
            summary = db.scalar(select(Critique).where(
                Critique.session_id == self.session_id,
                Critique.streamer_id == self.streamer_id,
                Critique.level == CritiqueLevel.summary,
                Critique.category == "整场总结",
            ))
            data = json.loads(summary.data)

        self.assertIn("保留原有行动项", data["action_items"])
        self.assertIn({"issue": "保留原有问题"}, data["top_issues"])
        r3_actions = [
            item for item in data["action_items"]
            if isinstance(item, dict) and item.get("source") == "rhythm_analysis"
        ]
        r3_issues = [
            item for item in data["top_issues"]
            if isinstance(item, dict) and item.get("source") == "rhythm_analysis"
        ]
        self.assertTrue(any(item.get("action") == "第二轮新动作。" for item in r3_actions))
        self.assertFalse(any(
            "福利预告控制" in item.get("action", "") for item in r3_actions
        ))
        self.assertEqual(
            [item["issue"] for item in r3_issues],
            ["节奏对比：第二轮发现内容偏题。"],
        )

    async def test_validation_window_stops_at_the_next_streamer_turn(self) -> None:
        transcripts = self._take_transcripts()
        rhythm_analysis.plan_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        )
        self._restore_transcripts(transcripts)

        with self.sessions() as db:
            db.add_all([
                StreamerTurn(
                    session_id=self.session_id,
                    streamer_id=self.streamer_id,
                    seq=1,
                    start_ms=0,
                    end_ms=100_000,
                ),
                StreamerTurn(
                    session_id=self.session_id,
                    streamer_id=self.other_streamer_id,
                    seq=2,
                    start_ms=100_000,
                    end_ms=300_000,
                ),
                CompassPoint(
                    session_id=self.session_id,
                    start_ms=0,
                    data=json.dumps({"成交件数": 1}, ensure_ascii=False),
                ),
                CompassPoint(
                    session_id=self.session_id,
                    start_ms=60_000,
                    data=json.dumps({"成交件数": 2}, ensure_ascii=False),
                ),
                CompassPoint(
                    session_id=self.session_id,
                    start_ms=120_000,
                    data=json.dumps({"成交件数": 3}, ensure_ascii=False),
                ),
                CompassPoint(
                    session_id=self.session_id,
                    start_ms=180_000,
                    data=json.dumps({"成交件数": 4}, ensure_ascii=False),
                ),
            ])
            db.commit()
        result = await rhythm_analysis.analyze_session_rhythm(
            self.session_id, self.streamer_id, self.adopted_template_id,
        )

        validation = result["validation"]
        self.assertEqual(validation["status"], "mixed")
        self.assertIsNone(result["effective"])
        self.assertEqual(validation["positive_count"], 1)
        self.assertEqual([item["at_ms"] for item in validation["evidence"]], [60_000])
        self.assertEqual(validation["windows"][0]["end_ms"], 100_000)
        self.assertTrue(all(
            item["at_ms"] <= 100_000 for item in validation["evidence"]
        ))


if __name__ == "__main__":
    unittest.main()
