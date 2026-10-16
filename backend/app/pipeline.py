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
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

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
    Severity,
    Streamer,
    StreamerTurn,
    Transcript,
)
from . import media_store, runtime
from .asr_stream import MockASR, get_asr_session
from .capture import FFmpegCapture
from .corrector import correct_sentence
from .knowledge import load_kb
from .ocr import MockOCR, get_ocr
from .prosody import chunk_features, summarize_prosody
from .review.rhythm import aggregate_metrics, compute_rhythm_metrics
from .review.segment import review_segment
from .review.sentence import check_sentence
from .review.summary import review_summary
from .core.safe_error import safe_error_text, safe_exception_code

SEGMENT_MAX_SENTENCES = 5
_SWITCH_KW = ["下一款", "换一款", "接着讲", "下一个产品", "再看下一"]

_AUDIO_Q_MAX = 64          # ≈ 64*200ms = 12.8s 缓冲，满了丢最旧保实时
_ASR_FEED_TIMEOUT = 3.0    # 单次喂音频上限，超时即视为 ASR 卡顿

_running: dict[int, "ReplayPipeline"] = {}


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

        # 采集 / ASR 解耦
        self._asr = None
        self._llm_name = ""
        self._audio_q: asyncio.Queue | None = None
        self._capture_done = asyncio.Event()
        self._stopping = False
        self._asr_degraded = False
        self._share_link = ""

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

    # ---------- DB 小工具 ----------
    def _update_session(self, **fields) -> None:
        with SessionLocal() as db:
            row = db.get(ReplaySession, self.session_id)
            for k, v in fields.items():
                setattr(row, k, v)
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

    async def switch_streamer(self, sid: int) -> None:
        """切换当前主播：冲洗当前段（归属离场主播）→ 关旧班次、开新班次 →
        离场主播的整场点评放后台生成（不阻塞切换，稍后经 WS 推达）。"""
        await self._maybe_segment(force=True)
        outgoing = self._current_sid
        self._close_turn(self._now_ms)
        await self._open_turn(sid, self._now_ms)
        if outgoing is not None:
            asyncio.create_task(self._finalize_streamer_summary(outgoing))

    async def _finalize_streamer_summary(self, sid) -> None:
        """对某主播本场表现出整场点评（切换或结束时调用，幂等）。"""
        async with self._summarize_lock:
            if sid in self._summarized:
                return
            self._summarized.add(sid)
        sents = self._sent_by.get(sid, [])
        if not sents and not self._flags_by.get(sid):
            return
        digest = self._digest_sample(sents)
        rhythm = aggregate_metrics(self._metrics_by.get(sid, []))
        emotions = self._emotions_by.get(sid, [])
        data = await review_summary(digest, self._flags_by.get(sid, []),
                                    self._segs_by.get(sid, []), rhythm=rhythm, emotions=emotions,
                                    scenes=self._scenes_by.get(sid, []),
                                    loops=self._loops_by.get(sid, []))
        self._save_critique(CritiqueLevel.summary, Severity.info, "整场总结",
                            0, 0, "", data.get("summary", ""), data.get("overall_score"),
                            data=json.dumps(data, ensure_ascii=False), sid=sid)
        await bus.publish(self.session_id, {
            "type": "summary", "streamer_id": sid, "streamer": self._sname(sid),
            "llm": self._llm_name, **data})

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
                print(
                    "[legacy-pipeline] stage=asr_consumer; "
                    f"code={safe_exception_code(e, prefix='PIPELINE')}; "
                    f"error={safe_error_text(e)}"
                )
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

    def _save_ocr(self, text: str, start_ms: int, end_ms: int) -> None:
        with SessionLocal() as db:
            db.add(OcrRecord(session_id=self.session_id, streamer_id=self._current_sid,
                             text=text, start_ms=start_ms, end_ms=end_ms))
            db.commit()

    def _save_critique(self, level, severity, category, start_ms, end_ms,
                       quote="", comment="", score=None, data="", sid=...) -> None:
        # sid 默认取当前主播；显式传入则归属指定主播（避免改写全局 _current_sid 引发竞态）
        owner = self._current_sid if sid is ... else sid
        with SessionLocal() as db:
            db.add(Critique(
                session_id=self.session_id, streamer_id=owner,
                level=level, severity=_sev(severity) if isinstance(severity, str) else severity,
                category=category, start_ms=start_ms, end_ms=end_ms,
                quote=quote, comment=comment, score=score, data=data,
            ))
            db.commit()

    # ---------- 段级 ----------
    async def _maybe_segment(self, *, force: bool = False) -> None:
        async with self._seg_lock:
            if not self._seg_buffer:
                return
            if not force and len(self._seg_buffer) < SEGMENT_MAX_SENTENCES:
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
        data = await review_segment(sentences, must, metrics=metrics, prosody=prosody,
                                    danmaku=dm_texts)
        emotion = (data.get("emotion") or "").strip()
        if emotion:
            self._emotions_by[sid].append(emotion)
        # 实时 ASR 逐句情绪取多数 → 段级 asr_emotion（复用金色🎯展示 + 收播整场聚合）
        if seg_emos:
            from .qwen_emotion import EMO_ZH
            asr_emo = max(set(seg_emos), key=seg_emos.count)
            data["asr_emotion"] = asr_emo
            data["asr_emotion_label"] = EMO_ZH.get(asr_emo, asr_emo)
        if data.get("scene"):
            self._scenes_by[sid].append(data["scene"])   # 收集各段场景 → 整场环节结构
        if data.get("loop"):
            self._loops_by[sid].append(data["loop"])     # 收集讲款段闭环 → 整场讲款诊断
        card = f"[{data.get('score')}分] {data.get('comment','')}"
        self._segs_by[sid].append(card)
        # 段级 critique 归属该段主播（传 sid，不改全局），并记录该段时间范围 + 节奏/声学/情绪（供导出/前端）
        self._save_critique(CritiqueLevel.segment, Severity.info, "节奏/表现",
                            seg_start, seg_end, "", data.get("comment", ""), data.get("score"),
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
                                             "asr_emotion_label": data.get("asr_emotion_label", "")},
                                            ensure_ascii=False),
                            sid=sid)
        await bus.publish(self.session_id, {
            "type": "segment", "score": data.get("score"),
            "comment": data.get("comment", ""), "issues": data.get("issues", []),
            "metrics": metrics, "emotion": emotion, "llm": self._llm_name,
            "scene": data.get("scene", ""), "scene_label": data.get("scene_label", ""),
            "loop": data.get("loop"), "asr_emotion_label": data.get("asr_emotion_label", ""),
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
            print(
                "[legacy-pipeline] stage=prediction; "
                f"code={safe_exception_code(e, prefix='PIPELINE')}; "
                f"error={safe_error_text(e)}"
            )

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
        corrected = await correct_sentence(raw, self.kb.glossary() if self.kb else [])
        with SessionLocal() as db:
            db.add(Transcript(
                session_id=self.session_id, streamer_id=sid, seq=self._seq,
                start_ms=start_ms, end_ms=end_ms, raw_text=raw, corrected_text=corrected,
            ))
            db.commit()
        self._seq += 1
        self._sent_by[sid].append(corrected)
        await bus.publish(self.session_id, {
            "type": "sentence", "seq": self._seq, "text": corrected, "raw": raw,
            "start_ms": start_ms, "end_ms": end_ms,
            "streamer_id": sid, "streamer": self._sname(sid),
        })

        for f in check_sentence(corrected, self.kb, list(self._recent_ocr)):
            self._save_critique(CritiqueLevel.sentence, f.severity, f.category,
                                start_ms, end_ms, f.quote, f.comment, sid=sid)
            self._flags_by[sid].append(f"[{f.category}] {f.comment}")
            await bus.publish(self.session_id, {
                "type": "flag", "source": "speech", "severity": f.severity,
                "category": f.category, "quote": f.quote, "comment": f.comment,
                "start_ms": start_ms, "end_ms": end_ms,
                "streamer_id": sid, "streamer": self._sname(sid),
            })

        self._seg_buffer.append({"text": corrected, "start_ms": start_ms, "end_ms": end_ms})
        if emotion:                       # 实时 ASR 逐句情绪，攒到段结算取多数
            self._seg_emos.append(emotion)
        if any(k in corrected for k in _SWITCH_KW):
            await self._maybe_segment(force=True)
        else:
            await self._maybe_segment()

    # ---------- 主流程 ----------
    async def run(self) -> None:
        _running[self.session_id] = self
        try:
            await self._run_inner()
        except Exception as e:  # noqa: BLE001
            self._update_session(
                status=SessionStatus.error,
                error_msg="直播处理异常，请稍后重试或联系管理员",
            )
            await bus.publish(self.session_id,
                              {"type": "status", "status": "error",
                               "error": "直播处理异常，请稍后重试或联系管理员"})
        finally:
            # 异常/正常都兜底停掉采集与弹幕，避免 ffmpeg/帧目录/Chromium 泄漏
            self._stopping = True
            if self._capture:
                self._capture.stop()
            if self._danmaku:
                self._danmaku.stop()
            _running.pop(self.session_id, None)
            bus.clear_recent(self.session_id)   # 结束后清回放缓存（之后走历史详情）

    def _load_context(self):
        with SessionLocal() as db:
            row = db.get(ReplaySession, self.session_id)
            stream_url = row.stream_url
            share_link = row.share_link
            room_id = row.room_id
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

    async def _on_danmaku(self, nick: str, content: str) -> None:
        """真实弹幕（无头浏览器抓取）：落库 + 推到弹幕面板。"""
        t_ms = self._now_ms
        shown = f"{nick}：{content}" if nick else content
        self._save_ocr(shown, t_ms, t_ms)
        await bus.publish(self.session_id, {"type": "ocr", "text": shown, "t_ms": t_ms})

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
        self._llm_name = get_llm().name   # 点评用的 LLM；mock 时前端会打"假数据"标

        if is_mock_capture:
            self._asr = MockASR(self.kb.hotwords())
            self._ocr = MockOCR()
        else:
            self._asr = get_asr_session(self.kb.hotwords())
            self._ocr = MockOCR()  # 真实场弹幕走无头浏览器，OCR 不再用于弹幕

        self._audio_q = asyncio.Queue(maxsize=_AUDIO_Q_MAX)
        ext = "mp4" if runtime.record_video() else "m4a"
        audio_file = self.settings.audio_path / f"session_{self.session_id}.{ext}"
        self._capture = FFmpegCapture(
            stream_url, audio_file,
            frame_interval_sec=self.settings.frame_interval_sec,
            on_audio=self._on_audio,
            on_frame=self._on_frame if is_mock_capture else None,
            mock=is_mock_capture,
            extract_frames=is_mock_capture,   # 真实场不抽帧（弹幕改用无头浏览器抓 DOM）
            url_provider=None if is_mock_capture else self._refresh_stream_url,
            record_video=runtime.record_video(),
        )

        with SessionLocal() as db:
            row = db.get(ReplaySession, self.session_id)
            if row.audio is None:
                db.add(AudioFile(session_id=self.session_id, filename=audio_file.name,
                                 path=str(audio_file), status=AudioStatus.recording,
                                 media_kind=("video" if runtime.record_video() else "audio")))
            row.status = SessionStatus.live
            row.started_at = datetime.now()
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

        # 真实场：无头浏览器抓弹幕（独立线程 + 默认 loop，避免 Playwright 与 uvloop 冲突）
        if not is_mock_capture:
            from .danmaku import DanmakuScraper
            from .douyin import extract_url
            try:
                entry = extract_url(share_link) if share_link else f"https://live.douyin.com/{room_id}"
            except Exception:  # noqa: BLE001
                entry = f"https://live.douyin.com/{room_id}"
            self._danmaku = DanmakuScraper(entry, self._on_danmaku, asyncio.get_running_loop())
            self._danmaku.start()

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
        if self._danmaku:
            await asyncio.to_thread(self._danmaku.join, 8)

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
            audio_file.unlink(missing_ok=True)
            msg = "拉流失败：直播间可能未开播，或分享链接/地址已失效。请确认正在直播后重新粘链接重试。"
            with SessionLocal() as db:
                row = db.get(ReplaySession, self.session_id)
                if row.audio:
                    row.audio.status = AudioStatus.deleted
                row.status = SessionStatus.error
                row.error_msg = msg
                row.ended_at = datetime.now()
                db.commit()
            await bus.publish(self.session_id, {"type": "error", "message": msg})
            await bus.publish(self.session_id, {"type": "status", "status": "error"})
            return
        # 若录制中用户已「彻底删除」音频：收尾不得复活它——不上传 COS、删掉刚合并的成品、保持 deleted
        with SessionLocal() as db:
            _a = db.get(ReplaySession, self.session_id).audio
            user_deleted = bool(_a and _a.status == AudioStatus.deleted)
        if user_deleted:
            audio_file.unlink(missing_ok=True)
        # 上传 COS：成功后删本地，服务器盘不涨（未配置 COS 则留在本地盘）
        cos_key = ""
        if not user_deleted and size and media_store.enabled():
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
            row.ended_at = datetime.now()
            db.commit()
        await bus.publish(self.session_id, {"type": "status", "status": "processing"})

        # 态二：拉到了音频、但 ASR 全程零转写（密钥/网络/额度问题）→ 录音已保留，
        # 别静默 done 成空场；标可恢复 error 并提示可重试（不做整场结算/情绪精算）
        if not is_mock_capture and self._seq == 0:
            msg = "录音已保存，但本场语音识别全程失败（可能是网络或接口额度问题），未生成复盘内容。可稍后重试。"
            self._update_session(status=SessionStatus.error, error_msg=msg)
            await bus.publish(self.session_id, {"type": "error", "message": msg})
            await bus.publish(self.session_id, {"type": "status", "status": "error"})
            return

        # 态三 正常：结束时给尚未结算的主播（含最后一位）补整场点评（切换时已结算的会跳过）
        for sid in self._order:
            await self._finalize_streamer_summary(sid)

        self._update_session(status=SessionStatus.done)
        await bus.publish(self.session_id, {"type": "status", "status": "done"})

        # 收播后自动用 qwen3-asr-flash 精算段级情绪并聚合整场（离线、后台线程，不阻塞收尾）
        try:
            from ..config import get_settings
            from .emotion_recompute import recompute_session_emotions
            if runtime.auto_emotion():
                asyncio.create_task(asyncio.to_thread(recompute_session_emotions, self.session_id))
        except Exception as e:  # noqa: BLE001
            print(
                "[legacy-pipeline] stage=emotion_schedule; "
                f"code={safe_exception_code(e, prefix='PIPELINE')}; "
                f"error={safe_error_text(e)}"
            )

    def stop(self) -> None:
        self._stopping = True
        if self._capture:
            self._capture.stop()
        if self._danmaku:
            self._danmaku.stop()


def start_pipeline(session_id: int) -> None:
    if session_id in _running:
        return
    asyncio.create_task(ReplayPipeline(session_id).run())


def stop_pipeline(session_id: int) -> None:
    p = _running.get(session_id)
    if p:
        p.stop()


def get_running(session_id: int) -> "ReplayPipeline | None":
    return _running.get(session_id)
