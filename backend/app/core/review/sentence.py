"""句级硬错检测（即时）。

规则 + 知识库为主：快、可解释、便宜，不依赖 LLM。检测：
  1) 违禁词 / 合规风险（医疗功效、绝对化、虚假紧迫）
  2) 产品规格讲错（阻抗数值对不上、难推机器说成好推）
  3) 口播价格 与 画面 OCR 价格 不一致
命中即产出 flag，由 pipeline 落库并即时推到看板。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..cn_num import parse_cn_number
from ..knowledge import KnowledgeBase, Product


def _official_price_check() -> bool:
    """口播/画面价与知识库官方价比对。直播间价格随活动变动，官网价不作依据，默认关闭
    （2026-07-15 用户规则：价格不做质检，只报破价；2026-09-29 用户重申）。"""
    from ...config import get_settings   # 延迟导入：规则模块保持纯函数
    return bool(getattr(get_settings(), "official_price_check_enabled", False))

_AMOUNT = r"(?:\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_PRICE_PATTERNS = (
    re.compile(rf"(?P<raw>(?P<cue>优惠后(?:只需|只要)|到手价?|只需|只要|售价|价格|[¥￥])\s*(?:是|为|：|:)?\s*(?P<num>{_AMOUNT})\s*(?:元|块钱?|圆)?)"),
    re.compile(rf"(?P<raw>(?P<num>{_AMOUNT})\s*(?P<cue>元|块钱?|价格))"),
)
_IMPEDANCE_NEAR = re.compile(r"阻抗[^0-9零〇一二两三四五六七八九十百千万]{0,4}"
                             r"([0-9零〇一二两三四五六七八九十百千万]+)\s*欧")
_EASY_DRIVE_KW = ["好推", "很好推", "容易推", "手机推", "手机直接", "手机就能",
                  "小尾巴就能", "随便推", "不挑前端"]


@dataclass
class Flag:
    severity: str       # info | warn | error
    category: str       # 违禁词 | 规格 | 价格 | ...
    quote: str
    comment: str


@dataclass(frozen=True)
class PriceMatch:
    value: float
    raw: str
    start: int
    promotional: bool = False


def _extract_price_match(text: str) -> PriceMatch | None:
    """Extract only amounts tied to an explicit price cue, on either side.

    If a sentence contains several amounts, a promotional/final-price cue wins;
    otherwise the last explicit price mention wins. The raw span is retained for audit.
    """
    found: list[PriceMatch] = []
    seen: set[tuple[int, str]] = set()
    for pattern in _PRICE_PATTERNS:
        for match in pattern.finditer(text or ""):
            raw = match.group("raw")
            key = (match.start(), raw)
            if key in seen:
                continue
            seen.add(key)
            number = match.group("num").replace(",", "").replace("，", "")
            try:
                value = float(number)
            except ValueError:
                continue
            if value <= 0:
                continue
            cue = match.group("cue") or ""
            found.append(PriceMatch(value, raw, match.start(), any(x in cue for x in ("到手", "优惠", "只需", "只要"))))
    if not found:
        return None
    return max(found, key=lambda item: (item.promotional, item.start))


def _extract_price(text: str) -> float | None:
    match = _extract_price_match(text)
    return match.value if match else None


def _ocr_price_for(product: Product | None, recent_ocr: list[str]) -> int | None:
    """从近窗 OCR 文本里取价格：优先匹配同型号那条，否则取最近一条带价的。"""
    candidates = recent_ocr[::-1]  # 由近及远
    if product is not None:
        for t in candidates:
            if product.matches(t) and ("¥" in t or "￥" in t or "价" in t):
                return parse_cn_number(t[t.find("¥") if "¥" in t else 0:])
    for t in candidates:
        if "¥" in t or "￥" in t:
            return parse_cn_number(t[t.find("¥"):])
    return None


def check_sentence(
    text: str, kb: KnowledgeBase, recent_ocr: list[str] | None = None,
    *, check_forbidden: bool = True,
) -> list[Flag]:
    flags: list[Flag] = []
    recent_ocr = recent_ocr or []

    # 1) 违禁词
    if check_forbidden:
        for w in kb.hit_forbidden(text):
            flags.append(
                Flag("error", "违禁词", w,
                     f"出现疑似违禁/违规表述「{w}」，存在合规风险，需立即纠正话术。")
            )

    product = kb.find_product(text)

    # 2) 规格讲错
    if product is not None:
        m = _IMPEDANCE_NEAR.search(text)
        if m and product.impedance_ohm is not None:
            claimed = parse_cn_number(m.group(1))
            if claimed is not None and claimed != product.impedance_ohm:
                flags.append(
                    Flag("error", "规格",
                         f"{product.name} 阻抗{m.group(1)}欧",
                         f"口播阻抗 {claimed}Ω 与知识库 {product.impedance_ohm}Ω 不符，"
                         f"请纠正。")
                )
        if product.drivability == "hard" and any(k in text for k in _EASY_DRIVE_KW):
            flags.append(
                Flag("error", "规格", product.name,
                     f"{product.name} 实际较难驱动（需大功率耳放），"
                     f"不应宣称易推/手机可推，易误导消费者。")
            )

    # 3) 口播价 vs 画面 OCR 价（仅在本句点名产品时做，避免跨句串味的误报）
    price_match = _extract_price_match(text)
    spoken = price_match.value if price_match else None
    if spoken is not None and product is not None:
        ocr_price = _ocr_price_for(product, recent_ocr)
        if ocr_price is not None and ocr_price != spoken:
            flags.append(
                Flag("warn", "价格",
                     f"口播「{price_match.raw}」={spoken:g} / 画面 {ocr_price}",
                     f"{product.name} 口播价 {spoken} 与画面价格牌 {ocr_price} 不一致，"
                     f"请核对以哪个为准。")
            )
        # 顺带：活动价高于官方价的异常（默认关闭，见 _official_price_check）
        if (_official_price_check() and product.official_price
                and spoken > product.official_price):
            flags.append(
                Flag("warn", "价格", f"{product.name} 口播「{price_match.raw}」={spoken:g}",
                     f"口播价 {spoken} 高于知识库官方价 {product.official_price}，请确认。")
            )

    return flags
