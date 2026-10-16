"""节奏指标计算（纯函数，不依赖 DB / 网络）。

把一段话术（带时间戳）算成可复现的节奏数据：语速、停顿、冷场、重复、单品讲解
时长占比、互动密度。这些数字喂给段级 LLM，让"节奏复盘"从'凭感觉'变成'看数说话'。

batch 每条：{"text": str, "start_ms": int, "end_ms": int}
"""

from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher

# 互动 / 促单 / 提问 话术信号（命中即记一次互动）
_ASK_RE = re.compile(
    r"[?？]|吗|有没有|想不想|要不要|能不能|可以吗|多少钱|几个|扣\s*1|抠\s*1|"
    r"评论区|点关注|加购|上车|想要的|扣起来|打在公屏|听到的"
)

# 直播带货节奏基线（内部参考，不作为写死给用户的"标准"）
BASELINES = (
    "句间停顿正常 <1.5 秒、>3 秒算冷场；单品建议集中讲解（时长占比 >50%、少频繁跳品）；"
    "每段互动/提问 ≥2 次。"
)

# 语速判断原则：结合"在讲什么"给理由，不套统一字数标准（喂给段级 LLM）
PACE_GUIDE = (
    "【直播间定位】本直播间是高客单 HIFI 耳机专业自播（舒缓专业型），不是激情冲量播——"
    "整体语言风格偏舒缓是定位使然，评语速先按这个类型判，不得照搬通用带货行业的激情播标准扣分。"
    "语速没有统一字数标准，要匹配当前在讲什么：讲关键参数/价格/高端款宜放慢、留停顿让观众消化；"
    "促单逼单可紧凑提节奏、营造紧迫；答疑要清晰不慌；开场要有精神不拖沓。"
    "只有明显过快(听不清、赶)或过慢(拖沓、冷场)才需调整，并说明『因为在讲什么所以该快/该慢』。"
    "【冷场判定】无弹幕互动但主播在持续讲产品 = 『主动过品』，是有效动作，绝不判为冷场或没有互动；"
    "真正的冷场 = 既无互动又无实质内容（长时间沉默、设备闲扯、无关闲聊）。"
)

_NONWORD = re.compile(r"\s+")


def _chars(text: str) -> int:
    return len(_NONWORD.sub("", text or ""))


def compute_rhythm_metrics(batch: list[dict], kb=None, danmaku_count: int = 0) -> dict:
    """从一段带时间戳话术算节奏指标。空段返回零值结构。"""
    batch = [b for b in (batch or []) if (b.get("text") or "").strip()]
    if not batch:
        return {
            "wpm": 0, "avg_pause_ms": 0, "max_pause_ms": 0, "cold_opens": 0,
            "repeat_ratio": 0.0, "products": [], "dominant_share": 0.0,
            "danmaku": int(danmaku_count), "ask_count": 0,
            "duration_sec": 0, "sentences": 0,
        }

    seg_start = batch[0].get("start_ms", 0)
    seg_end = max(b.get("end_ms", 0) for b in batch)
    duration_sec = max(1, round((seg_end - seg_start) / 1000))
    total_chars = sum(_chars(b["text"]) for b in batch)
    wpm = round(total_chars / (duration_sec / 60)) if duration_sec else 0

    # 句间停顿 / 冷场
    pauses = []
    for i in range(1, len(batch)):
        gap = batch[i].get("start_ms", 0) - batch[i - 1].get("end_ms", 0)
        pauses.append(max(0, gap))
    avg_pause = round(sum(pauses) / len(pauses)) if pauses else 0
    max_pause = max(pauses) if pauses else 0
    cold_opens = sum(1 for p in pauses if p > 3000)

    # 相邻句重复率
    rep = 0
    for i in range(1, len(batch)):
        if SequenceMatcher(None, batch[i - 1]["text"], batch[i]["text"]).ratio() > 0.7:
            rep += 1
    repeat_ratio = round(rep / (len(batch) - 1), 2) if len(batch) > 1 else 0.0

    # 单品讲解时长占比
    prod_time: dict[str, int] = defaultdict(int)
    total_time = 0
    for b in batch:
        dur = max(0, b.get("end_ms", 0) - b.get("start_ms", 0))
        total_time += dur
        p = kb.find_product(b["text"]) if kb else None
        if p:
            prod_time[p.name] += dur
    dominant_share = (round(max(prod_time.values()) / total_time, 2)
                      if prod_time and total_time else 0.0)

    ask_count = sum(1 for b in batch if _ASK_RE.search(b["text"]))

    return {
        "wpm": wpm,
        "avg_pause_ms": avg_pause,
        "max_pause_ms": max_pause,
        "cold_opens": cold_opens,
        "repeat_ratio": repeat_ratio,
        "products": list(prod_time.keys()),
        "dominant_share": dominant_share,
        "danmaku": int(danmaku_count),
        "ask_count": ask_count,
        "duration_sec": duration_sec,
        "sentences": len(batch),
    }


def aggregate_metrics(metrics_list: list[dict]) -> dict:
    """把多段节奏指标聚合成整场视角（供整场总结用）。"""
    ms = [m for m in (metrics_list or []) if m]
    if not ms:
        return {}
    n = len(ms)
    wpms = [m.get("wpm", 0) for m in ms if m.get("wpm")]
    products: list[str] = []
    for m in ms:
        for p in m.get("products", []):
            if p not in products:
                products.append(p)
    return {
        "avg_wpm": round(sum(wpms) / len(wpms)) if wpms else 0,
        "total_cold_opens": sum(m.get("cold_opens", 0) for m in ms),
        "max_pause_ms": max((m.get("max_pause_ms", 0) for m in ms), default=0),
        "avg_repeat_ratio": round(sum(m.get("repeat_ratio", 0.0) for m in ms) / n, 2),
        "total_danmaku": sum(m.get("danmaku", 0) for m in ms),
        "total_ask": sum(m.get("ask_count", 0) for m in ms),
        "products": products,
        "segments": n,
    }


def format_metrics(m: dict | None) -> str:
    """指标 → 人读文本（喂 LLM / 导出用）。"""
    if not m:
        return "（无）"
    cv = m.get("energy_cv")
    cv_txt = f"；响度起伏 {cv}" if cv is not None else ""
    return (
        f"语速 {m.get('wpm', 0)} 字/分钟；句间停顿均 {m.get('avg_pause_ms', 0)}ms、"
        f"最长 {m.get('max_pause_ms', 0)}ms；冷场(>3s) {m.get('cold_opens', 0)} 次；"
        f"相邻句重复率 {int(m.get('repeat_ratio', 0) * 100)}%；"
        f"本段产品 {('、'.join(m.get('products', [])) or '无')}、主讲占比 "
        f"{int(m.get('dominant_share', 0) * 100)}%；互动/提问 {m.get('ask_count', 0)} 次；"
        f"弹幕 {m.get('danmaku', 0)} 条{cv_txt}；段时长 {m.get('duration_sec', 0)}s、"
        f"{m.get('sentences', 0)} 句"
    )
