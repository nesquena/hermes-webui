"""Regression coverage for single-pass sidebar session partitioning."""

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


def _partition_block() -> str:
    return _function_block("_partitionSidebarSessionRows")


def test_render_uses_single_pass_partition_helper():
    render_body = _function_block("renderSessionListFromCache")

    assert "_partitionSidebarSessionRows(allMatched, activeSidForSidebar)" in render_body
    assert "_renderSidebarRowsFromRawSessions(sessionsRaw)" in render_body
    assert "withMessages.filter(" not in render_body


def test_partition_helper_applies_message_source_project_and_archive_gates():
    block = _partition_block()

    assert "function _sidebarRowHasVisibleMessages(s, activeSidForSidebar)" in SESSIONS_JS
    assert "_sidebarRowHasVisibleMessages(s, activeSidForSidebar)" in block
    assert "if(_sessionSourceFilter==='cli' && !window._showCliSessions && cliSessionCount===0)" in block
    assert "const showCliOnly=_sessionSourceFilter==='cli';" in block
    assert "if(!_showArchived&&s.archived) continue;" in block
    assert "if(s.archived){" in block
    assert "archivedCount: showCliOnly ? cliArchivedCount : webuiArchivedCount," in block
    assert "return {" in block
    assert "profileFiltered: showCliOnly ? cliProfileFiltered : webuiProfileFiltered," in block
    assert "sessionsRaw: showCliOnly ? cliSessionsRaw : webuiSessionsRaw," in block


def test_partition_helper_keeps_raw_source_counts_while_render_owns_visible_counts():
    render_body = _function_block("renderSessionListFromCache")

    assert "webuiSessionCount," in _partition_block()
    assert "cliSessionCount," in _partition_block()
    assert "webuiSessionsRaw," in _partition_block()
    assert "cliSessionsRaw," in _partition_block()
    assert "const renderedWebuiSessionCount=" in render_body
    assert "const renderedCliSessionCount=" in render_body
