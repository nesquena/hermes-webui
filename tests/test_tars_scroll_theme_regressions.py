from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")


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
    expand_idx = body.index("_messageRenderWindowSize=_currentMessageRenderWindowSize()")
    render_idx = body.index("renderMessages({ preserveScroll: true });")

    assert prepend_idx < expand_idx < render_idx, (
        "scroll-to-top paging must expand the DOM render window before renderMessages(); "
        "otherwise fetched older messages stay hidden and only the hidden counter changes"
    )
    assert "Math.max(addedRenderable, MESSAGE_RENDER_WINDOW_DEFAULT)" in body


def test_scroll_to_bottom_settles_across_late_markdown_layout_growth():
    settle = _function_body(UI_JS, "function _settleMessageScrollToBottom")
    scroll = _function_body(UI_JS, "function scrollToBottom")
    pinned = _function_body(UI_JS, "function scrollIfPinned")

    assert "requestAnimationFrame" in settle
    assert "setTimeout" in settle
    assert "const passes=[0,16,80,180]" in settle
    assert "_settleMessageScrollToBottom(true)" in scroll
    assert "_settleMessageScrollToBottom(false)" in pinned
    assert "!_scrollPinned" in settle, "pinned auto-scroll passes must still respect manual scroll-up"
    assert "const token=++_bottomSettleToken" in settle
    assert "token!==_bottomSettleToken" in settle


def test_user_scroll_and_older_history_load_cancel_delayed_bottom_settling():
    listener_block = UI_JS[UI_JS.index("el.addEventListener('scroll'") : UI_JS.index("})();", UI_JS.index("el.addEventListener('scroll'"))]
    record = _function_body(UI_JS, "function _recordNonMessageScrollIntent")
    load_older = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    assert "function _cancelBottomSettle" in UI_JS
    assert "_cancelBottomSettle();" in listener_block
    assert "e.deltaY<0" in record
    assert "_scrollPinned=false" in record

    restore_idx = load_older.index("container.scrollTop = prevScrollTop + addedHeight")
    cancel_idx = load_older.rindex("_cancelBottomSettle", 0, restore_idx)
    unpin_idx = load_older.rindex("_scrollPinned = false")
    assert "const prevScrollTop = container ? container.scrollTop : 0" in load_older
    assert cancel_idx < restore_idx < unpin_idx


def test_older_history_scroll_prefetches_and_preserves_viewport_without_smooth_jump():
    load_older = _function_body(SESSIONS_JS, "async function _loadOlderMessages")
    listener_block = UI_JS[UI_JS.index("el.addEventListener('scroll'") : UI_JS.index("})();", UI_JS.index("el.addEventListener('scroll'"))]

    assert "renderMessages({ preserveScroll: true });" in load_older
    assert "const prevScrollTop = container ? container.scrollTop : 0" in load_older
    assert "const oldTop = container.scrollTop" not in load_older
    assert "const addedHeight = Math.max(0, newScrollH - prevScrollH)" in load_older
    assert "container.scrollTop = prevScrollTop + addedHeight" in load_older
    assert "scrollTo({ top:" not in load_older
    assert "behavior: reduceMotion ? 'auto' : 'smooth'" not in load_older
    assert "const olderPrefetchPx=Math.max(600,el.clientHeight*1.5)" in listener_block
    assert "_isSessionEndlessScrollEnabled()&&el.scrollTop<olderPrefetchPx" in listener_block
    assert "else if(typeof _loadOlderMessages==='function') _loadOlderMessages();" in UI_JS


def test_windowed_render_load_earlier_preserves_viewport_without_bottom_repin():
    show_earlier = _function_body(UI_JS, "function _showEarlierRenderedMessages")

    prev_scroll_idx = show_earlier.index("const prevScrollTop=container?container.scrollTop:0")
    cancel_idx = show_earlier.index("_cancelBottomSettle")
    unpin_idx = show_earlier.index("_scrollPinned=false")
    programmatic_idx = show_earlier.index("_programmaticScroll=true")
    render_idx = show_earlier.index("renderMessages({ preserveScroll:true })")
    restore_idx = show_earlier.index("container.scrollTop=prevScrollTop+(newScrollH-prevScrollH)")
    clear_idx = show_earlier.index("requestAnimationFrame(()=>{ _programmaticScroll=false; })")

    assert prev_scroll_idx < cancel_idx < unpin_idx < programmatic_idx < render_idx < restore_idx < clear_idx
    assert "renderMessages();" not in show_earlier


