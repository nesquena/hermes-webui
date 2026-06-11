"""Regression coverage for single-pass sidebar session partitioning."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _partition_block() -> str:
    start = SESSIONS_JS.index("function _partitionSidebarSessionRows(")
    end = SESSIONS_JS.index("function renderSessionListFromCache()", start)
    return SESSIONS_JS[start:end]


def test_render_uses_single_pass_partition_helper():
    render_start = SESSIONS_JS.index("function renderSessionListFromCache()")
    render_end = SESSIONS_JS.index("function _showProjectPicker", render_start)
    render_body = SESSIONS_JS[render_start:render_end]

    assert "_partitionSidebarSessionRows(allMatched, activeSidForSidebar)" in render_body
    assert "withMessages.filter(" not in render_body


def test_partition_helper_applies_message_source_project_and_archive_gates():
    block = _partition_block()

    assert "function _sidebarRowHasVisibleMessages(s, activeSidForSidebar)" in SESSIONS_JS
    assert "_sidebarRowHasVisibleMessages(s, activeSidForSidebar)" in block
    assert "if(_sessionSourceFilter==='cli' && !window._showCliSessions && cliSessionCount===0)" in block
    assert "const showCliOnly=_sessionSourceFilter==='cli';" in block
    assert "if(!_showArchived&&s.archived) continue;" in block
    assert "if(s.archived) archivedCount++;" in block
    assert "return {" in block
    assert "profileFiltered," in block
    assert "sessionsRaw," in block
