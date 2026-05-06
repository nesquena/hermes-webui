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


def test_showing_chat_css_makes_panel_chat_a_sidecar():
    """CSS ensures #panelChat renders as a 260px left sidecar inside #mainChat
    when the showing-chat class is on .main."""
    assert (
        "body.dashboard-shell-mode main.main.showing-chat #mainChat>#panelChat"
        in STYLE
    ), "showing-chat sidecar rule missing"
    # Width pinned to match the skills sidecar so visual rhythm is identical.
    rule_idx = STYLE.find(
        "body.dashboard-shell-mode main.main.showing-chat #mainChat>#panelChat"
    )
    snip = STYLE[rule_idx:rule_idx + 400]
    assert "width:260px" in snip
    assert "border-right" in snip


def test_showing_chat_css_makes_main_chat_a_flex_row():
    """The container must flex horizontally so the sidecar and the
    transcript share the same row."""
    assert (
        "body.dashboard-shell-mode main.main.showing-chat>#mainChat"
        in STYLE
    )
    rule_idx = STYLE.find(
        "body.dashboard-shell-mode main.main.showing-chat>#mainChat"
    )
    snip = STYLE[rule_idx:rule_idx + 200]
    assert "flex-direction:row" in snip