def test_jump_to_session_start_button_loads_all_history_and_scrolls_top():
    jump = _function_body(UI_JS, "async function jumpToSessionStart")
    update = _function_body(UI_JS, "function _updateSessionStartJumpButton")
    load_older = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    assert 'id="jumpToSessionStartBtn"' in INDEX_HTML
    assert 'id="scrollToBottomBtn"' in INDEX_HTML
    assert "session-jump-btn session-jump-btn--start" in INDEX_HTML
    assert "session-jump-btn session-jump-btn--end" in INDEX_HTML
    assert "session-jump-btn--start has-tooltip" not in INDEX_HTML
    assert "session-jump-btn--end has-tooltip" not in INDEX_HTML
    assert "data-i18n=\"session_jump_start\"" in INDEX_HTML
    assert "data-i18n=\"session_jump_end\"" in INDEX_HTML
    assert "data-i18n-aria-label=\"session_jump_start_label\"" in INDEX_HTML
    assert "data-i18n-aria-label=\"session_jump_end_label\"" in INDEX_HTML
    assert ".session-jump-btn" in STYLE_CSS
    assert ".session-jump-btn--start{top:16px" in STYLE_CSS
    assert ".session-jump-btn--end{bottom:16px" in STYLE_CSS
    assert "_sessionStartJumpAvailable" in UI_JS
    assert "function _isSessionJumpButtonsEnabled" in UI_JS
    assert "!_isSessionJumpButtonsEnabled()" in update
    assert "!_isSessionJumpButtonsEnabled()||_scrollPinned" in UI_JS
    assert "_markSessionStartJumpAvailable" in load_older
    assert "_oldestIdx = 0" in _function_body(SESSIONS_JS, "async function _ensureAllMessagesLoaded")
    assert "_ensureAllMessagesLoaded" in jump
    assert "_messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(),_messageRenderableMessageCount())" in jump
    assert "renderMessages({ preserveScroll:true })" in jump
    assert "container.scrollTop=0" in jump
    assert "_sessionStartJumpAvailable=true" in jump
    assert "later scrolls down and wants to return" in jump
    assert "btn.style.display=(hasSession&&canRevealStart&&awayFromStart)?'flex':'none'" in update


def test_session_jump_buttons_are_i18n_localized_in_text_tooltip_and_aria():
    for key in [
        "session_jump_start",
        "session_jump_start_label",
        "session_jump_end",
        "session_jump_end_label",
        "settings_label_session_jump_buttons",
        "settings_desc_session_jump_buttons",
        "settings_label_session_endless_scroll",
        "settings_desc_session_endless_scroll",
    ]:
        assert I18N_JS.count(f"{key}:") >= 8, f"missing locale entries for {key}"
    assert "document.querySelectorAll('[data-i18n-aria-label]')" in I18N_JS
    assert "el.setAttribute('aria-label', val)" in I18N_JS
    assert "data-i18n-title=\"session_jump_start_label\"" in INDEX_HTML
    assert "data-i18n-title=\"session_jump_end_label\"" in INDEX_HTML


def test_session_history_navigation_options_are_appearance_opt_in():
    assert 'id="settingsSessionJumpButtons"' in INDEX_HTML
    assert 'id="settingsSessionEndlessScroll"' in INDEX_HTML
    assert 'data-i18n="settings_label_session_jump_buttons"' in INDEX_HTML
    assert 'data-i18n="settings_label_session_endless_scroll"' in INDEX_HTML
    assert '"session_jump_buttons": False' in CONFIG_PY
    assert '"session_endless_scroll": False' in CONFIG_PY
    assert '"session_jump_buttons"' in CONFIG_PY and '"session_endless_scroll"' in CONFIG_PY
    assert "session_jump_buttons: !!($('settingsSessionJumpButtons')||{}).checked" in PANELS_JS
    assert "session_endless_scroll: !!($('settingsSessionEndlessScroll')||{}).checked" in PANELS_JS
    assert "window._sessionJumpButtonsEnabled=!!s.session_jump_buttons" in BOOT_JS
    assert "window._sessionEndlessScrollEnabled=!!s.session_endless_scroll" in BOOT_JS
    assert "window._sessionJumpButtonsEnabled=false" in BOOT_JS
    assert "window._sessionEndlessScrollEnabled=false" in BOOT_JS


def test_session_navigation_helpers_do_not_collide_with_window_preference_values():
    assert "function _sessionJumpButtonsEnabled" not in UI_JS
    assert "function _sessionEndlessScrollEnabled" not in UI_JS
    assert "function _isSessionJumpButtonsEnabled" in UI_JS
    assert "function _isSessionEndlessScrollEnabled" in UI_JS
    assert "window._sessionJumpButtonsEnabled" in BOOT_JS
    assert "window._sessionEndlessScrollEnabled" in BOOT_JS
