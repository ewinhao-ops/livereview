"""Insert explicitly fictional example records, only into an empty database.

Run from backend/: uv run python ../scripts/seed_demo.py
No network requests, real media, accounts or paid model calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (Critique, CritiqueLevel, KnowledgeItem, ReplaySession,  # noqa: E402
                        SessionStatus, Severity, Streamer, StreamerTurn, Transcript)


def main():
    init_db()
    with SessionLocal() as db:
        if db.query(ReplaySession).first() or db.query(Streamer).first() or db.query(KnowledgeItem).first():
            raise SystemExit("数据库已有内容，为避免混入示例数据，本次未写入。")
        anchors = [Streamer(name="示例主播 A", note="虚构人物，仅用于演示", color="#55dac1"),
                   Streamer(name="示例主播 B", note="虚构人物，仅用于演示", color="#89a8ff")]
        db.add_all(anchors)
        db.flush()
        db.add(KnowledgeItem(kind="product", name="Demo One 耳机", aliases='["演示耳机"]',
                            attrs=json.dumps({"impedance_ohm":32,"sensitivity_db":100,
                                              "sellpoints":["虚构产品，用于展示知识库"]}, ensure_ascii=False),
                            note="所有参数均为虚构，不能用于真实销售讲解"))
        lines = [
            "欢迎来到今天的直播。先聊聊大家平时用什么设备听音乐。",
            "这位朋友说自己通勤多，想要佩戴轻一些的耳机。",
            "今天用虚构的 Demo One 做演示，先介绍使用场景，再解释参数。",
            "这款演示产品标注阻抗为三十二欧，选择设备时也要考虑灵敏度。",
            "大家可以说说预算和听歌习惯，我会按实际需求来推荐。",
            "我们先总结刚才的两种使用场景，再进入下一款产品。",
        ]
        titles = ["演示 · 新品讲解与需求确认", "演示 · 产品对比与问答", "演示 · 开场节奏练习", "演示 · 场景化推荐"]
        for i, title in enumerate(titles):
            a = anchors[i % 2]
            start = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0) - timedelta(days=i+1)
            r = ReplaySession(title=title, status=SessionStatus.done,
                              report_status="ready", deep_status="not_requested",
                              started_at=start, ended_at=start+timedelta(minutes=30),
                              lineup=json.dumps([a.id]), current_streamer_id=a.id)
            db.add(r)
            db.flush()
            db.add(StreamerTurn(session_id=r.id, streamer_id=a.id, start_ms=0, end_ms=1800000))
            for n, line in enumerate(lines):
                db.add(Transcript(session_id=r.id, streamer_id=a.id, seq=n,
                                  start_ms=n*60000, end_ms=n*60000+10000,
                                  raw_text=line, corrected_text=line))
            summary = {
                "overall_score": round(8.6-i*.3, 1),
                "dimensions":{"讲解准确性":9.0,"节奏":8.0,"转化引导":7.8,"合规":9.2},
                "summary":"【虚构演示报告】先询问使用场景，再介绍产品参数，讲解顺序清楚。可以在产品切换前增加一句总结，并把观众的问题完整复述。",
                "top_issues":[{"category":"讲解衔接","quote":lines[-1],"issue":"总结不够具体", "suggestion":"用两句话对照两类需求，再进入下一个产品。"}],
                "action_items":["下一场开场先收集两位观众的使用需求。","每次切换产品前，用一句话总结适用人群。"],
                "demo":True,
            }
            db.add(Critique(session_id=r.id, streamer_id=a.id, level=CritiqueLevel.summary,
                            severity=Severity.info, score=summary["overall_score"],
                            comment=summary["summary"], data=json.dumps(summary,ensure_ascii=False),
                            llm_provider="demo", llm_requested_provider="demo"))
            db.add(Critique(session_id=r.id, streamer_id=a.id, level=CritiqueLevel.segment,
                            severity=Severity.info, start_ms=60000, end_ms=120000,
                            quote=lines[1], comment="【演示】先确认需求，再解释参数，便于观众理解。", score=8.5,
                            data=json.dumps({"scene_label":"需求确认"}, ensure_ascii=False), llm_provider="demo"))
        db.commit()
    print("已写入 4 场虚构演示记录。没有创建真实账号、录制直播或调用付费模型。")


if __name__ == "__main__":
    main()
