"""Regression checks for #856 active-session unread state handling."""

from pathlib import Path


MESSAGES_JS = (Path(__file__).resolve().parent.parent / "static" / "messages.js").read_text()


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
    cancel_block = MESSAGES_JS[cancel_idx:MESSAGES_JS.find("async function _restoreSettledSession()", cancel_idx)]
    assert "_markSessionViewed(activeSid" in cancel_block, (
        "cancel handler must mark the active session as viewed after settling messages"
    )


def test_restore_and_error_paths_mark_active_session_as_viewed():
    restore_idx = MESSAGES_JS.find("async function _restoreSettledSession()")
    assert restore_idx != -1, "_restoreSettledSession() not found in messages.js"
    restore_block = MESSAGES_JS[restore_idx:MESSAGES_JS.find("function _handleStreamError()", restore_idx)]
    assert "const completedSid=session.session_id||activeSid;" in restore_block
    assert "_markSessionViewed(completedSid" in restore_block, (
        "_restoreSettledSession() must mark the final session id as viewed"
    )

    error_idx = MESSAGES_JS.find("function _handleStreamError()")
    assert error_idx != -1, "_handleStreamError() not found in messages.js"
    error_block = MESSAGES_JS[error_idx:]
    assert "_markSessionViewed(activeSid" in error_block, (
        "_handleStreamError() must mark the active session as viewed"
    )
