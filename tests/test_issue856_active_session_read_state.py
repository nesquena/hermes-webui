"""Regression checks for #856 active-session unread state handling."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"could not find body for {signature}")


def test_messages_js_defines_active_session_viewed_helper():
    assert "function _markSessionViewed(" in MESSAGES_JS, (
        "messages.js should define a helper that marks the active session as viewed"
    )
    assert "_setSessionViewedCount" in MESSAGES_JS, (
        "active-session viewed helper must delegate to the sidebar viewed-count store"
    )


def test_done_path_marks_active_session_as_viewed():
    done_idx = MESSAGES_JS.find("source.addEventListener('done'")
    assert done_idx != -1, "done handler not found in messages.js"
    done_block = MESSAGES_JS[done_idx:MESSAGES_JS.find("source.addEventListener('stream_end'", done_idx)]
    assert "const completedSid=completedSession.session_id||activeSid;" in done_block
    assert "_markSessionViewed(completedSid" in done_block, (
        "done handler must mark the final active session id as viewed so unread dot "
        "does not linger after compression rotates session_id"
    )


def test_cancel_path_marks_active_session_as_viewed():
    cancel_idx = MESSAGES_JS.find("source.addEventListener('cancel'")
    assert cancel_idx != -1, "cancel handler not found in messages.js"
    restore_marker = "async function _restoreSettledSession(source"
    cancel_block = MESSAGES_JS[cancel_idx:MESSAGES_JS.find(restore_marker, cancel_idx)]
    assert "_markSessionViewed(activeSid" in cancel_block, (
        "cancel handler must mark the active session as viewed after settling messages"
    )


def test_restore_and_error_paths_mark_active_session_as_viewed():
    restore_idx = MESSAGES_JS.find("async function _restoreSettledSession(source")
    assert restore_idx != -1, "_restoreSettledSession(source) not found in messages.js"
    restore_block = MESSAGES_JS[restore_idx:MESSAGES_JS.find("function _handleStreamError(source)", restore_idx)]
    assert "const completedSid=session.session_id||activeSid;" in restore_block
    assert "_markSessionViewed(completedSid" in restore_block, (
        "_restoreSettledSession() must mark the final session id as viewed"
    )

    error_idx = MESSAGES_JS.find("function _handleStreamError(source)")
    assert error_idx != -1, "_handleStreamError(source) not found in messages.js"
    error_block = MESSAGES_JS[error_idx:]
    assert "_markSessionViewed(activeSid" in error_block, (
        "_handleStreamError() must mark the active session as viewed"
    )


def test_active_server_idle_row_overrides_stale_local_busy_sidebar_state():
    effective = _function_body(SESSIONS_JS, "function _isSessionEffectivelyStreaming(s)")
    idle_guard = _function_body(SESSIONS_JS, "function _isActiveSessionIdleForSidebar(s)")

    assert "if (_isActiveSessionIdleForSidebar(s)) return false;" in effective
    assert "_isSessionLocallyStreaming(s)" in effective

    assert "S.session.session_id !== s.session_id" in idle_guard
    assert "!_isServerIdleSessionRow(s)" in idle_guard
    assert "stale local" in idle_guard
    assert "S.busy flag must not keep the active sidebar row yellow forever (#856)" in idle_guard


def test_optimistic_send_start_window_is_not_mistaken_for_stale_yellow_dot():
    idle_guard = _function_body(SESSIONS_JS, "function _isActiveSessionIdleForSidebar(s)")

    assert "typeof _sendInProgress !== 'undefined'" in idle_guard
    assert "_sendInProgress && s.session_id === _sendInProgressSid" in idle_guard
    assert "return false;" in idle_guard
