"""Onda 7 — surface a "Nova conversa" entry point inside the dashboard.

The Neo fork hides the legacy panelChat header behind the dashboard rail.
Without a button on the dashboard chat embed, the only way to start a fresh
conversation was to switch panels first — the upstream WebUI button (#btnNewChat)
lives in panelChat. The user reported this as a missing feature on the fork.
"""
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
STYLE = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_dashboard_chat_header_has_new_chat_button():
    assert 'id="btnDashboardNewChat"' in HTML, (
        "dashboard-chat-header must expose a Nova conversa button"
    )


def test_button_uses_new_conversation_i18n_key():
    """Reuse the existing translation key — pt-BR is already populated."""
    snippet_idx = HTML.find('id="btnDashboardNewChat"')
    assert snippet_idx != -1
    snippet = HTML[snippet_idx:snippet_idx + 600]
    assert 'data-i18n-title="new_conversation"' in snippet
    assert 'data-i18n="new_conversation"' in snippet


def test_button_is_inside_dashboard_chat_header():
    header_idx = HTML.find('class="dashboard-chat-header"')
    button_idx = HTML.find('id="btnDashboardNewChat"')
    next_section_idx = HTML.find('class="dashboard-chat-body"', header_idx)
    assert header_idx != -1 and button_idx != -1 and next_section_idx != -1
    assert header_idx < button_idx < next_section_idx, (
        "button must live inside the .dashboard-chat-header block, "
        "not after the chat body"
    )


def test_boot_js_wires_button_to_newSession():
    """The handler must call newSession() and refresh the sidebar list."""
    block_start = BOOT_JS.find("btnDashboardNewChat")
    assert block_start != -1, "boot.js must wire btnDashboardNewChat"
    block = BOOT_JS[block_start:block_start + 500]
    assert "newSession()" in block, "must call newSession()"
    assert "renderSessionList()" in block, "must refresh sidebar after creating"


def test_boot_js_skips_creating_empty_dup_session():
    """Reuse the upstream guard: if current session has no messages, just focus
    the composer instead of stacking another empty session in the sidebar
    (matches the upstream btnNewChat behaviour pinned to #1171)."""
    block_start = BOOT_JS.find("btnDashboardNewChat")
    block = BOOT_JS[block_start:block_start + 500]
    assert "(S.session.message_count||0)===0" in block, (
        "must short-circuit when current session is empty (use the compact "
        "form so test_workspace_panel_persists_on_empty_boot still resolves "
        "the ephemeral-session boot guard, not this handler)"
    )


def test_button_has_dashboard_chat_new_btn_class():
    """Style hook: the button needs its own class so the mobile breakpoint can
    drop the label without affecting other dashboard buttons."""
    snippet = HTML[HTML.find('id="btnDashboardNewChat"'):HTML.find('id="btnDashboardNewChat"') + 600]
    assert "dashboard-chat-new-btn" in snippet


def test_mobile_breakpoint_hides_label():
    """At <=760px the label is hidden so the icon stays inside the 48px header."""
    media = STYLE[STYLE.find("@media(max-width:760px)"):]
    media = media[: media.find("\n}\n") + 2]
    assert ".dashboard-chat-new-btn-label{display:none" in media
