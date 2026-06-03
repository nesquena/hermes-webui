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
