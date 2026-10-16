from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from app.api.sessions import _summary_md
from app.core.review import summary


class _CapturingLLM:
    def __init__(self, payload: dict):
        self.payload = payload
        self.system = ""
        self.user = ""
        self.json_mode = False

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        self.system = system
        self.user = user
        self.json_mode = json_mode
        return json.dumps(self.payload, ensure_ascii=False)


class PersonalizedSummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_embed_one = summary.embed_one
        self.original_retrieve = summary.retrieve_detailed
        self.original_get_llm = summary.get_llm
        self.original_best_practices = summary.best_practices

    def tearDown(self):
        summary.embed_one = self.original_embed_one
        summary.retrieve_detailed = self.original_retrieve
        summary.get_llm = self.original_get_llm
        summary.best_practices = self.original_best_practices

    async def test_r2_r5_and_general_experience_are_injected_and_traced(self):
        calls: list[dict] = []

        async def fake_embed(_text: str):
            return [0.2, 0.8]

        async def fake_retrieve(query: str, **kwargs):
            calls.append({"query": query, **kwargs})
            category = kwargs.get("category", "")
            if category == "优秀节奏":
                row = {
                    "text": "开场先说明目标人群，再用一个问题引导评论。",
                    "source": "[优秀节奏] 优秀主播训练课",
                    "kind": "distilled",
                    "category": "优秀节奏",
                    "scene": "opening",
                    "origin_type": "rhythm_template",
                    "origin_id": 7,
                    "score": 0.91,
                    "semantic": True,
                }
                return [row, dict(row)]  # 相同 provenance 必须去重
            if category == "本店经验":
                return [{
                    "text": "【本店验证有效·产品讲解】；有效动作：报价后立即补适配建议",
                    "source": "复盘沉淀",
                    "kind": "distilled",
                    "category": "本店经验",
                    "scene": "product",
                    "origin_type": "experience_tip",
                    "origin_id": 9,
                    "score": 0.83,
                    "semantic": True,
                }]
            return [{
                "text": "型号事实：阻抗以产品知识库为准。",
                "source": "产品资料",
                "kind": "doc",
                "category": "规格",
                "scene": "",
                "origin_type": "",
                "origin_id": None,
                "score": 0.8,
                "semantic": True,
            }]

        llm = _CapturingLLM({
            "overall_score": 8.2,
            "summary": "整场专业，但开场互动和报价承接可加强。",
            "top_issues": ["开场互动不足"],
            "action_items": [
                {"action": "开场增加目标人群提问", "source": "[优秀节奏:R2-1]"},
                {"action": "报价后补适配建议", "source": "[本店经验:R5-1]"},
                {"action": "保留通用复述确认", "source": "[通用经验]"},
            ],
            "experience_citations": ["R2-1", "[本店经验:R5-1]", "R2-999"],
        })
        summary.embed_one = fake_embed
        summary.retrieve_detailed = fake_retrieve
        summary.get_llm = lambda _purpose: llm
        summary.best_practices = lambda _query: ["回答问题后复述结论并给行动指令"]

        result = await summary.review_summary(
            "欢迎新朋友，今天讲耳机适配。现在报价，大家还有什么问题？",
            [], ["[8分] 产品讲解准确"],
        )

        self.assertTrue(llm.json_mode)
        self.assertIn("[优秀节奏:R2-1] 来源：优秀主播训练课", llm.user)
        self.assertIn("[本店经验:R5-1] 来源：本店复盘沉淀（经验 #9）", llm.user)
        self.assertIn("回答问题后复述结论并给行动指令", llm.user)
        self.assertIn("experience_citations", llm.system)
        self.assertEqual(result["experience_citations"], ["R2-1", "R5-1"])
        self.assertEqual(len(result["experience_sources"]), 2)
        self.assertTrue(all(item["used"] for item in result["experience_sources"]))
        self.assertEqual(result["personalized_experience"], {
            "retrieved": 2,
            "cited": 2,
            "applied": True,
            "r2_retrieved": 1,
            "r5_retrieved": 1,
        })
        self.assertTrue(any(
            call.get("exclude_categories") == ("优秀节奏", "本店经验")
            for call in calls
        ))
        self.assertEqual(
            {call.get("category") for call in calls if call.get("category")},
            {"优秀节奏", "本店经验"},
        )
        self.assertTrue(all(
            call.get("query_vector") == [0.2, 0.8]
            for call in calls
        ))

    async def test_experience_retrieval_failure_falls_back_to_general_experience(self):
        async def fake_embed(_text: str):
            return None

        async def fake_retrieve(_query: str, **kwargs):
            if kwargs.get("category") == "优秀节奏":
                raise RuntimeError("temporary retrieval failure")
            return []

        llm = _CapturingLLM({
            "overall_score": 7,
            "summary": "按通用经验完成点评。",
            "action_items": [{"action": "先总结再促单", "source": "[通用经验]"}],
            "experience_citations": ["R2-1"],
        })
        summary.embed_one = fake_embed
        summary.retrieve_detailed = fake_retrieve
        summary.get_llm = lambda _purpose: llm
        summary.best_practices = lambda _query: ["讲解结束后给明确行动指令"]

        result = await summary.review_summary("本场只有通用讲解", [], [])

        self.assertIn("无相关且已人工采纳的 R2 优秀节奏", llm.user)
        self.assertIn("讲解结束后给明确行动指令", llm.user)
        self.assertEqual(result["experience_citations"], [])
        self.assertEqual(result["experience_sources"], [])
        self.assertEqual(result["personalized_experience"]["retrieved"], 0)
        self.assertFalse(result["personalized_experience"]["applied"])

    async def test_retrieved_but_uncited_experience_is_not_reported_as_applied(self):
        async def fake_embed(_text: str):
            return [1.0]

        async def fake_retrieve(_query: str, **kwargs):
            if kwargs.get("category") == "优秀节奏":
                return [{
                    "text": "促单时明确库存与行动指令。",
                    "source": "主播案例库",
                    "category": "优秀节奏",
                    "scene": "urge",
                    "origin_type": "rhythm_template",
                    "origin_id": 12,
                    "score": 0.7,
                    "semantic": True,
                }]
            return []

        llm = _CapturingLLM({
            "overall_score": 6,
            "summary": "促单动作不足。",
            "action_items": [{"action": "增加行动指令", "source": "[通用经验]"}],
            "experience_citations": ["R5-404"],
        })
        summary.embed_one = fake_embed
        summary.retrieve_detailed = fake_retrieve
        summary.get_llm = lambda _purpose: llm
        summary.best_practices = lambda _query: ["促单给明确行动指令"]

        result = await summary.review_summary("现在看一下产品，稍后再说优惠", [], [])

        self.assertEqual(result["experience_citations"], [])
        self.assertEqual(len(result["experience_sources"]), 1)
        self.assertFalse(result["experience_sources"][0]["used"])
        self.assertEqual(result["personalized_experience"]["retrieved"], 1)
        self.assertFalse(result["personalized_experience"]["applied"])


class PersonalizedSummaryExportTests(unittest.TestCase):
    def test_markdown_export_contains_experience_source_status(self):
        critique = SimpleNamespace(
            streamer_id=1,
            score=8,
            comment="整场表现稳定。",
            data=json.dumps({
                "experience_sources": [{
                    "citation": "[本店经验:R5-1]",
                    "source": "本店复盘沉淀（经验 #9）",
                    "scene": "product",
                    "used": True,
                }],
            }, ensure_ascii=False),
        )
        lines: list[str] = []

        _summary_md(lines, critique, lambda _sid: "主播甲")

        rendered = "\n".join(lines)
        self.assertIn("个性化经验来源", rendered)
        self.assertIn("已引用 `[本店经验:R5-1]`", rendered)
        self.assertIn("本店复盘沉淀（经验 #9）", rendered)


if __name__ == "__main__":
    unittest.main()
