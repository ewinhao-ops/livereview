"""ffmpeg 拉流采集。

一个 ffmpeg 进程，单输入三路输出：
  1) 音频 → 16k 单声道 PCM 走 stdout(pipe:1)，按块喂流式 ASR；
  2) 录音 → AAC 写【音频分段文件】，收尾时合并成单一音频（唯一持久化的媒体）；
  3) 抽帧 → 每 N 秒一张 JPEG 写到临时目录，watcher 读取后【立即删除】，绝不入库。

长直播稳定性（P0）：抖音拉流地址带 sign/expire（数分钟过期），ffmpeg 退出后
若有 url_provider 则【刷新拉流地址再重连】；每次重连写新分段，收尾 concat，
保证录音跨重连不丢。判定直播结束：连续多次"拉起即断"则停止。
mock 模式不依赖真实直播，用 lavfi 合成正弦音 + 测试卡帧，便于无密钥跑通全链路。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx

from .safe_error import safe_error_text

AudioCb = Callable[[bytes, int], Awaitable[None]]    # (pcm_chunk, t_ms)
FrameCb = Callable[[bytes, int], Awaitable[None]]     # (jpeg_bytes, t_ms)
UrlProvider = Callable[[], Awaitable[str | None]]     # 返回新的拉流地址（刷新签名）

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2                                  # s16le 单声道
CHUNK_MS = 200
CHUNK_BYTES = SAMPLE_RATE * BYTES_PER_SAMPLE * CHUNK_MS // 1000
# 判"本次拉起真的拿到了流"的最小连续音频量（约 2 秒 PCM）。
# 单个非空 chunk 不足以证明拉流成功——坏流也可能吐几字节握手静音后即断。
MIN_VALID_AUDIO_BYTES = SAMPLE_RATE * BYTES_PER_SAMPLE * 2
PROCESS_STOP_TIMEOUT_SEC = 5.0
# 收尾合并的限时按分片总大小放宽（2026-10-03 改）：原来固定 30 秒，9-29 那场近 16 小时的伴随音频
# （385MB）合并到一半被掐断。当天在服务器上实测「合并 + faststart 整理」约 10MB/秒，这里按 2MB/秒估、
# 留约 5 倍余量（直播收尾时机器还在忙别的）；最少 FINALIZE_TIMEOUT_SEC，最多 FINALIZE_MAX_TIMEOUT_SEC。
# 超时放宽一倍再试一次；还不行就判失败：不出成品、原始分片原样保留。
FINALIZE_TIMEOUT_SEC = 60.0
FINALIZE_BYTES_PER_SEC = 2 * 1024 * 1024
FINALIZE_MAX_TIMEOUT_SEC = 1800.0
PROBE_TIMEOUT_SEC = 5.0

logger = logging.getLogger(__name__)

_HTTP_GONE_RE = re.compile(
    r"(?i)(?:http(?:s)?[^\r\n]*\b(404|410)\b|server returned\s+(404|410)|"
    r"http error\s+(404|410)|\b(404|410)\s+(?:not found|gone)\b)"
)
def _safe_log_text(value: object, limit: int = 500) -> str:
    return safe_error_text(value, fallback="no error details", limit=limit)


def finalize_timeout(total_bytes: int) -> float:
    """一次合并最多等多少秒：最少 FINALIZE_TIMEOUT_SEC，按 2MB/秒放宽，封顶 FINALIZE_MAX_TIMEOUT_SEC。"""
    return min(FINALIZE_MAX_TIMEOUT_SEC,
               FINALIZE_TIMEOUT_SEC + max(0, int(total_bytes or 0)) / FINALIZE_BYTES_PER_SEC)


class FFmpegCapture:
    def __init__(
        self,
        stream_url: str,
        audio_file: Path,
        *,
        frame_interval_sec: int = 3,
        on_audio: AudioCb | None = None,
        on_frame: FrameCb | None = None,
        mock: bool = False,
        mock_duration_sec: int = 16,
        extract_frames: bool = True,
        url_provider: UrlProvider | None = None,
        record_video: bool = False,
        hls: bool = False,
    ) -> None:
        self.stream_url = stream_url
        self.audio_file = Path(audio_file)        # 成品媒体文件（.mp4 录像 / .m4a 录音）
        self.hls = hls and record_video           # HLS 直传模式：视频切片实时上云，不落大文件
        self.hls_dir = self.audio_file.parent / f"{self.audio_file.stem}_hls"
        self.frame_interval_sec = max(1, frame_interval_sec)
        self.on_audio = on_audio
        self.on_frame = on_frame
        self.mock = mock
        self.mock_duration_sec = mock_duration_sec
        self.extract_frames = extract_frames
        self.url_provider = url_provider
        self.record_video = record_video

        self._proc: asyncio.subprocess.Process | None = None
        self._stop = asyncio.Event()
        self._frames_dir = Path(tempfile.mkdtemp(prefix="replay_frames_"))
        self._audio_ms = 0
        self._frame_seq = 0
        # 跨重连录音分段
        self._part_idx = 0
        self._cur_audio = self.audio_file
        self._audio_parts: list[Path] = []
        # 拉流成功判据：ffmpeg 对 404 会内部无限重连不退出，靠"无数据看门狗"识别失败。
        # _got_data_round：本轮累计音频是否已达最小有效量（供看门狗/放弃判定用，每轮重置）
        # _round_bytes：本轮累计音频字节；ever_got_data：整场是否有过一轮拿到足量连续音频
        # （权威的"拉流是否成功过"信号，收尾判定用它而非下游 ASR 的转写句数）
        self._got_data_round = False
        self._round_bytes = 0
        self.ever_got_data = False
        self._last_ffmpeg_error = ""
        # 最近一次收到音频数据的时刻（monotonic 秒；0=从未）。停录宽限探活用：
        # 打卡下播后要停录前先看流是否还活着，活着就不强停
        self.last_data_ts = 0.0
        # 采集终止原因与音频收尾结果由 pipeline 统一落库。
        self.end_reason = ""
        self.error_code = ""
        self.error_message = ""
        self.finalize_status = "pending"       # pending | success | error | timeout
        self.finalize_error_code = ""
        self.finalize_error_message = ""
        self._termination_task: asyncio.Task | None = None
        self._termination_proc: asyncio.subprocess.Process | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._finalize_proc: asyncio.subprocess.Process | None = None
        self._finalize_lock = asyncio.Lock()
        self._finalized = False

    # ---------- 命令构造 ----------
    def _ffmpeg_cmd(self) -> list[str]:
        if self.mock:
            inputs = [
                # -re 让合成流按真实时间推进（否则瞬间跑完会把识别提前掐断）
                "-re", "-f", "lavfi", "-i", f"sine=frequency=440:duration={self.mock_duration_sec}",
                "-re", "-f", "lavfi", "-i",
                f"testsrc=size=640x360:rate=5:duration={self.mock_duration_sec}",
            ]
            amap, vmap = "0:a", "1:v"
        else:
            host = (urlsplit(self.stream_url).hostname or "").lower()
            pace_controlled_source = host in {"127.0.0.1", "localhost", "::1"}
            inputs = [
                # 本地可控文件源按真实时间推进，避免短音频瞬间灌满实时 ASR。
                *(["-re"] if pace_controlled_source else []),
                # 抖音 CDN 防盗链：必须带 Referer，否则 403
                "-headers", "Referer: https://live.douyin.com/\r\n",
                "-user_agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                # 直播流的网络抖动靠 ffmpeg 原生重连兜一层
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-i", self.stream_url,
            ]
            amap, vmap = "0:a", "0:v"

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            *inputs,
            # 1) PCM → stdout 喂 ASR
            "-map", amap, "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
        ]
        # 2) 录制输出
        if self.hls:
            # HLS 直传：10s 一个 ts 切片，守护实时传 COS 后删本地——磁盘占用恒定，
            # 崩溃最多丢最后一片。文件名带轮次前缀，重连不覆盖、m3u8 侧插 DISCONTINUITY。
            self.hls_dir.mkdir(parents=True, exist_ok=True)
            enc = (["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"]
                   if self.mock else ["-c", "copy"])
            cmd += ["-map", vmap, "-map", amap, *enc,
                    # 切片时间戳必须连续(HLS 规范)——reset_timestamps 曾让每片归零,
                    # 2878 片全部重叠在同一 10s 区间,播放器时间轴彻底错乱(声音拖慢/卡死/无声)
                    "-f", "segment", "-segment_time", "10",
                    "-segment_format", "mpegts",
                    str(self.hls_dir / f"seg_{self._part_idx:02d}_%05d.ts")]
            # 伴随音频小文件（4h≈170MB）：导出/按主播裁剪/情绪识别都用它，
            # 不必从几个 GB 的视频切片里抽音轨；走既有 part→合并→上传链路
            cmd += ["-map", amap, "-c:a", "aac", "-b:a", "96k",
                    "-movflags", "+empty_moov+default_base_moof",
                    "-frag_duration", "1000000",
                    "-flush_packets", "1", "-avioflags", "direct",
                    "-y", str(self._cur_audio)]
        elif self.record_video:
            if self.mock:
                # 合成流是原始帧，需编码；ultrafast 省 CPU
                cmd += ["-map", vmap, "-map", amap, "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                        "-y", str(self._cur_audio)]
            else:
                # 真实流 H.264 直接 copy（不转码、几乎不耗 CPU）。
                # fMP4：普通 mp4 在进程被杀时 moov 没写入、整段报废（重启丢录像的元凶）；
                # fragmented 模式边写边可读，录制中也能导出/试听，收尾合并时再转 faststart 成品
                cmd += ["-map", vmap, "-map", amap, "-c", "copy",
                        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                        "-y", str(self._cur_audio)]
        else:
            cmd += ["-map", amap, "-c:a", "aac", "-b:a", "128k",
                    # ADTS is append/stream friendly and has no trailer index;
                    # a forced Windows process stop cannot destroy earlier audio.
                    "-f", "adts",
                    "-flush_packets", "1", "-avioflags", "direct",
                    "-y", str(self._cur_audio)]
        if self.extract_frames:
            # 3) 抽帧 → 临时目录（用完即删）
            cmd += ["-map", vmap, "-vf", f"fps=1/{self.frame_interval_sec}",
                    "-q:v", "4", "-y", str(self._frames_dir / "f_%06d.jpg")]
        return cmd

    # ---------- 子任务 ----------
    async def _pump_audio(self) -> None:
        assert self._proc and self._proc.stdout
        while not self._stop.is_set():
            chunk = await self._proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            # 累计到最小有效量才算"拉流成功"（单个非空 chunk 不算，防坏流吐零星静音逃过放弃）
            self._round_bytes += len(chunk)
            self.last_data_ts = time.monotonic()
            if not self._got_data_round and self._round_bytes >= MIN_VALID_AUDIO_BYTES:
                self._got_data_round = True
                self.ever_got_data = True
            if self.on_audio:
                await self.on_audio(chunk, self._audio_ms)
            self._audio_ms += len(chunk) * 1000 // (SAMPLE_RATE * BYTES_PER_SAMPLE)

    async def _watch_frames(self) -> None:
        """轮询临时目录，新帧读出 → 回调 → 立即删除（绝不持久化）。"""
        seen: set[str] = set()
        while not self._stop.is_set():
            try:
                files = sorted(p for p in self._frames_dir.glob("f_*.jpg"))
            except FileNotFoundError:
                files = []
            # 跳过最后一个（可能 ffmpeg 还在写）
            for fp in files[:-1] if len(files) > 1 else []:
                if fp.name in seen:
                    continue
                seen.add(fp.name)
                try:
                    data = fp.read_bytes()
                finally:
                    fp.unlink(missing_ok=True)   # 用完即弃
                t_ms = self._frame_seq * self.frame_interval_sec * 1000
                self._frame_seq += 1
                if self.on_frame and data:
                    await self.on_frame(data, t_ms)
            await asyncio.sleep(0.5)

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while not self._stop.is_set():
            line = await self._proc.stderr.readline()
            if not line:
                break
            decoded = line.decode(errors="ignore").rstrip()
            safe_line = _safe_log_text(decoded)
            self._last_ffmpeg_error = safe_line
            gone = _HTTP_GONE_RE.search(decoded)
            if gone:
                status = int(next(group for group in gone.groups() if group))
                self._set_terminal_http_error(status, source="ffmpeg")
                self._stop.set()
                if self._proc.returncode is None:
                    try:
                        self._proc.kill()
                    except ProcessLookupError:
                        pass
            # ffmpeg 警告留痕，便于排查解析/拉流问题
            logger.warning("[ffmpeg] %s", safe_line)

    def _set_terminal_http_error(self, status: int, *, source: str) -> None:
        self.end_reason = "live_ended_or_stream_expired"
        self.error_code = f"STREAM_HTTP_{status}"
        self.error_message = (
            f"拉流地址明确返回 HTTP {status}，直播已结束或拉流地址已永久失效"
            f"（来源：{source}）"
        )

    async def _probe_stream_url(self) -> tuple[str, int | None, str]:
        """Small ranged GET before ffmpeg; only 404/410 are terminal."""
        headers = {
            "Referer": "https://live.douyin.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Range": "bytes=0-1",
        }
        try:
            timeout = httpx.Timeout(PROBE_TIMEOUT_SEC)
            # 拉流不继承 HTTP_PROXY：签名 CDN 和本地可控源都应直接连接，
            # 否则 127.0.0.1 也可能被错误送到企业代理而探测失败。
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, trust_env=False,
            ) as client:
                async with client.stream("GET", self.stream_url, headers=headers) as response:
                    status = response.status_code
            if status in (404, 410):
                return "gone", status, f"HTTP {status}"
            if 200 <= status < 400:
                return "ok", status, ""
            # 403 and other statuses refresh/retry; they are not proof that the live ended.
            return "retry", status, f"HTTP {status}"
        except httpx.TimeoutException:
            return "retry", None, "probe timeout"
        except httpx.RequestError as exc:
            return "retry", None, f"network error ({type(exc).__name__})"
        except Exception as exc:  # noqa: BLE001
            return "retry", None, f"probe error ({type(exc).__name__})"

    async def _refresh_url(self) -> bool:
        if self.url_provider is None:
            return False
        try:
            new_url = await self.url_provider()
            if new_url:
                changed = new_url != self.stream_url
                self.stream_url = new_url
                if changed:
                    logger.info("[capture] 已刷新拉流地址，准备重连")
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[capture] 刷新拉流地址失败; exception=%s; error=%s",
                type(exc).__name__, _safe_log_text(exc),
            )
        return False

    async def _no_data_watchdog(self, deadline_sec: float = 25.0) -> None:
        """拉起后 deadline 秒内一直没收到音频字节 → 判定这次拉流没起来，
        杀掉 ffmpeg（否则它会对 404 内部无限重连、永不退出，导致场次卡 live）。
        一旦收到过数据就认为流正常，看门狗退出、不再干预（掉线重连交给外层）。"""
        waited = 0.0
        while waited < deadline_sec:
            if self._stop.is_set() or self._got_data_round:
                return
            await asyncio.sleep(1.0)
            waited += 1.0
        if not self._got_data_round and self._proc and self._proc.returncode is None:
            self.error_code = "STREAM_NO_DATA_TIMEOUT"
            self.error_message = f"拉起后 {int(deadline_sec)} 秒未收到有效音频数据"
            print(f"[capture] 拉起后 {int(deadline_sec)}s 未收到任何音频数据，判定拉流未成功，终止本次并重试")
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass

    # ---------- 生命周期 ----------
    async def run(self) -> None:
        """启动并阻塞至直播结束/停止。掉流/地址过期时刷新地址重连（mock 不重连）。"""
        self._loop = asyncio.get_running_loop()
        try:
            await self._run_loop()
        except asyncio.CancelledError:
            self.stop()
            raise
        except Exception as exc:  # noqa: BLE001
            self.end_reason = self.end_reason or "ffmpeg_error"
            self.error_code = self.error_code or "CAPTURE_RUNTIME_ERROR"
            self.error_message = self.error_message or (
                f"采集异常退出（{type(exc).__name__}）：{_safe_log_text(exc)}"
            )
            logger.error(
                "[capture] 采集异常; exception=%s; error=%s",
                type(exc).__name__, _safe_log_text(exc),
            )
        finally:
            budget = self._finalize_budget()
            try:
                # 外层兜底：两次合并的限时之和再留一点余量（合并自己会先按限时收手）
                await asyncio.wait_for(self._finalize_audio(), timeout=budget)
            except asyncio.TimeoutError:
                self.finalize_status = "timeout"
                self.finalize_error_code = "AUDIO_FINALIZE_TIMEOUT"
                self.finalize_error_message = (
                    f"音频收尾超过 {budget:.0f} 秒，已终止合并进程；原始分片保留在服务器上"
                )
                await self._kill_finalize_process()
                self._discard_partial_output()
                logger.error("[capture] %s", self.finalize_error_message)
            except Exception as exc:  # noqa: BLE001
                self.finalize_status = "error"
                self.finalize_error_code = "AUDIO_FINALIZE_ERROR"
                self.finalize_error_message = (
                    f"音频收尾失败（{type(exc).__name__}）：{_safe_log_text(exc)}"
                )
                logger.error("[capture] %s", self.finalize_error_message)
            self._cleanup_frames_dir()

    async def _run_loop(self) -> None:
        fast_fails = 0
        while not self._stop.is_set():
            if not self.mock:
                probe, status, probe_error = await self._probe_stream_url()
                if probe == "gone":
                    self._set_terminal_http_error(int(status), source="preflight")
                    logger.info("[capture] %s", self.error_message)
                    break
                if probe == "retry":
                    fast_fails += 1
                    self.error_code = (
                        f"STREAM_PROBE_HTTP_{status}" if status is not None
                        else "STREAM_PROBE_NETWORK_ERROR"
                    )
                    self.error_message = f"拉流前有效性探测失败：{probe_error}"
                    logger.warning(
                        "[capture] 拉流前探测失败; attempt=%s; code=%s; error=%s",
                        fast_fails, self.error_code, probe_error,
                    )
                    await self._refresh_url()
                    if fast_fails >= 3:
                        self.end_reason = "stream_url_invalid"
                        break
                    await asyncio.sleep(min(5, 1 + fast_fails))
                    continue
                if self.error_code.startswith("STREAM_PROBE_"):
                    self.error_code = ""
                    self.error_message = ""

            # 本次拉起写入新分段（跨重连录音不被 -y 覆盖丢失）
            self._cur_audio = self._part_path(self._part_idx)
            self._part_idx += 1
            self._audio_parts.append(self._cur_audio)

            self._got_data_round = False
            self._round_bytes = 0
            self._last_ffmpeg_error = ""
            self._proc = await asyncio.create_subprocess_exec(
                *self._ffmpeg_cmd(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._termination_task = None
            self._termination_proc = None
            tasks = [
                asyncio.create_task(self._pump_audio()),
                asyncio.create_task(self._drain_stderr()),
            ]
            if not self.mock:
                tasks.append(asyncio.create_task(self._no_data_watchdog()))
            if self.extract_frames:
                tasks.append(asyncio.create_task(self._watch_frames()))
            await self._wait_current_process(self._proc)
            if self.mock:
                self._stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # 收尾再扫一遍残帧并清空
            await self._flush_remaining_frames()

            if self._termination_task is not None:
                await asyncio.gather(self._termination_task, return_exceptions=True)
            if self._stop.is_set() or self.mock:
                break

            if (self._proc.returncode not in (None, 0)
                    and self.error_code != "STREAM_NO_DATA_TIMEOUT"):
                self.error_code = f"FFMPEG_EXIT_{self._proc.returncode}"
                detail = f"：{self._last_ffmpeg_error}" if self._last_ffmpeg_error else ""
                self.error_message = f"ffmpeg 异常退出，返回码 {self._proc.returncode}{detail}"

            # 真实流：ffmpeg 退出 = 掉线 / 地址过期 / 直播结束 / 看门狗判无数据杀掉。
            # 判定失败以"本轮是否收到过音频"为准：收到过=真流只是抖动(归零、继续重连兜)，
            # 全程无数据=拉流没起来(累积、够次数就放弃)。比单看存活时长更准。
            fast_fails = 0 if self._got_data_round else fast_fails + 1
            if self._got_data_round and (
                self.error_code.startswith("FFMPEG_EXIT_")
                or self.error_code == "STREAM_NO_DATA_TIMEOUT"
            ):
                self.error_code = ""
                self.error_message = ""
            if fast_fails >= 3:
                self.end_reason = "stream_url_invalid"
                if not self.error_code:
                    self.error_code = "STREAM_RETRY_EXHAUSTED"
                    self.error_message = "连续 3 次拉流均未收到有效音频数据"
                print("[capture] 连续多次拉起均无音频数据，判定直播未开播/地址失效，停止采集")
                break
            # 关键：抖音拉流地址带 sign/expire，过期后必须刷新再重连
            await self._refresh_url()
            await asyncio.sleep(min(5, 1 + fast_fails))   # 退避重连

    async def _flush_remaining_frames(self) -> None:
        for fp in sorted(self._frames_dir.glob("f_*.jpg")):
            try:
                data = fp.read_bytes()
            finally:
                fp.unlink(missing_ok=True)
            t_ms = self._frame_seq * self.frame_interval_sec * 1000
            self._frame_seq += 1
            if self.on_frame and data:
                await self.on_frame(data, t_ms)

    def _part_path(self, idx: int) -> Path:
        suffix = self.audio_file.suffix if self.record_video else ".aac"
        return self.audio_file.parent / f"{self.audio_file.stem}.part{idx}{suffix}"

    def _set_finalize_error(self, code: str, message: str, *, status: str = "error") -> bool:
        self.finalize_status = status
        self.finalize_error_code = code
        self.finalize_error_message = message
        return False

    def _parts_total_bytes(self) -> int:
        total = 0
        for part in self._audio_parts:
            try:
                total += max(0, part.stat().st_size)
            except OSError:
                continue
        return total

    def _finalize_budget(self) -> float:
        first = finalize_timeout(self._parts_total_bytes())
        second = min(first * 2, FINALIZE_MAX_TIMEOUT_SEC)
        return (first + second) * 1.2 + min(PROBE_TIMEOUT_SEC, first)

    def _discard_partial_output(self) -> None:
        """合并没成的成品文件是半截（没有索引、读不了）：删掉，免得后面当成品上传。分片不动。"""
        try:
            if self._audio_parts and self.audio_file not in self._audio_parts:
                self.audio_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[capture] 半截合并文件没删掉; error=%s", _safe_log_text(exc))

    async def _validate_media_file(self, path: Path) -> bool:
        if not path.exists() or path.stat().st_size <= 0:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return proc.returncode == 0 and bool(stdout.strip())
        except (FileNotFoundError, asyncio.TimeoutError, OSError):
            return False

    async def _kill_finalize_process(self) -> None:
        proc = self._finalize_proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.error("[capture] 音频合并进程 kill 后仍未退出")

    async def _merge_once(self, listfile: Path, timeout: float) -> tuple[str, str]:
        """跑一次 ffmpeg 合并：返回 ("ok"|"timeout"|"failed", 失败原因)。超时会杀掉合并进程。"""
        self._finalize_proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listfile),
            "-c", "copy", "-movflags", "+faststart", "-y", str(self.audio_file),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                self._finalize_proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._kill_finalize_process()
            return "timeout", ""
        if self._finalize_proc.returncode != 0:
            return "failed", _safe_log_text((stderr or b"").decode(errors="ignore"))
        return "ok", ""

    async def _finalize_audio(self) -> bool:
        """合并各重连分段为单一音频（跨重连连续），并 +faststart 便于按段快速跳转回听。

        合并限时按分片总大小放宽；超时放宽一倍再试一次。合并失败或超时：删掉半截成品、
        原始分片原样保留（事后可以手工重新合并），状态记 error/timeout，不出成品。
        """
        async with self._finalize_lock:
            if self._finalized:
                return self.finalize_status == "success"
            self._finalized = True
            part_stats = [
                (part, part.stat().st_size if part.exists() else -1)
                for part in self._audio_parts
            ]
            logger.info(
                "[capture] 音频收尾; parts=%s; sizes=%s",
                len(part_stats), [size for _part, size in part_stats],
            )
            parts = [part for part, size in part_stats if size > 0]
            if not parts:
                return self._set_finalize_error(
                    "AUDIO_NO_VALID_PARTS", "没有存在且非 0 字节的有效音频分片",
                )

            listfile = self.audio_file.parent / f"{self.audio_file.stem}.concat.txt"
            listfile.write_text(
                "\n".join(f"file '{part.as_posix()}'" for part in parts), encoding="utf-8",
            )
            total = sum(size for _part, size in part_stats if size > 0)
            timeout = finalize_timeout(total)
            try:
                for attempt in (1, 2):
                    outcome, detail = await self._merge_once(listfile, timeout)
                    if outcome == "ok":
                        break
                    self._discard_partial_output()
                    if outcome == "failed":
                        logger.error("[capture] 音频合并失败，原始分片已保留; parts=%s; bytes=%s",
                                     len(parts), total)
                        return self._set_finalize_error(
                            "AUDIO_MERGE_FAILED", f"ffmpeg 合并失败：{detail}",
                        )
                    if attempt == 2:
                        logger.error(
                            "[capture] 音频合并两次都超时，原始分片已保留; bytes=%s; last_timeout=%.0fs",
                            total, timeout,
                        )
                        return self._set_finalize_error(
                            "AUDIO_FINALIZE_TIMEOUT",
                            f"音频合并超时（{total / 1024 / 1024:.0f}MB，第二次等了 {timeout:.0f} 秒），"
                            "已终止；原始分片保留在服务器上",
                            status="timeout",
                        )
                    longer = min(timeout * 2, FINALIZE_MAX_TIMEOUT_SEC)
                    logger.warning(
                        "[capture] 音频合并超时，放宽后再试一次; bytes=%s; timeout=%.0fs→%.0fs",
                        total, timeout, longer,
                    )
                    timeout = longer
                if not await self._validate_media_file(self.audio_file):
                    self._discard_partial_output()
                    return self._set_finalize_error(
                        "AUDIO_OUTPUT_UNREADABLE", "合并后的媒体文件为 0 字节或无法读取",
                    )
                self.finalize_status = "success"
                self.finalize_error_code = ""
                self.finalize_error_message = ""
                for part in parts:
                    part.unlink(missing_ok=True)
                return True
            except asyncio.CancelledError:
                await self._kill_finalize_process()
                self._discard_partial_output()
                raise
            finally:
                listfile.unlink(missing_ok=True)
                self._finalize_proc = None

    async def _terminate_after_timeout(self, proc: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=PROCESS_STOP_TIMEOUT_SEC)
            return
        except asyncio.TimeoutError:
            logger.warning("[capture] ffmpeg terminate 5 秒未退出，执行 kill")
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.error("[capture] ffmpeg kill 后仍未确认退出，继续统一收尾")

    async def _wait_current_process(self, proc: asyncio.subprocess.Process) -> None:
        """Wait normally, but let the bounded stop task release lifecycle finalization."""
        process_wait = asyncio.create_task(proc.wait())
        stop_wait = asyncio.create_task(self._stop.wait())
        done, _pending = await asyncio.wait(
            (process_wait, stop_wait), return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_wait in done and not process_wait.done():
            self._request_stop_on_loop()
            if self._termination_task is not None:
                await asyncio.gather(self._termination_task, return_exceptions=True)
            if not process_wait.done():
                process_wait.cancel()
        if process_wait in done:
            stop_wait.cancel()
        await asyncio.gather(process_wait, stop_wait, return_exceptions=True)

    def _request_stop_on_loop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.returncode is None:
            if self._termination_proc is proc:
                return
            self._termination_proc = proc
            try:
                proc.terminate()
            except ProcessLookupError:
                return
            self._termination_task = asyncio.create_task(self._terminate_after_timeout(proc))

    def stop(self) -> None:
        """Request an async terminate→5s→kill sequence without blocking the caller."""
        try:
            caller_loop = asyncio.get_running_loop()
        except RuntimeError:
            caller_loop = None
        if self._loop is not None and self._loop.is_running() and caller_loop is not self._loop:
            self._loop.call_soon_threadsafe(self._request_stop_on_loop)
            return
        self._request_stop_on_loop()

    def _cleanup_frames_dir(self) -> None:
        shutil.rmtree(self._frames_dir, ignore_errors=True)
