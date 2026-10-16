"""公屏（弹幕）弱信号识别与归因（D1）。

把观众公屏文字分类成弱信号：疑问 / 购买意向 / 异议 / 正向 / 负向，
按点评段归组，结合话术场景做归因（如"逼单段公屏在问怎么买=引导有效"、
"讲款段异议多=卖点没铺够"）。纯文本规则，快、免 LLM。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Critique, CritiqueLevel, OcrRecord

# 分类关键词，按优先级从上到下匹配（命中即归该类）
_SIGNAL_KW: list[tuple[str, str, list[str]]] = [
    ("intent", "购买意向", ["想要", "要了", "买了", "下单", "拍了", "拍下", "上车", "来一个",
                          "来一套", "求链接", "链接呢", "入手", "冲了", "秒了", "已拍",
                          "已下单", "怎么买", "哪里买", "怎么下单", "买它"]),
    ("objection", "异议", ["太贵", "好贵", "贵了", "便宜点", "划算吗", "值吗", "值不值",
                         "犹豫", "再看看", "肉疼", "降价", "优惠点", "买不起", "穷"]),
    ("question", "疑问", ["多少钱", "怎么样", "能不能", "可以吗", "参数", "阻抗", "有货",
                        "什么时候", "区别", "对比", "推荐", "适合吗", "?", "？"]),
    ("negative", "负向", ["难听", "垃圾", "骗", "假货", "割韭菜", "差评", "踩雷", "翻车"]),
    ("positive", "正向", ["好听", "喜欢", "绝了", "心动", "爱了", "真香", "好评",
                        "支持", "漂亮", "舒服", "yyds"]),
]

_LABEL = {k: name for k, name, _ in _SIGNAL_KW}
_ORDER = [k for k, _n, _w in _SIGNAL_KW]


def classify_danmaku(text: str) -> str:
    """一条公屏 → 信号 key（intent/objection/question/negative/positive）；无则空串。"""
    t = text or ""
    for key, _name, words in _SIGNAL_KW:
        if any(w in t for w in words):
            return key
    return ""


def _attribution(counts: dict, seg_signal: dict) -> list[str]:
    """结合整场信号 + 各场景段内信号，产出归因提示。"""
    tips: list[str] = []
    total = sum(counts.values())
    if not total:
        return ["公屏信号很少，互动氛围偏冷，建议多抛提问/扣屏引导发言。"]
    if counts.get("intent"):
        tips.append(f"公屏出现 {counts['intent']} 条购买意向（想要/怎么买/上车），"
                    "是转化窗口，主播/场控应第一时间接单、报下单步骤。")
    # 逼单场景段里观众在问购买方式 → 引导有效但落地不畅
    urge_q = seg_signal.get("urge", {}).get("question", 0) + seg_signal.get("urge", {}).get("intent", 0)
    if urge_q >= 2:
        tips.append("逼单时公屏在问价格/怎么买，说明促单点燃了需求，但落地路径不够清晰，"
                    "建议直接口播'点下方小黄车→选型号→拍下'。")
    prod_obj = seg_signal.get("product", {}).get("objection", 0)
    if prod_obj >= 2:
        tips.append("讲款阶段异议偏多（贵/值不值），价值塑造或人群适配没铺够，"
                    "建议先讲'对谁值'再报价，并给对比锚点。")
    if counts.get("objection", 0) >= max(3, total * 0.2):
        tips.append(f"价格异议较集中（{counts['objection']} 条），需强化优惠对比与稀缺，弱化价格敏感。")
    if counts.get("question", 0) >= max(3, total * 0.25):
        tips.append(f"疑问较多（{counts['question']} 条），存在没讲清的点，可安排场控集中答疑或主播复述关键参数。")
    if counts.get("negative"):
        tips.append(f"出现 {counts['negative']} 条负向信号，注意甄别是否口碑/售后问题。")
    if counts.get("positive", 0) >= max(3, total * 0.3):
        tips.append(f"正向反馈活跃（{counts['positive']} 条），可趁热度顺势逼单。")
    return tips or ["公屏以闲聊为主，未见明显购买/异议信号。"]


def analyze_session_danmaku(session_id: int) -> dict:
    """整场公屏信号：分布 + 按场景聚合 + 归因提示。纯读，不落库。"""
    with SessionLocal() as db:
        ocr = db.execute(select(OcrRecord).where(OcrRecord.session_id == session_id)
                         .order_by(OcrRecord.start_ms)).scalars().all()
        segs = db.execute(select(Critique).where(
            Critique.session_id == session_id,
            Critique.level == CritiqueLevel.segment).order_by(Critique.start_ms)).scalars().all()
    if not ocr:
        return {"total": 0, "counts": {}, "attribution": [], "labels": _LABEL}

    # 段边界 + 场景
    seg_spans = []
    for i, c in enumerate(segs):
        end = segs[i + 1].start_ms if i + 1 < len(segs) else 10 ** 15
        try:
            scene = (json.loads(c.data) or {}).get("scene") if c.data else ""
        except json.JSONDecodeError:
            scene = ""
        seg_spans.append((c.start_ms, end, scene or "general"))

    counts: dict[str, int] = {}
    seg_signal: dict[str, dict[str, int]] = {}   # scene -> {signal: n}
    samples: dict[str, list[dict]] = {}          # signal -> [{at, text}]（展示原文）
    for o in ocr:
        sig = classify_danmaku(o.text)
        if not sig:
            continue
        counts[sig] = counts.get(sig, 0) + 1
        if len(samples.setdefault(sig, [])) < 30:
            samples[sig].append({"at": o.start_ms, "text": (o.text or "")[:80]})
        scene = next((sc for (s, e, sc) in seg_spans if s <= o.start_ms < e), "general")
        seg_signal.setdefault(scene, {})[sig] = seg_signal.setdefault(scene, {}).get(sig, 0) + 1

    dist = [{"key": k, "label": _LABEL[k], "count": counts[k]}
            for k in _ORDER if counts.get(k)]
    return {"total": sum(counts.values()), "danmaku_total": len(ocr),
            "counts": counts, "dist": dist, "samples": samples,
            "attribution": _attribution(counts, seg_signal), "labels": _LABEL}
