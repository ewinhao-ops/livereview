from __future__ import annotations

import ast
from pathlib import Path

from app.core.safe_error import safe_error_text


def test_runtime_error_redacts_credentials_cookie_signed_url_payload_and_paths() -> None:
    raw = (
        "Authorization: Bearer real-secret Cookie=session=private api_key=key-123 "
        "https://media.example/live.flv?sign=signed-value&expire=1 "
        "content='完整音频内容' D:\\private\\live-review\\data\\replay.db "
        "/home/operator/live-review/audio/session.m4a"
    )
    safe = safe_error_text(raw)
    for forbidden in (
        "real-secret", "session=private", "key-123", "signed-value",
        "完整音频内容", "D:\\private", "/home/operator",
    ):
        assert forbidden not in safe
    assert "<redacted>" in safe
    assert "<redacted-path>" in safe


def test_runtime_error_is_single_line_and_bounded() -> None:
    safe = safe_error_text("failure\n" + "x" * 1000, limit=80)
    assert "\n" not in safe
    assert len(safe) <= 80


def _unsafe_exception_reference(node: ast.AST, name: str) -> bool:
    if isinstance(node, ast.Call):
        function = node.func
        safe_name = (
            function.id if isinstance(function, ast.Name)
            else function.attr if isinstance(function, ast.Attribute) else ""
        )
        if safe_name in {"safe_error_text", "safe_exception_code", "_safe_log_text", "type"}:
            return False
    if isinstance(node, ast.Name) and node.id == name:
        return True
    return any(_unsafe_exception_reference(child, name) for child in ast.iter_child_nodes(node))


def test_runtime_code_never_prints_or_persists_raw_exception_text() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
            if not handler.name:
                continue
            for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
                if isinstance(node, ast.Call):
                    function = node.func
                    call_name = (
                        function.id if isinstance(function, ast.Name)
                        else function.attr if isinstance(function, ast.Attribute) else ""
                    )
                    if call_name in {"print", "warning", "error", "exception", "info"} and any(
                        _unsafe_exception_reference(arg, handler.name) for arg in node.args
                    ):
                        offenders.append(f"{path.relative_to(app_root)}:{node.lineno}")
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Attribute) and target.attr in {"error_msg", "detail"}
                    for target in node.targets
                ) and _unsafe_exception_reference(node.value, handler.name):
                    offenders.append(f"{path.relative_to(app_root)}:{node.lineno}")
    assert offenders == []
