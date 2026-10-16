"""流式 ASR 抽象。

接口：feed(pcm, t_ms) 喂音频；对象本身是 ASREvent 的异步迭代器，产出
partial（实时滚动字幕，不入库）与 final（定句，入库 + 触发校正/点评）。

mock 只允许显式的本地合成测试流使用。真实直播唯一使用 DashScope
``paraformer-realtime-v2``；配置不完整时抛错，绝不回退模拟数据。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass

from ..config import get_settings
from .safe_error import safe_exception_code

logger = logging.getLogger(__name__)
REAL_ASR_PROVIDER = "dashscope"
REAL_ASR_MODEL = "paraformer-realtime-v2"


class ASRConfigurationError(RuntimeError):
    """真实直播 ASR 未按生产要求配置。异常文本不包含任何凭据。"""


def real_asr_configuration_error(settings=None) -> str:
    settings = settings or get_settings()
    provider = (settings.asr_provider or "").strip().lower()
    if provider != REAL_ASR_PROVIDER:
        return f"ASR_PROVIDER 必须设置为 {REAL_ASR_PROVIDER}"
    if (settings.asr_model or "").strip() != REAL_ASR_MODEL:
        return f"ASR_MODEL 必须设置为 {REAL_ASR_MODEL}"
    if not (settings.aliyun_dashscope_api_key or "").strip():
        return "ALIYUN_DASHSCOPE_API_KEY 未配置"
    return ""


def ensure_real_asr_configured(settings=None) -> None:
    error = real_asr_configuration_error(settings)
    if error:
        raise ASRConfigurationError(error)


def _request_fingerprint(value: str) -> str:
    return hashlib.sha256((value or "unknown").encode("utf-8")).hexdigest()[:12]

# mock 脚本：每条都"埋"了点东西，对应知识库里的规格/价格/违禁词
_MOCK_SCRIPT = [
    "大家好 欢迎来到 LiveReview 官方直播间 今天给大家带来旗舰平板耳机",
    "这款 演示旗舰 是我们的旗舰 阻抗只有六欧姆 非常好推 手机直接就能推",
    "今天直播间活动价 两万九千八 原价三万六",
    "再看这款 Demo Air 演示轻量 今天直播间特价 四千九百九",
    "我跟大家保证 这个耳机能治疗耳鸣 戴上保证根治听力问题",
    "全网最低价 错过今天就再也没有了 赶紧下单",
    "好 我们接着讲下一款产品",
]

# 每条句子在时间轴上的近似跨度（毫秒），mock 用
_MOCK_SPAN_MS = 4000


@dataclass
class ASREvent:
    kind: str          # "partial" | "final"
    text: str
    start_ms: int
    end_ms: int
    emotion: str = ""  # 实时情绪(7类英文枚举)；仅 qwen3-asr-flash-realtime 有，其它为空


class MockASR:
    def __init__(self, hotwords: list[str] | None = None) -> None:
        self.hotwords = hotwords or []
        self._q: asyncio.Queue[ASREvent | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._cursor_ms = 0

    async def feed(self, pcm: bytes, t_ms: int) -> None:
        # mock 不消费音频，仅在首次喂入时启动脚本生成
        if self._task is None:
            self._task = asyncio.create_task(self._generate())

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._generate())

    async def _generate(self) -> None:
        try:
            for line in _MOCK_SCRIPT:
                start = self._cursor_ms
                end = start + _MOCK_SPAN_MS
                await asyncio.sleep(1.0)
                half = max(1, len(line) // 2)
                await self._q.put(ASREvent("partial", line[:half], start, start))
                await asyncio.sleep(0.3)
                await self._q.put(ASREvent("final", line, start, end))
                self._cursor_ms = end
        finally:
            await self._q.put(None)

    def __aiter__(self) -> "MockASR":
        return self

    async def __anext__(self) -> ASREvent:
        ev = await self._q.get()
        if ev is None:
            raise StopAsyncIteration
        return ev

    async def aclose(self) -> None:
        if self._task:
            self._task.cancel()
        await self._q.put(None)


class DashScopeASR:
    """阿里云百炼 Paraformer 实时语音识别（WebSocket 双工流式）。

    协议：连接 → 发 run-task → 收 task-started → 持续发 PCM 二进制帧 →
    收 result-generated（含 partial/句末）→ aclose() 发 finish-task → 收 task-finished。
    与 MockASR 接口一致：feed/start/异步迭代 ASREvent/aclose。出错时安全收尾。
    """

    name = "dashscope"
    URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"

    def __init__(self, api_key: str, model: str, hotwords: list[str] | None = None) -> None:
        self.api_key = api_key
        self.model = model
        self.hotwords = hotwords or []
        self._q: asyncio.Queue[ASREvent | None] = asyncio.Queue()
        self._ws = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._task_id = uuid.uuid4().hex
        self._closed = False
        self._request_id = self._task_id
        self._started_at = 0.0
        self._status = "starting"

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def feed(self, pcm: bytes, t_ms: int) -> None:
        if self._task is None:
            self.start()
        await self._ready.wait()
        if self._ws is not None and not self._closed:
            try:
                await self._ws.send(pcm)        # 二进制音频帧
            except Exception:  # noqa: BLE001
                pass

    async def _run(self) -> None:
        import websockets
        self._started_at = time.monotonic()
        try:
            headers = {
                "Authorization": f"bearer {self.api_key}",
                "X-DashScope-DataInspection": "enable",
            }
            async with websockets.connect(
                self.URL, additional_headers=headers, max_size=None
            ) as ws:
                self._ws = ws
                run_task = {
                    "header": {"action": "run-task", "task_id": self._task_id,
                               "streaming": "duplex"},
                    "payload": {"task_group": "audio", "task": "asr",
                                "function": "recognition", "model": self.model,
                                "parameters": {"format": "pcm", "sample_rate": 16000},
                                "input": {}},
                }
                await ws.send(json.dumps(run_task))
                async for msg in ws:
                    if isinstance(msg, (bytes, bytearray)):
                        continue
                    data = json.loads(msg)
                    header = data.get("header", {})
                    self._request_id = header.get("request_id") or self._request_id
                    event = header.get("event")
                    if event == "task-started":
                        self._status = "running"
                        self._ready.set()
                    elif event == "result-generated":
                        sent = data.get("payload", {}).get("output", {}).get("sentence", {})
                        text = (sent.get("text") or "").strip()
                        if text:
                            kind = "final" if sent.get("sentence_end") else "partial"
                            await self._q.put(ASREvent(
                                kind, text, sent.get("begin_time") or 0,
                                sent.get("end_time") or 0))
                    elif event in ("task-finished", "task-failed"):
                        if event == "task-failed":
                            self._status = "failed"
                            code = re.sub(
                                r"[^A-Z0-9_]+", "_",
                                str(header.get("error_code") or "UNKNOWN").upper(),
                            )[:64]
                            logger.warning(
                                "asr_request request_id=%s model=%s elapsed_ms=%d "
                                "status=failed code=%s",
                                _request_fingerprint(self._request_id), self.model,
                                int((time.monotonic() - self._started_at) * 1000), code,
                            )
                        else:
                            self._status = "finished"
                        break
        except Exception as e:  # noqa: BLE001
            self._status = "failed"
            # 只记录异常类型，避免 SDK/网络异常把鉴权头或完整请求写入日志。
            logger.warning(
                "asr_request request_id=%s model=%s elapsed_ms=%d status=failed code=%s",
                _request_fingerprint(self._request_id), self.model,
                int((time.monotonic() - self._started_at) * 1000),
                safe_exception_code(e, prefix="ASR"),
            )
        finally:
            elapsed_ms = int((time.monotonic() - self._started_at) * 1000)
            logger.info(
                "asr_request request_id=%s model=%s elapsed_ms=%d status=%s",
                _request_fingerprint(self._request_id), self.model, elapsed_ms, self._status,
            )
            self._ready.set()                    # 解除 feed 等待
            await self._q.put(None)

    async def aclose(self) -> None:
        self._closed = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(json.dumps({
                    "header": {"action": "finish-task", "task_id": self._task_id,
                               "streaming": "duplex"},
                    "payload": {"input": {}},
                }))
            except Exception:  # noqa: BLE001
                pass
            try:                                  # 给服务端一点时间回传最后一句（进 _q 尾）
                await asyncio.sleep(1.0)
            except Exception:  # noqa: BLE001
                pass
            try:                                  # 关 WS → _run 结束 → finally 放哨兵
                await asyncio.wait_for(ws.close(), timeout=3)
            except Exception:  # noqa: BLE001
                pass
        # 双保险：直接入队哨兵，确保消费端立刻收到终止、不永久阻塞
        await self._q.put(None)
        if self._task is not None:
            self._task.cancel()

    def __aiter__(self) -> "DashScopeASR":
        return self

    async def __anext__(self) -> ASREvent:
        ev = await self._q.get()
        if ev is None:
            raise StopAsyncIteration
        return ev


class QwenRealtimeASR:
    """阿里云百炼 Qwen3-ASR-Flash 实时识别（OpenAI-Realtime 风格 WebSocket）。

    相比 Paraformer 多一个能力：转写同时**逐句返回情绪**(7 类)。协议：
    连 wss://…/api-ws/v1/realtime?model=… → 收 session.created → 发 session.update
    (input_audio_format=pcm/16k, server_vad) → 收 session.updated 就绪 →
    持续发 input_audio_buffer.append(base64 PCM) →
    收 …input_audio_transcription.text(增量,文本在 stash) / .completed(整句 transcript)，
    两者顶层带 emotion。与其它 ASR 接口一致：feed/start/异步迭代 ASREvent/aclose。
    realtime 事件无时间戳，用采集侧游标(feed 的 t_ms)近似。
    """

    name = "qwen"
    URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={model}"

    def __init__(self, api_key: str, model: str, hotwords: list[str] | None = None) -> None:
        self.api_key = api_key
        self.model = model
        self.hotwords = hotwords or []
        self._q: asyncio.Queue[ASREvent | None] = asyncio.Queue()
        self._ws = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._closed = False
        self._last_t = 0        # 最近喂入音频的时间游标(ms)
        self._seg_start = 0     # 上一整句结束位置，作为下一句起点
        self._started_at = 0.0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def feed(self, pcm: bytes, t_ms: int) -> None:
        if self._task is None:
            self.start()
        self._last_t = t_ms
        await self._ready.wait()
        if self._ws is not None and not self._closed:
            import base64
            try:
                await self._ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm).decode(),
                }))
            except Exception:  # noqa: BLE001
                pass

    async def _run(self) -> None:
        import websockets
        self._started_at = time.monotonic()
        try:
            url = self.URL.format(model=self.model)
            headers = {"Authorization": f"bearer {self.api_key}"}
            async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
                self._ws = ws
                async for msg in ws:
                    if isinstance(msg, (bytes, bytearray)):
                        continue
                    data = json.loads(msg)
                    ev = data.get("type", "")
                    if ev == "session.created":
                        await ws.send(json.dumps({"type": "session.update", "session": {
                            "modalities": ["text"], "input_audio_format": "pcm",
                            "sample_rate": 16000, "input_audio_transcription": {},
                            "turn_detection": {"type": "server_vad", "threshold": 0.2,
                                               "prefix_padding_ms": 300,
                                               "silence_duration_ms": 800}}}))
                    elif ev == "session.updated":
                        self._ready.set()
                    elif ev.endswith("input_audio_transcription.text"):
                        txt = (data.get("stash") or "").strip()
                        if txt:
                            await self._q.put(ASREvent("partial", txt, self._last_t,
                                                       self._last_t, data.get("emotion") or ""))
                    elif ev.endswith("input_audio_transcription.completed"):
                        txt = (data.get("transcript") or "").strip()
                        if txt:
                            start = self._seg_start or self._last_t
                            self._seg_start = self._last_t
                            await self._q.put(ASREvent("final", txt, start, self._last_t,
                                                       data.get("emotion") or ""))
                    elif ev == "error":
                        logger.warning(
                            "asr_request request_id=%s model=%s elapsed_ms=%d "
                            "status=failed code=QWEN_PROVIDER_ERROR",
                            _request_fingerprint("qwen-realtime"), self.model,
                            int((time.monotonic() - self._started_at) * 1000),
                        )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "asr_request request_id=%s model=%s elapsed_ms=%d status=failed code=%s",
                _request_fingerprint("qwen-realtime"), self.model,
                int((time.monotonic() - self._started_at) * 1000),
                safe_exception_code(e, prefix="QWEN_ASR"),
            )
        finally:
            self._ready.set()
            await self._q.put(None)

    async def aclose(self) -> None:
        self._closed = True
        ws = self._ws
        if ws is not None:
            try:                                  # 促使最后一句断句
                await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            except Exception:  # noqa: BLE001
                pass
            try:                                  # 给服务端一点时间回传最后一句 completed（进 _q 尾）
                await asyncio.sleep(1.0)
            except Exception:  # noqa: BLE001
                pass
            try:                                  # 关 WS → _run 的 async for 结束 → finally 放哨兵
                await asyncio.wait_for(ws.close(), timeout=3)
            except Exception:  # noqa: BLE001
                pass
        # 双保险：直接入队哨兵。FIFO 保证它排在已入队的 final 之后，既不丢最后一句、又不永久阻塞
        await self._q.put(None)
        if self._task is not None:
            self._task.cancel()

    def __aiter__(self) -> "QwenRealtimeASR":
        return self

    async def __anext__(self) -> ASREvent:
        ev = await self._q.get()
        if ev is None:
            raise StopAsyncIteration
        return ev


def get_asr_session(hotwords: list[str] | None = None):
    s = get_settings()
    ensure_real_asr_configured(s)
    return DashScopeASR(s.aliyun_dashscope_api_key, REAL_ASR_MODEL, hotwords)
