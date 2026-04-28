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


def _sessions_function_block(name: str, next_name: str) -> str:
    start = SESSIONS_JS.find(f"function {name}")
    assert start != -1, f"{name} not found in sessions.js"
    end = SESSIONS_JS.find(f"function {next_name}", start)
    assert end != -1, f"{next_name} not found after {name}"
    return SESSIONS_JS[start:end]


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


def test_polling_transition_marks_completion_unread_without_sse_done():
    transition_block = _sessions_function_block(
        "_markPollingCompletionUnreadTransitions",
        "newSession",
    )
    effective_block = _sessions_function_block(
        "_isSessionEffectivelyStreaming",
        "_markPollingCompletionUnreadTransitions",
    )
    render_idx = SESSIONS_JS.find("async function renderSessionList()")
    assert render_idx != -1, "renderSessionList not found"
    render_block = SESSIONS_JS[render_idx:SESSIONS_JS.find("// ── Gateway session SSE", render_idx)]

    assert "const _sessionStreamingById = new Map();" in SESSIONS_JS
    assert "const wasStreaming = _sessionStreamingById.get(sid);" in transition_block
    assert "const isStreaming = _isSessionEffectivelyStreaming(s);" in transition_block
    assert "s.is_streaming || _isSessionLocallyStreaming(s)" in effective_block
    assert "wasStreaming === true && !isStreaming" in transition_block, (
        "polling fallback must only fire on an observed streaming -> stopped transition"
    )
    assert "_markSessionCompletionUnread(sid, s.message_count);" in transition_block
    assert "_sessionStreamingById.set(sid, isStreaming);" in transition_block
    assert "_markPollingCompletionUnreadTransitions(_allSessions);" in render_block


def test_polling_transition_does_not_mark_historical_first_render():
    transition_block = _sessions_function_block(
        "_markPollingCompletionUnreadTransitions",
        "newSession",
    )

    assert "wasStreaming === true && !isStreaming" in transition_block
    assert "wasStreaming && !isStreaming" not in transition_block, (
        "first-render undefined state must not be treated as a completed stream"
    )
    mark_idx = transition_block.find("_markSessionCompletionUnread(sid")
    set_idx = transition_block.find("_sessionStreamingById.set(sid, isStreaming)")
    assert mark_idx != -1 and set_idx != -1 and mark_idx < set_idx, (
        "the current render should seed streaming state only after checking for "
        "a prior observed streaming state"
    )


def test_polling_transition_skips_visible_focused_active_session():
    helper_block = _sessions_function_block(
        "_isSessionActivelyViewedForList",
        "_markPollingCompletionUnreadTransitions",
    )
    transition_block = _sessions_function_block(
        "_markPollingCompletionUnreadTransitions",
        "newSession",
    )

    assert "S.session.session_id !== sid" in helper_block
    assert "_loadingSessionId !== sid" in helper_block
    assert "document.visibilityState !== 'visible'" in helper_block
    assert "!document.hasFocus()" in helper_block
    assert "!_isSessionActivelyViewedForList(sid)" in transition_block, (
        "polling fallback must not create an unread marker for a session the "
        "user is visibly and focusedly reading"
    )


def test_polling_transition_tracks_the_same_effective_streaming_state_as_sidebar():
    local_block = _sessions_function_block(
        "_isSessionLocallyStreaming",
        "_isSessionEffectivelyStreaming",
    )
    effective_block = _sessions_function_block(
        "_isSessionEffectivelyStreaming",
        "_markPollingCompletionUnreadTransitions",
    )
    render_idx = SESSIONS_JS.find("function _renderOneSession")
    assert render_idx != -1, "_renderOneSession not found"
    render_block = SESSIONS_JS[render_idx:SESSIONS_JS.find("const hasUnread=", render_idx)]

    assert "(isActive && S.busy)" in local_block
    assert "INFLIGHT && INFLIGHT[s.session_id]" in local_block
    assert "s.is_streaming || _isSessionLocallyStreaming(s)" in effective_block
    assert "const isStreaming=_isSessionEffectivelyStreaming(s);" in render_block, (
        "the row spinner and polling completion transition must use the same "
        "effective streaming source, including local INFLIGHT-only streams"
    )


def test_active_done_marks_viewed_without_setting_unread_marker():
    done_block = _done_block()
    marker_idx = done_block.find("_markSessionCompletionUnread(activeSid")
    active_guard_idx = done_block.find("if(isActiveSession){", marker_idx)
    viewed_guard_idx = done_block.find("if(isSessionViewed) _markSessionViewed(activeSid", active_guard_idx)

    assert marker_idx != -1, "background completion marker call missing"
    assert active_guard_idx != -1, "done handler must guard active-session UI updates"
    assert viewed_guard_idx != -1, "active/current completion must still mark session viewed when visible/focused"
    assert active_guard_idx < viewed_guard_idx, (
        "active-session viewed write must remain inside isSessionViewed guard so "
        "switch-away races cannot mark a background completion read"
    )


