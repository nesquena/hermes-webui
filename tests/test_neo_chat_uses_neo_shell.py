"""Onda 10 — Conversas keeps the Neo dashboard shell instead of falling back
to the legacy hermes layout.

Without these guarantees the user reported that clicking 'Conversas'
swapped out the Neo chrome (rail menu, topbar, hero) for the upstream
hermes panelChat shell, defeating the point of the fork. The wiring
mirrors the skills pattern (panelSkills mounted into mainSkills as a
260px sidecar) but applied to panelChat / mainChat.
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text()
DASHBOARD_JS = (REPO_ROOT / "static" / "dashboard.js").read_text()
STYLE = (REPO_ROOT / "static" / "style.css").read_text()


def test_chat_in_neo_shell_panels():
    m = re.search(r"const NEO_SHELL_PANELS = new Set\(\[([^\]]+)\]\)", PANELS_JS)
    assert m, "NEO_SHELL_PANELS declaration not found"
    members = [s.strip().strip("'\"") for s in m.group(1).split(",")]
    assert "chat" in members, (
        "chat must be in NEO_SHELL_PANELS so switchPanel('chat') keeps "
        "body.dashboard-shell-mode active"
    )


def test_main_view_class_includes_chat():
    assert "chat: 'showing-chat'" in PANELS_JS, (
        "MAIN_VIEW_CLASS_BY_PANEL must map chat -> showing-chat so the main "
        "container picks up the Conversas layout"
    )


def test_switch_panel_calls_mount_dashboard_chat_list():
    assert "mountDashboardChatList" in PANELS_JS, (
        "switchPanel must call mountDashboardChatList when entering chat"
    )
    assert "restoreDashboardChatList" in PANELS_JS, (
        "switchPanel must call restoreDashboardChatList when leaving chat"
    )


def test_dashboard_js_defines_mount_and_restore():
    assert "function mountDashboardChatList(" in DASHBOARD_JS
    assert "function restoreDashboardChatList(" in DASHBOARD_JS
    # Anchor + insert pattern mirrors mountDashboardSkills exactly.
    snip_idx = DASHBOARD_JS.find("function mountDashboardChatList(")
    snip = DASHBOARD_JS[snip_idx:snip_idx + 800]
    assert "panelChat" in snip
    assert "mainChat" in snip
    assert "createComment(" in snip, "anchor pattern must use a comment node"


def test_app_titlebar_for_chat_uses_tab_conversations():
    """Header label on the Conversas panel must say 'Conversas', not 'Chat',
    so it lines up with the menu entry the user just clicked."""
    m = re.search(r"const APP_TITLEBAR_KEYS = \{([^}]+)\}", PANELS_JS, re.S)
    assert m
    block = m.group(1)
    assert "chat: 'tab_conversations'" in block, (
        "chat panel must surface tab_conversations in the app titlebar"
    )


def test_showing_chat_css_renders_panel_chat_full_width():
    """Conversas panel is browse-only: #panelChat takes the full width of
    #mainChat. Clicking a session promotes to dashboard panel via
    switchPanel('dashboard'), where the transcript renders properly."""
    rule_idx = STYLE.find(
        "body.dashboard-shell-mode main.main.showing-chat #mainChat>#panelChat"
    )
    assert rule_idx != -1, "showing-chat panelChat rule missing"
    snip = STYLE[rule_idx:rule_idx + 400]
    assert "flex:1" in snip, "panelChat should fill #mainChat"
    assert "width:260px" not in snip, (
        "Sidecar layout removed — panelChat now occupies the full main area"
    )


def test_showing_chat_css_hides_messages_and_composer():
    """The transcript and composer never render inside the Conversas panel
    itself; they are only revealed in the dashboard chat embed."""
    rule_idx = STYLE.find(
        "body.dashboard-shell-mode main.main.showing-chat #mainChat>.messages"
    )
    assert rule_idx != -1, (
        "showing-chat must hide #messages so the broken side-by-side layout "
        "the user reported never reappears"
    )
    snip = STYLE[rule_idx:rule_idx + 400]
    assert "display:none" in snip


def test_session_click_promotes_to_dashboard():
    """Clicking a session in the Conversas list must call
    switchPanel('dashboard') so the transcript opens in the dashboard chat
    embed (where the full layout renders), not in the cramped sidecar."""
    sessions_js = (REPO_ROOT / "static" / "sessions.js").read_text()
    snippet_idx = sessions_js.find("await loadSession(s.session_id);")
    assert snippet_idx != -1
    block = sessions_js[snippet_idx:snippet_idx + 800]
    assert "_currentPanel==='chat'" in block, (
        "session click must check current panel before redirecting"
    )
    assert "switchPanel('dashboard')" in block, (
        "session click must call switchPanel('dashboard')"
    )
