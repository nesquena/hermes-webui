from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function body not found: {signature}")


def _scroll_listener_block() -> str:
    start = UI_JS.index("el.addEventListener('scroll'")
    return UI_JS[start : UI_JS.index("})();", start)]


def test_clicking_current_session_is_noop_before_load_session_side_effects():
    load_session = _function_body(SESSIONS_JS, "async function loadSession")

    current_idx = load_session.index("const currentSid = S.session ? S.session.session_id : null")
    noop_idx = load_session.index("if(currentSid===sid && !forceReload) return")
    loading_idx = load_session.index("_loadingSessionId = sid")
    stop_idx = load_session.index("stopApprovalPolling")

    assert current_idx < noop_idx < loading_idx < stop_idx, (
        "clicking the already-open sidebar row must be a no-op before loadSession() "
        "mutates loading/runtime state or scroll-affecting UI"
    )


def test_scroll_to_bottom_settles_across_late_markdown_layout_growth():
    settle = _function_body(UI_JS, "function _settleMessageScrollToBottom")
    scroll = _function_body(UI_JS, "function scrollToBottom")
    pinned = _function_body(UI_JS, "function scrollIfPinned")

    # The settle survives late markdown layout growth (Prism/KaTeX/Mermaid/images)
    # via a ResizeObserver on the growing content node (#msgInner) plus a 2s
    # static-content fallback — this replaced the old [0,16,80,180]ms setTimeout
    # fan-out + double-rAF scrollHeight polling (#3920, Firefox reflow jitter).
    assert "requestAnimationFrame" in settle
    assert "setTimeout" in settle
    assert "new ResizeObserver" in settle
    assert "getElementById('msgInner')" in settle, (
        "the ResizeObserver must observe the growing content node (#msgInner), "
        "not the fixed #messages scroll container"
    )
    assert "2000" in settle, "a 2s fallback must cover fully-static content that never resizes"
    # scrollToBottom uses force=false so the observer runs, and explicit=true so the
    # settle still runs even when Auto-follow is off (explicit user jump). The
    # automatic scrollIfPinned() path passes no explicit flag (stays auto-gated).
    assert "_settleMessageScrollToBottom(false, true)" in scroll
    assert "_settleMessageScrollToBottom(false)" in pinned
    assert "!_scrollPinned" in settle
    assert "const token=++_bottomSettleToken" in settle
    assert "token!==_bottomSettleToken" in settle


def test_scroll_to_bottom_writes_scroll_position_immediately_before_delayed_settle():
    scroll = _function_body(UI_JS, "function scrollToBottom")

    immediate_idx = scroll.index("_setMessageScrollToBottom();")
    settle_idx = scroll.index("_settleMessageScrollToBottom(false, true)")

    assert immediate_idx < settle_idx, (
        "scrollToBottom() must write scrollTop synchronously before scheduling the "
        "ResizeObserver settle; otherwise a DOM-rebuild scroll event can cancel the "
        "delayed settle and strand the viewport at the top"
    )


def test_message_scroll_listener_does_not_downgrade_explicit_bottom_pin_on_first_near_bottom_event():
    listener_block = _scroll_listener_block()
    set_bottom = _function_body(UI_JS, "function _setMessageScrollToBottom")

    assert "_nearBottomCount=2" in set_bottom
    assert "_scrollPinned=_nearBottomCount>=2" not in listener_block
    assert "if(_nearBottomCount>=2){" in listener_block
    assert "_scrollPinned=false" in listener_block


def test_user_scroll_cancels_delayed_bottom_settling():
    listener_block = _scroll_listener_block()
    record = _function_body(UI_JS, "function _recordNonMessageScrollIntent")
    pinned = _function_body(UI_JS, "function scrollIfPinned")
    final = _function_body(UI_JS, "function _settleFinalScroll")

    assert "function _cancelBottomSettle" in UI_JS
    assert "_cancelBottomSettle();" in listener_block
    assert "e.deltaY< -30" in record
    assert "_cancelBottomSettle();" in record
    assert "_lastNonMessageScrollIntentMs=performance.now();" in record
    assert "_scrollPinned=false" in record
    assert "if(_messageUserUnpinned) return;" in pinned
    assert "_messageUserUnpinned" in final and "return" in final
    assert "_recentMessageUpwardIntent()" not in pinned


def test_external_active_refresh_defers_while_reader_is_manually_unpinned():
    refresh = _function_body(SESSIONS_JS, "async function refreshActiveSessionIfExternallyUpdated")

    assert "_isMessageReaderUnpinned" in UI_JS
    assert "_deferActiveSessionExternalRefresh" in SESSIONS_JS
    assert "typeof _isMessageReaderUnpinned==='function'&&_isMessageReaderUnpinned()" in refresh
    assert "_deferActiveSessionExternalRefresh(reason||'poll');" in refresh
    assert "await loadSession(sid, {force:true, externalRefreshReason:reason||'poll'});" in refresh


def test_session_switch_clears_deferred_active_refresh_reason():
    load = _function_body(SESSIONS_JS, "async function loadSession")
    assert "function _clearDeferredActiveSessionExternalRefresh()" in SESSIONS_JS
    assert "_deferredActiveSessionExternalRefreshReason = '';" in SESSIONS_JS
    assert "if (currentSid !== sid) {\n    _clearDeferredActiveSessionExternalRefresh();\n  }" in load


