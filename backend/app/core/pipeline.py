"""实时编排：拉流 → ASR/OCR 并行 → 校正 → 多层点评 → WebSocket 推送 + 落库。

多主播：维护"当前主播"，每条转写/OCR/点评都按当前主播 stamp streamer_id；
切换主播时关闭旧班次、开新班次（并先把当前段冲洗成段级点评，干净归属）。
结束时按主播分别聚合出整场总结。

稳定性（P0）：
- 会话生命周期由【采集】驱动（直播结束/手动停才结束），不再由 ASR 迭代驱动；
- 采集与 ASR 用【有界队列】解耦，feed 带超时，ASR 卡住不会拖垮采集/录音；
- ASR 中途断开自动重连，录音照常；拉流地址过期自动刷新（见 _refresh_stream_url）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from datetime import datetime

from ..bus import bus
from ..config import get_settings
from ..db import SessionLocal
from ..models import (
    AudioFile,
    AudioStatus,
    Critique,
    CritiqueLevel,
    OcrRecord,
    ReplaySession,
    SessionStatus,
    SessionType,
    Severity,
    Streamer,
    StreamerTurn,
    Transcript,
)
from . import media_store, runtime
from .hls_uploader import HlsUploader
from .asr_stream import MockASR, get_asr_session
from .capture import FFmpegCapture
from .corrector import correct_sentence
from .knowledge import load_kb
from .ocr import MockOCR
from .prosody import chunk_features, summarize_prosody
from .review.rhythm import compute_rhythm_metrics
from .review.segment import review_segment
from .review.sentence import Flag, check_sentence
from .safe_error import safe_error_text, safe_exception_code
from .usage import llm_context

# Five sentences was too chatty for a long stream.  The setting remains
# runtime-configurable so a short demo can opt back into smaller segments.
SEGMENT_MAX_SENTENCES = 20
_SWITCH_KW = ["下一款", "换一款", "接着讲", "下一个产品", "再看下一"]

_AUDIO_Q_MAX = 64          # ≈ 64*200ms = 12.8s 缓冲，满了丢最旧保实时
_ASR_FEED_TIMEOUT = 3.0    # 单次喂音频上限，超时即视为 ASR 卡顿


def _looks_like_question(text: str) -> bool:
    clean = (text or "").strip()
    if len(clean) < 3 or len(clean) > 160:
        return False
    return ("?" in clean or "？" in clean or clean.endswith(("吗", "呢", "么"))
            or any(x in clean for x in ("怎么", "如何", "多少", "什么", "哪款", "能不能", "可以不")))

_running: dict[int, "ReplayPipeline"] = {}
_pipeline_tasks: dict[int, asyncio.Task[None]] = {}


def _log_failure(stage: str, exc: BaseException, *, session_id: int | None = None) -> None:
    context = f" session={session_id}" if session_id is not None else ""
    print(
        f"[pipeline]{context} stage={stage}; "
        f"code={safe_exception_code(exc, prefix='PIPELINE')}; "
        f"error={safe_error_text(exc)}"
    )


def _sev(s: str) -> Severity:
    return {"info": Severity.info, "warn": Severity.warn, "error": Severity.error}.get(
        s, Severity.info
    )


class ReplayPipeline:
    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self.settings = get_settings()
        self.kb = None
        self._recent_ocr: deque[str] = deque(maxlen=20)
        self._seq = 0
        self._now_ms = 0
        self._seg_lock = asyncio.Lock()
        self._capture: FFmpegCapture | None = None
        self._danmaku = None
        self._last_ocr_text: str | None = None
        self._last_ocr_start = 0
        self._danmaku_answered: set[str] = set()
        self._danmaku_answer_count = 0
        self._last_danmaku_answer_at = 0.0

        # 采集 / ASR 解耦
        self._asr = None
        self._llm_name = ""
        self._audio_q: asyncio.Queue | None = None
        self._capture_done = asyncio.Event()
        self._stopping = False
        self._realtime_q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._asr_degraded = False
        self._share_link = ""
        self._observation = False

        # 多主播
        self._streamers: dict[int, dict] = {}          # id -> {name,color}
        self._current_sid: int | None = None
        self._turn_seq = 0
        # 段级缓冲（随当前主播）
        self._seg_buffer: list[str] = []
        self._seg_emos: list[str] = []      # 当前未结算段的实时 ASR 逐句情绪
        # 按主播聚合，供结束时分别出总结
        self._sent_by: dict[int | None, list[str]] = defaultdict(list)
        self._flags_by: dict[int | None, list[str]] = defaultdict(list)
        self._segs_by: dict[int | None, list[str]] = defaultdict(list)
        self._metrics_by: dict[int | None, list[dict]] = defaultdict(list)
        self._emotions_by: dict[int | None, list[str]] = defaultdict(list)
        self._scenes_by: dict[int | None, list[str]] = defaultdict(list)
        self._loops_by: dict[int | None, list[dict]] = defaultdict(list)
        self._prosody: deque = deque(maxlen=4000)   # (t_ms, rms, zcr) 逐块声学特征，段级聚合
        self._order: list[int | None] = []             # 出现过的主播顺序
        self._summarized: set[int | None] = set()      # 已出整场点评的主播（切换即结算）
        self._summarize_lock = asyncio.Lock()          # 保护 _summarized 检查-写入
        self._background_tasks: set[asyncio.Task] = set()  # 收尾后仍需完成的可观测任务
        self._danmaku_status = ""                      # 场次上记的弹幕采集情况（pending/ok/missing）

    # ---------- DB 小工具 ----------
    def _update_session(self, **fields) -> None:
        with SessionLocal() as db:
            row = db.get(ReplaySession, self.session_id)
            if row is None:
                return
            for k, v in fields.items():
                setattr(row, k, v)
            db.commit()

    def _report_artifacts_ready(self) -> bool:
        """Return whether the minimum user-visible report has actually persisted."""
        with SessionLocal() as db:
            session = db.get(ReplaySession, self.session_id)
            if session is None:
                return False
            transcript_count = db.query(Transcript).filter(
                Transcript.session_id == self.session_id,
            ).count()
            summary_count = db.query(Critique).filter(
                Critique.session_id == self.session_id,
                Critique.level == CritiqueLevel.summary,
            ).count()
            expected_summary_count = len({
                sid for sid, sentences in self._sent_by.items()
                if sid is not None and sentences
            })
            return transcript_count > 0 and (
                expected_summary_count == 0 or summary_count >= expected_summary_count
            )

    def _mark_media_error(self, code: str, message: str) -> None:
        """Idempotent shared terminal write for capture/audio failures."""
        with SessionLocal() as db:
            row = db.get(ReplaySession, self.session_id)
            if row is None:
                return
            if row.audio and row.audio.status != AudioStatus.deleted:
                row.audio.status = AudioStatus.error
                row.audio.size_bytes = 0
            row.status = SessionStatus.error
            row.error_code = code
            row.error_msg = message
            row.ended_at = row.ended_at or datetime.now()
            db.commit()

    def _sname(self, sid: int | None) -> str:
        if sid is None:
            return "未指定主播"
        return self._streamers.get(sid, {}).get("name", f"主播{sid}")

    def _track(self, sid: int | None) -> None:
        if sid not in self._order:
            self._order.append(sid)

    # ---------- 班次 ----------
    async def _open_turn(self, sid: int | None, t_ms: int) -> None:
        self._current_sid = sid
        self._track(sid)
        if sid is not None:
            with SessionLocal() as db:
                db.add(StreamerTurn(
                    session_id=self.session_id, streamer_id=sid,
                    seq=self._turn_seq, start_ms=t_ms, end_ms=t_ms,
                ))
                db.get(ReplaySession, self.session_id).current_streamer_id = sid
                db.commit()
            self._turn_seq += 1
        await bus.publish(self.session_id, {
            "type": "streamer", "streamer_id": sid, "name": self._sname(sid),
            "color": self._streamers.get(sid, {}).get("color", ""), "t_ms": t_ms,
        })

    def _close_turn(self, t_ms: int) -> None:
        if self._current_sid is None:
            return
        with SessionLocal() as db:
            turn = (
                db.query(StreamerTurn)
                .filter(StreamerTurn.session_id == self.session_id,
                        StreamerTurn.streamer_id == self._current_sid)
                .order_by(StreamerTurn.seq.desc())
                .first()
            )
            if turn:
                turn.end_ms = max(t_ms, turn.start_ms)
                db.commit()

    def _backfill_gap_transcripts(self, prev_sid, upto_ms: int) -> None:
        """切人间隙回填（2026-07-15）：上一位下卡到此刻之间归属为空的转写，补记给上一位——
        直播不会真空，间隙里几乎都是上一位在收尾/交接（实测 s12/s14 边界样本全部吻合）；
        否则这段话术复盘时无人认领（忘打卡时误差更大）。"""
        if prev_sid is None:
            return
        try:
            with SessionLocal() as db:
                db.query(Transcript).filter(
                    Transcript.session_id == self.session_id,
                    Transcript.streamer_id.is_(None),
                    Transcript.start_ms <= upto_ms,
                ).update({Transcript.streamer_id: prev_sid}, synchronize_session=False)
                db.commit()
        except Exception as e:  # noqa: BLE001
            _log_failure("gap_transcript_backfill", e, session_id=self.session_id)

    def _claim_unassigned(self, sid: int, upto_ms: int) -> None:
        """首次打卡晚于开录时，把此前无人认领的全链路数据归给该主播。"""
        try:
            with SessionLocal() as db:
                for model in (Transcript, OcrRecord, Critique):
                    db.query(model).filter(
                        model.session_id == self.session_id,
                        model.streamer_id.is_(None),
                        model.start_ms <= upto_ms,
                    ).update({model.streamer_id: sid}, synchronize_session=False)
                db.commit()
        except Exception as e:  # noqa: BLE001
            _log_failure("first_streamer_backfill", e, session_id=self.session_id)

    async def switch_streamer(self, sid: int) -> None:
        """切换当前主播：冲洗当前段（归属离场主播）→ 关旧班次、开新班次 →
        离场主播的整场点评放后台生成（不阻塞切换，稍后经 WS 推达）。"""
        await self._maybe_segment(force=True)
        outgoing = self._current_sid
        self._close_turn(self._now_ms)
        if outgoing is None:
            self._claim_unassigned(sid, self._now_ms)
        else:
            self._backfill_gap_transcripts(outgoing, self._now_ms)
        await self._open_turn(sid, self._now_ms)
        if outgoing is not None:
            asyncio.create_task(self._finalize_streamer_summary(outgoing))

    async def close_current_turn(self) -> None:
        """结班（业绩系统下播打卡触发）：冲洗当前段（归属离场主播）→ 关班次 →
        当前主播置空 → 后台生成离场主播整场总结。录制本身继续跑——交接班不断录，
        真正收播由断流/无数据看门狗负责。幂等：当前无主播时是 no-op。"""
        await self._maybe_segment(force=True)
        outgoing = self._current_sid
        if outgoing is None:
            return
        self._close_turn(self._now_ms)
        await self._open_turn(None, self._now_ms)   # 推 WS"未指定主播"，sid=None 不写 turn 表
        with SessionLocal() as db:                   # _open_turn(None) 不写库，这里显式置空
            db.get(ReplaySession, self.session_id).current_streamer_id = None
            db.commit()
        asyncio.create_task(self._finalize_streamer_summary(outgoing))

    def recently_receiving(self, within_sec: float = 30.0) -> bool:
        """采集流最近 within_sec 秒内是否收到过音频数据（停录宽限探活用）。"""
        if self._capture is None or self._capture.last_data_ts <= 0:
            return False
        return (time.monotonic() - self._capture.last_data_ts) < within_sec

    def _compare_with_prev(self, sid: int | None) -> dict | None:
        """跨场改进对比（管理员/主播端的改进跟踪数据源）：
        与该主播上一场（有整场总结的场次）按问题类别集合比较——
        上场有本场无=已改进；两场都有=仍需改进；本场新出现=新问题。"""
        if sid is None:
            return None
        with SessionLocal() as db:
            prev_summary = db.query(Critique).filter(
                Critique.streamer_id == sid,
                Critique.level == CritiqueLevel.summary,
                Critique.session_id != self.session_id,
            ).order_by(Critique.id.desc()).first()
            if prev_summary is None:
                return None
            def issue_cats(session_id: int) -> set[str]:
                rows = db.query(Critique.category).filter(
                    Critique.session_id == session_id,
                    Critique.streamer_id == sid,
                    Critique.level != CritiqueLevel.summary,
                    Critique.severity.in_([Severity.warn, Severity.error]),
                ).all()
                return {r[0] for r in rows if r[0]}
            prev_cats = issue_cats(prev_summary.session_id)
            cur_cats = issue_cats(self.session_id)
        return {
            "prev_session_id": prev_summary.session_id,
            "prev_score": prev_summary.score,
            "improved": sorted(prev_cats - cur_cats),
            "persisting": sorted(prev_cats & cur_cats),
            "new_issues": sorted(cur_cats - prev_cats),
        }

    def _collect_evidence(self, sid) -> tuple[list, dict]:
        """从本场段级点评聚合：①错失清单（各段 missed 汇总，带时间戳可跳转）
        ②案例证据链（按段分最高/最低各取 2 段，总结里的结论能指到具体案例）。"""
        missed: list[dict] = []
        scored: list[dict] = []
        with SessionLocal() as db:
            rows = db.query(Critique).filter(
                Critique.session_id == self.session_id,
                Critique.streamer_id == sid,
                Critique.level == CritiqueLevel.segment,
            ).order_by(Critique.start_ms).all()
            for r in rows:
                try:
                    d = json.loads(r.data) if r.data else {}
                except json.JSONDecodeError:
                    continue
                for m in d.get("missed", [])[:3]:
                    missed.append({**m, "start_ms": r.start_ms})
                if r.score is not None:
                    scored.append({"start_ms": r.start_ms, "score": r.score,
                                   "scene_label": d.get("scene_label", ""),
                                   "comment": (r.comment or "")[:80]})
        scored.sort(key=lambda x: x["score"])
        examples = {"worst": scored[:2], "best": scored[-2:][::-1]} if scored else {}
        return missed[:12], examples

    async def _finalize_streamer_summary(self, sid) -> None:
        """对某主播本场表现出整场点评（切换或结束时调用，幂等）。"""
        async with self._summarize_lock:
            if sid in self._summarized:
                return
            self._summarized.add(sid)
        sents = self._sent_by.get(sid, [])
        if not sents and not self._flags_by.get(sid):
            return
        # Stop-time and manual regeneration deliberately share one authoritative service.
        # It re-reads persisted transcripts/critiques/alerts instead of trusting pipeline memory.
        from .summary_service import generate_summary
        try:
            with llm_context(self.session_id, "summary"):
                data = await generate_summary(self.session_id, sid)
        except BaseException:
            # 失败不是“已总结”；允许结束流程或人工请求稍后安全重试。
            async with self._summarize_lock:
                self._summarized.discard(sid)
            raise
        try:
            data["prev_compare"] = self._compare_with_prev(sid)
        except Exception as e:  # noqa: BLE001  对比失败绝不影响总结主链路
            _log_failure("previous_session_compare", e, session_id=self.session_id)
        try:
            # 2026-07-10 整改：错失清单聚合 + 结论证据链（纯聚合，指向具体段供前端跳转）
            data["missed_summary"], data["examples"] = self._collect_evidence(sid)
        except Exception as e:  # noqa: BLE001
            _log_failure("summary_evidence", e, session_id=self.session_id)
        await bus.publish(self.session_id, {
            "type": "summary", "streamer_id": sid, "streamer": self._sname(sid),
            "llm": self._llm_name, **data})
        # v2 深度复盘（2026-07-15 页面改版）：五块分析异步补挂到 summary.data.v2，失败不影响主链
        async def _v2():
            try:
                from .review.v2_jobs import start_job, wait_job
                job, _created = start_job(self.session_id, sid)
                await wait_job(job)
            except Exception as e:  # noqa: BLE001
                _log_failure("v2_review", e, session_id=self.session_id)
        if getattr(self.settings, "v2_auto_enabled", False):
            task = asyncio.create_task(_v2())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        # 改进动作闭环（2026-10 第一期）：核对这位主播在练的动作，把本场能检查的问题变成新动作。后台跑，失败只记日志，
        # 不影响总结和直播；服务重启丢掉的由补漏循环（review.focus.focus_backfill_loop）接上。
        if sid is not None and getattr(self.settings, "focus_enabled", True):
            async def _focus():
                try:
                    from .review.focus import process_focus
                    await process_focus(self.session_id, sid)
                except Exception as e:  # noqa: BLE001
                    _log_failure("focus_actions", e, session_id=self.session_id)
            focus_task = asyncio.create_task(_focus())
            self._background_tasks.add(focus_task)
            focus_task.add_done_callback(self._background_tasks.discard)

    # ---------- 采集 → 音频队列（非阻塞，绝不卡住采集）----------
    async def _on_audio(self, pcm: bytes, t_ms: int) -> None:
        self._now_ms = max(self._now_ms, t_ms)
        # 声学韵律 tap：算本块响度/过零率，段级聚合出"语气情绪"的声学依据
        try:
            rms, zcr = chunk_features(pcm)
            if rms > 0:
                self._prosody.append((t_ms, rms, zcr))
        except Exception:  # noqa: BLE001  声学失败绝不影响主链路
            pass
        if self._audio_q is None:
            return
        try:
            self._audio_q.put_nowait((pcm, t_ms))
        except asyncio.QueueFull:
            try:
                self._audio_q.get_nowait()      # 丢最旧，保实时
            except asyncio.QueueEmpty:
                pass
            try:
                self._audio_q.put_nowait((pcm, t_ms))
            except asyncio.QueueFull:
                pass

    async def _asr_feeder(self) -> None:
        """从队列取音频喂 ASR，带超时；ASR 卡住只丢这条、不拖垮采集。"""
        fails = 0
        while not self._stopping:
            try:
                pcm, t_ms = await asyncio.wait_for(self._audio_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if self._capture_done.is_set():
                    break
                continue
            if self._asr is None:
                continue
            try:
                await asyncio.wait_for(self._asr.feed(pcm, t_ms), timeout=_ASR_FEED_TIMEOUT)
                fails = 0
            except Exception:  # noqa: BLE001  含 TimeoutError
                fails += 1
                if fails == 5 and not self._asr_degraded:
                    self._asr_degraded = True
                    await bus.publish(self.session_id, {
                        "type": "status", "status": "asr_warn",
                        "error": "语音识别暂时无响应，录音仍在继续，转写可能中断"})

    async def _asr_consumer(self, is_mock: bool) -> None:
        """消费 ASR 事件；真实流中途断开则重连（录音/采集不受影响）。"""
        while not self._stopping:
            asr = self._asr
            try:
                async for ev in asr:
                    if ev.kind == "partial":
                        await bus.publish(self.session_id, {
                            "type": "partial", "text": ev.text, "start_ms": ev.start_ms,
                            "streamer": self._sname(self._current_sid)})
                    else:
                        await self._handle_final(ev.text, ev.start_ms, ev.end_ms, ev.emotion)
            except Exception as e:  # noqa: BLE001
                _log_failure("asr_consumer", e, session_id=self.session_id)
            # ASR 结束：mock / 正在收尾 / 采集已结束 → 不重连
            if is_mock or self._stopping or self._capture_done.is_set():
                break
            await asyncio.sleep(1)
            if self._stopping or self._capture_done.is_set():
                break
            # 真实流中途断开 → 重连一个新 ASR 会话
            self._asr = get_asr_session(self.kb.hotwords())
            if hasattr(self._asr, "start"):
                self._asr.start()
            self._asr_degraded = False
            await bus.publish(self.session_id, {
                "type": "status", "status": "asr_reconnect", "error": "语音识别已重连"})

    async def _on_frame(self, jpeg: bytes, t_ms: int) -> None:
        # OCR 现在只返回弹幕（观众评论）；模型判断无弹幕时回 "无"
        text = (await self._ocr.recognize(jpeg)).strip()
        if not text or text in ("无", "无。", "none", "None") or text == self._last_ocr_text:
            return
        if self._last_ocr_text is not None:
            self._save_ocr(self._last_ocr_text, self._last_ocr_start, t_ms)
        self._last_ocr_text = text
        self._last_ocr_start = t_ms
        self._recent_ocr.append(text)
        # 弹幕仅记录/展示（观众发的内容，不做主播合规点评）
        await bus.publish(self.session_id, {"type": "ocr", "text": text, "t_ms": t_ms})
        if self._current_sid is not None and _looks_like_question(text):
            self._schedule_danmaku_answer(text, t_ms, self._current_sid)

    def _schedule_danmaku_answer(self, text: str, t_ms: int, sid: int) -> None:
        """Apply a session budget before creating any paid answer task."""
        if not getattr(self.settings, "danmaku_answer_enabled", False):
            return
        question = " ".join((text or "").split())[:160]
        key = question.casefold()
        now = time.monotonic()
        max_answers = max(0, int(getattr(self.settings, "danmaku_answer_max_per_session", 20)))
        cooldown = max(0, int(getattr(self.settings, "danmaku_answer_cooldown_sec", 30)))
        if not question or key in self._danmaku_answered:
            return
        if self._danmaku_answer_count >= max_answers:
            return
        if now - self._last_danmaku_answer_at < cooldown:
            return
        self._danmaku_answered.add(key)
        self._danmaku_answer_count += 1
        self._last_danmaku_answer_at = now
        task = asyncio.create_task(self._answer_danmaku(question, t_ms, sid))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _answer_danmaku(self, question: str, t_ms: int, sid: int) -> None:
        try:
            from ..api.anchor import create_alert
            from .embed import retrieve_detailed
            from .llm import get_llm
            kb = await retrieve_detailed(question, top_k=5,
                                         exclude_categories=("优秀节奏", "本店经验"))
            facts = "\n".join(f"- {x['text']}" for x in kb) or "（知识库无相关事实）"
            system = ("你是直播场控助手。根据知识库给主播一条可直接照说的简短回答。"
                      "不得编造产品参数、价格或承诺；资料不足时明确建议主播稍后核实。只输出回答。")
            with llm_context(self.session_id, "danmaku"):
                answer = (await get_llm("fast").complete(
                    system, f"知识库：\n{facts}\n\n观众问题：{question}"))[:500].strip()
            event = create_alert(self.session_id, sid, kind="danmaku_answer", severity="info",
                                 title="观众提问", message=question, quote=question,
                                 suggestion=answer, at_ms=t_ms)
            if event:
                await bus.publish_streamer(sid, event)
        except Exception as exc:  # noqa: BLE001
            _log_failure("danmaku_answer", exc, session_id=self.session_id)

    def _save_ocr(self, text: str, start_ms: int, end_ms: int) -> None:
        with SessionLocal() as db:
            db.add(OcrRecord(session_id=self.session_id, streamer_id=self._current_sid,
                             text=text, start_ms=start_ms, end_ms=end_ms))
            db.commit()

    def _save_critique(self, level, severity, category, start_ms, end_ms,
                       quote="", comment="", score=None, data="", sid=...,
                       llm_provider="", llm_requested_provider="",
                       is_fallback=False) -> None:
        # sid 默认取当前主播；显式传入则归属指定主播（避免改写全局 _current_sid 引发竞态）
        owner = self._current_sid if sid is ... else sid
        with SessionLocal() as db:
            db.add(Critique(
                session_id=self.session_id, streamer_id=owner,
                level=level, severity=_sev(severity) if isinstance(severity, str) else severity,
                category=category, start_ms=start_ms, end_ms=end_ms,
                quote=quote, comment=comment, score=score, data=data,
                llm_provider=llm_provider,
                llm_requested_provider=llm_requested_provider,
                is_fallback=is_fallback,
            ))
            db.commit()

    # ---------- 段级 ----------
    async def _maybe_segment(self, *, force: bool = False) -> None:
        async with self._seg_lock:
            if not self._seg_buffer:
                return
            max_sentences = max(
                1, int(getattr(self.settings, "segment_max_sentences", SEGMENT_MAX_SENTENCES))
            )
            if not force and len(self._seg_buffer) < max_sentences:
                return
            batch = self._seg_buffer[:]
            self._seg_buffer.clear()
            seg_emos = self._seg_emos[:]
            self._seg_emos.clear()
            sid = self._current_sid
        sentences = [b["text"] for b in batch]
        seg_start = batch[0]["start_ms"]
        seg_end = batch[-1]["end_ms"]
        # 节奏指标（语速/停顿/冷场/重复/单品占比/互动），段内弹幕数从库里取
        dm = self._count_danmaku(seg_start, seg_end)
        dm_texts = self._danmaku_texts(seg_start, seg_end)   # 段内公屏文本，供点评判断观众反应
        metrics = compute_rhythm_metrics(batch, kb=self.kb, danmaku_count=dm)
        prosody = summarize_prosody(list(self._prosody), seg_start, seg_end)
        if prosody:
            metrics["energy_cv"] = prosody.get("energy_cv", 0)   # 响度起伏并入指标展示
        self._metrics_by[sid].append(metrics)
        must = [r.get("name", "") for r in (self.kb.script_rules if self.kb else [])]
        try:
            with llm_context(self.session_id, "segment"):
                data = await review_segment(sentences, must, metrics=metrics, prosody=prosody,
                                            danmaku=dm_texts)
        except Exception as exc:  # noqa: BLE001  点评失败（余额不足/网络等）只丢本段卡片，
            # 不能抛进语音识别消费循环（会触发 ASR 重连丢句），也不能打断结班出总结
            _log_failure("segment_review", exc, session_id=self.session_id)
            return
        emotion = (data.get("emotion") or "").strip()
        if emotion:
            self._emotions_by[sid].append(emotion)
        # 实时 ASR 逐句情绪取多数 → 段级 asr_emotion（复用金色🎯展示 + 收播整场聚合）
        if seg_emos:
            from .qwen_emotion import EMO_ZH
            asr_emo = max(set(seg_emos), key=seg_emos.count)
            data["asr_emotion"] = asr_emo
            data["asr_emotion_label"] = EMO_ZH.get(asr_emo, asr_emo)
        # R8：把同一时间段的真实语音情绪、话术与弹幕交给阿里云大模型对齐判断。
        # 失败时不阻断实时复盘；明确且高置信的不匹配同时进入 R7 主播提醒通道。
        try:
            if not getattr(self.settings, "emotion_context_enabled", False):
                emotion_match = None
            else:
                from .emotion_context import analyze_emotion_context
                with llm_context(self.session_id, "emotion_context"):
                    emotion_match = await analyze_emotion_context(
                        actual_emotion=data.get("asr_emotion_label") or emotion,
                        transcript=sentences,
                        danmaku=dm_texts,
                        scene=data.get("scene_label") or data.get("scene") or "",
                    )
            if emotion_match:
                data["emotion_match"] = emotion_match
                if not emotion_match["matched"] and emotion_match["confidence"] >= 0.75:
                    from ..api.anchor import create_alert
                    event = create_alert(
                        self.session_id, sid, kind="emotion_mismatch", severity="warn",
                        title="情绪与语境不匹配",
                        message=emotion_match["reason"] or
                                f"实际{emotion_match['actual_emotion']}，建议{emotion_match['expected_emotion']}",
                        quote=emotion_match["transcript_context"],
                        suggestion=emotion_match["suggestion"], at_ms=seg_start,
                    )
                    if event:
                        await bus.publish_streamer(sid, event)
        except Exception as exc:  # noqa: BLE001
            _log_failure("emotion_context", exc, session_id=self.session_id)
        if data.get("scene"):
            self._scenes_by[sid].append(data["scene"])   # 收集各段场景 → 整场环节结构
        if data.get("loop"):
            self._loops_by[sid].append(data["loop"])     # 收集讲款段闭环 → 整场讲款诊断
        card = f"[{data.get('score')}分] {data.get('comment','')}"
        self._segs_by[sid].append(card)
        # 段级 critique 归属该段主播（传 sid，不改全局），并记录该段时间范围 + 节奏/声学/情绪（供导出/前端）
        seg_quote = " / ".join(t for t in sentences[:2] if t)[:120]   # 段首话术作证据原话
        self._save_critique(CritiqueLevel.segment, Severity.info, "节奏/表现",
                            seg_start, seg_end, seg_quote, data.get("comment", ""), data.get("score"),
                            data=json.dumps({"issues": data.get("issues", []), "metrics": metrics,
                                             "emotion": emotion, "prosody": prosody,
                                             "scene": data.get("scene", ""),
                                             "scene_label": data.get("scene_label", ""),
                                             "loop": data.get("loop"),
                                             "seg_type": data.get("seg_type", ""),
                                             "pace_review": data.get("pace_review", ""),
                                             "audience": data.get("audience", ""),
                                             "highlights": data.get("highlights", []),
                                             "gaps": data.get("gaps", []),
                                             "asr_emotion": data.get("asr_emotion", ""),
                                             "asr_emotion_label": data.get("asr_emotion_label", ""),
                                             "emotion_match": data.get("emotion_match"),
                                             "llm": data.get("llm", ""),
                                             "llm_requested": data.get("llm_requested", ""),
                                             "is_fallback": bool(data.get("is_fallback"))},
                                            ensure_ascii=False),
                            sid=sid, llm_provider=data.get("llm", ""),
                            llm_requested_provider=data.get("llm_requested", ""),
                            is_fallback=bool(data.get("is_fallback")))
        await bus.publish(self.session_id, {
            "type": "segment", "score": data.get("score"),
            "comment": data.get("comment", ""), "issues": data.get("issues", []),
            "metrics": metrics, "emotion": emotion,
            "llm": data.get("llm", self._llm_name),
            "llm_requested": data.get("llm_requested", self._llm_name),
            "is_fallback": bool(data.get("is_fallback")),
            "scene": data.get("scene", ""), "scene_label": data.get("scene_label", ""),
            "loop": data.get("loop"), "asr_emotion_label": data.get("asr_emotion_label", ""),
            "emotion_match": data.get("emotion_match"),
            "seg_type": data.get("seg_type", ""), "pace_review": data.get("pace_review", ""),
            "audience": data.get("audience", ""), "highlights": data.get("highlights", []),
            "gaps": data.get("gaps", []),
            "streamer_id": sid, "streamer": self._sname(sid),
        })
        # 实时预测预警：按该主播历史规律，当前段缺关键环节且历史高频缺 → 推提示
        try:
            from .predictor import predict
            preds = await asyncio.to_thread(predict, sid, data.get("scene", ""),
                                            data.get("loop"), self.session_id)
            for p in preds:
                await bus.publish(self.session_id, {
                    "type": "predict", "tip": p["tip"], "prob": p["prob"],
                    "streamer_id": sid, "streamer": self._sname(sid)})
        except Exception as e:  # noqa: BLE001
            _log_failure("prediction", e, session_id=self.session_id)

    def _count_danmaku(self, start_ms: int, end_ms: int) -> int:
        with SessionLocal() as db:
            return db.query(OcrRecord).filter(
                OcrRecord.session_id == self.session_id,
                OcrRecord.start_ms >= start_ms,
                OcrRecord.start_ms <= end_ms,
            ).count()

    def _danmaku_texts(self, start_ms: int, end_ms: int, cap: int = 20) -> list[str]:
        """段内公屏文本（供段级点评判断观众反应/情绪语境）。"""
        with SessionLocal() as db:
            rows = db.query(OcrRecord).filter(
                OcrRecord.session_id == self.session_id,
                OcrRecord.start_ms >= start_ms,
                OcrRecord.start_ms <= end_ms,
            ).order_by(OcrRecord.start_ms).limit(cap).all()
            return [r.text for r in rows if (r.text or "").strip()]

    @staticmethod
    def _digest_sample(sents: list[str], cap: int = 90) -> str:
        """整场摘要分层采样：保开场 + 中段均匀抽 + 末段，避免长直播只看到结尾。"""
        if len(sents) <= cap:
            return "\n".join(sents)
        head, tail = sents[:20], sents[-30:]
        mid_pool = sents[20:-30]
        need = cap - len(head) - len(tail)
        if mid_pool and need > 0:
            step = max(1, len(mid_pool) // need)
            mid = mid_pool[::step][:need]
        else:
            mid = []
        return "\n".join([*head, "…（中段节选）…", *mid, "…（接近尾声）…", *tail])

    async def _segment_timer(self) -> None:
        interval = max(30, self.settings.segment_interval_sec)
        try:
            while not self._stopping:
                await asyncio.sleep(interval)
                await self._maybe_segment(force=True)
        except asyncio.CancelledError:
            pass

    # ---------- 句级 ----------
    async def _handle_final(self, raw: str, start_ms: int, end_ms: int,
                            emotion: str = "") -> None:
        self._now_ms = max(self._now_ms, end_ms)
        sid = self._current_sid
        if self._observation or not getattr(self.settings, "realtime_correction_enabled", False):
            corrected = (raw or "").strip()
        else:
            with llm_context(self.session_id, "correction"):
                corrected = await correct_sentence(
                    raw, self.kb.glossary() if self.kb else [],
                )
        with SessionLocal() as db:
            transcript = Transcript(
                session_id=self.session_id, streamer_id=sid, seq=self._seq,
                start_ms=start_ms, end_ms=end_ms, raw_text=raw, corrected_text=corrected,
            )
            db.add(transcript)
            db.flush()
            transcript_id = transcript.id
            db.commit()
        self._seq += 1
        self._sent_by[sid].append(corrected)
        if (sid is not None and not self._observation
                and getattr(self.settings, "realtime_review_enabled", False)):
            try:
                self._realtime_q.put_nowait(
                    (corrected, start_ms, end_ms, sid, list(self._sent_by[sid][-4:]))
                )
            except asyncio.QueueFull:
                print("[r7] 实时质检队列已满，跳过一条非阻塞语义检查")
        await bus.publish(self.session_id, {
            "type": "sentence", "seq": self._seq, "text": corrected, "raw": raw,
            "start_ms": start_ms, "end_ms": end_ms,
            "streamer_id": sid, "streamer": self._sname(sid),
        })

        # 观摩学习只保留音频、逐字稿和弹幕，不产生任何实时质检/点评/主播提醒。
        if self._observation:
            return

        from .compliance_rules import evaluate_sentence
        rule_decisions = evaluate_sentence(
            self.session_id, transcript_id, corrected, start_ms,
        )
        rule_flags = [
            Flag(
                "error", "违禁词", decision["keyword"],
                f"命中禁止规则「{decision['keyword']}」，存在合规风险，请立即纠正话术。",
            )
            for decision in rule_decisions if decision["result"] == "violation"
        ]

        for f in [*rule_flags, *check_sentence(
            corrected, self.kb, list(self._recent_ocr), check_forbidden=False,
        )]:
            self._save_critique(CritiqueLevel.sentence, f.severity, f.category,
                                start_ms, end_ms, f.quote, f.comment, sid=sid)
            self._flags_by[sid].append(f"[{f.category}] {f.comment}")
            await bus.publish(self.session_id, {
                "type": "flag", "source": "speech", "severity": f.severity,
                "category": f.category, "quote": f.quote, "comment": f.comment,
                "start_ms": start_ms, "end_ms": end_ms,
                "streamer_id": sid, "streamer": self._sname(sid),
            })
            from ..api.anchor import create_alert
            kind = "compliance" if f.category == "违禁词" else "product_error"
            event = create_alert(self.session_id, sid, kind=kind, severity=f.severity,
                                 title="合规用语提醒" if kind == "compliance" else "产品讲解纠错",
                                 message=f.comment, quote=f.quote,
                                 suggestion="请立即按提示更正口播", at_ms=start_ms)
            if event and sid is not None:
                await bus.publish_streamer(sid, event)

        self._seg_buffer.append({"text": corrected, "start_ms": start_ms, "end_ms": end_ms})
        if emotion:                       # 实时 ASR 逐句情绪，攒到段结算取多数
            self._seg_emos.append(emotion)
        if any(k in corrected for k in _SWITCH_KW):
            await self._maybe_segment(force=True)
        else:
            await self._maybe_segment()

    # ---------- 主流程 ----------
    async def run(self) -> None:
        # start_pipeline reserves the id before scheduling this coroutine.  The
        # setdefault also protects direct callers used by maintenance/tests.
        if _running.setdefault(self.session_id, self) is not self:
            return
        try:
            await self._run_inner()
        except Exception as e:  # noqa: BLE001
            public_error = "直播处理异常，请稍后重试或联系管理员"
            error_code = safe_exception_code(e, prefix="PIPELINE")
            self._update_session(
                status=SessionStatus.error,
                error_code=error_code,
                error_msg=public_error,
                ended_at=datetime.now(),
            )
            print(
                f"[pipeline] session={self.session_id} failed; code={error_code}; "
                f"error={safe_error_text(e)}"
            )
            await bus.publish(self.session_id, {
                "type": "status", "status": "error", "code": error_code,
                "error": public_error,
            })
        finally:
            # 异常/正常都兜底停掉采集与弹幕，避免 ffmpeg/帧目录/Chromium 泄漏
            self._stopping = True
            if self._capture:
                self._capture.stop()
            if self._danmaku:
                self._danmaku.stop()
            # 任一异常路径都必须离开 live/processing；重复 stop 只会重复写同一终态。
            with SessionLocal() as db:
                row = db.get(ReplaySession, self.session_id)
                if row and row.status in (SessionStatus.live, SessionStatus.processing):
                    row.status = SessionStatus.error
                    row.report_status = "failed"
                    row.error_code = row.error_code or "PIPELINE_FINALIZE_INCOMPLETE"
                    row.error_msg = row.error_msg or "直播收尾未完整完成"
                    row.ended_at = row.ended_at or datetime.now()
                    db.commit()
            if _running.get(self.session_id) is self:
                _running.pop(self.session_id, None)
            bus.clear_recent(self.session_id)   # 结束后清回放缓存（之后走历史详情）

    def _load_context(self):
        with SessionLocal() as db:
            row = db.get(ReplaySession, self.session_id)
            stream_url = row.stream_url
            share_link = row.share_link
            room_id = row.room_id
            self._observation = row.session_type == SessionType.observation
            try:
                lineup = json.loads(row.lineup) if row.lineup else []
            except json.JSONDecodeError:
                lineup = []
            init_sid = row.current_streamer_id or (lineup[0] if lineup else None)
            for s in db.query(Streamer).all():
                self._streamers[s.id] = {"name": s.name, "color": s.color}
            self.kb = load_kb(db)
        return stream_url, init_sid, share_link, room_id

    async def _refresh_stream_url(self) -> str | None:
        """抖音拉流地址过期时重新解析拿新签名（供 capture 重连用），并更新库里地址供视频代理。"""
        if not self._share_link:
            return None
        from .douyin import resolve_stream
        try:
            res = await resolve_stream(share_link=self._share_link,
                                       cookie=self.settings.douyin_cookie)
        except Exception:  # noqa: BLE001
            return None
        url = res.stream_url or None
        if url:
            self._update_session(stream_url=url)
        return url

    def _set_danmaku_status(self, status: str) -> None:
        if self._danmaku_status == status:
            return
        self._danmaku_status = status
        try:
            self._update_session(danmaku_status=status)
        except Exception as e:  # noqa: BLE001  只是个标记，写不上不影响直播
            _log_failure("danmaku_status", e, session_id=self.session_id)

    async def _on_danmaku_hooked(self) -> None:
        """弹幕列表挂上了（哪怕暂时没人发言）：场次记为采到了。"""
        self._set_danmaku_status("ok")

    async def _on_danmaku(self, nick: str, content: str) -> None:
        """真实弹幕（无头浏览器抓取）：落库 + 推到弹幕面板。"""
        if self._danmaku_status != "ok":
            self._set_danmaku_status("ok")
        t_ms = self._now_ms
        shown = f"{nick}：{content}" if nick else content
        self._save_ocr(shown, t_ms, t_ms)
        await bus.publish(self.session_id, {"type": "ocr", "text": shown, "t_ms": t_ms})
        if self._observation:
            return
        if self._current_sid is not None:
            from ..api.anchor import create_alert
            from .danmaku_alert import analyze_danmaku_alert

            alert = analyze_danmaku_alert(content)
            if alert:
                event = create_alert(
                    self.session_id, self._current_sid, at_ms=t_ms, **alert
                )
                if event:
                    await bus.publish_streamer(self._current_sid, event)
        if self._current_sid is not None and _looks_like_question(content):
            self._schedule_danmaku_answer(content, t_ms, self._current_sid)

    async def _realtime_review_worker(self) -> None:
        from ..api.anchor import create_alert
        from .realtime_review import analyze_realtime_sentence
        while True:
            item = await self._realtime_q.get()
            if item is None:
                self._realtime_q.task_done()
                return
            text, start_ms, end_ms, sid, context = item
            try:
                with llm_context(self.session_id, "realtime_review"):
                    alert = await analyze_realtime_sentence(text, context)
                if alert:
                    category = "AI实时合规" if alert["kind"] == "compliance" else "AI产品纠错"
                    self._save_critique(CritiqueLevel.sentence, alert["severity"], category,
                                        start_ms, end_ms, alert["quote"] or text,
                                        alert["message"], sid=sid)
                    event = create_alert(self.session_id, sid, kind=alert["kind"],
                                         severity=alert["severity"], title=alert["title"],
                                         message=alert["message"], quote=alert["quote"] or text,
                                         suggestion=alert["suggestion"], at_ms=start_ms)
                    if event:
                        await bus.publish_streamer(sid, event)
            except Exception as exc:  # noqa: BLE001
                print(
                    "[r7] 大模型实时质检降级为规则检测; "
                    f"code={safe_exception_code(exc, prefix='REALTIME_REVIEW')}; "
                    f"error={safe_error_text(exc)}"
                )
            finally:
                self._realtime_q.task_done()

    async def _run_inner(self) -> None:
        stream_url, init_sid, share_link, room_id = self._load_context()
        self._share_link = share_link or ""
        # 无真实拉流地址：仅 mock 演示环境(asr_provider=mock)才走合成测试流；
        # 生产(真实 provider)解析不到地址=链接无效/未开播，明确报错，不放 mock 彩条误导用户
        is_mock_capture = (not stream_url) and self.settings.asr_provider == "mock"
        if not is_mock_capture and not stream_url:
            msg = "链接无效或直播未开播：没解析到拉流地址，请粘贴正在直播的抖音分享链接后重试。"
            self._update_session(status=SessionStatus.error, error_msg=msg)
            await bus.publish(self.session_id, {"type": "error", "message": msg})
            await bus.publish(self.session_id, {"type": "status", "status": "error"})
            return

        from .llm import get_llm
        self._llm_name = "disabled" if self._observation else get_llm().name

        if is_mock_capture:
            self._asr = MockASR(self.kb.hotwords())
            self._ocr = MockOCR()
        else:
            self._asr = get_asr_session(self.kb.hotwords())
            self._ocr = MockOCR()  # 真实场弹幕走无头浏览器，OCR 不再用于弹幕

        self._audio_q = asyncio.Queue(maxsize=_AUDIO_Q_MAX)
        use_hls_plan = runtime.record_video() and media_store.enabled()
        # HLS 模式：视频走切片直传，audio_file 是伴随音频成品（m4a）；
        # 单文件模式：视频 mp4 / 纯音频 m4a
        ext = "m4a" if (use_hls_plan or not runtime.record_video()) else "mp4"
        audio_file = self.settings.audio_path / f"session_{self.session_id}.{ext}"
        use_hls = use_hls_plan
        self._capture = FFmpegCapture(
            stream_url, audio_file,
            frame_interval_sec=self.settings.frame_interval_sec,
            on_audio=self._on_audio,
            on_frame=self._on_frame if is_mock_capture else None,
            mock=is_mock_capture,
            extract_frames=is_mock_capture,   # 真实场不抽帧（弹幕改用无头浏览器抓 DOM）
            url_provider=None if is_mock_capture else self._refresh_stream_url,
            record_video=runtime.record_video(),
            hls=use_hls,
        )
        # HLS 直传守护：切片实时上云并删本地，磁盘占用恒定（超长直播不爆盘）
        self._hls_uploader: HlsUploader | None = None
        self._hls_task: asyncio.Task | None = None
        if use_hls:
            self._hls_uploader = HlsUploader(
                self.session_id, self._capture.hls_dir, f"replay/session_{self.session_id}")
            self._hls_task = asyncio.create_task(self._hls_uploader.run())

        with SessionLocal() as db:
            row = db.get(ReplaySession, self.session_id)
            if row.audio is None:
                db.add(AudioFile(session_id=self.session_id, filename=audio_file.name,
                                 path=str(audio_file), status=AudioStatus.recording,
                                 media_kind=("video" if runtime.record_video() else "audio")))
            elif row.audio.status != AudioStatus.deleted:
                row.audio.status = AudioStatus.recording
                row.audio.size_bytes = 0
            row.status = SessionStatus.live
            row.report_status = "processing"
            row.deep_status = "not_requested"
            row.error_code = ""
            row.error_msg = ""
            row.started_at = datetime.now()
            row.ended_at = None
            db.commit()
        await bus.publish(self.session_id, {"type": "status", "status": "live"})

        # 开播：开首个主播班次
        await self._open_turn(init_sid, 0)

        if hasattr(self._asr, "start"):
            self._asr.start()

        capture_task = asyncio.create_task(self._capture.run())
        feeder_task = asyncio.create_task(self._asr_feeder())
        consumer_task = asyncio.create_task(self._asr_consumer(is_mock_capture))
        timer_task = asyncio.create_task(self._segment_timer())
        realtime_task = asyncio.create_task(self._realtime_review_worker())

        # 真实抖音场才启动无头浏览器。可控/本地音频源没有直播间 DOM，
        # 启动 Chromium 只会制造孤儿进程和无意义的网络错误。
        if not is_mock_capture and (share_link or room_id):
            from .danmaku import DanmakuScraper
            from .douyin import extract_url
            try:
                entry = extract_url(share_link) if share_link else f"https://live.douyin.com/{room_id}"
            except Exception:  # noqa: BLE001
                entry = f"https://live.douyin.com/{room_id}"
            self._danmaku = DanmakuScraper(entry, self._on_danmaku, asyncio.get_running_loop(),
                                           on_hooked=self._on_danmaku_hooked)
            self._danmaku.start()
            self._set_danmaku_status("pending")

        # 会话生命周期由【采集】驱动：直播结束 / 手动停 → capture 结束（ASR 抖动不影响）
        try:
            await capture_task
        finally:
            self._capture_done.set()

        # 收尾：停弹幕 → 通知 ASR 结束 → 等 feeder/consumer/timer 退出
        self._stopping = True
        if self._danmaku:
            self._danmaku.stop()
        try:
            if hasattr(self._asr, "aclose"):
                await self._asr.aclose()
        except Exception:  # noqa: BLE001
            pass
        timer_task.cancel()
        await asyncio.gather(feeder_task, consumer_task, timer_task, return_exceptions=True)
        await self._realtime_q.join()
        self._realtime_q.put_nowait(None)
        await asyncio.gather(realtime_task, return_exceptions=True)
        if self._danmaku:
            await asyncio.to_thread(self._danmaku.join, 8)
            if self._danmaku.status == "missing" and self._danmaku_status != "ok":
                # 整场都没采到弹幕：记到场次上，整场总结据此不评价公屏互动
                self._set_danmaku_status("missing")
                print(f"[danmaku] session {self.session_id} 整场没采到弹幕，总结里不评价公屏互动")

        if self._last_ocr_text is not None:
            self._save_ocr(self._last_ocr_text, self._last_ocr_start, self._last_ocr_start)
        await self._maybe_segment(force=True)
        self._close_turn(self._now_ms)

        size = audio_file.stat().st_size if audio_file.exists() else 0
        # 拉流是否成功以【上游采集】是否拿到过足量音频为准，而非下游 ASR 转写句数——
        # 否则"音频正常但 ASR 全程失败"会被误判成功、"真实短录音"会被误判失败并误删。
        got_audio = is_mock_capture or (self._capture is not None and self._capture.ever_got_data)
        # 态一：拉流全程失败（从未拿到足量音频）→ 明确报错、删空文件，别静默变"已完成"空场
        if not got_audio:
            if self._hls_uploader is not None:
                await self._hls_uploader.finalize()   # 无切片：只是停守护、清目录
                if self._hls_task:
                    await asyncio.gather(self._hls_task, return_exceptions=True)
            if audio_file.exists() and audio_file.stat().st_size == 0:
                audio_file.unlink(missing_ok=True)
            code = (
                self._capture.error_code or self._capture.finalize_error_code
                or "STREAM_NO_AUDIO"
            )
            msg = (
                self._capture.error_message or self._capture.finalize_error_message
                or "拉流失败：直播间可能未开播，或分享链接/地址已失效。"
                "请确认正在直播后重新粘链接重试。"
            )
            # error 记录保留（autopilot 失败冷却依赖"上一场是拉流失败"判断），
            # 但所有列表接口/前端默认隐藏 error 场次，且 autopilot 定期清理 24h 前的旧记录
            self._mark_media_error(code, msg)
            await bus.publish(self.session_id, {"type": "error", "message": msg})
            await bus.publish(self.session_id, {"type": "status", "status": "error"})
            return
        # 单文件录制必须有经过 ffprobe 可读性校验的非零成品。收尾失败时有效分片仍在，
        # 标为可恢复 error，绝不能把 0 字节或不可读文件发布为 available。
        if self._hls_uploader is None and (
            self._capture.finalize_status != "success" or size <= 0
        ):
            code = self._capture.finalize_error_code or "AUDIO_OUTPUT_EMPTY"
            msg = self._capture.finalize_error_message or "音频成品为空或无法读取"
            self._mark_media_error(code, msg)
            await bus.publish(self.session_id, {"type": "error", "code": code, "message": msg})
            await bus.publish(self.session_id, {"type": "status", "status": "error"})
            return
        # 若录制中用户已「彻底删除」音频：收尾不得复活它——不上传 COS、删掉刚合并的成品、保持 deleted
        with SessionLocal() as db:
            _a = db.get(ReplaySession, self.session_id).audio
            user_deleted = bool(_a and _a.status == AudioStatus.deleted)
        if user_deleted:
            audio_file.unlink(missing_ok=True)
        cos_key = ""
        if self._hls_uploader is not None:
            # HLS 模式：切片已实时上云，这里只收尾（补传残片 + 写 ENDLIST）
            hls_ok, seg_n = await self._hls_uploader.finalize()
            if self._hls_task:
                await asyncio.gather(self._hls_task, return_exceptions=True)
            if hls_ok and media_store.enabled():
                # upload_file 的成功返回不能单独证明索引已经可读；收尾时再做一次
                # HEAD/读取校验，避免把缺少 index.m3u8 的场次标成可播放。
                playlist = await asyncio.to_thread(
                    media_store.get_text, self._hls_uploader.m3u8_key,
                )
                if not playlist or "#EXTM3U" not in playlist:
                    hls_ok = False
                    self._capture.finalize_error_code = "HLS_INDEX_UNREADABLE"
                    self._capture.finalize_error_message = "HLS 索引上传后不可读，媒体未发布"
            if user_deleted:
                await asyncio.to_thread(media_store.delete_prefix, f"replay/session_{self.session_id}/")
            elif hls_ok:
                cos_key = self._hls_uploader.m3u8_key
                print(f"[hls] session {self.session_id} 收尾完成，共 {seg_n} 个切片")
                size = await self._publish_companion_audio(audio_file, size)
            else:
                code = self._capture.finalize_error_code or "HLS_FINALIZE_FAILED"
                msg = self._capture.finalize_error_message or "HLS 切片收尾失败，媒体不可用"
                self._mark_media_error(code, msg)
                await bus.publish(
                    self.session_id, {"type": "error", "code": code, "message": msg},
                )
                await bus.publish(self.session_id, {"type": "status", "status": "error"})
                return
        # 单文件模式：上传 COS 成功后删本地，服务器盘不涨（未配置 COS 则留在本地盘）
        elif not user_deleted and size and media_store.enabled():
            key = f"replay/session_{self.session_id}{audio_file.suffix}"
            if await asyncio.to_thread(media_store.upload_file, str(audio_file), key):
                cos_key = key
                audio_file.unlink(missing_ok=True)
        with SessionLocal() as db:
            row = db.get(ReplaySession, self.session_id)
            if row.audio and not user_deleted:     # 已被用户删除的保持 deleted，绝不复活成 available
                row.audio.status = AudioStatus.available
                row.audio.size_bytes = size
                if cos_key:
                    row.audio.cos_key = cos_key
            row.status = SessionStatus.processing
            row.report_status = "processing"
            if self._capture.error_code:
                row.error_code = self._capture.error_code
                row.error_msg = self._capture.error_message
            row.ended_at = datetime.now()
            db.commit()
        await bus.publish(self.session_id, {"type": "status", "status": "processing"})

        # 态二：拉到了音频、但 ASR 全程零转写（密钥/网络/额度问题）→ 录音已保留，
        # 别静默 done 成空场；标可恢复 error 并提示可重试（不做整场结算/情绪精算）
        if not is_mock_capture and self._seq == 0 and not self._observation:
            msg = "录音已保存，但本场语音识别全程失败（可能是网络或接口额度问题），未生成复盘内容。可稍后重试。"
            self._update_session(
                status=SessionStatus.error, report_status="failed", error_msg=msg,
            )
            await bus.publish(self.session_id, {"type": "error", "message": msg})
            await bus.publish(self.session_id, {"type": "status", "status": "error"})
            return

        if self._observation:
            # 只执行确定性的弹幕热度离线计算；优秀话术/可选情绪由显式提炼接口触发。
            try:
                from .observation import compute_and_store_heat
                await asyncio.to_thread(compute_and_store_heat, self.session_id)
            except Exception as exc:  # noqa: BLE001
                _log_failure("observation_heat", exc, session_id=self.session_id)
            self._update_session(status=SessionStatus.done, report_status="ready")
            await bus.publish(self.session_id, {"type": "status", "status": "done"})
            return

        # 收播核对班次（2026-10-04）：拿上下播打卡对一遍每段记在谁名下；改了归属的，拿到段落的主播总结要重出
        try:
            from .turn_check import reconcile_turns
            gained = await asyncio.to_thread(reconcile_turns, self.session_id)
        except Exception as exc:  # noqa: BLE001  核对失败不影响出总结
            gained = set()
            _log_failure("turn_reconcile", exc, session_id=self.session_id)
        for sid in gained:
            self._track(sid)
            async with self._summarize_lock:
                self._summarized.discard(sid)

        # 态三 正常：结束时给尚未结算的主播（含最后一位）补整场点评（切换时已结算的会跳过）
        summary_failures = []
        for sid in self._order:
            try:
                await self._finalize_streamer_summary(sid)
            except Exception as exc:  # noqa: BLE001
                summary_failures.append(sid)
                _log_failure("whole_session_summary", exc, session_id=self.session_id)

        if summary_failures:
            msg = "采集已完成，整场总结生成失败，可在复盘详情中重新生成"
            with SessionLocal() as db:
                row = db.get(ReplaySession, self.session_id)
                row.status = SessionStatus.done
                row.report_status = "partial"
                row.error_code = "SUMMARY_GENERATION_FAILED"
                row.error_msg = msg
                db.commit()
            await bus.publish(self.session_id, {
                "type": "status", "status": "done", "summary_failed": True,
                "message": msg,
            })
        else:
            report_ready = self._report_artifacts_ready()
            if report_ready:
                self._update_session(status=SessionStatus.done, report_status="ready")
                await bus.publish(self.session_id, {"type": "status", "status": "done"})
            else:
                msg = "采集已完成，但基础复盘产物不完整，请重试生成报告"
                self._update_session(
                    status=SessionStatus.done, report_status="partial",
                    error_code="REPORT_INCOMPLETE", error_msg=msg,
                )
                await bus.publish(self.session_id, {
                    "type": "status", "status": "done", "report_partial": True,
                    "message": msg,
                })

        # R3 必须覆盖主播本场全部轮次：只在采集彻底结束后统一重跑，避免主播返场时
        # 沿用第一次换班时的不完整提取。后台执行，不阻塞主报告落库和 done 状态。
        async def _r3_all_turns():
            try:
                from .review.rhythm_analysis import analyze_session_rhythm
                owners = list(dict.fromkeys(
                    sid for sid in self._order if sid is not None
                ))
                semaphore = asyncio.Semaphore(2)

                async def run(owner_id):
                    async with semaphore:
                        await analyze_session_rhythm(self.session_id, owner_id)
                results = await asyncio.gather(
                    *(run(owner_id) for owner_id in owners),
                    return_exceptions=True,
                )
                for owner_id, result in zip(owners, results):
                    if isinstance(result, BaseException):
                        print(
                            f"[pipeline] R3 节奏分析失败 sid={owner_id}; "
                            f"code={safe_exception_code(result, prefix='RHYTHM')}; "
                            f"error={safe_error_text(result)}"
                        )
            except Exception as e:  # noqa: BLE001
                # 后台任务自身也必须兜底；否则 import/调度异常会成为未消费的 task 异常。
                print(
                    "[pipeline] R3 节奏分析调度失败; "
                    f"code={safe_exception_code(e, prefix='RHYTHM_SCHEDULE')}; "
                    f"error={safe_error_text(e)}"
                )
        r3_task = asyncio.create_task(_r3_all_turns())
        self._background_tasks.add(r3_task)

        def _r3_done(task: asyncio.Task) -> None:
            self._background_tasks.discard(task)
            if task.cancelled():
                return
            try:
                error = task.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                print(
                    "[pipeline] R3 节奏分析后台任务异常; "
                    f"code={safe_exception_code(error, prefix='RHYTHM_TASK')}; "
                    f"error={safe_error_text(error)}"
                )

        r3_task.add_done_callback(_r3_done)

        # 收播后自动用 qwen3-asr-flash 精算段级情绪并聚合整场（离线、后台线程，不阻塞收尾）
        try:
            from .emotion_recompute import recompute_session_emotions
            if runtime.auto_emotion():
                asyncio.create_task(asyncio.to_thread(recompute_session_emotions, self.session_id))
        except Exception as e:  # noqa: BLE001
            print(
                "[pipeline] 自动情绪精算调度失败; "
                f"code={safe_exception_code(e, prefix='EMOTION_SCHEDULE')}; "
                f"error={safe_error_text(e)}"
            )

        # 罗盘数据变化归因 + 成功技巧提炼（离线后台，不阻塞收尾）
        try:
            asyncio.create_task(self._compass_review_and_distill())
        except Exception as e:  # noqa: BLE001
            print(
                "[pipeline] 罗盘归因调度失败; "
                f"code={safe_exception_code(e, prefix='COMPASS_SCHEDULE')}; "
                f"error={safe_error_text(e)}"
            )

    async def _publish_companion_audio(self, audio_file, size: int) -> int:  # noqa: ANN001
        """切片直传场次的伴随音频（导出/按主播裁剪/情绪用）：合并成功才传 COS，传完删本地。

        合并失败或超时（2026-10-03 改）：不上传——半截文件没有索引、读不了，传上去会被当成整段
        音频用——原始分片留在服务器上，事后可手工重新合并；切片视频照常可回看。
        返回要记进 audio_files.size_bytes 的值（切片媒体本身可用，没有伴随音频时记 1）。
        """
        if self._capture.finalize_status == "success" and size and audio_file.exists():
            akey = f"replay/session_{self.session_id}.m4a"
            if await asyncio.to_thread(media_store.upload_file, str(audio_file), akey):
                audio_file.unlink(missing_ok=True)
            return size
        print(
            f"[hls] session {self.session_id} 伴随音频没合并成"
            f"（code={self._capture.finalize_error_code or '-'}），不上传；"
            "原始分片保留在服务器上，切片视频回看不受影响"
        )
        return 1

    async def _compass_review_and_distill(self) -> None:
        """收播后：罗盘归因 → 全变化持续采样 → 相似动作统计达标后待人工采纳。

        单次正向变化只会进入 observing 统计账本，不会直接成为 pending 或写知识库。
        离线后台执行，任何异常不影响主复盘。
        """
        try:
            from .compass_review import analyze_compass_review
            from .review.experience import save_candidates
            result = await analyze_compass_review(self.session_id)
            if result.get("changes"):
                self._save_critique(
                    CritiqueLevel.summary, Severity.info, "罗盘归因", 0, 0,
                    comment=result.get("summary", ""),
                    data=json.dumps({"changes": result["changes"],
                                     "summary": result.get("summary", "")}, ensure_ascii=False),
                    sid=None)
            samples = result.get("samples") or result.get("changes") or []
            if samples:
                sid = self._order[0] if self._order else None
                n = await save_candidates(samples, streamer_id=sid)
                if n:
                    print(f"[compass] session {self.session_id} 有 {n} 类技巧统计达标，待人工采纳")
        except Exception as e:  # noqa: BLE001
            print(
                "[compass] 罗盘归因/提炼失败; "
                f"code={safe_exception_code(e, prefix='COMPASS')}; "
                f"error={safe_error_text(e)}"
            )
        # 标准自生长（2026-07-10）：片段效果对齐 → 模式账本记账 → 比例制晋升。
        # 纯算法+向量，异常不影响主复盘。
        try:
            from .pattern_mining import mine_session
            st = await mine_session(self.session_id)
            if st.get("recorded"):
                print(f"[pattern] session {self.session_id} 记账 {st['recorded']} 段"
                      f"（新组 {st['new_groups']}，晋升 {st['promoted']}）")
        except Exception as e:  # noqa: BLE001
            print(
                "[pattern] 模式挖掘失败; "
                f"code={safe_exception_code(e, prefix='PATTERN')}; "
                f"error={safe_error_text(e)}"
            )
        # 罗盘归因/上报可能晚于主报告；此处重算明确声明执行的模板效果，并用 CAS
        # 回写报告。未声明执行的“仅对比”记录不会被误标有效。
        try:
            from .review.rhythm_analysis import refresh_session_validations

            refreshed = await asyncio.to_thread(
                refresh_session_validations, self.session_id,
            )
            if refreshed:
                print(f"[rhythm] session {self.session_id} 已刷新 {refreshed} 条模板验证")
        except Exception as e:  # noqa: BLE001
            print(
                "[rhythm] 模板效果验证失败; "
                f"code={safe_exception_code(e, prefix='RHYTHM_VALIDATE')}; "
                f"error={safe_error_text(e)}"
            )

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        if self._capture:
            self._capture.stop()
        if self._danmaku:
            self._danmaku.stop()


def start_pipeline(session_id: int) -> bool:
    if session_id in _running:
        return False
    pipeline = ReplayPipeline(session_id)
    # Reserve synchronously before create_task.  Without this reservation two
    # consecutive/concurrent start requests can both schedule an ffmpeg worker.
    _running[session_id] = pipeline
    try:
        task = asyncio.create_task(pipeline.run())
        _pipeline_tasks[session_id] = task

        def discard(done: asyncio.Task[None]) -> None:
            if _pipeline_tasks.get(session_id) is done:
                _pipeline_tasks.pop(session_id, None)

        task.add_done_callback(discard)
    except BaseException:
        if _running.get(session_id) is pipeline:
            _running.pop(session_id, None)
        raise
    return True


def stop_pipeline(session_id: int) -> bool:
    p = _running.get(session_id)
    if p:
        p.stop()
        return True
    return False


def get_running(session_id: int) -> "ReplayPipeline | None":
    return _running.get(session_id)


async def stop_all_pipelines(timeout: float = 20.0) -> None:
    """Stop and await all capture workers during service shutdown."""
    for pipeline in list(_running.values()):
        pipeline.stop()
    tasks = [task for task in _pipeline_tasks.values() if not task.done()]
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
