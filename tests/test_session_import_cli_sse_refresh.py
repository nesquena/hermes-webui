"""Regression guard for CLI import refresh overwriting active transcript."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_sse_import_cli_guard_skips_shorter_transcript_overwrite():
    """The SSE import refresh path should refuse stale/shorter transcripts."""
    start = SESSIONS_JS.index("function startGatewaySSE")
    stop = SESSIONS_JS.index("function stopGatewaySSE", start)
    sse_block = SESSIONS_JS[start:stop]

    assert "const prev = S.messages.length;" in sse_block
    assert "const next = res.session.messages.filter(m => m && m.role);" in sse_block
    assert "if (next.length < prev) return;" in sse_block
    assert "if (prev > 0 && !_isCliImportRefreshPrefixMatch(S.messages, next)) return;" in sse_block
    assert "S.messages = next;" in sse_block


def test_sse_import_cli_refresh_prefix_helper_ignores_timestamps():
    """Refresh-prefix helper used by SSE should compare messages without timestamp keys."""
    assert "function _normalizeMessageForCliImportComparison(message)" in SESSIONS_JS
    assert "delete clone.timestamp;" in SESSIONS_JS
    assert "delete clone._ts;" in SESSIONS_JS
    assert "function _isCliImportRefreshPrefixMatch(localMessages, freshMessages)" in SESSIONS_JS
    assert "_normalizeMessageForCliImportComparison" in SESSIONS_JS
    assert "localMessages.length > freshMessages.length" in SESSIONS_JS
