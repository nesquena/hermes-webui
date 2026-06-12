"""Static-analysis tests for the conversation outline panel (issue #2124)."""

from pathlib import Path
import re

ROOT        = Path(__file__).parent.parent
STATIC      = ROOT / "static"
INDEX_HTML  = (STATIC / "index.html").read_text(encoding="utf-8")
I18N_JS     = (STATIC / "i18n.js").read_text(encoding="utf-8")
OUTLINE_JS  = (STATIC / "outline.js").read_text(encoding="utf-8")
STYLE_CSS   = (STATIC / "style.css").read_text(encoding="utf-8")
BOOT_JS     = (STATIC / "boot.js").read_text(encoding="utf-8")
PANELS_JS   = (STATIC / "panels.js").read_text(encoding="utf-8")
CONFIG_PY   = (ROOT / "api" / "config.py").read_text(encoding="utf-8")

# Number of locale blocks in i18n.js: en, it, ja, ru, es, de, zh, zh-Hant, pt, ko, fr, tr
LOCALE_COUNT = 12


def test_outline_panel_html_and_i18n_contract():
    """The panel shell, script tag, and locale keys must be present."""
    assert 'src="static/outline.js?v=__WEBUI_VERSION__"' in INDEX_HTML
    assert "outline.js?v=__WEBUI_VERSION__" in INDEX_HTML
    assert re.search(r'<script[^>]+outline\.js\?v=__WEBUI_VERSION__[^>]+defer', INDEX_HTML)
    assert re.search(r'id="outlineToggleBtn"[^>]*hidden', INDEX_HTML)
    assert re.search(r'id="outlinePanelWrapper"[^>]*hidden', INDEX_HTML)
    assert 'id="outlinePanel"' in INDEX_HTML

    for key in (
        "outline_title:",
        "outline_empty:",
        "outline_loading:",
        "settings_label_conversation_outline:",
        "settings_desc_conversation_outline:",
    ):
        assert I18N_JS.count(key) >= LOCALE_COUNT


def test_outline_setting_round_trip_contract():
    """The outline preference must default off and use the normal settings path."""
    assert '"show_conversation_outline": False' in CONFIG_PY
    assert '"show_conversation_outline",' in CONFIG_PY
    assert 'id="settingsShowConversationOutline"' in INDEX_HTML
    assert "payload.show_conversation_outline=showConversationOutlineCb.checked;" in PANELS_JS
    assert "settings.show_conversation_outline===true" in PANELS_JS
    assert "body.show_conversation_outline=showConversationOutline===true;" in PANELS_JS
    assert "window._showConversationOutline=s.show_conversation_outline===true" in BOOT_JS
    assert "window._showConversationOutline=false" in BOOT_JS


def test_outline_navigation_and_long_session_contract():
    """Outline entries must come from session messages and recover off-window targets."""
    for marker in (
        "_outlineSid",
        "window.toggleOutlinePanel",
        "window._outlineJump",
        "S.messages",
        "'msg-user-'",
        "/api/session",
        "_ensureOutlineMessagesLoaded",
        "_ensureAllMessagesLoaded()",
        "_messagesTruncated",
        "_expandOutlineRenderWindow()",
        "_messageRenderWindowSize = Math.max(",
        "renderMessages({ preserveScroll: true })",
        "if (S.busy || S.activeStreamId) return;",
    ):
        assert marker in OUTLINE_JS


def test_outline_opt_in_layout_and_render_state_contract():
    """The enabled outline must stay desktop-only and avoid stale render states."""
    for marker in (
        "window._showConversationOutline === true",
        "toggle.hidden = !enabled",
        "wrapper.hidden = true",
        "window.applyConversationOutlinePreference",
        "matchMedia('(max-width:900px)')",
        "--outline-workspace-offset",
        "panel.offsetWidth",
        "data-workspace-panel",
        "window._outlineRenderHookPending",
        "if (!S.messages) {",
    ):
        assert marker in OUTLINE_JS

    assert "#outlineToggleBtn,#outlinePanelWrapper{display:none!important;}" in STYLE_CSS
    assert "right:calc(var(--outline-workspace-offset, 0px) + 20px)" in STYLE_CSS
    assert "if (!S.messages || !S.messages.length)" not in OUTLINE_JS

    before_hooked = OUTLINE_JS.index("window._outlineRenderHooked = true")
    before_wrapper = OUTLINE_JS.index("window.renderMessages = function")
    missing_render_block = OUTLINE_JS.split("if (typeof _orig !== 'function')", 1)[1]
    missing_render_block = missing_render_block.split("window._outlineRenderHooked = true", 1)[0]
    assert before_hooked < before_wrapper
    assert "window._outlineRenderHooked = true" not in missing_render_block


def test_outline_is_chat_only_and_closes_on_panel_switch():
    """The outline is a chat-view affordance: leaving chat must hide the toggle
    AND close the panel, and returning to chat restores the toggle. (Auto-close
    on panel switch — review follow-up.)"""
    # _outlineAllowed() gates on the active panel, not just the setting + width.
    assert "_currentPanel" in OUTLINE_JS
    assert "panel === 'chat'" in OUTLINE_JS
    # Re-evaluated on main-view changes: switchPanel() toggles `showing-<panel>`
    # on <main>, and the MutationObserver watches that class to re-run the gate.
    assert "main.main" in OUTLINE_JS
    assert "attributeFilter: ['class']" in OUTLINE_JS


def test_outline_wrapper_hidden_attr_actually_hides():
    """The #outlinePanelWrapper id selector sets display:flex, which outranks the
    UA [hidden]{display:none} rule — so the hidden attribute alone would NOT hide
    the panel (the close button / auto-close set wrapper.hidden=true). An explicit
    #outlinePanelWrapper[hidden]{display:none} rule restores the expected behavior."""
    assert "#outlinePanelWrapper[hidden]{display:none;}" in STYLE_CSS
