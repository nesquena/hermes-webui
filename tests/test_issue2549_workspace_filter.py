"""Regression tests for issue #2549: per-active-workspace session filter (Slice A).

Salvaged from PR #2951 (author @Sanjays2402) and adapted to master's refactored
sidebar render chain (the single-pass _partitionSidebarSessionRows partition).
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_show_all_workspaces_setting_defaults_true():
    src = read("api/config.py")
    assert '"show_all_workspaces": True' in src, (
        "show_all_workspaces must default to True for backward compat"
    )
    # Belongs to the bool validator set
    assert '"show_all_workspaces"' in src.split("_SETTINGS_BOOL_KEYS")[1].split("}")[0]


def test_sessions_js_applies_workspace_filter_in_partition():
    src = read("static/sessions.js")
    # The per-workspace exemption check must live inside the partition loop, gated on
    # the show-all flag, and must exempt pinned rows + rows without a workspace.
    assert "_showAllWorkspacesActive" in src
    assert "_activeWorkspacePathForSidebar" in src
    # The drop condition: not show-all, an active workspace path, not pinned,
    # has a workspace that differs from the active one.
    assert "!_showAllWs&&_activeWsPath&&!s.pinned&&s.workspace&&s.workspace!==_activeWsPath" in src


def test_default_true_means_no_filtering_when_setting_absent():
    """When window._showAllWorkspaces is undefined / true, no rows are dropped."""
    src = read("static/sessions.js")
    # Guard expression: _showAllWs defaults to true (back-compat)
    assert "window._showAllWorkspaces!==false" in src


def test_pinned_and_no_workspace_rows_are_exempt():
    src = read("static/sessions.js")
    # Pinned rows bypass the filter, same as rows missing s.workspace.
    drop = "if(!_showAllWs&&_activeWsPath&&!s.pinned&&s.workspace&&s.workspace!==_activeWsPath) continue;"
    assert drop in src


def test_sessions_js_has_workspace_empty_state():
    src = read("static/sessions.js")
    assert "No sessions in this workspace yet." in src
    # Empty state only fires when the workspace filter is the active rejector.
    assert "!_showAllWorkspacesActive()&&_activeWorkspacePathForSidebar()&&sessions.length===0" in src


def test_index_html_has_settings_toggle():
    src = read("static/index.html")
    assert 'id="settingsShowAllWorkspaces"' in src
    assert 'data-i18n="settings_label_all_workspaces"' in src


def test_panels_js_loads_and_saves_setting():
    src = read("static/panels.js")
    assert "settingsShowAllWorkspaces" in src
    assert "show_all_workspaces" in src
    assert "window._showAllWorkspaces=" in src


def test_boot_js_initializes_setting():
    src = read("static/boot.js")
    # Hydrates from server settings and clears on logout
    assert "window._showAllWorkspaces=s.show_all_workspaces!==false" in src
    assert "window._showAllWorkspaces=true" in src


def test_locale_strings_present_in_english():
    src = read("static/i18n.js")
    assert "settings_label_all_workspaces: 'Show sessions from all workspaces'" in src
    assert "settings_desc_all_workspaces:" in src


def test_locale_parity_for_new_keys():
    """Strict locale parity: every locale block that has the sibling external_sessions
    key must also carry both new workspace keys (a key in English-only breaks ~6
    locale tests)."""
    src = read("static/i18n.js")
    label_count = src.count("settings_label_all_workspaces:")
    desc_count = src.count("settings_desc_all_workspaces:")
    ext_label_count = src.count("settings_label_external_sessions:")
    assert label_count == ext_label_count, (
        f"label parity mismatch: {label_count} vs {ext_label_count} locales"
    )
    assert desc_count == ext_label_count, (
        f"desc parity mismatch: {desc_count} vs {ext_label_count} locales"
    )
