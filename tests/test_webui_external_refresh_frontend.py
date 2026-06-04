from pathlib import Path


SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")
UI_JS = Path("static/ui.js").read_text(encoding="utf-8")


def test_load_session_supports_force_reload_for_external_refresh():
    assert "async function loadSession(sid)" in SESSIONS_JS
    assert "const opts = arguments[1] || {};" in SESSIONS_JS
    assert "const forceReload = !!opts.force" in SESSIONS_JS
    assert "if(currentSid===sid && !forceReload) return;" in SESSIONS_JS
    assert "loadSession(sid, {force:true" in SESSIONS_JS


def test_active_session_external_refresh_uses_metadata_then_force_reload():
    assert "function ensureActiveSessionExternalRefreshPoll()" in SESSIONS_JS
    assert "async function refreshActiveSessionIfExternallyUpdated(reason)" in SESSIONS_JS
    assert "messages=0&resolve_model=0" in SESSIONS_JS
    assert "remoteCount > localCount || remoteLast > localLast" in SESSIONS_JS
    assert "if(S.busy || S.activeStreamId) return;" in SESSIONS_JS
    assert "document.hidden" in SESSIONS_JS


def test_active_session_external_refresh_has_focus_and_visibility_hooks():
    assert "visibilitychange" in SESSIONS_JS
    assert "window.addEventListener('focus'" in SESSIONS_JS
    assert "ensureActiveSessionExternalRefreshPoll();" in SESSIONS_JS


def test_session_list_external_refresh_uses_sse_invalidation_not_polling():
    """New sessions should refresh the sidebar from server invalidation events."""
    assert "async function refreshSessionList(reason='manual', opts={})" in SESSIONS_JS
    assert "function ensureSessionEventsSSE()" in SESSIONS_JS
    assert "new EventSource('api/sessions/events')" in SESSIONS_JS
    assert "addEventListener('sessions_changed'" in SESSIONS_JS
    assert "function _scheduleSessionEventsRefresh(reason)" in SESSIONS_JS
    assert "_sessionEventsNeedsRefreshOnOpen = true" in SESSIONS_JS
    assert "void refreshSessionList('reconnect')" in SESSIONS_JS
    assert "renderSessionList({deferWhileInteracting:!force})" in SESSIONS_JS
    assert "const refreshActive = !!(opts && opts.refreshActive)" in SESSIONS_JS
    assert "if(refreshActive) await refreshActiveSessionIfExternallyUpdated(reason||'session-list')" in SESSIONS_JS
    assert "_sessionListRefreshPendingReason = reason || 'session-list'" in SESSIONS_JS
    assert "if(pendingReason) _scheduleSessionEventsRefresh(pendingReason)" in SESSIONS_JS
    assert "ensureSessionEventsSSE();" in SESSIONS_JS
    assert "document._hermesSessionEventsVisibilityHook" in SESSIONS_JS
    ensure_fn = SESSIONS_JS[SESSIONS_JS.find("function ensureSessionEventsSSE()") :]
    assert ensure_fn.find("document._hermesSessionEventsVisibilityHook") < ensure_fn.find("document.hidden) return")
    assert "_sessionListExternalRefreshMs" not in SESSIONS_JS


def test_pwa_pull_to_refresh_refreshes_session_list_not_page_when_available():
    assert "window.refreshSessionList('pull', {force:true, refreshActive:true})" in UI_JS
    assert "Promise.resolve(window.refreshSessionList('pull', {force:true, refreshActive:true})).catch(()=>{}).finally(_ptrReset)" in UI_JS


def test_force_reload_clears_stale_blocking_prompts_immediately():
    """External refresh should not leave old approval/clarify modals blocking the composer.

    hideApprovalCard() and hideClarifyCard() defer hiding for their minimum-visible
    timers unless force=true. That is correct for active streams, but when a
    same-session external state.db update triggers loadSession(..., {force:true}),
    the session has completed elsewhere and stale prompts should be removed now.
    """
    assert "hideApprovalCard(forceReload)" in SESSIONS_JS
    assert "hideClarifyCard(forceReload, forceReload?'external-refresh':'dismissed')" in SESSIONS_JS


