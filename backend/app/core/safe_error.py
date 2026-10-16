"""Public/log-safe error formatting shared by runtime components.

Never pass raw third-party exceptions to the database, browser, or logs: provider
SDKs sometimes include request URLs, headers, request bodies, and local paths.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)(authorization|cookie|set-cookie|api[-_ ]?key|access[-_ ]?token|"
    r"refresh[-_ ]?token|token|secret|signature|sign)"
    r"(\s*[:=]\s*)([^\s,;]+|\"[^\"]*\"|'[^']*')"
)
_CONTENT_FIELD_RE = re.compile(
    r"(?is)([\"']?(?:messages|content|prompt|request_body|audio)[\"']?\s*[:=]\s*)"
    r"(\[[^\]]*\]|\{[^}]*\}|\"[^\"]*\"|'[^']*')"
)
_URL_RE = re.compile(r"https?://[^\s\]\[<>{}\"']+")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/])[^\s\"'<>|]+")
_PRIVATE_UNIX_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])/(?:Users|home|root|var|tmp|opt|srv|app|workspace|mnt)/"
    r"[^\s\"'<>|]+"
)


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or "redacted-host"
        if parsed.port:
            host += f":{parsed.port}"
        query = "<redacted>" if parsed.query else ""
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    except ValueError:
        return "<redacted-url>"


def safe_error_text(value: object, *, fallback: str = "操作失败", limit: int = 300) -> str:
    """Return a single-line diagnostic with secrets, payloads, URLs and paths redacted."""
    detail = str(value or "").strip()
    detail = _BEARER_RE.sub("Bearer <redacted>", detail)
    detail = _SENSITIVE_FIELD_RE.sub(r"\1\2<redacted>", detail)
    detail = _CONTENT_FIELD_RE.sub(r"\1<redacted>", detail)
    detail = _URL_RE.sub(_redact_url, detail)
    detail = _WINDOWS_PATH_RE.sub("<redacted-path>", detail)
    detail = _PRIVATE_UNIX_PATH_RE.sub("<redacted-path>", detail)
    detail = " ".join(detail.split())[:limit]
    return detail or fallback


def safe_exception_code(exc: BaseException, *, prefix: str = "RUNTIME") -> str:
    """Stable machine code that never includes exception text."""
    name = re.sub(r"[^A-Z0-9]+", "_", type(exc).__name__.upper()).strip("_")
    return f"{prefix}_{name or 'ERROR'}"[:64]
