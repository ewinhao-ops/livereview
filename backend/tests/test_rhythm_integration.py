from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.api import rhythm_analysis as rhythm_api
from app.api import sessions as sessions_api
from app.core.review import rhythm_analysis as rhythm_service
from app.db import Base
from app.models import (
    Critique,
    ExperienceCorpus,
    ReplaySession,
    RhythmTemplate,
    SessionRhythmAnalysis,
    Streamer,
)


class RhythmExportAndApiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "r3-integration.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False,
        )
        self.original_rhythm_session = rhythm_service.SessionLocal
        rhythm_service.SessionLocal = self.sessions

        with self.sessions() as db:
            streamer = Streamer(name="集成测试主播")
            db.add(streamer)
            db.flush()
            self.streamer_id = streamer.id
            replay = ReplaySession(
                title="仅 R3 导出场次",
                lineup=json.dumps([streamer.id]),
                current_streamer_id=streamer.id,
            )
            db.add(replay)
            db.flush()
            self.session_id = replay.id
            corpus = ExperienceCorpus(
                source="优秀节奏课",
                corpus_type="live_skill",
                text="开场先定位，再给福利钩子。",
                status="ready",
            )
            db.add(corpus)
            db.flush()
            steps = json.dumps([{
                "order": 1,
                "phase": "欢迎定位",
                "duration_sec": 20,
                "content": "说明直播间价值",
                "action": "二十秒内完成定位",
            }], ensure_ascii=False)
            adopted = RhythmTemplate(
                corpus_id=corpus.id,
                corpus_type="live_skill",
                title="优秀开场节奏",
                scene="opening",
                total_duration_sec=20,
                steps=steps,
                status="adopted",
            )
            pending = RhythmTemplate(
                corpus_id=corpus.id,
                corpus_type="live_skill",
                title="待审核开场节奏",
                scene="opening",
                total_duration_sec=20,
                steps=steps,
                status="pending",
            )
            db.add_all([adopted, pending])
            db.flush()
            self.adopted_template_id = adopted.id
            self.pending_template_id = pending.id
            db.commit()

        api_app = FastAPI()
        api_app.include_router(rhythm_api.router)
        self.client = TestClient(api_app)

    def tearDown(self) -> None:
        self.client.close()
        rhythm_service.SessionLocal = self.original_rhythm_session
        self.engine.dispose()
        self.tmp.cleanup()

    def _add_rhythm_row(self, *, malformed: bool = False) -> None:
        if malformed:
            actual = {
                "blocks": [
                    None,
                    "损坏块",
                    {
                        "time_range": "00:00-00:20",
                        "scene_label": "开场留人",
                        "duration_sec": 20,
                        "content": "真实开场内容",
                    },
                ],
            }
            comparison = {
                "match_score": 40,
                "summary": "损坏嵌套结构仍应可导出",
                "differences": [
                    None,
                    "损坏差异",
                    {
                        "step_order": 1,
                        "template_phase": "欢迎定位",
                        "status": "missing",
                        "difference": "缺少福利钩子",
                        "evidence": {"错误": "本应为数组"},
                    },
                    {
                        "step_order": 2,
                        "status": "wrong_content",
                        "difference": "内容偏离",
                        "evidence": [
                            None,
                            "损坏证据",
                            {"time_range": "00:10-00:20", "quote": "可用证据"},
                        ],
                    },
                ],
                "extra_issues": [None, "损坏问题", {"diagnosis": "闲聊过多"}],
                "action_items": [None, {"action": "压缩闲聊"}],
            }
            validation = {
                "verdict": "待补数据",
                "evidence": [None, "损坏罗盘证据", {"time": "00:20", "metric": "在线"}],
            }
        else:
            actual = {"blocks": [{
                "order": 1,
                "time_range": "00:00-00:20",
                "scene": "opening",
                "scene_label": "开场留人",
                "duration_sec": 20,
                "content": "欢迎新客并说明本场价值",
            }]}
            comparison = {
                "match_score": 76,
                "summary": "定位完整，但福利钩子出现偏晚。",
                "differences": [{
                    "step_order": 1,
                    "template_phase": "欢迎定位",
                    "expected_duration_sec": 20,
                    "actual_duration_sec": 20,
                    "status": "matched",
                    "difference": "内容和时长基本符合",
                    "evidence": [{
                        "time_range": "00:00-00:20",
                        "quote": "欢迎新来的朋友",
                    }],
                    "action": "保持二十秒内完成定位",
                }],
                "extra_issues": [],
                "action_items": ["下一场提前福利钩子"],
            }
            validation = {
                "status": "unverified",
                "verdict": "未声明执行",
                "reason": "本次仅做复盘对比。",
                "evidence": [],
            }
        with self.sessions() as db:
            db.add(SessionRhythmAnalysis(
                session_id=self.session_id,
                streamer_id=self.streamer_id,
                template_id=self.adopted_template_id,
                selection_mode="manual",
                declared_execution=False,
                status="ready",
                analysis_run_id="integration-export",
                template_snapshot=json.dumps({
                    "id": self.adopted_template_id,
                    "title": "优秀开场节奏",
                    "source": "优秀节奏课",
                }, ensure_ascii=False),
                extracted=json.dumps(actual, ensure_ascii=False),
                comparison=json.dumps(comparison, ensure_ascii=False),
                validation=json.dumps(validation, ensure_ascii=False),
                match_score=40 if malformed else 76,
            ))
            db.commit()

    @staticmethod
    def _docx_text(data: bytes) -> str:
        document = Document(io.BytesIO(data))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)

    def test_r3_only_session_is_exported_to_markdown_and_docx(self) -> None:
        self._add_rhythm_row()
        with self.sessions() as db:
            replay = db.get(ReplaySession, self.session_id)
            self.assertEqual(db.scalar(select(func.count()).select_from(Critique)), 0)
            markdown = sessions_api._build_markdown(db, replay)
            docx_data = sessions_api._build_docx(db, replay)

        self.assertIn("R3 话术节奏分析与优秀模板对比", markdown)
        self.assertIn("优秀开场节奏", markdown)
        self.assertIn("欢迎新客并说明本场价值", markdown)
        self.assertIn("定位完整，但福利钩子出现偏晚", markdown)
        docx_text = self._docx_text(docx_data)
        self.assertIn("R3 话术节奏分析与优秀模板对比", docx_text)
        self.assertIn("优秀开场节奏", docx_text)
        self.assertIn("欢迎新客并说明本场价值", docx_text)

    def test_malformed_nested_differences_and_evidence_do_not_break_exports(self) -> None:
        self._add_rhythm_row(malformed=True)
        with self.sessions() as db:
            replay = db.get(ReplaySession, self.session_id)
            markdown = sessions_api._build_markdown(db, replay)
            docx_data = sessions_api._build_docx(db, replay)

        self.assertIn("损坏嵌套结构仍应可导出", markdown)
        self.assertIn("本场证据：未找到对应逐字稿片段", markdown)
        self.assertIn("可用证据", markdown)
        docx_text = self._docx_text(docx_data)
        self.assertIn("损坏嵌套结构仍应可导出", docx_text)
        self.assertIn("本场证据：未找到对应逐字稿片段", docx_text)
        self.assertIn("可用证据", docx_text)

    def test_api_maps_missing_resources_and_invalid_states(self) -> None:
        missing_session = self.client.get(
            "/api/sessions/999999/rhythm-analysis",
            params={"streamer_id": self.streamer_id},
        )
        self.assertEqual(missing_session.status_code, 404)
        self.assertIn("场次不存在", missing_session.json()["detail"])

        missing_template = self.client.post(
            f"/api/sessions/{self.session_id}/rhythm-analysis/plan",
            json={"streamer_id": self.streamer_id, "template_id": 999999},
        )
        self.assertEqual(missing_template.status_code, 404)
        self.assertIn("节奏模板不存在", missing_template.json()["detail"])

        pending_template = self.client.post(
            f"/api/sessions/{self.session_id}/rhythm-analysis/plan",
            json={
                "streamer_id": self.streamer_id,
                "template_id": self.pending_template_id,
            },
        )
        self.assertEqual(pending_template.status_code, 409)
        self.assertIn("已人工采纳", pending_template.json()["detail"])

        planned = self.client.post(
            f"/api/sessions/{self.session_id}/rhythm-analysis/plan",
            json={
                "streamer_id": self.streamer_id,
                "template_id": self.adopted_template_id,
            },
        )
        self.assertEqual(planned.status_code, 200)
        self.assertEqual(planned.json()["status"], "planned")
        verify = self.client.post(
            f"/api/sessions/{self.session_id}/rhythm-analysis/verify",
            params={"streamer_id": self.streamer_id},
        )
        self.assertEqual(verify.status_code, 409)
        self.assertIn("尚未完成", verify.json()["detail"])


if __name__ == "__main__":
    unittest.main()
