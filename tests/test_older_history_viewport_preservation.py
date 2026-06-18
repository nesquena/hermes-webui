from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
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


def test_loading_older_messages_expands_render_window_before_rendering():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    replace_idx = body.index("S.messages = nextMessages")
    expand_idx = body.index("_messageRenderWindowSize=_currentMessageRenderWindowSize()")
    render_idx = body.index("renderMessages({ preserveScroll: true });")

    assert replace_idx < expand_idx < render_idx, (
        "scroll-to-top paging must expand the DOM render window before renderMessages(); "
        "otherwise fetched older messages stay hidden and only the hidden counter changes"
    )
    assert "if(typeof _messageIsRenderable==='function') return _messageIsRenderable(m);" in body
    assert "Math.max(addedRenderable, MESSAGE_RENDER_WINDOW_DEFAULT)" in body


def test_loading_older_messages_preserves_viewport_without_bottom_snap():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    assert "renderMessages({ preserveScroll: true });" in body
    assert "const viewportAnchor = (container && typeof _captureMessageViewportAnchor === 'function')" in body
    assert "_captureMessageViewportAnchor()" in body
    assert "_restoreMessageViewportAnchor(viewportAnchor, olderMsgs.length)" in body
    assert "const restoredViaAnchor = (viewportAnchor && typeof _restoreMessageViewportAnchor === 'function')" in body
    assert "if (!restoredViaAnchor) {" in body
    assert "const virtualAddedHeight = (typeof _messageVirtualPrependedHeightDelta === 'function')" in body
    assert "_messageVirtualPrependedHeightDelta(addedRenderable)" in body
    assert "const addedHeight = Number.isFinite(virtualAddedHeight)" in body
    assert "container.scrollTop = oldTop + addedHeight" in body
    assert "container.scrollTop = newScrollH - prevScrollH" not in body

    restore_idx = body.index("_restoreMessageViewportAnchor(viewportAnchor, olderMsgs.length)")
    virtual_idx = body.index("_messageVirtualPrependedHeightDelta(addedRenderable)")
    scroll_delta_idx = body.index("Math.max(0, newScrollH - prevScrollH)")
    unpin_idx = body.rindex("_scrollPinned = false")
    assert restore_idx < virtual_idx < scroll_delta_idx < unpin_idx


def test_loading_older_messages_marks_scroll_programmatic_while_anchoring():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    set_idx = body.index("_programmaticScroll = true;")
    restore_idx = body.index("container.scrollTop = oldTop + addedHeight")
    clear_idx = body.index("requestAnimationFrame(()=>{ _programmaticScroll = false; })")
    assert set_idx < restore_idx < clear_idx


def test_loading_older_messages_captures_anchor_before_replacing_messages():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    anchor_idx = body.index("const viewportAnchor = (container && typeof _captureMessageViewportAnchor === 'function')")
    replace_idx = body.index("S.messages = nextMessages")
    render_idx = body.index("renderMessages({ preserveScroll: true });")
    restore_idx = body.index("_restoreMessageViewportAnchor(viewportAnchor, olderMsgs.length)")

    assert anchor_idx < replace_idx < render_idx < restore_idx
