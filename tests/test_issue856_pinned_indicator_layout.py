"""Regression checks for #856 pinned-star layout in the session list."""

from pathlib import Path


SESSIONS_JS = (Path(__file__).resolve().parent.parent / "static" / "sessions.js").read_text()
STYLE_CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text()


def test_pinned_indicator_renders_inside_title_row():
    title_row_idx = SESSIONS_JS.find("titleRow.className='session-title-row';")
    assert title_row_idx != -1, "session title row construction not found"

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


def test_state_indicator_always_appended_to_prevent_layout_shift():
    """State span is always added to the DOM (visibility:hidden when inactive) to prevent
    titles shifting left/right when the spinner or unread dot appears/disappears."""
    title_row_idx = SESSIONS_JS.find("titleRow.className='session-title-row';")
    assert title_row_idx != -1, "title row construction not found"

    # state span must be appended unconditionally (no surrounding if-check)
    append_idx = SESSIONS_JS.find("titleRow.appendChild(state);", title_row_idx)
    assert append_idx != -1, "state span must always be appended to titleRow"

    # Verify CSS uses visibility:hidden to reserve the slot
    assert "session-state-indicator{" in STYLE_CSS, "session-state-indicator CSS rule missing"
    base_block_start = STYLE_CSS.find("session-state-indicator{")
    base_block_end = STYLE_CSS.find("}", base_block_start)
    base_block = STYLE_CSS[base_block_start:base_block_end]
    assert "visibility:hidden;" in base_block, (
        "session-state-indicator should default to visibility:hidden so it reserves slot "
        "without being visible — prevents title layout shift on state changes"
    )


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
