"""Tests for sprint 51 sidebar UX fixes.

Covers:
  - #431: left sidebar can collapse independently on desktop, with persisted
          state and a visible way to reopen it.
"""

import pathlib


REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text()


def test_sidebar_toggle_controls_exist_in_html():
    src = read("static/index.html")
    assert 'id="btnCollapseSidebar"' in src, (
        "Sidebar needs an in-panel collapse button"
    )
    assert 'id="btnSidebarPanelToggle"' in src, (
        "Topbar needs a visible sidebar toggle so desktop users can reopen the sidebar"
    )


def test_sidebar_state_bootstrapped_from_local_storage():
    src = read("static/index.html")
    assert "hermes-webui-sidebar-panel" in src, (
        "Sidebar collapsed state should be restored from localStorage before CSS loads"
    )
    assert "document.documentElement.dataset.sidebarPanel" in src, (
        "Initial sidebar state should be reflected on documentElement dataset"
    )


def test_boot_js_has_sidebar_panel_state_machine():
    src = read("static/boot.js")
    for needle in (
        "let _sidebarPanelMode",
        "function _setSidebarPanelMode(",
        "function syncSidebarPanelUI(",
        "function toggleSidebarPanel(",
        "hermes-webui-sidebar-panel",
    ):
        assert needle in src, f"{needle} must exist in static/boot.js"


def test_desktop_sidebar_collapsed_css_exists():
    src = read("static/style.css")
    assert 'html[data-sidebar-panel="closed"] .sidebar' in src, (
        "Desktop collapsed sidebar CSS selector must exist"
    )
    assert ".layout.sidebar-collapsed .sidebar" in src, (
        "Layout class should also be able to drive collapsed sidebar state"
    )
    assert ".sidebar-toggle-btn" in src, (
        "Topbar sidebar toggle styling must exist"
    )
