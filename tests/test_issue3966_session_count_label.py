"""Regression coverage for sidebar source counts using rendered rows."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_render_counts_use_post_collapse_rows():
    render_start = SESSIONS_JS.index("function renderSessionListFromCache()")
    render_end = SESSIONS_JS.index("function _showProjectPicker", render_start)
    render_body = SESSIONS_JS[render_start:render_end]

    assert "const sessions=_renderSidebarRowsFromRawSessions(sessionsRaw);" in render_body
    assert "? sessions.length" in render_body
    assert ": _countRenderedSidebarRows(allMatched, activeSidForSidebar, false);" in render_body
    assert ": _countRenderedSidebarRows(allMatched, activeSidForSidebar, true);" in render_body
    assert "const count=filter==='cli'?renderedCliSessionCount:renderedWebuiSessionCount;" in render_body
    assert "const count=filter==='cli'?cliSessionCount:webuiSessionCount;" not in render_body


def test_rendered_count_helper_collapses_before_counting():
    helper_start = SESSIONS_JS.index("function _countRenderedSidebarRows(")
    helper_end = SESSIONS_JS.index("function renderSessionListFromCache()", helper_start)
    helper_body = SESSIONS_JS[helper_start:helper_end]

    assert "sessionsRaw.push(s);" in helper_body
    assert "return _renderSidebarRowsFromRawSessions(sessionsRaw).length;" in helper_body
    assert "function _renderSidebarRowsFromRawSessions(sessionsRaw){" in SESSIONS_JS
    assert "_attachChildSessionsToSidebarRows(_collapseSessionLineageForSidebar(sessionsRaw), sessionsRaw)" in SESSIONS_JS
