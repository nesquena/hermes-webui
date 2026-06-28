"""Regression: edit/regenerate use absolute keep_count (#2184 pattern)."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    needle_async = f"async function {name}"
    start = src.index(needle_async)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name!r} body not found")


def test_submit_edit_uses_absolute_keep_count():
    body = _function_body(UI_JS, "submitEdit")
    assert re.search(r"absoluteKeepCount\s*=\s*_oldestIdx\s*\+\s*msgIdx", body)
    assert "keep_count: absoluteKeepCount" in body


def test_regenerate_uses_absolute_keep_count():
    body = _function_body(UI_JS, "regenerateResponse")
    assert re.search(r"absoluteKeepCount\s*=\s*_oldestIdx\s*\+\s*assistantIdx", body)
    assert "keep_count: absoluteKeepCount" in body


def test_submit_edit_captures_absolute_before_await():
    body = _function_body(UI_JS, "submitEdit")
    cap = re.search(r"absoluteKeepCount\s*=\s*_oldestIdx\s*\+\s*msgIdx", body)
    assert cap
    first_await = re.search(r"\bawait\b", body)
    assert first_await and cap.start() < first_await.start()