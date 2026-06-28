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
    assert "_renderSidebarRowsFromRawSessions(sessionsRaw, referenceRaw)" in render_body
    assert "const sessions=_renderSidebarRowsFromRawSessions(sessionsRaw, referenceRaw);" in render_body
    assert "let originOptions=_sidebarOriginOptions(originCounts, originLabels);" in render_body
    assert "_ensureOriginFilterDefaults(originOptions)" in render_body
    assert "const count=filter==='cli'?renderedCliSessionCount:renderedWebuiSessionCount;" not in render_body
    assert "withMessages.filter(" not in render_body


def test_partition_helper_applies_message_source_project_and_archive_gates():
    block = _partition_block()

    assert "function _sidebarRowHasVisibleMessages(s, activeSidForSidebar)" in SESSIONS_JS
    assert "_sidebarRowHasVisibleMessages(s, activeSidForSidebar)" in block
    assert "const origin=_sidebarOriginIdForSession(s);" in block
    assert "if(origin==='cli') cliSessionCount++;" in block
    assert "else if(origin==='webui') webuiSessionCount++;" in block
    assert "originCounts.set(origin, Number(originCounts.get(origin) || 0) + 1);" in block
    assert "originLabels.set(origin, _sidebarOriginLabelForId(origin, _getChannelLabel(s) || ''));" in block
    assert "if(!_activeOriginFilters.has(origin)) continue;" in block
    assert "if(!_showArchived&&s.archived) continue;" in block
    assert "archivedCount," in block
    assert "return {" in block
    assert "sourceFiltered," in block
    assert "profileFiltered," in block
    assert "sessionsRaw," in block
    assert "referenceRaw," in block


def test_partition_helper_keeps_single_pass_filtering_and_shared_render_path():
    render_body = _function_block("renderSessionListFromCache")
    block = _partition_block()

    assert "webuiSessionCount," in block
    assert "cliSessionCount," in block
    assert "originCounts," in block
    assert "originLabels," in block
    assert "webuiReferenceRaw," not in block
    assert "cliReferenceRaw," not in block
    assert "webuiSessionsRaw," not in block
    assert "cliSessionsRaw," not in block
    assert "const renderedWebuiSessionCount=" not in render_body
    assert "const renderedCliSessionCount=" not in render_body
    assert "_renderSidebarRowsFromRawSessions(webuiSessionsRaw, webuiReferenceRaw).length" not in render_body
    assert "_renderSidebarRowsFromRawSessions(cliSessionsRaw, cliReferenceRaw).length" not in render_body
    assert "const sessions=_renderSidebarRowsFromRawSessions(sessionsRaw, referenceRaw);" in render_body
    assert "function _countRenderedSidebarRowsFromRawSessions" not in SESSIONS_JS
    assert "function _renderSidebarRowsFromRawSessions(sessionsRaw, referenceSessionsRaw){" in SESSIONS_JS
    assert "_attachChildSessionsToSidebarRows(_collapseSessionLineageForSidebar(sessionsRaw), sessionsRaw, referenceRows)" in SESSIONS_JS
