"""Regression checks for #856 pinned-star layout in the session list."""

from pathlib import Path


SESSIONS_JS = (Path(__file__).resolve().parent.parent / "static" / "sessions.js").read_text()
STYLE_CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text()


def test_pinned_indicator_renders_inside_title_row():
    title_row_idx = SESSIONS_JS.find("titleRow.className='session-title-row';")
    assert title_row_idx != -1, "session title row construction not found"

    assert "body.appendChild(_renderOneSession(s, Boolean(g.isPinned)))" in SESSIONS_JS
    assert "function _renderOneSession(s, isPinnedGroup=false)" in SESSIONS_JS
    assert "if(s.pinned&&!isPinnedGroup){" in SESSIONS_JS

    pin_idx = SESSIONS_JS.find("pinInd.className='session-pin-indicator';", title_row_idx)
    assert pin_idx != -1, "pinned indicator creation not found after title row"

    append_to_title_row_idx = SESSIONS_JS.find("titleRow.appendChild(pinInd);", pin_idx)
    assert append_to_title_row_idx != -1, "pinned indicator should be appended to titleRow"

    append_to_el_idx = SESSIONS_JS.find("el.appendChild(pinInd);", pin_idx)
    assert append_to_el_idx == -1, (
        "pinned indicator should not be appended to the outer session row; "
        "it must align inside the title row with the spinner/unread indicator"
    )


def test_pinned_indicator_uses_fixed_indicator_box():
    assert ".session-pin-indicator{" in STYLE_CSS, "session pin indicator CSS block missing"
    css_block = STYLE_CSS[STYLE_CSS.find(".session-pin-indicator{"):STYLE_CSS.find(".session-pin-indicator svg{")]
    assert "width:10px;" in css_block, "pin indicator should reserve a fixed 10px width"
    assert "height:10px;" in css_block, "pin indicator should reserve a fixed 10px height"
    assert "justify-content:center;" in css_block, "pin indicator should center the star inside its box"


def test_state_indicator_uses_right_actions_slot_to_prevent_title_shift():
    """State span reuses the right-side action slot so the title start position
    does not shift when the spinner or unread dot appears/disappears."""
    title_row_idx = SESSIONS_JS.find("titleRow.className='session-title-row';")
    assert title_row_idx != -1, "title row construction not found"

    title_row_append_idx = SESSIONS_JS.find("titleRow.appendChild(state);", title_row_idx)
    assert title_row_append_idx == -1, (
        "state indicator should not be inserted before the title; it should reuse "
        "the right-side actions slot to avoid title shift"
    )

    state_idx = SESSIONS_JS.find("state.className='session-attention-indicator session-state-indicator'")
    assert state_idx != -1, "right-side attention indicator creation not found"

    # state is now appended to .session-time-wrapper (the right-side slot), not el
    append_to_wrapper_idx = SESSIONS_JS.find("wrapper.appendChild(state);", state_idx)
    assert append_to_wrapper_idx != -1, "state indicator should be appended to .session-time-wrapper"

    assert ".session-attention-indicator{" in STYLE_CSS, "attention indicator CSS rule missing"
    # The indicator itself has no position:absolute; it is inside .session-time-wrapper
    # which is the right-side absolute-positioned slot. Check positioning on the wrapper.
    assert ".session-time-wrapper{" in STYLE_CSS, ".session-time-wrapper should exist"
    wrapper_block = STYLE_CSS[STYLE_CSS.find(".session-time-wrapper{"):]
    wrapper_block = wrapper_block[:wrapper_block.find("}")]
    assert "position:absolute;" in wrapper_block, ".session-time-wrapper should be position:absolute"
    assert "right:6px;" in wrapper_block, ".session-time-wrapper should align with right:6px"


