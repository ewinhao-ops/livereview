"""主播 / 场控双线识别（从内容自动分「搭话」）。

同一主播的转写流里，区分【主讲 host】（承担产品讲解主线）与【场控搭话 assist】
（短促接话、答疑救场、上链接/催单/宠粉配合）。用启发式规则（快、免 LLM）。

注：当前场控不出声，全部会被判为 host；本模块先备好，等场控开始说话即自动生效。
展示上刻意低调（前端折叠入口、导出仅在确有搭话时才出现）。
"""

from __future__ import annotations

import re

# 场控职能话术（上链接/催单/宠粉/答疑救场）——命中且句子不太长即判搭话
_ASSIST_FUNC = [
    "上链接", "上车", "小黄车", "拍下方", "链接放", "链接在", "放链接", "福袋", "发福袋",
    "中奖", "恭喜", "扣1", "扣一", "扣起来", "扣屏", "公屏", "没听清", "在的扣",
    "这位家人", "有人问", "刚才有", "帮你问", "解答一下", "库存加", "补个库存", "改价",
]
# 短应答 / 附和（很短时才算搭话）
_ASSIST_ECHO = ["对对", "对的", "是的", "没错", "好的", "好嘞", "可以的", "嗯嗯",
                "收到", "安排", "对对对", "是的呢"]

_STRIP = re.compile(r"[\s，。！？、,.!?~…—·\-]+")


def _clean_len(text: str) -> int:
    return len(_STRIP.sub("", text or ""))


def detect_role(text: str) -> str:
    """一句话 → 'host'(主讲) | 'assist'(场控搭话)。默认 host。"""
    t = text or ""
    n = _clean_len(t)
    if not n:
        return "host"
    if any(w in t for w in _ASSIST_FUNC) and n <= 30:   # 场控职能话术（略长也算）
        return "assist"
    if n <= 10 and any(w in t for w in _ASSIST_ECHO):   # 极短附和
        return "assist"
    return "host"


def analyze_roles(items: list, max_samples: int = 8) -> dict:
    """转写列表（须有 .corrected_text / .start_ms）→ 主播/场控分工统计 + 搭话样例。"""
    host = assist = 0
    samples: list[dict] = []
    for it in items:
        text = getattr(it, "corrected_text", "") or ""
        if not text.strip():
            continue
        if detect_role(text) == "assist":
            assist += 1
            if len(samples) < max_samples:
                samples.append({"start_ms": getattr(it, "start_ms", 0), "text": text})
        else:
            host += 1
    total = host + assist
    return {
        "host_count": host, "assist_count": assist,
        "assist_ratio": round(assist * 100 / total) if total else 0,
        "samples": samples,
    }
