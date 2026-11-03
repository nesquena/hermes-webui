"""Regression guard for CLI import refresh overwriting active transcript."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_sse_import_cli_guard_skips_shorter_transcript_overwrite():
    """The SSE import refresh path should refuse stale/shorter transcripts.

    #7882 (greptile round-6) moved the import_cli refresh body out of
    startGatewaySSE() into the module-local _applyGatewayCliImportRefresh()
    helper (executed coverage), so the guard pins now target the helper plus
    the delegation site inside startGatewaySSE."""
    start = SESSIONS_JS.index("function _applyGatewayCliImportRefresh")
    stop = SESSIONS_JS.index("async function probeGatewaySSEStatus", start)
    sse_block = SESSIONS_JS[start:stop]

    assert "const prev = S.messages.length;" in sse_block
    assert "const next = res.session.messages.filter(m => m && m.role);" in sse_block
    assert "if (next.length < prev) return;" in sse_block
    assert "if (prev > 0 && !_isCliImportRefreshPrefixMatch(S.messages, next)) return;" in sse_block
    # #3306 added an ephemeral-field carry-forward before the assignment, so the
    # replace RHS is now `_nextToAssign` (= carry-forward of `next`). The guard
    # invariants above are what this test protects; the wholesale replace remains.
    assert "S.messages = _nextToAssign;" in sse_block
    assert "S.session.message_count = next.length;" in sse_block
    assert "renderMessages({preserveScroll:true});" in sse_block
    # The helper must stay wired into the SSE handler — the guards above are
    # dead otherwise.
    sse_start = SESSIONS_JS.index("function startGatewaySSE")
    sse_stop = SESSIONS_JS.index("function stopGatewaySSE", sse_start)
    assert "_applyGatewayCliImportRefresh(" in SESSIONS_JS[sse_start:sse_stop]


def test_sse_import_cli_refresh_prefix_helper_ignores_timestamps():
    """Refresh-prefix helper used by SSE should compare messages without timestamp keys."""
    assert "function _normalizeMessageForCliImportComparison(message)" in SESSIONS_JS
    assert "delete clone.timestamp;" in SESSIONS_JS
    assert "delete clone._ts;" in SESSIONS_JS
    assert "function _isCliImportRefreshPrefixMatch(localMessages, freshMessages)" in SESSIONS_JS
    assert "_normalizeMessageForCliImportComparison" in SESSIONS_JS
    assert "localMessages.length > freshMessages.length" in SESSIONS_JS
