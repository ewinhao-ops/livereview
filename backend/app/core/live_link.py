"""直播链接规范化。

此规则必须与 ``frontend/client_state.js::normalizeLiveLink`` 保持逐项一致：
提取分享文案中的首个 URL、统一主机名、移除片段和追踪参数，并稳定排序查询参数。
规范化结果只用于场次幂等键；不会包含页面内容或凭据。
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_URL_RE = re.compile(r"https?://[^\s，。、；;）)\]】}>\"']+", re.I)
_TRACKING_PARAMS = {
    "enter_from_merge",
    "enter_method",
    "action_type",
    "from",
    "share_token",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "timestamp",
}


def _fallback(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).split("#", 1)[0].strip()


def normalize_live_link(value: str | None) -> str:
    """返回跨前后端一致的直播链接幂等键；空输入返回空串。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = _URL_RE.search(raw)
    candidate = match.group(0).rstrip("/") if match else raw
    try:
        parsed = urlsplit(candidate)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if scheme not in {"http", "https"} or not host:
            return _fallback(raw)
        if host == "www.live.douyin.com":
            host = "live.douyin.com"
        port = parsed.port
        netloc = host
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc = f"{host}:{port}"

        path = re.sub(r"/+$", "", parsed.path or "/") or "/"
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if host == "live.douyin.com":
            pairs = []
        else:
            pairs = [
                (key, val)
                for key, val in pairs
                if key.lower() not in _TRACKING_PARAMS
                and not key.lower().startswith("utm_")
            ]
            # URLSearchParams.sort() 只按 key 稳定排序；同名参数保持原顺序。
            pairs.sort(key=lambda item: item[0])
        return urlunsplit((scheme, netloc, path, urlencode(pairs, doseq=True), ""))
    except (ValueError, UnicodeError):
        return _fallback(raw)