def test_timestamp_hidden_when_attention_state_is_present():
    assert "+(hasUnread?' unread':'')" in SESSIONS_JS
    assert "const hasAttentionState=isStreaming;" in SESSIONS_JS
    assert "ts.className='session-time'+(hasAttentionState?' is-hidden':'');" in SESSIONS_JS
    assert "ts.textContent=hasAttentionState?'':_formatRelativeSessionTime(tsMs);" in SESSIONS_JS
    assert ".session-time.is-hidden{display:none;}" in STYLE_CSS
    # Timestamp lives in .session-time-wrapper which is position:absolute on the
    # right side; .session-time itself has no margin-left:auto requirement.
    assert ".session-item{padding:8px 8px;" in STYLE_CSS
    # PR #1110: :hover removed from the COMBINED padding-right rule (touch layout-shift fix).
    # Instead, hover padding is restored via @media (hover:hover) which only applies to
    # devices with a real hover capability (mouse). Touch/iPad devices satisfy hover:none
    # and skip that block, preventing the layout-reflow mid-tap bug.
    assert ".session-item.streaming,.session-item.unread,.session-item:focus-within,.session-item.menu-open{padding-right:40px;}" in STYLE_CSS
    # Desktop hover padding restored via media query (mouse devices only)
    assert "@media (hover:hover)" in STYLE_CSS
    assert ".session-item:hover{padding-right:40px;}" in STYLE_CSS
    assert ".session-item{min-height:44px;padding:10px 40px 10px 12px;}" in STYLE_CSS
    # .session-time-wrapper is the absolute-positioned right-side slot; .session-time
    # itself does not need margin-left:auto since the wrapper handles the positioning.
    session_time_block = STYLE_CSS[
        STYLE_CSS.find(".session-time{"):
        STYLE_CSS.find(".session-time.is-hidden")
    ]
    assert "position:absolute;display:none" not in session_time_block, (
        "Timestamp must not be position:absolute"
    )
    assert ".session-item:hover .session-time" in STYLE_CSS
    assert ".session-item.streaming:not(:hover):not(:focus-within):not(.menu-open) .session-actions" in STYLE_CSS
    assert ".session-item.unread:not(:hover):not(:focus-within):not(.menu-open) .session-actions" in STYLE_CSS


def test_sidebar_uses_local_inflight_state_for_immediate_spinner():
    messages_js = (Path(__file__).resolve().parent.parent / "static" / "messages.js").read_text()

    assert "const isLocalStreaming=Boolean(" in SESSIONS_JS
    assert "(isActive&&S.busy)" in SESSIONS_JS
    assert "INFLIGHT[s.session_id]" in SESSIONS_JS
    assert "const isStreaming=Boolean(s.is_streaming||isLocalStreaming);" in SESSIONS_JS
    assert "if(typeof renderSessionListFromCache==='function') renderSessionListFromCache();" in messages_js


def test_date_group_caret_expanded_down_collapsed_right():
    assert "caret.textContent='\\u25BE';" in SESSIONS_JS
    assert ".session-date-caret{" in STYLE_CSS
    caret_block = STYLE_CSS[
        STYLE_CSS.find(".session-date-caret{"):
        STYLE_CSS.find(".session-date-caret.collapsed")
    ]
    assert "transform:rotate(0deg);" in caret_block
    assert ".session-date-caret.collapsed{transform:rotate(-90deg);}" in STYLE_CSS


def test_apperror_path_calls_render_session_list():
    """apperror handler must call renderSessionList() to clear the streaming indicator
    immediately rather than waiting for the 5s streaming poll interval."""
    messages_js = (Path(__file__).resolve().parent.parent / "static" / "messages.js").read_text()
    apperror_idx = messages_js.find("source.addEventListener('apperror'")
    assert apperror_idx != -1, "apperror handler not found in messages.js"
    warning_idx = messages_js.find("source.addEventListener('warning'", apperror_idx)
    assert warning_idx != -1, "warning handler not found after apperror handler"
    apperror_block = messages_js[apperror_idx:warning_idx]
    assert "renderSessionList()" in apperror_block, (
        "apperror handler must call renderSessionList() so the streaming indicator "
        "clears immediately on server errors, not after a 5s poll delay"
    )


def test_pointerup_ignores_non_primary_mouse_buttons():
    """Right-click and middle-click must not trigger session navigation.
    onpointerup fires for all mouse buttons; we filter to button===0
    (primary). pointerType==='mouse' scopes the check to mouse only —
    touch/stylus always report button===0 so they're unaffected."""
    assert "e.pointerType==='mouse' && e.button!==0" in SESSIONS_JS, (
        "pointerup handler must filter out non-primary mouse buttons "
        "(right-click / middle-click must not navigate)"
    )
