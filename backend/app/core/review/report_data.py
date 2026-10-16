"""并发安全地合并整场报告 JSON 子树。

整场报告会被 v2、情绪精算和 R3 在不同后台任务里补写。Critique.data 是一整段
JSON 文本，普通的“读取 -> 修改 -> 写回”会让较晚提交的任务覆盖别人的字段；这里
用旧文本做乐观锁，冲突时重读并重试。
"""

from __future__ import annotations

import json
from collections.abc import Callable

from sqlalchemy import select, update

from ...db import SessionLocal
from ...models import Critique, CritiqueLevel


def merge_summary_data(
    session_id: int,
    streamer_id: int | None,
    mutate: Callable[[dict], None],
    *,
    retries: int = 5,
) -> dict | None:
    """把 mutate 应用到该主播主 summary.data；不存在主报告时返回 None。"""
    for _ in range(max(1, retries)):
        with SessionLocal() as db:
            row = db.execute(select(Critique).where(
                Critique.session_id == session_id,
                Critique.streamer_id == streamer_id,
                Critique.level == CritiqueLevel.summary,
                Critique.category == "整场总结",
            ).order_by(Critique.id).limit(1)).scalars().first()
            if row is None:
                return None
            old = row.data or ""
            try:
                data = json.loads(old) if old else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            mutate(data)
            new = json.dumps(data, ensure_ascii=False)
            if new == old:
                return data
            changed = db.execute(
                update(Critique)
                .where(Critique.id == row.id, Critique.data == old)
                .values(data=new)
            )
            if changed.rowcount == 1:
                db.commit()
                return data
            db.rollback()
    raise RuntimeError("整场报告正在被其他分析任务更新，请稍后重试")
