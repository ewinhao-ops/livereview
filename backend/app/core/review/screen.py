"""画面文字实时点评（针对 OCR 出的画面文字本身）。

与句级口播检测的区别：这里点评的是"画面上写了什么"——价格牌、贴片、商品标题。
检测：
  1) 画面违禁词（贴片/价格牌上的合规红线，比口播更硬，因为是白纸黑字）
  2) 画面型号规格写错（如阻抗标错）
  3) 画面报价高于知识库官方价（标价异常）
规则 + 知识库，毫秒级，适合实时浮层提示。
"""

from __future__ import annotations

from ..cn_num import parse_cn_number
from ..knowledge import KnowledgeBase
from .sentence import Flag, _IMPEDANCE_NEAR, _extract_price, _official_price_check


def check_screen(text: str, kb: KnowledgeBase) -> list[Flag]:
    flags: list[Flag] = []
    text = (text or "").strip()
    if not text:
        return flags

    # 1) 画面违禁词
    for w in kb.hit_forbidden(text):
        flags.append(
            Flag("error", "画面违禁词", w,
                 f"画面文字出现违禁/违规表述「{w}」，价格牌/贴片需立即整改（白纸黑字风险更高）。")
        )

    product = kb.find_product(text)
    if product is not None:
        # 2) 画面阻抗标错
        m = _IMPEDANCE_NEAR.search(text)
        if m and product.impedance_ohm is not None:
            claimed = parse_cn_number(m.group(1))
            if claimed is not None and claimed != product.impedance_ohm:
                flags.append(
                    Flag("error", "画面规格", f"{product.name} 阻抗{m.group(1)}欧",
                         f"画面标注阻抗 {claimed}Ω 与知识库 {product.impedance_ohm}Ω 不符。")
                )
        # 3) 画面报价高于官方价
        price = _extract_price(text)
        if (price is not None and _official_price_check() and product.official_price
                and price > product.official_price):
            flags.append(
                Flag("warn", "画面价格", f"{product.name} 画面 {price}",
                     f"画面标价 {price} 高于知识库官方价 {product.official_price}，请核对。")
            )

    return flags
