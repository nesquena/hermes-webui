from pathlib import Path

from tests.test_ui_tool_call_cleanup import _function_body

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
PREPEND_RENDER = "renderMessages({preserveScroll:true, _prependAnchor:viewportAnchor, _ownedPrepend:true});"


def test_loading_older_messages_expands_render_window_before_rendering():
    body = _function_body(SESSIONS_JS, "_loadOlderMessages")
    replace_idx = body.index("S.messages = nextMessages")
    expand_idx = body.index("_messageRenderWindowSize=_currentMessageRenderWindowSize()")
    render_idx = body.index(PREPEND_RENDER)
    assert replace_idx < expand_idx < render_idx, (
        "scroll-to-top paging must expand the DOM render window before renderMessages(); "
        "otherwise fetched older messages stay hidden and only the hidden counter changes"
    )
    assert "if(typeof _messageIsRenderable==='function') return _messageIsRenderable(m);" in body
    assert "Math.max(addedRenderable, MESSAGE_RENDER_WINDOW_DEFAULT)" in body


def test_loading_older_messages_preserves_viewport_without_bottom_snap():
    body = _function_body(SESSIONS_JS, "_loadOlderMessages")
    render = _function_body(UI_JS, "renderMessages")
    commit = _function_body(UI_JS, "_commitMessageWindow")
    restore = _function_body(UI_JS, "_restoreMessageWindowReader")
    assert PREPEND_RENDER in body
    assert "const ownedWindow=windowOnly||!!(options&&options._ownedPrepend)" in render
    assert "const windowAnchor=ownedWindow?((options&&options._prependAnchor)||_messageWindowSnapshot()):null" in render
    assert "_commitMessageWindow(liveInner,inner,windowAnchor,windowOnly)" in render
    assert "_restoreMessageWindowReader(target,anchor)" in commit
    # Compensation is now by a stable content landmark, not estimated prepended
    # height. Both retained nodes and replacement nodes must resolve that owner.
    assert "anchor.node.isConnected?anchor.node:null" in restore
    assert "Number(node.dataset.sessionMsgIdx)===anchor.sessionIndex" in restore
    assert "row.getBoundingClientRect().top-container.getBoundingClientRect().top-anchor.offset" in restore
    assert "container.scrollTop+=delta" in restore
    assert "container.scrollTop = newScrollH - prevScrollH" not in body
    assert body.index(PREPEND_RENDER) < body.rindex("_scrollPinned = false")


def test_loading_older_messages_marks_scroll_programmatic_while_anchoring():
    body = _function_body(UI_JS, "_restoreMessageWindowReader")
    set_idx = body.index("_programmaticScroll=true;")
    restore_idx = body.index("container.scrollTop+=delta;")
    baseline_idx = body.index("_lastScrollTop=container.scrollTop;")
    clear_idx = body.index("_deferClearProgrammaticScroll();")
    assert set_idx < restore_idx < baseline_idx < clear_idx


def test_loading_older_messages_captures_anchor_before_replacing_messages():
    body = _function_body(SESSIONS_JS, "_loadOlderMessages")
    anchor_idx = body.index("const viewportAnchor = container ? _messageWindowSnapshot() : null;")
    replace_idx = body.index("S.messages = nextMessages")
    render_idx = body.index(PREPEND_RENDER)
    # Sampling before an await would overwrite reader movement during the fetch.
    assert body.rindex("await ") < anchor_idx < replace_idx < render_idx
