from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core import compass_review
from app.core.compass_review import detect_changes
from app.core.review import experience
from app.db import Base
from app.models import (
    ExperienceTip,
    ExperienceTipSample,
    CompassPoint,
    KnowledgeChunk,
    ReplaySession,
    SystemSetting,
    Transcript,
)


class CompassStatisticalSamplingTests(unittest.TestCase):
    def test_statistical_detection_is_not_limited_by_display_top_three(self) -> None:
        values = [100, 150, 225, 340, 510, 770]
        points = [
            SimpleNamespace(
                start_ms=index * 300_001,
                data=json.dumps({"实时在线人数": value}, ensure_ascii=False),
            )
            for index, value in enumerate(values)
        ]

        displayed = detect_changes(points)
        sampled = detect_changes(
            points, max_per_metric=None, max_sales_events=None,
        )

        self.assertEqual(len(displayed), 3)
        self.assertEqual(len(sampled), 5)


class CompassReviewAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "compass-review.db"
        self.engine = create_engine(f"sqlite:///{db_path}", future=True)
        self.sessions = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False,
        )
        Base.metadata.create_all(self.engine)
        with self.sessions() as db:
            db.add(ReplaySession(id=1, title="归因测试"))
            db.add_all([
                CompassPoint(
                    session_id=1, start_ms=0,
                    data=json.dumps({"实时在线人数": 100}, ensure_ascii=False),
                ),
                CompassPoint(
                    session_id=1, start_ms=300_001,
                    data=json.dumps({"实时在线人数": 150}, ensure_ascii=False),
                ),
                Transcript(
                    session_id=1, seq=1, start_ms=280_000, end_ms=290_000,
                    raw_text="这款佩戴很舒服，但只是普通产品介绍。",
                ),
            ])
            db.commit()

        self.original_session = compass_review.SessionLocal
        self.original_get_llm = compass_review.get_llm
        self.original_experience_session = experience.SessionLocal
        compass_review.SessionLocal = self.sessions
        experience.SessionLocal = self.sessions

        class FakeLLM:
            async def complete(self, _system, _user, json_mode=False):
                return json.dumps({
                    "items": [{
                        "idx": 0,
                        "verdict": "动作驱动",
                        "action": "普通产品介绍",
                        "quote": "这款佩戴很舒服",
                        "reusable": False,
                        "tip_action": "不应保存",
                        "tip_template": "不应保存",
                    }],
                }, ensure_ascii=False)

        compass_review.get_llm = lambda _tier: FakeLLM()

    async def asyncTearDown(self) -> None:
        compass_review.SessionLocal = self.original_session
        compass_review.get_llm = self.original_get_llm
        experience.SessionLocal = self.original_experience_session
        self.engine.dispose()
        self.tmp.cleanup()

    async def test_non_reusable_action_is_explicitly_excluded_from_tip_clustering(self) -> None:
        result = await compass_review.analyze_compass_review(1)

        self.assertEqual(len(result["samples"]), 1)
        self.assertFalse(result["samples"][0]["reusable"])
        self.assertEqual(result["samples"][0]["tip_action"], "")
        self.assertEqual(result["tip_candidates"], [])
        self.assertEqual(await experience.save_candidates(result["samples"]), 0)
        with self.sessions() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(ExperienceTipSample)), 1,
            )
            self.assertEqual(
                db.scalar(select(func.count()).select_from(ExperienceTip)), 0,
            )


class ExperienceStatisticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "experience-statistics.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False,
        )

        with self.sessions() as db:
            db.add_all([
                SystemSetting(key="experience_min_matches", value="3"),
                SystemSetting(key="experience_min_rate", value="0.5"),
                SystemSetting(key="experience_similarity", value="0.8"),
            ])
            db.add_all([
                ReplaySession(id=session_id, title=f"统计测试场次 {session_id}")
                for session_id in range(1, 10)
            ])
            db.commit()

        self.original_session = experience.SessionLocal
        self.original_embed_one = experience.embed_one
        experience.SessionLocal = self.sessions
        self.embedding_calls: list[str] = []

        async def fake_embedding(text: str) -> list[float]:
            self.embedding_calls.append(text)
            if "产品" in text or "佩戴" in text:
                return [1.0, 0.0]
            if "福利" in text or "福袋" in text:
                return [0.0, 1.0]
            return [0.7, 0.7]

        experience.embed_one = fake_embedding

    async def asyncTearDown(self) -> None:
        experience.SessionLocal = self.original_session
        experience.embed_one = self.original_embed_one
        self.engine.dispose()
        self.tmp.cleanup()

    @staticmethod
    def _change(
        session_id: int,
        at_ms: int,
        *,
        metric: str = "实时在线人数",
        direction: str = "up",
        action: str = "",
        reusable: bool = False,
        verdict: str | None = None,
    ) -> dict:
        if verdict is None:
            verdict = "动作驱动" if reusable else "自然波动"
        return {
            "session_id": session_id,
            "scene": "product",
            "metric": metric,
            "direction": direction,
            "verdict": verdict,
            "effect": f"{metric} 在 {at_ms}ms 发生{direction}",
            "action": action,
            "quote": f"样本原话 {at_ms}",
            "template": f"样本模板 {at_ms}" if reusable else "",
            "at_ms": at_ms,
            "reusable": reusable,
        }

    async def test_all_changes_enter_denominator_and_event_scope_is_isolated(self) -> None:
        changes = [
            self._change(
                1, 1_000, action="介绍产品佩戴舒适卖点", reusable=True,
            ),
            self._change(1, 2_000, metric="平均在线人数"),
            self._change(
                1, 3_000, action="模型未确认可复用的产品介绍", verdict="分析失败",
            ),
            self._change(
                1, 4_000, direction="down", action="介绍产品佩戴舒适卖点",
                reusable=True, verdict="流失",
            ),
            self._change(
                1, 5_000, metric="成交件数", action="介绍产品佩戴舒适卖点",
                reusable=True,
            ),
            # 同一罗盘点的成交金额/件数是同一笔成交，只能算一条统计证据。
            self._change(
                1, 5_000, metric="成交金额", action="介绍产品佩戴舒适卖点",
                reusable=True,
            ),
        ]

        promoted = await experience.save_candidates(changes)

        self.assertEqual(promoted, 0)
        with self.sessions() as db:
            samples = db.scalars(
                select(ExperienceTipSample).order_by(ExperienceTipSample.at_ms)
            ).all()
            self.assertEqual(len(samples), 5)
            self.assertEqual(
                [sample.event_key for sample in samples],
                ["online:up", "online:up", "online:up", "online:down", "sale:up"],
            )
            self.assertEqual(sum(sample.tip_id is None for sample in samples), 3)

            tips = db.scalars(select(ExperienceTip).order_by(ExperienceTip.id)).all()
            self.assertEqual(len(tips), 2)
            online_tip = next(tip for tip in tips if tip.event_key == "online:up")
            sale_tip = next(tip for tip in tips if tip.event_key == "sale:up")
            self.assertEqual(
                (online_tip.matched_count, online_tip.change_sample_count), (1, 3),
            )
            self.assertAlmostEqual(online_tip.matched_rate, 1 / 3)
            self.assertEqual(
                (sale_tip.matched_count, sale_tip.change_sample_count), (1, 1),
            )
            self.assertEqual(
                db.scalar(select(func.count()).select_from(KnowledgeChunk)), 0,
            )

    async def test_similar_actions_cluster_while_dissimilar_actions_split(self) -> None:
        await experience.save_candidates([
            self._change(
                1, 1_000, action="介绍产品佩戴舒适卖点", reusable=True,
            ),
            self._change(
                1, 2_000, action="讲清这款产品佩戴舒适优势", reusable=True,
            ),
            self._change(
                1, 3_000, action="发福袋引导观众扣一", reusable=True,
            ),
        ])

        with self.sessions() as db:
            tips = db.scalars(select(ExperienceTip).order_by(ExperienceTip.id)).all()
            samples = db.scalars(
                select(ExperienceTipSample).order_by(ExperienceTipSample.at_ms)
            ).all()
            self.assertEqual(len(tips), 2)
            self.assertEqual(samples[0].tip_id, samples[1].tip_id)
            self.assertNotEqual(samples[0].tip_id, samples[2].tip_id)

            product_tip = db.get(ExperienceTip, samples[0].tip_id)
            welfare_tip = db.get(ExperienceTip, samples[2].tip_id)
            self.assertEqual(
                (product_tip.matched_count, product_tip.change_sample_count), (2, 3),
            )
            self.assertEqual(
                (welfare_tip.matched_count, welfare_tip.change_sample_count), (1, 3),
            )
            self.assertEqual(product_tip.status, "observing")

    async def test_two_of_four_stays_observing_then_three_of_six_becomes_pending(self) -> None:
        first_batch = [
            self._change(
                1, 1_000, action="介绍产品佩戴舒适卖点", reusable=True,
            ),
            self._change(
                1, 2_000, action="讲清这款产品佩戴舒适优势", reusable=True,
            ),
            self._change(1, 3_000),
            self._change(1, 4_000),
        ]
        self.assertEqual(await experience.save_candidates(first_batch), 0)

        with self.sessions() as db:
            tip = db.scalar(select(ExperienceTip))
            tip_id = tip.id
            self.assertEqual(tip.status, "observing")
            self.assertEqual((tip.matched_count, tip.change_sample_count), (2, 4))
            self.assertEqual(tip.matched_rate, 0.5)

        second_batch = [
            self._change(
                2, 1_000, action="再次讲产品佩戴舒适优势", reusable=True,
            ),
            self._change(2, 2_000),
        ]
        self.assertEqual(await experience.save_candidates(second_batch), 1)

        with self.sessions() as db:
            tip = db.get(ExperienceTip, tip_id)
            self.assertEqual(tip.status, "pending")
            self.assertEqual((tip.matched_count, tip.change_sample_count), (3, 6))
            self.assertEqual(tip.matched_rate, 0.5)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(KnowledgeChunk)), 0,
            )
        linked = experience.list_tips(session_id=2)
        self.assertEqual([item["id"] for item in linked], [tip_id])
        self.assertEqual(linked[0]["session_count"], 2)
        self.assertEqual(linked[0]["threshold"]["min_matches"], 3)
        self.assertEqual(
            [item["id"] for item in experience.list_tips(status="pending")],
            [tip_id],
        )

        # 人工采纳前门槛持续生效：新分母让占比跌破 50% 时退回观察。
        self.assertEqual(await experience.save_candidates([
            self._change(3, 1_000),
        ]), 0)
        with self.sessions() as db:
            tip = db.get(ExperienceTip, tip_id)
            self.assertEqual(tip.status, "observing")
            self.assertEqual((tip.matched_count, tip.change_sample_count), (3, 7))
        self.assertEqual(experience.list_tips(status="pending"), [])

    async def test_rerunning_the_same_session_is_idempotent(self) -> None:
        changes = [
            self._change(
                1, 1_000, action="介绍产品佩戴舒适卖点", reusable=True,
            ),
            self._change(
                1, 2_000, action="讲清这款产品佩戴舒适优势", reusable=True,
            ),
            self._change(1, 3_000),
            self._change(1, 4_000),
        ]
        self.assertEqual(await experience.save_candidates(changes), 0)
        with self.sessions() as db:
            before = db.scalar(select(ExperienceTip))
            snapshot = {
                "id": before.id,
                "centroid": json.loads(before.centroid),
                "matched": before.matched_count,
                "total": before.change_sample_count,
                "rate": before.matched_rate,
                "status": before.status,
            }

        self.assertEqual(await experience.save_candidates(changes), 0)

        with self.sessions() as db:
            tip = db.get(ExperienceTip, snapshot["id"])
            self.assertEqual(
                db.scalar(select(func.count()).select_from(ExperienceTipSample)), 4,
            )
            self.assertEqual(db.scalar(select(func.count()).select_from(ExperienceTip)), 1)
            self.assertEqual(json.loads(tip.centroid), snapshot["centroid"])
            self.assertEqual(tip.matched_count, snapshot["matched"])
            self.assertEqual(tip.change_sample_count, snapshot["total"])
            self.assertEqual(tip.matched_rate, snapshot["rate"])
            self.assertEqual(tip.status, snapshot["status"])

    async def test_observing_tip_cannot_be_adopted(self) -> None:
        await experience.save_candidates([
            self._change(
                1, 1_000, action="介绍产品佩戴舒适卖点", reusable=True,
            ),
        ])
        with self.sessions() as db:
            tip_id = db.scalar(select(ExperienceTip.id))
        calls_before_adopt = len(self.embedding_calls)

        result = await experience.adopt_tip(tip_id)

        self.assertFalse(result["ok"])
        self.assertIn("统计验证", result["error"])
        self.assertEqual(len(self.embedding_calls), calls_before_adopt)
        with self.sessions() as db:
            self.assertEqual(db.get(ExperienceTip, tip_id).status, "observing")
            self.assertEqual(
                db.scalar(select(func.count()).select_from(KnowledgeChunk)), 0,
            )

    async def test_pending_tip_only_enters_knowledge_after_manual_adoption(self) -> None:
        changes = [
            self._change(
                1, 1_000, action="介绍产品佩戴舒适卖点", reusable=True,
            ),
            self._change(
                1, 2_000, action="讲清这款产品佩戴舒适优势", reusable=True,
            ),
            self._change(
                2, 1_000, action="再次讲产品佩戴舒适优势", reusable=True,
            ),
            self._change(1, 3_000),
            self._change(1, 4_000),
            self._change(2, 2_000),
        ]
        self.assertEqual(await experience.save_candidates(changes), 1)

        with self.sessions() as db:
            tip = db.scalar(select(ExperienceTip))
            tip_id = tip.id
            self.assertEqual(tip.status, "pending")
            self.assertEqual((tip.matched_count, tip.change_sample_count), (3, 6))
            self.assertEqual(
                db.scalar(select(func.count()).select_from(KnowledgeChunk)), 0,
            )

        adopted = await experience.adopt_tip(tip_id)
        self.assertTrue(adopted["ok"])

        with self.sessions() as db:
            tip = db.get(ExperienceTip, tip_id)
            chunks = db.scalars(select(KnowledgeChunk)).all()
            self.assertEqual(tip.status, "adopted")
            self.assertEqual(len(chunks), 1)
            self.assertEqual(chunks[0].category, "本店经验")
            self.assertEqual(chunks[0].origin_type, "experience_tip")
            self.assertEqual(chunks[0].origin_id, tip_id)
            self.assertIn("同类变化 6 次中命中 3 次（50%）", chunks[0].text)

        adopted_again = await experience.adopt_tip(tip_id)
        self.assertTrue(adopted_again["already"])
        with self.sessions() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(KnowledgeChunk)), 1,
            )


if __name__ == "__main__":
    unittest.main()
