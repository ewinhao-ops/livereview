"""主播声纹识别：sherpa-onnx + CAM++（3D-Speaker 中文声纹）。

提取 192 维声纹 embedding，余弦比对主播档案库，自动判断"当前是谁在讲"。
模型 models/campplus.onnx（28MB，sherpa-onnx 轻量 onnxruntime，无 torch/modelscope）。
无状态：只负责 embed + 比对；连续确认/重叠保护的切换状态机在 pipeline 维护。
"""

from __future__ import annotations

import subprocess

import numpy as np

from ..config import BACKEND_ROOT
from .safe_error import safe_error_text, safe_exception_code

_extractor = None


def _model_file():
    return BACKEND_ROOT / "models" / "campplus.onnx"


def enabled() -> bool:
    return _model_file().exists()


def _get_extractor():
    global _extractor
    if _extractor is None:
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(_model_file()), num_threads=1)
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    return _extractor


def _embed(samples: np.ndarray) -> list[float] | None:
    """16k mono float32[-1,1] → 归一化 192 维 embedding。样本 <1 秒判不可靠返回 None。"""
    if samples is None or len(samples) < 16000:
        return None
    try:
        ext = _get_extractor()
        s = ext.create_stream()
        s.accept_waveform(16000, samples.astype(np.float32))
        s.input_finished()
        e = np.array(ext.compute(s), dtype=np.float32)
    except Exception as ex:  # noqa: BLE001
        print(
            "[voiceprint] extraction status=failed; "
            f"code={safe_exception_code(ex, prefix='VOICEPRINT')}; "
            f"error={safe_error_text(ex)}"
        )
        return None
    n = float(np.linalg.norm(e))
    return (e / n).tolist() if n > 0 else None


def embed_file(path: str) -> list[float] | None:
    """任意音频文件（wav/mp3/m4a/webm…）→ ffmpeg 转 16k mono → 声纹 embedding。"""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"],
            capture_output=True, timeout=90)
    except Exception as ex:  # noqa: BLE001
        print(
            "[voiceprint] transcode status=failed; "
            f"code={safe_exception_code(ex, prefix='VOICEPRINT_FFMPEG')}; "
            f"error={safe_error_text(ex)}"
        )
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    return _embed(np.frombuffer(out.stdout, dtype=np.float32))


def embed_pcm16(pcm: bytes) -> list[float] | None:
    """采集侧 16k s16le PCM 字节 → 声纹 embedding（供 pipeline 实时识别）。"""
    if not pcm:
        return None
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return _embed(samples)


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return -1.0
    va, vb = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    d = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return float(np.dot(va, vb) / d) if d > 0 else -1.0


def identify(emb: list[float], roster_vp: dict[int, list[float]],
             threshold: float = 0.45) -> tuple[int | None, float, float]:
    """emb 与主播档案库比对 → (最像的主播id, 最高分, 次高分)。

    最高分 < threshold 视为"未识别"（嘉宾/纯 BGM），返回 (None, 最高分, 次高分)。
    次高分供上层判"重叠/交接段"（top1、top2 太接近时不切）。
    """
    scored = sorted(((cosine(emb, vp), sid) for sid, vp in (roster_vp or {}).items()
                     if vp), reverse=True)
    if not scored:
        return None, 0.0, 0.0
    best_score, best_sid = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < threshold:
        return None, best_score, second
    return best_sid, best_score, second
