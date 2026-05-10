from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
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


def test_session_jump_buttons_are_opt_in_and_keep_existing_bottom_button():
    assert '"session_jump_buttons": False' in CONFIG_PY
    assert '"session_jump_buttons"' in CONFIG_PY
    assert "window._sessionJumpButtonsEnabled=!!s.session_jump_buttons" in BOOT_JS
    assert "window._sessionJumpButtonsEnabled=false" in BOOT_JS
    assert (
        "session_jump_buttons: !!($('settingsSessionJumpButtons')||{}).checked"
        in PANELS_JS
    )

    scroll_listener = UI_JS[
        UI_JS.index("el.addEventListener('scroll'") : UI_JS.index(
            "})();", UI_JS.index("el.addEventListener('scroll'")
        )
    ]
    assert "if(btn) btn.style.display=_scrollPinned?'none':'flex'" in scroll_listener
    assert "!_isSessionJumpButtonsEnabled()||_scrollPinned" not in UI_JS


def test_jump_to_session_start_button_loads_full_history_and_scrolls_top():
    jump = _function_body(UI_JS, "async function jumpToSessionStart")
    update = _function_body(UI_JS, "function _updateSessionStartJumpButton")

    assert 'id="jumpToSessionStartBtn"' in INDEX_HTML
    assert 'class="session-jump-btn session-jump-btn--start"' in INDEX_HTML
    assert 'data-i18n="session_jump_start"' in INDEX_HTML
    assert 'data-i18n="session_jump_end"' in INDEX_HTML
    assert 'data-i18n-aria-label="session_jump_start_label"' in INDEX_HTML
    assert 'data-i18n-aria-label="session_jump_end_label"' in INDEX_HTML

    assert "_ensureAllMessagesLoaded" in jump
    assert (
        "_messageRenderWindowSize=Math.max(_currentMessageRenderWindowSize(),_messageRenderableMessageCount())"
        in jump
    )
    assert "renderMessages({ preserveScroll:true })" in jump
    assert "container.scrollTop=0" in jump
    assert (
        "btn.style.display=(hasSession&&canRevealStart&&awayFromStart)?'flex':'none'"
        in update
    )


def test_session_jump_buttons_match_pill_layout_without_regressing_default_arrow():
    assert ".session-jump-btn" in STYLE_CSS
    assert ".session-jump-btn--start{top:16px" in STYLE_CSS
    assert ".session-jump-btn__text{display:none" in STYLE_CSS
    assert ".messages.session-nav-enabled .scroll-to-bottom-btn" in STYLE_CSS
    assert (
        ".messages.session-nav-enabled .session-jump-btn__text{display:inline"
        in STYLE_CSS
    )
    assert (
        "classList.toggle('session-nav-enabled',_isSessionJumpButtonsEnabled())"
        in UI_JS
    )


def test_session_jump_buttons_are_i18n_localized_in_text_tooltip_and_aria():
    english_literals = {
        "session_jump_start": "Start",
        "session_jump_start_label": "Jump to beginning of session",
        "session_jump_end": "End",
        "session_jump_end_label": "Jump to end of session",
        "settings_label_session_jump_buttons": "Show session jump buttons",
        "settings_desc_session_jump_buttons": "Show floating Start and End buttons while reading long session histories.",
    }
    for key in english_literals:
        assert I18N_JS.count(f"{key}:") >= 8, f"missing locale entries for {key}"
    for key, value in english_literals.items():
        assert I18N_JS.count(f"{key}: '{value}'") == 1, (
            f"non-English locale still uses English literal for {key}"
        )
    assert "document.querySelectorAll('[data-i18n-aria-label]')" in I18N_JS
    assert "el.setAttribute('aria-label', val)" in I18N_JS
