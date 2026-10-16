"""语音识别常见同音错字纠正（2026-10-04）：分析前把型号昵称改回来，免得总结把识别错字当成主播口误。

逐字稿里「中鹅、大鹅、小鹅」（天鹅系列的口语叫法）一半以上被识别成「中额 / 中俄 / 大额 / 小额」，
「XS / XV」被识别成「叉S / 叉V / 叉威」。只改这些确定的写法；「大额券」「小额支付」这类正常用词不动。
"""

from __future__ import annotations

import re

# 后面跟这些字时是正常用词（大额券、大额优惠、小额支付、额度……），不改
_KEEP_AFTER = "券优惠红包补贴满减度外定头支付免密贷款订单返现"

_GOOSE = re.compile(rf"([大中小])(?:额|俄|饿|恶|娥|厄|e)(?![{_KEEP_AFTER}])")
_X_MODEL = re.compile(r"叉\s?(S|s|V|v|威)(?![a-zA-Z])")


def fix_text(text: str | None) -> str:
    """把识别错的型号昵称改回来；其余原样。"""
    if not text:
        return text or ""
    out = _GOOSE.sub(lambda m: f"{m.group(1)}鹅", text)
    return _X_MODEL.sub(lambda m: "XV" if m.group(1) in ("V", "v", "威") else "XS", out)


def fix_lines(lines: list[str]) -> list[str]:
    return [fix_text(line) for line in lines]