def test_deferred_active_refresh_keeps_idle_reconcile_over_poll():
    defer = _function_body(SESSIONS_JS, "function _deferActiveSessionExternalRefresh")
    assert "const nextReason = reason || 'poll';" in defer
    assert "_deferredActiveSessionExternalRefreshReason==='idle-reconcile'&&nextReason==='poll'" in defer
    assert "_deferredActiveSessionExternalRefreshReason = nextReason;" in defer


def test_scroll_to_bottom_flushes_deferred_active_refresh_after_explicit_repin():
    scroll = _function_body(UI_JS, "function scrollToBottom")

    assert "_flushDeferredActiveSessionExternalRefresh" in SESSIONS_JS
    assert "_flushDeferredActiveSessionExternalRefresh" in scroll


def test_preserve_scroll_restores_unpinned_viewport_after_dom_rebuild():
    render = _function_body(UI_JS, "function renderMessages")
    after_render = _function_body(UI_JS, "function _scrollAfterMessageRender")
    follow = _function_body(UI_JS, "function _followMessagesAfterDomReplace")
    capture = _function_body(UI_JS, "function _captureMessageScrollSnapshot")
    restore = _function_body(UI_JS, "function _restoreMessageScrollSnapshot")

    snapshot_idx = render.index("const scrollSnapshot=(preserveScroll||_messageUserUnpinned)?_captureMessageScrollSnapshot():null")
    inner_idx = render.index("const inner=$('msgInner')")
    final_scroll_idx = render.rindex("_scrollAfterMessageRender(preserveScroll, scrollSnapshot)")

    assert snapshot_idx < inner_idx < final_scroll_idx, (
        "renderMessages({preserveScroll:true}) must capture #messages.scrollTop before "
        "replacing transcript DOM, then pass that snapshot to the post-render scroll helper"
    )
    assert "if(!readerAwayFromBottom && !_messageUserUnpinned && _followMessagesAfterDomReplace()) return;" in after_render
    assert "_restoreMessageScrollSnapshot(scrollSnapshot);\n    _maybeShowNewMessageScrollCue(scrollSnapshot);" in after_render
    assert "_shouldFollowMessagesOnDomReplace()" in follow
    assert "scrollToBottom();" in follow
    assert "anchor:(typeof _captureMessageViewportAnchor==='function')?_captureMessageViewportAnchor():null" in capture
    assert "sessionIdx:Number.isFinite(sessionIdx)?sessionIdx:_messageSessionIndexForRawIdx(rawIdx)" in UI_JS
    assert "key:row&&row.dataset?String(row.dataset.messageAnchorKey||''):''" in UI_JS
    assert "row.dataset.sessionMsgIdx=_messageSessionIndexForRawIdx(rawIdx);" in UI_JS
    assert "seg.dataset.sessionMsgIdx=_messageSessionIndexForRawIdx(rawIdx);" in UI_JS
    assert "row.dataset.messageAnchorKey=_messageViewportAnchorKeyForMessage(m);" in UI_JS
    assert "seg.dataset.messageAnchorKey=_messageViewportAnchorKeyForMessage(m);" in UI_JS
    assert "container.querySelector(`[data-session-msg-idx=\"${sessionIdx}\"]`)" in UI_JS
    assert "if(!row&&anchorKey) return false;" in UI_JS
    assert "if(!row&&hasSessionIdx) return false;" in UI_JS
    assert "_restoreMessageViewportAnchor(snapshot.anchor,0)" in restore
    assert "if(!restoredViaAnchor){" in restore
    assert "el.scrollTop=Math.max(0,Math.min(Number(snapshot.top)||0,maxTop))" in restore
    assert "_programmaticScroll=true" in restore


def test_same_session_reload_anchor_uses_absolute_session_message_index():
    assert "function _messageSessionIndexBase()" in UI_JS
    assert "return _messageSessionIndexBase()+n;" in UI_JS
    assert "return n-_messageSessionIndexBase();" in UI_JS
    assert "data-session-msg-idx" in UI_JS
    assert "data-message-anchor-key" in UI_JS
    assert "function _messageViewportAnchorKeyForMessage" in UI_JS
    assert "function _messageVisibleIndexForAnchorKey" in UI_JS
    assert "function _remountMessageViewportAnchor" in UI_JS
    assert "visibleKeyNode=anchorKey" in UI_JS
    assert "visIdx=anchorKey?_messageVisibleIndexForAnchorKey(anchorKey,visWithIdx):-1" in UI_JS
    assert "if(!restoredViaAnchor&&typeof _remountMessageViewportAnchor==='function'&&_remountMessageViewportAnchor(snapshot.anchor))" in UI_JS
    assert "const rawFromSession=_messageRawIdxForSessionIndex(sessionIdx);" in UI_JS


def test_refresh_session_updates_message_window_offset_before_rerender():
    refresh = _function_body(UI_JS, "function refreshSession")
    messages_idx = refresh.index("S.messages = data.session.messages || [];")
    truncated_idx = refresh.index("_messagesTruncated = !!data.session._messages_truncated;")
    offset_idx = refresh.index("_oldestIdx = data.session._messages_offset || 0;")
    render_idx = refresh.index("_renderMessagesWithScrollSnapshot();")

    assert messages_idx < truncated_idx < offset_idx < render_idx
