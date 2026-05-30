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

    assert "requestAnimationFrame" in settle
    assert "setTimeout" in settle
    assert "const passes=[0,16,80,180]" in settle
    assert "_settleMessageScrollToBottom(true)" in scroll
    assert "_settleMessageScrollToBottom(false)" in pinned
    assert "!_scrollPinned" in settle
    assert "const token=++_bottomSettleToken" in settle
    assert "token!==_bottomSettleToken" in settle


def test_scroll_to_bottom_writes_scroll_position_immediately_before_delayed_settle():
    scroll = _function_body(UI_JS, "function scrollToBottom")

    immediate_idx = scroll.index("_setMessageScrollToBottom();")
    settle_idx = scroll.index("_settleMessageScrollToBottom(true)")

    assert immediate_idx < settle_idx, (
        "scrollToBottom() must write scrollTop synchronously before scheduling delayed settles; "
        "otherwise a DOM-rebuild scroll event can cancel the delayed passes and strand the viewport at the top"
    )


def test_message_scroll_listener_does_not_downgrade_explicit_bottom_pin_on_first_near_bottom_event():
    listener_block = _scroll_listener_block()
    set_bottom = _function_body(UI_JS, "function _setMessageScrollToBottom")

    assert "_nearBottomCount=2" in set_bottom
    assert "_scrollPinned=_nearBottomCount>=2" not in listener_block
    assert "if(_nearBottomCount>=2) _scrollPinned=true" in listener_block
    assert "else { _nearBottomCount=0; _scrollPinned=false; }" in listener_block


def test_user_scroll_cancels_delayed_bottom_settling():
    listener_block = _scroll_listener_block()
    record = _function_body(UI_JS, "function _recordNonMessageScrollIntent")

    assert "function _cancelBottomSettle" in UI_JS
    assert "_cancelBottomSettle();" in listener_block
    assert "e.deltaY<0" in record
    assert "_cancelBottomSettle();" in record
    assert "_scrollPinned=false" in record


def test_preserve_scroll_restores_unpinned_viewport_after_dom_rebuild():
    render = _function_body(UI_JS, "function renderMessages")
    after_render = _function_body(UI_JS, "function _scrollAfterMessageRender")
    restore = _function_body(UI_JS, "function _restoreMessageScrollSnapshot")

    snapshot_idx = render.index("const scrollSnapshot=preserveScroll?_captureMessageScrollSnapshot():null")
    inner_idx = render.index("const inner=$('msgInner')")
    final_scroll_idx = render.rindex("_scrollAfterMessageRender(preserveScroll, scrollSnapshot)")

    assert snapshot_idx < inner_idx < final_scroll_idx, (
        "renderMessages({preserveScroll:true}) must capture #messages.scrollTop before "
        "replacing transcript DOM, then pass that snapshot to the post-render scroll helper"
    )
    assert "if(_scrollPinned) scrollIfPinned()" in after_render
    assert "else _restoreMessageScrollSnapshot(scrollSnapshot)" in after_render
    assert "el.scrollTop=Math.max(0,Math.min(Number(snapshot.top)||0,maxTop))" in restore
    assert "_programmaticScroll=true" in restore


def test_same_session_force_reload_preserves_transcript_scroll_on_render():
    load_session = _function_body(SESSIONS_JS, "async function loadSession")

    assert "const preserveTranscriptScroll = forceReload && currentSid===sid" in load_session, (
        "same-session force reloads should flag transcript scroll preservation so external refreshes "
        "do not snap a reader to the bottom"
    )
    assert "renderMessages({preserveScroll:preserveTranscriptScroll});" in load_session, (
        "loadSession() should pass preserveScroll on same-session force reload renders instead of "
        "reusing the default scrollToBottom path"
    )


def test_same_session_force_reload_keeps_loaded_transcript_window_instead_of_resetting_to_tail_30():
    ensure_loaded = _function_body(SESSIONS_JS, "async function _ensureMessagesLoaded")
    reload_limit = _function_body(SESSIONS_JS, "function _messageReloadLimitForSession")

    assert "if (_messagesTruncated) return Math.max(_INITIAL_MSG_LIMIT, loadedCount);" in reload_limit, (
        "same-session refreshes should preserve at least the currently loaded suffix width so a long "
        "transcript does not collapse back to the default 30-message tail while the user is reading"
    )
    assert "return 0;" in reload_limit, (
        "when the active session already has the full transcript loaded, force reloads should omit msg_limit "
        "instead of silently truncating the chat again"
    )
    assert "const msgLimit = _messageReloadLimitForSession(sid)" in ensure_loaded, (
        "_ensureMessagesLoaded() must consult the same-session reload helper before building the fetch URL"
    )
    assert "msg_limit=${msgLimit}" in ensure_loaded, (
        "truncated long-session refreshes should reuse the loaded window width rather than hardcoding 30"
    )
    assert "messages=1&resolve_model=0`" in ensure_loaded, (
        "fully loaded same-session refreshes should support the no-msg_limit path so the whole transcript stays available"
    )


def test_chat_refresh_polls_are_not_tuned_to_five_second_repaint_churn():
    assert "const _streamingPollMs = 15000;" in SESSIONS_JS, (
        "sidebar streaming poll should stay at 15s so normal chat use does not feel like a 5s refresh loop"
    )
    assert "const _activeSessionExternalRefreshMs = 20000;" in SESSIONS_JS, (
        "active-session external refresh should stay at 20s so long transcripts are not repeatedly reloaded while reading"
    )
