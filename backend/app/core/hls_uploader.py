"""HLS 切片实时上传守护：录制产生的 ts 切片一完成就传 COS 并删本地。

设计目标：本地磁盘占用恒定（只保留正在写的最后一个切片 + 在途几片），
录多久都不会写满磁盘；崩溃/断电最多丢最后一个切片（~10s）。

切片由 ffmpeg `-f segment` 产出，文件名 seg_{round:02d}_{index:05d}.ts——
round 是拉流重连轮次（时间戳不连续），m3u8 在轮次边界插 EXT-X-DISCONTINUITY。
m3u8 由本守护自行维护（不依赖 ffmpeg 的 hls muxer），每上传一片就更新一次
COS 上的 index.m3u8，因此录制过程中即可实时点播已录部分。
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from . import media_store
from .safe_error import safe_error_text, safe_exception_code

_SEG_RE = re.compile(r"^seg_(\d+)_(\d+)\.ts$")


def _probe_duration(path: Path) -> float:
    """ffprobe 单片时长（秒）；失败回退切片名义时长 10s。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        v = float(r.stdout.strip())
        if v > 0:
            return v
    except Exception:  # noqa: BLE001
        pass
    return 10.0


class HlsUploader:
    """扫描 seg_dir，把完成的切片传到 COS `{prefix}/`，维护 `{prefix}/index.m3u8`。"""

    def __init__(self, session_id: int, seg_dir: Path, prefix: str) -> None:
        self.session_id = session_id
        self.seg_dir = Path(seg_dir)
        self.prefix = prefix.rstrip("/")
        self._entries: list[str] = []      # m3u8 正文行（EXTINF/DISCONTINUITY/文件名）
        self._done: set[str] = set()       # 已上传切片名
        self._last_round: int | None = None
        self._max_dur = 10.0
        self._stop = asyncio.Event()
        self._failed_streak = 0

    # ---------- m3u8 ----------
    def _playlist(self, end: bool) -> str:
        head = ["#EXTM3U", "#EXT-X-VERSION:3",
                f"#EXT-X-TARGETDURATION:{int(self._max_dur) + 1}",
                "#EXT-X-MEDIA-SEQUENCE:0",
                f"#EXT-X-PLAYLIST-TYPE:{'VOD' if end else 'EVENT'}"]
        tail = ["#EXT-X-ENDLIST"] if end else []
        return "\n".join(head + self._entries + tail) + "\n"

    def _push_playlist(self, end: bool = False) -> bool:
        tmp = self.seg_dir / ".index.m3u8"
        tmp.write_text(self._playlist(end), encoding="utf-8")
        ok = media_store.upload_file(tmp, f"{self.prefix}/index.m3u8")
        tmp.unlink(missing_ok=True)
        return ok

    # ---------- 切片 ----------
    def _pending(self, include_last: bool) -> list[Path]:
        segs = sorted(p for p in self.seg_dir.glob("seg_*.ts") if _SEG_RE.match(p.name))
        if not include_last and segs:
            segs = segs[:-1]   # 最新一个可能还在被 ffmpeg 写，留给下一轮
        return [p for p in segs if p.name not in self._done]

    async def _upload_one(self, p: Path) -> bool:
        m = _SEG_RE.match(p.name)
        rnd = int(m.group(1)) if m else 0
        dur = await asyncio.to_thread(_probe_duration, p)
        ok = await asyncio.to_thread(media_store.upload_file, p, f"{self.prefix}/{p.name}")
        if not ok:
            return False
        if self._last_round is not None and rnd != self._last_round:
            self._entries.append("#EXT-X-DISCONTINUITY")   # 重连边界：时间戳不连续
        self._last_round = rnd
        self._max_dur = max(self._max_dur, dur)
        self._entries.append(f"#EXTINF:{dur:.3f},")
        self._entries.append(p.name)
        self._done.add(p.name)
        p.unlink(missing_ok=True)                          # 传完即删，本地不涨
        return True

    async def _sweep(self, include_last: bool) -> None:
        for p in self._pending(include_last):
            if await self._upload_one(p):
                self._failed_streak = 0
                self._push_playlist()
            else:
                # 上传失败：切片留在本地，退避后重试（COS 恢复后自动追传）
                self._failed_streak += 1
                if self._failed_streak in (1, 10):
                    print(f"[hls] session {self.session_id} 切片上传失败（连续 {self._failed_streak} 次），"
                          "本地保留待重试")
                break

    async def run(self) -> None:
        """录制期间常驻：每 3 秒扫一轮。stop() 后由 finalize() 收尾。"""
        while not self._stop.is_set():
            try:
                await self._sweep(include_last=False)
            except Exception as e:  # noqa: BLE001
                print(
                    f"[hls] session={self.session_id}; "
                    f"code={safe_exception_code(e, prefix='HLS')}; "
                    f"error={safe_error_text(e)}"
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    async def finalize(self) -> tuple[bool, int]:
        """收尾：把剩余切片（含最后一个）全部上传，写 ENDLIST。
        返回 (是否有可播内容, 切片总数)。上传失败的切片保留本地供人工补救。"""
        self.stop()
        for _ in range(3):   # 收尾多试几轮，给网络抖动机会
            await self._sweep(include_last=True)
            if not self._pending(include_last=True):
                break
            await asyncio.sleep(2)
        ok = bool(self._done)
        if ok:
            self._push_playlist(end=True)
        leftovers = self._pending(include_last=True)
        if not leftovers:
            try:
                self.seg_dir.rmdir()
            except OSError:
                pass
        else:
            print(f"[hls] session {self.session_id} 有 {len(leftovers)} 个切片未能上传，保留在 {self.seg_dir}")
        # 收尾核对：COS 实际对象数应 ≈ 切片数 + 1(index.m3u8)。明显偏少即告警——
        # 防"upload 返回成功但对象没真落云"这类静默丢失，日志留痕便于排查录像放不出来。
        if ok:
            actual = media_store.count_prefix(f"{self.prefix}/")
            expected = len(self._done) + 1
            if 0 <= actual < expected:
                print(f"[hls] ⚠️ session {self.session_id} 收尾核对异常：COS 实际 {actual} 个对象，"
                      f"预期 {expected}（切片 {len(self._done)}+索引），疑有切片未真正落云")
            else:
                print(f"[hls] session {self.session_id} 收尾核对通过：COS {actual} 对象（切片 {len(self._done)}）")
        return ok, len(self._done)

    def resume_from_cos(self) -> None:
        """崩溃恢复：读云端已有 index.m3u8，恢复 entries/done，之后补传本地残片即可续上。"""
        text = media_store.get_text(self.m3u8_key)
        if not text:
            return
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("#EXTINF") or s == "#EXT-X-DISCONTINUITY":
                self._entries.append(s)
            elif s and not s.startswith("#"):
                self._entries.append(s)
                self._done.add(s)
                m = _SEG_RE.match(s)
                if m:
                    self._last_round = int(m.group(1))

    @property
    def m3u8_key(self) -> str:
        return f"{self.prefix}/index.m3u8"
