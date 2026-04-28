"""Regression checks for #856 background completion unread markers."""

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def _done_block() -> str:
    start = MESSAGES_JS.find("source.addEventListener('done'")
    assert start != -1, "done handler not found in messages.js"
    end = MESSAGES_JS.find("source.addEventListener('stream_end'", start)
    assert end != -1, "stream_end handler not found after done handler"
    return MESSAGES_JS[start:end]


def test_background_completion_unread_uses_explicit_marker_not_message_delta():
    """A background completion must stay unread even when message_count has no delta."""
    assert "SESSION_COMPLETION_UNREAD_KEY = 'hermes-session-completion-unread'" in SESSIONS_JS
    assert "function _markSessionCompletionUnread(" in SESSIONS_JS
    assert "function _clearSessionCompletionUnread(" in SESSIONS_JS
    assert "function _hasSessionCompletionUnread(" in SESSIONS_JS

    has_unread_idx = SESSIONS_JS.find("function _hasUnreadForSession(s)")
    assert has_unread_idx != -1, "_hasUnreadForSession not found"
    has_unread_block = SESSIONS_JS[has_unread_idx:SESSIONS_JS.find("async function newSession", has_unread_idx)]

    marker_idx = has_unread_block.find("_hasSessionCompletionUnread(s.session_id)")
    count_idx = has_unread_block.find("s.message_count > Number")
    assert marker_idx != -1, "_hasUnreadForSession must check explicit completion unread marker"
    assert count_idx != -1, "_hasUnreadForSession must keep the existing message_count fallback"
    assert marker_idx < count_idx, (
        "explicit completion unread marker must be checked before message_count delta, "
        "because completed streams can have viewed_count == message_count"
    )


def test_background_done_sets_marker_when_session_not_actively_viewed():
    done_block = _done_block()
    assert "const isSessionViewed=_isSessionActivelyViewed(activeSid);" in done_block
    assert "if(!isSessionViewed && typeof _markSessionCompletionUnread==='function')" in done_block
    assert "_markSessionCompletionUnread(activeSid, d.session&&d.session.message_count);" in done_block


def test_active_done_marks_viewed_without_setting_unread_marker():
    done_block = _done_block()
    marker_idx = done_block.find("_markSessionCompletionUnread(activeSid")
    viewed_guard_idx = done_block.find("if(isSessionViewed){", marker_idx)
    viewed_mark_idx = done_block.find("_markSessionViewed(activeSid", viewed_guard_idx)

    assert marker_idx != -1, "background completion marker call missing"
    assert viewed_guard_idx != -1, "done handler must guard active-session UI updates"
    assert viewed_mark_idx != -1, "active/current completion must still mark session viewed"
    assert viewed_guard_idx < viewed_mark_idx, (
        "active-session viewed write must remain inside isSessionViewed guard so "
        "switch-away races cannot mark a background completion read"
    )


def test_switching_away_counts_as_background_completion():
    helper_idx = MESSAGES_JS.find("function _isSessionActivelyViewed(sid)")
    assert helper_idx != -1, "_isSessionActivelyViewed helper missing"
    helper_block = MESSAGES_JS[helper_idx:MESSAGES_JS.find("async function send()", helper_idx)]

    assert "S.session.session_id!==sid" in helper_block
    assert "_loadingSessionId" in helper_block
    assert "_loadingSessionId!==sid" in helper_block, (
        "if loadSession(B) is in flight while done(A) arrives, A must be treated "
        "as background even though S.session can still temporarily point at A"
    )


def test_completion_unread_clears_only_when_session_is_opened():
    load_idx = SESSIONS_JS.find("async function loadSession(sid)")
    assert load_idx != -1, "loadSession not found"
    load_block = SESSIONS_JS[load_idx:SESSIONS_JS.find("function _resolveSessionModelForDisplaySoon", load_idx)]

    stale_guard_idx = load_block.find("if (_loadingSessionId !== sid) return;")
    clear_idx = load_block.find("_clearSessionCompletionUnread(S.session.session_id);")
    set_viewed_idx = load_block.find("_setSessionViewedCount(S.session.session_id")

    assert clear_idx != -1, "loadSession must clear explicit completion unread when the user opens the session"
    assert stale_guard_idx != -1 and stale_guard_idx < clear_idx, (
        "stale loadSession responses must not clear unread markers for sessions the user did not actually open"
    )
    assert set_viewed_idx != -1 and set_viewed_idx < clear_idx, (
        "completion unread should clear at the same point the session is marked viewed"
    )


def test_historical_sessions_are_not_marked_unread_on_list_render():
    """The explicit unread marker must be event-driven, not initialized by _hasUnreadForSession."""
    has_unread_idx = SESSIONS_JS.find("function _hasUnreadForSession(s)")
    assert has_unread_idx != -1
    has_unread_block = SESSIONS_JS[has_unread_idx:SESSIONS_JS.find("async function newSession", has_unread_idx)]

    assert "_markSessionCompletionUnread" not in has_unread_block, (
        "rendering old historical sessions must not create completion-unread markers"
    )
    assert "_setSessionViewedCount(s.session_id, Number(s.message_count || 0));" in has_unread_block, (
        "missing viewed-count baseline should still initialize as read for historical sessions"
    )
