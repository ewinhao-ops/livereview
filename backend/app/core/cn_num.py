"""中文/口语数字 → 整数。用于把口播价格、参数解析成可比较的数值。

支持："两万九千八" "三万六" "四千九百九" "六" "29800" 等常见口播写法。
口播省略尾数（"两万九千八" = 29800、"四千九百九" = 4990）按习惯补零。
"""

from __future__ import annotations

import re

_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}


def parse_cn_number(text: str) -> int | None:
    """从一段文字里解析第一个数字（阿拉伯或中文），失败返回 None。"""
    text = text.strip()
    # 阿拉伯数字（允许逗号）
    m = re.search(r"\d[\d,]*", text)
    if m:
        return int(m.group(0).replace(",", ""))

    # 截取连续的中文数字片段
    m = re.search(r"[零〇一二两三四五六七八九十百千万亿]+", text)
    if not m:
        return None
    return _parse_cn_segment(m.group(0))


def _parse_cn_segment(s: str) -> int | None:
    total = 0          # 已结算（万以上）部分
    section = 0        # 当前万以内累加
    current = 0        # 当前数字
    last_unit = 0      # 上一个用到的小单位，用于口播补零
    for ch in s:
        if ch in _DIGITS:
            current = _DIGITS[ch]
        elif ch in _UNITS:
            unit = _UNITS[ch]
            if unit == 10000 or unit == 100000000:
                section = (section + current) * unit
                total += section
                section = 0
            else:
                section += (current if current else 1) * unit
            last_unit = unit
            current = 0
        else:
            return None
    # 处理口播省略：如"四千九百九" → 末位 9 落在 last_unit 的下一档
    if current:
        if last_unit >= 10:
            section += current * (last_unit // 10)
        else:
            section += current
    result = total + section
    return result or None
