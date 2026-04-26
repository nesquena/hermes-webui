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

    append_to_row_idx = SESSIONS_JS.find("el.appendChild(state);", state_idx)
    assert append_to_row_idx != -1, "state indicator should be appended to the outer row"

    actions_idx = SESSIONS_JS.find("actions.className='session-actions';", append_to_row_idx)
    assert actions_idx != -1, "session actions should still be appended after attention indicator"

    assert ".session-attention-indicator{" in STYLE_CSS, "attention indicator CSS rule missing"
    css_block = STYLE_CSS[
        STYLE_CSS.find(".session-attention-indicator{"):
        STYLE_CSS.find(".session-item:hover .session-attention-indicator")
    ]
    assert "position:absolute;" in css_block, "attention indicator should be positioned in the row action slot"
    assert "right:6px;" in css_block, "attention indicator should align with the actions trigger"
    assert "width:26px;" in css_block, "attention indicator should use the same width as the actions trigger"
    assert "height:26px;" in css_block, "attention indicator should use the same height as the actions trigger"
    assert ".session-attention-indicator.is-streaming::before{" in STYLE_CSS
    inner_spinner_block = STYLE_CSS[
        STYLE_CSS.find(".session-attention-indicator.is-streaming::before{"):
        STYLE_CSS.find(".session-attention-indicator.is-unread::before{")
    ]
    assert "width:10px;" in inner_spinner_block, "spinner glyph should stay 10px inside the 26px action slot"
    assert "height:10px;" in inner_spinner_block, "spinner glyph should stay 10px inside the 26px action slot"

    hover_rule = ".session-item:hover .session-attention-indicator"
    assert hover_rule in STYLE_CSS, "hover rule should hide attention indicator when actions appear"


def test_timestamp_hidden_when_attention_state_is_present():
    assert "+(hasUnread?' unread':'')" in SESSIONS_JS
    assert "const hasAttentionState=isStreaming||hasUnread;" in SESSIONS_JS
    assert "ts.className='session-time'+(hasAttentionState?' is-hidden':'');" in SESSIONS_JS
    assert "ts.textContent=hasAttentionState?'':_formatRelativeSessionTime(tsMs);" in SESSIONS_JS
    assert ".session-time.is-hidden{display:none;}" in STYLE_CSS
    assert ".session-item{padding:8px 86px 8px 8px;" in STYLE_CSS
    assert ".session-item.streaming,.session-item.unread{padding-right:40px;}" in STYLE_CSS
    assert ".session-item{min-height:44px;padding:10px 86px 10px 12px;}" in STYLE_CSS
    session_time_block = STYLE_CSS[
        STYLE_CSS.find(".session-time{"):
        STYLE_CSS.find(".session-time.is-hidden")
    ]
    assert "position:absolute;" in session_time_block
    assert "right:10px;" in session_time_block
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