def test_same_session_force_reload_preserves_non_empty_composer_input():
    """A slow same-session refresh must not roll back text typed meanwhile.

    The active-session refresh path can finish seconds after it started. If the
    user kept typing, restoring the server draft at the end of that load would
    replace newer local input with an older debounced draft.
    """
    assert "function _restoreComposerDraft(draft, targetSid, opts={})" in SESSIONS_JS
    assert "const preserveActiveInput = !!(opts && opts.preserveActiveInput);" in SESSIONS_JS
    assert "if (preserveActiveInput && current && current !== text) return;" in SESSIONS_JS
    assert "_restoreComposerDraft(_draft, sid, {preserveActiveInput:currentSid===sid&&forceReload});" in SESSIONS_JS


def test_same_session_force_reload_keeps_loaded_transcript_width_hint():
    """Same-session force refresh must not collapse a long transcript to the tail."""
    assert "let _sameSessionForceReloadHint = null;" in SESSIONS_JS
    assert "function _captureSameSessionForceReloadHint(sid)" in SESSIONS_JS
    assert "loaded_renderable_count:loadedRenderableCount" in SESSIONS_JS
    assert "message_count:knownMessageCount" in SESSIONS_JS
    assert "truncated:!!_messagesTruncated" in SESSIONS_JS
    assert "function _messageReloadLimitForSession(sid)" in SESSIONS_JS
    assert "if(!hint.truncated) return null;" in SESSIONS_JS
    assert "const appendedMessageCount=Math.max(0,currentMessageCount-previousMessageCount);" in SESSIONS_JS
    assert "return Math.max(_INITIAL_MSG_LIMIT,loadedRenderableCount,loadedMessageCount+appendedMessageCount);" in SESSIONS_JS
    assert "const reloadLimit = _messageReloadLimitForSession(sid);" in SESSIONS_JS
    assert "const reloadLimitParam = reloadLimit ? `&msg_limit=${reloadLimit}` : '';" in SESSIONS_JS
    assert "finally {\n    _clearSameSessionForceReloadHint(sid);\n  }" in SESSIONS_JS

    load_start = SESSIONS_JS.index("async function loadSession(sid)")
    load_end = SESSIONS_JS.index("// ── Handoff hint logic", load_start)
    load_body = SESSIONS_JS[load_start:load_end]
    capture_pos = load_body.index("if (sameSessionForceReload) _captureSameSessionForceReloadHint(sid);")
    clear_pos = load_body.index("else _clearSameSessionForceReloadHint();", capture_pos)
    reset_pos = load_body.index("S.messages = [];", clear_pos)
    assert capture_pos < clear_pos < reset_pos
    assert "const sameSessionForceReload = forceReload && currentSid===sid;" in load_body
    assert "renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined)" in load_body


def test_same_width_force_reload_invalidates_visible_message_cache():
    """Replacing a transcript with the same length must still refresh cached rows."""
    clear_start = UI_JS.index("function clearVisibleMessageRowCache()")
    clear_end = UI_JS.index("function _resetMessageRenderWindow", clear_start)
    clear_body = UI_JS[clear_start:clear_end]
    assert "_visWithIdxCache=null;" in clear_body
    assert "_visWithIdxCacheLen=0;" in clear_body
    assert "clearVisibleMessageRowCache();" in UI_JS[UI_JS.index("function clearMessageRenderCache()") :]

    ensure_start = SESSIONS_JS.index("async function _ensureMessagesLoaded(sid)")
    ensure_end = SESSIONS_JS.index("function _messageComparableText", ensure_start)
    ensure_body = SESSIONS_JS[ensure_start:ensure_end]
    invalidate_pos = ensure_body.index("if(typeof clearVisibleMessageRowCache==='function') clearVisibleMessageRowCache();")
    replace_pos = ensure_body.index("S.messages = msgs;")
    assert invalidate_pos < replace_pos
