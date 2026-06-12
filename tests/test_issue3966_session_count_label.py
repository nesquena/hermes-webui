"""Regression coverage for sidebar source counts using rendered rows."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_block(name: str) -> str:
    start = SESSIONS_JS.index(f"function {name}(")
    brace = SESSIONS_JS.index("{", start)
    depth = 0
    for idx in range(brace, len(SESSIONS_JS)):
        char = SESSIONS_JS[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return SESSIONS_JS[start : idx + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_render_counts_use_post_collapse_rows():
    render_body = _function_block("renderSessionListFromCache")

    assert "const sessions=_renderSidebarRowsFromRawSessions(sessionsRaw);" in render_body
    assert "? sessions.length" in render_body
    assert ": _countRenderedSidebarRowsFromRawSessions(webuiSessionsRaw);" in render_body
    assert ": _countRenderedSidebarRowsFromRawSessions(cliSessionsRaw);" in render_body
    assert "const count=filter==='cli'?renderedCliSessionCount:renderedWebuiSessionCount;" in render_body
    assert "const count=filter==='cli'?cliSessionCount:webuiSessionCount;" not in render_body


def test_rendered_count_helper_collapses_before_counting():
    helper_body = _function_block("_countRenderedSidebarRowsFromRawSessions")

    assert "_collapseSessionLineageForSidebar(sessionsRaw).length;" in helper_body
    assert "function _renderSidebarRowsFromRawSessions(sessionsRaw){" in SESSIONS_JS
    assert "_attachChildSessionsToSidebarRows(_collapseSessionLineageForSidebar(sessionsRaw), sessionsRaw)" in SESSIONS_JS