def test_hidden_active_done_still_updates_current_pane_but_not_read_state():
    done_block = _done_block()

    active_const_idx = done_block.find("const isActiveSession=_isSessionCurrentPane(activeSid);")
    viewed_const_idx = done_block.find("const isSessionViewed=_isSessionActivelyViewed(activeSid);")
    active_guard_idx = done_block.find("if(isActiveSession){", viewed_const_idx)
    session_update_idx = done_block.find("S.session=d.session", active_guard_idx)
    render_idx = done_block.find("renderMessages()", active_guard_idx)
    load_dir_idx = done_block.find("loadDir('.')", active_guard_idx)
    mark_viewed_idx = done_block.find("if(isSessionViewed) _markSessionViewed(activeSid", active_guard_idx)

    assert active_const_idx != -1, "done handler must compute active/current pane separately"
    assert viewed_const_idx != -1, "done handler must still compute visible/focused read state"
    assert active_const_idx < viewed_const_idx
    assert session_update_idx != -1, "active hidden completion must still refresh S.session"
    assert render_idx != -1, "active hidden completion must still render the final assistant response"
    assert load_dir_idx != -1, "active hidden completion must keep normal active-session finalization"
    assert mark_viewed_idx != -1, "read-state write must stay gated by visible/focused viewing"
    assert session_update_idx < mark_viewed_idx < render_idx, (
        "hidden active completion should update the pane, but only mark read when "
        "isSessionViewed is true"
    )


def test_hidden_or_unfocused_active_session_counts_as_background_completion():
    helper_idx = MESSAGES_JS.find("function _isSessionActivelyViewed(sid)")
    assert helper_idx != -1, "_isSessionActivelyViewed helper missing"
    helper_block = MESSAGES_JS[helper_idx:MESSAGES_JS.find("function _markActiveSessionViewedOnReturn", helper_idx)]

    current_idx = MESSAGES_JS.find("function _isSessionCurrentPane(sid)")
    assert current_idx != -1, "_isSessionCurrentPane helper missing"
    assert "function _isDocumentVisibleAndFocused()" in MESSAGES_JS
    assert "document.visibilityState" in MESSAGES_JS
    assert "document.visibilityState!=='visible'" in MESSAGES_JS
    assert "document.hasFocus" in MESSAGES_JS
    assert "!document.hasFocus()" in MESSAGES_JS
    assert "if(!_isSessionCurrentPane(sid)) return false;" in helper_block
    assert "if(!_isDocumentVisibleAndFocused()) return false;" in helper_block, (
        "active session completion must be treated as unread when the tab is "
        "hidden or the window is unfocused"
    )


def test_switching_away_counts_as_background_completion():
    helper_idx = MESSAGES_JS.find("function _isSessionCurrentPane(sid)")
    assert helper_idx != -1, "_isSessionCurrentPane helper missing"
    helper_block = MESSAGES_JS[helper_idx:MESSAGES_JS.find("function _isSessionActivelyViewed", helper_idx)]

    assert "S.session.session_id!==sid" in helper_block
    assert "_loadingSessionId" in helper_block
    assert "_loadingSessionId!==sid" in helper_block, (
        "if loadSession(B) is in flight while done(A) arrives, A must be treated "
        "as background even though S.session can still temporarily point at A"
    )


def test_restore_settled_background_stream_marks_completion_unread():
    restore_idx = MESSAGES_JS.find("async function _restoreSettledSession()")
    assert restore_idx != -1, "_restoreSettledSession not found"
    restore_block = MESSAGES_JS[restore_idx:MESSAGES_JS.find("function _handleStreamError", restore_idx)]

    assert "const isSessionViewed=_isSessionActivelyViewed(activeSid);" in restore_block
    assert "if(!isSessionViewed && typeof _markSessionCompletionUnread==='function')" in restore_block
    assert "_markSessionCompletionUnread(activeSid, session.message_count);" in restore_block
    assert "if(isSessionViewed) _markSessionViewed(activeSid" in restore_block, (
        "restore-settled fallback must not mark a hidden/background completion read"
    )


def test_focus_visibility_return_marks_active_session_viewed_and_clears_marker():
    return_idx = MESSAGES_JS.find("function _markActiveSessionViewedOnReturn()")
    assert return_idx != -1, "_markActiveSessionViewedOnReturn helper missing"
    return_block = MESSAGES_JS[return_idx:MESSAGES_JS.find("async function send()", return_idx)]

    assert "if(!_isDocumentVisibleAndFocused() || !S.session || !S.session.session_id) return;" in return_block
    assert "_markSessionViewed(S.session.session_id" in return_block
    assert "_clearSessionCompletionUnread(S.session.session_id)" in return_block, (
        "returning to a visible/focused tab must clear the explicit unread marker "
        "for the active session the user is now viewing"
    )
    assert "renderSessionListFromCache()" in return_block
    assert "document.addEventListener('visibilitychange', _markActiveSessionViewedOnReturn);" in MESSAGES_JS
    assert "window.addEventListener('focus', _markActiveSessionViewedOnReturn);" in MESSAGES_JS


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
    has_unread_block = SESSIONS_JS[
        has_unread_idx:SESSIONS_JS.find("function _isSessionActivelyViewedForList", has_unread_idx)
    ]

    assert "_markSessionCompletionUnread" not in has_unread_block, (
        "rendering old historical sessions must not create completion-unread markers"
    )
    assert "_setSessionViewedCount(s.session_id, Number(s.message_count || 0));" in has_unread_block, (
        "missing viewed-count baseline should still initialize as read for historical sessions"
    )
