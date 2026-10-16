"""音频韵律特征（声学层，纯 Python，不依赖 librosa）。

从实时 16k 单声道 s16le PCM 块算轻量声学特征，按段聚合成"怎么说"的指标，
补足文本情感看不到的"听感"：
  - RMS 响度（音量）
  - energy_cv 响度起伏 = std/mean，越高越有抑扬顿挫；接近 0 = 语调平淡单调
  - ZCR 过零率，粗略反映音调高低 / 紧张度

这些和语速一起喂给段级 LLM，合成"语气情绪"判断。
"""

from __future__ import annotations

import array
import math

from .safe_error import safe_error_text, safe_exception_code


def chunk_features(pcm: bytes) -> tuple[float, float]:
    """一块 PCM → (rms, zcr)。空/极短返回 (0,0)。"""
    if len(pcm) < 4:
        return 0.0, 0.0
    a = array.array("h")
    a.frombytes(pcm[: (len(pcm) // 2) * 2])
    n = len(a)
    if not n:
        return 0.0, 0.0
    ss = 0.0
    zc = 0
    prev = a[0]
    for x in a:
        ss += x * x
        if (x >= 0) != (prev >= 0):
            zc += 1
        prev = x
    return math.sqrt(ss / n), zc / n


def summarize_prosody(samples: list[tuple[int, float, float]],
                      seg_start: int, seg_end: int) -> dict:
    """把段时间窗内的逐块特征聚合成段级声学指标。samples: [(t_ms, rms, zcr)]。"""
    win = [(r, z) for (t, r, z) in samples if seg_start <= t <= seg_end and r > 0]
    if not win:
        return {}
    rmss = [r for r, _ in win]
    zcrs = [z for _, z in win]
    m = sum(rmss) / len(rmss)
    if m <= 0:
        return {}
    var = sum((r - m) ** 2 for r in rmss) / len(rmss)
    return {
        "energy_cv": round((var ** 0.5) / m, 2),   # 响度起伏（抑扬顿挫）
        "zcr": round(sum(zcrs) / len(zcrs), 3),      # 音调倾向
        "voiced_chunks": len(win),
    }


def prosody_text(p: dict | None) -> str:
    """声学指标 → 人读文本（喂 LLM / 导出用）。"""
    if not p:
        return "（无声学数据）"
    cv = p.get("energy_cv", 0)
    tone = "平淡单调" if cv < 0.25 else ("起伏适中" if cv < 0.6 else "抑扬明显")
    txt = f"响度起伏 {cv}（{tone}）；过零率 {p.get('zcr', 0)}"
    if p.get("pitch"):
        txt += f"；{pitch_text(p['pitch'])}"
    return txt


# ---------- 语调：真实基频 F0（parselmouth/Praat）----------

def pitch_features(wav_path: str) -> dict:
    """一段音频文件 → 语调指标：基频均值/范围/变异 + 语调标签。失败返回 {}。

    用 Praat(parselmouth) 提真实基频 F0，比 ZCR 近似准得多。cv 高=抑扬顿挫、低=平淡。
    """
    try:
        import parselmouth
        snd = parselmouth.Sound(wav_path)
        f0 = snd.to_pitch().selected_array["frequency"]
        voiced = [x for x in f0 if x > 0]
        if len(voiced) < 5:
            return {}
        mean = sum(voiced) / len(voiced)
        std = (sum((x - mean) ** 2 for x in voiced) / len(voiced)) ** 0.5
        cv = std / mean if mean else 0.0
        lo, hi = min(voiced), max(voiced)
        return {"pitch_hz": round(mean), "pitch_min": round(lo), "pitch_max": round(hi),
                "pitch_cv": round(cv, 2), "voiced_ratio": round(len(voiced) / len(f0), 2),
                "label": _tone_label(cv)}
    except Exception as e:  # noqa: BLE001
        print(
            "[prosody] pitch status=failed; "
            f"code={safe_exception_code(e, prefix='PROSODY')}; "
            f"error={safe_error_text(e)}"
        )
        return {}


def _tone_label(cv: float) -> str:
    if cv < 0.12:
        return "平淡单调"
    if cv < 0.28:
        return "自然平稳"
    return "抑扬起伏"


def pitch_text(pt: dict | None) -> str:
    if not pt or not pt.get("pitch_hz"):
        return ""
    return (f"语调 均值 {pt['pitch_hz']}Hz（{pt.get('pitch_min', 0)}–{pt.get('pitch_max', 0)}Hz）"
            f"·起伏 {pt.get('pitch_cv', 0)}（{pt.get('label', '')}）")
