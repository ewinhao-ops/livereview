import json
import unittest
from unittest.mock import AsyncMock, patch

from app.core.emotion_context import analyze_emotion_context


class R8EmotionContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_matches_actual_emotion_with_transcript_and_danmaku(self):
        llm = AsyncMock()
        llm.complete.return_value = json.dumps({
            "expected_emotion": "热情有推动力",
            "matched": False,
            "confidence": 0.91,
            "reason": "正在发布福利，但实际语气平淡，观众也在催价格。",
            "suggestion": "提高音量和语速，强调限时福利并明确下单动作。",
        }, ensure_ascii=False)
        with patch("app.core.emotion_context.get_llm", return_value=llm):
            result = await analyze_emotion_context(
                actual_emotion="平静",
                transcript=["今天直播间有一波限时福利，马上拍。"],
                danmaku=["多少钱？", "链接在哪"],
                scene="福利促单",
            )

        self.assertFalse(result["matched"])
        self.assertEqual(result["actual_emotion"], "平静")
        self.assertEqual(result["expected_emotion"], "热情有推动力")
        self.assertEqual(result["danmaku_context"], "多少钱？；链接在哪")
        prompt = llm.complete.await_args.args[1]
        self.assertIn("今天直播间有一波限时福利", prompt)
        self.assertIn("多少钱？", prompt)
        self.assertIn("【实际情绪】平静", prompt)

    async def test_invalid_model_output_does_not_break_pipeline(self):
        llm = AsyncMock()
        llm.complete.return_value = "not-json"
        with patch("app.core.emotion_context.get_llm", return_value=llm):
            result = await analyze_emotion_context(
                actual_emotion="开心", transcript=["欢迎大家"], danmaku=[])
        self.assertIsNone(result)

    async def test_requires_real_emotion_and_transcript(self):
        with patch("app.core.emotion_context.get_llm") as get_llm:
            self.assertIsNone(await analyze_emotion_context(
                actual_emotion="", transcript=["欢迎"], danmaku=[]))
            self.assertIsNone(await analyze_emotion_context(
                actual_emotion="开心", transcript=[], danmaku=[]))
        get_llm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
