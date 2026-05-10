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

    prepend_idx = body.index("S.messages = [...olderMsgs, ...S.messages]")
    expand_idx = body.index(
        "_messageRenderWindowSize=_currentMessageRenderWindowSize()"
    )
    render_idx = body.index("renderMessages({ preserveScroll: true });")

    assert prepend_idx < expand_idx < render_idx, (
        "scroll-to-top paging must expand the DOM render window before renderMessages(); "
        "otherwise fetched older messages stay hidden and only the hidden counter changes"
    )
    assert "Math.max(addedRenderable, MESSAGE_RENDER_WINDOW_DEFAULT)" in body


def test_loading_older_messages_preserves_viewport_without_bottom_snap():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    assert "renderMessages({ preserveScroll: true });" in body
    assert "const oldTop = container.scrollTop" in body
    assert "const addedHeight = Math.max(0, newScrollH - prevScrollH)" in body
    assert "container.scrollTop = oldTop + addedHeight" in body
    assert "container.scrollTop = newScrollH - prevScrollH" not in body

    restore_idx = body.index("container.scrollTop = oldTop + addedHeight")
    unpin_idx = body.rindex("_scrollPinned = false")
    assert restore_idx < unpin_idx


def test_loading_older_messages_marks_scroll_programmatic_while_anchoring():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    set_idx = body.index("_programmaticScroll = true;")
    restore_idx = body.index("container.scrollTop = oldTop + addedHeight")
    clear_idx = body.index(
        "requestAnimationFrame(()=>{ _programmaticScroll = false; })"
    )
    assert set_idx < restore_idx < clear_idx
