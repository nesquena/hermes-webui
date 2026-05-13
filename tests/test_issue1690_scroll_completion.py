from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    start = src.index(f"function {name}")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name} body not found")


def _event_listener_body(src: str, event_name: str) -> str:
    needle = f"source.addEventListener('{event_name}'"
    start = src.index(needle)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"event listener {event_name!r} body not found")


def test_terminal_done_render_preserves_manual_scroll_after_active_stream_is_cleared():
    done_block = _event_listener_body(MESSAGES_JS, "done")

    clear_idx = done_block.index("S.activeStreamId=null")
    render_idx = done_block.index("renderMessages({preserveScroll:true})")

    assert clear_idx < render_idx, (
        "the done handler should clear stream liveness before the final render, "
        "but must pass preserveScroll so renderMessages does not infer bottom-pin "
        "from S.activeStreamId alone"
    )


def test_render_messages_preserve_scroll_option_uses_user_pin_state_not_stream_liveness():
    render_body = _function_body(UI_JS, "renderMessages")
    scroll_helper = _function_body(UI_JS, "_scrollAfterMessageRender")

    assert "function renderMessages(options)" in render_body
    assert "const preserveScroll=!!(options&&options.preserveScroll);" in render_body
    assert "_scrollAfterMessageRender(preserveScroll, scrollSnapshot);" in render_body
    assert "const scrollSnapshot=preserveScroll?_captureMessageScrollSnapshot():null" in render_body
    assert "if(preserveScroll){\n    if(_scrollPinned) scrollIfPinned();\n    else _restoreMessageScrollSnapshot(scrollSnapshot);\n    return;\n  }" in scroll_helper
    assert "if(S.activeStreamId){\n    scrollIfPinned();\n    return;\n  }" in scroll_helper


def test_cached_render_path_uses_same_scroll_policy_as_fresh_render():
    render_body = _function_body(UI_JS, "renderMessages")
    cached_branch = render_body[render_body.index("if(sid&&sid!==_sessionHtmlCacheSid") : render_body.index("const compressionState=")]

    assert "_scrollAfterMessageRender(preserveScroll, scrollSnapshot);" in cached_branch
    assert "if(S.activeStreamId){scrollIfPinned();}else{scrollToBottom();}" not in cached_branch


def test_session_switch_and_idle_session_load_keep_default_bottom_pin_behavior():
    load_session = _function_body(SESSIONS_JS, "loadSession")
    idle_branch = load_session[load_session.index("}else{\n      S.busy=false;") : load_session.index("// Sync context usage indicator")]

    assert "syncTopbar();renderMessages();" in idle_branch
    assert "preserveScroll:true" not in idle_branch
