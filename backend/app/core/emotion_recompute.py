"""整场情绪精算：对每个点评段用 qwen3-asr-flash 识别情绪，写回段级 + 按主播聚合整场分布。

供 API（手动触发）与 pipeline 收播后（自动）共用。qwen 调用是同步阻塞，
在异步场景（pipeline）用 asyncio.to_thread 调本函数。每段即时落库，可分批续跑。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from ..db import SessionLocal
from ..models import AudioStatus, Critique, CritiqueLevel
from ..storage.audio import get_audio, find_part_file
from . import media_store, qwen_emotion


def _audio_source(af) -> str | None:
    """裁剪源：本地文件优先，其次 COS 预签名 URL；录制中回退最新分段文件。"""
    if af.path and Path(af.path).exists():
        return str(af.path)
    if af.cos_key:
        akey = media_store.audio_track_key(af, af.session_id)
        if akey:
            url = media_store.presigned_url(akey, expires=3600)
            if url:
                return url
    part = find_part_file(af.path)
    return str(part) if part else None


def _overview(labels: list[str]) -> dict:
    n = len(labels)
    cnt = Counter(labels)
    return {"segments": n,
            "dominant": cnt.most_common(1)[0][0] if n else "",
            "dist": [{"label": k, "count": v, "pct": round(v * 100 / n)}
                     for k, v in cnt.most_common()]}


def _aggregate(db, session_id: int) -> dict:
    """按主播聚合段级 asr 情绪 → 写各自 summary 的 emotion_overview，返回全场分布。"""
    segs = db.execute(select(Critique).where(
        Critique.session_id == session_id,
        Critique.level == CritiqueLevel.segment)).scalars().all()
    by_sid: dict = {}
    for c in segs:
        try:
            lab = (json.loads(c.data) or {}).get("asr_emotion_label") if c.data else None
        except json.JSONDecodeError:
            lab = None
        if lab:
            by_sid.setdefault(c.streamer_id, []).append(lab)
    # summary.data 同时会被 v2 与 R3 后台任务补写，必须走乐观锁合并，避免后提交者
    # 覆盖前一个任务；同时只碰 category=整场总结，不污染“罗盘归因”等 summary 行。
    from .review.report_data import merge_summary_data
    for streamer_id, labs in by_sid.items():
        if not labs:
            continue
        overview = _overview(labs)
        merge_summary_data(
            session_id,
            streamer_id,
            lambda data, value=overview: data.__setitem__("emotion_overview", value),
        )
    alllabs = [x for v in by_sid.values() for x in v]
    return _overview(alllabs) if alllabs else {}


def recompute_session_emotions(session_id: int, streamer_id: int | None = None,
                               only_missing: bool = True) -> dict:
    """对该场各点评段用 qwen3-asr-flash 识别情绪并聚合整场分布。返回统计。"""
    if not qwen_emotion.enabled():
        return {"error": "no_key", "updated": 0, "skipped": 0, "total": 0}
    with SessionLocal() as db:
        af = get_audio(db, session_id)
        if not af or af.status in (AudioStatus.deleted, AudioStatus.error):
            return {"error": "no_audio", "updated": 0, "skipped": 0, "total": 0}
        source = _audio_source(af)
        if not source:
            return {"error": "no_source", "updated": 0, "skipped": 0, "total": 0}
        q = select(Critique).where(Critique.session_id == session_id,
                                   Critique.level == CritiqueLevel.segment)
        if streamer_id:
            q = q.where(Critique.streamer_id == streamer_id)
        segs = db.execute(q.order_by(Critique.start_ms)).scalars().all()
        if not segs:
            return {"updated": 0, "skipped": 0, "total": 0, "overview": {}}

        tmpdir = tempfile.mkdtemp(prefix=f"emo_{session_id}_")
        updated = skipped = 0
        try:
            for i, c in enumerate(segs):
                try:
                    data = json.loads(c.data) if c.data else {}
                except json.JSONDecodeError:
                    data = {}
                need_emo = not data.get("asr_emotion")
                need_pitch = not data.get("pitch")
                if only_missing and not need_emo and not need_pitch:
                    skipped += 1
                    continue
                start = max(0, c.start_ms) / 1000.0
                dur = ((c.end_ms - c.start_ms) / 1000.0) if (c.end_ms and c.end_ms > c.start_ms) else 240.0
                dur = min(max(dur, 1.0), 300.0)   # qwen3-asr-flash 同步上限 5 分钟
                out = os.path.join(tmpdir, f"seg{i}.wav")
                cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                       "-ss", f"{start:.3f}", "-i", source, "-t", f"{dur:.3f}",
                       "-vn", "-ac", "1", "-ar", "16000", out]
                try:
                    r = subprocess.run(cmd, capture_output=True, timeout=120)
                except subprocess.TimeoutExpired:
                    continue
                if r.returncode != 0 or not (os.path.exists(out) and os.path.getsize(out) > 0):
                    continue
                changed = False
                if need_emo:
                    emo = qwen_emotion.recognize_emotion(out)
                    if emo:
                        data["asr_emotion"] = emo["emotion"]
                        data["asr_emotion_label"] = emo["emotion_label"]
                        changed = True
                if need_pitch:
                    from .prosody import pitch_features
                    pf = pitch_features(out)
                    if pf:
                        data["pitch"] = pf
                        changed = True
                Path(out).unlink(missing_ok=True)
                if changed:
                    c.data = json.dumps(data, ensure_ascii=False)
                    db.commit()   # 每段即时落库，中断不丢、可续跑
                    updated += 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        overview = _aggregate(db, session_id)
        return {"updated": updated, "skipped": skipped, "total": len(segs), "overview": overview}
