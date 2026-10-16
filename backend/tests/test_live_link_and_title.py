from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.api.sessions import _automatic_session_title
from app.core.douyin import resolve_stream
from app.core.douyin import _parse_html, extract_live_identity
from app.core.live_link import normalize_live_link


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "douyin"


def test_server_link_normalization_contract() -> None:
    cases = json.loads((ROOT / "test-data" / "live-link-normalization.json").read_text("utf-8"))
    assert [normalize_live_link(case["input"]) for case in cases] == [
        case["expected"] for case in cases
    ]


def test_native_title_beats_advertising_and_seo_title() -> None:
    html = (FIXTURES / "native-title.html").read_text("utf-8")
    resolution = _parse_html(html, "10001")
    assert resolution.anchor_nickname == "主播小林"
    assert resolution.title == "新品平板耳机深度讲解"
    assert "广告" not in resolution.title


def test_escaped_room_data_is_supported() -> None:
    html = (FIXTURES / "escaped-room-info.html").read_text("utf-8")
    nickname, title = extract_live_identity(html)
    assert (nickname, title) == ("阿音", "夏日耳机试听专场")


def test_invalid_native_title_uses_nickname_then_generic_fallback() -> None:
    html = (FIXTURES / "nickname-fallback.html").read_text("utf-8")
    nickname, title = extract_live_identity(html)
    moment = datetime(2026, 8, 3, 18, 30)
    assert nickname == "测试主播三号"
    assert title == ""
    assert _automatic_session_title(title, nickname, now=moment) == "测试主播三号 · 08-03 18:30"
    assert _automatic_session_title(now=moment) == "直播场次 · 08-03 18:30"


def test_three_sanitized_samples_never_emit_ad_title() -> None:
    for path in sorted(FIXTURES.glob("*.html")):
        _nickname, title = extract_live_identity(path.read_text("utf-8"))
        assert "广告投放" not in title
        assert "SEO" not in title.upper()


def test_manual_controlled_source_preserves_explicit_http_scheme() -> None:
    import asyncio

    resolution = asyncio.run(resolve_stream(
        manual_url="http://127.0.0.1:19090/non-sensitive.wav?x=1\\u0026y=2",
    ))
    assert resolution.stream_url == (
        "http://127.0.0.1:19090/non-sensitive.wav?x=1&y=2"
    )
