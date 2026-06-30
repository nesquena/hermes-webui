"""Static / structural contract test for the opt-in Git controls UI (#2668 salvage).

This mirrors the repo's existing source-assertion test style (see
``tests/test_todo_panel_cold_load_static.py``): it reads the static front-end
sources and asserts the load-bearing substrings exist, rather than booting a
browser.

Scope note: PR #2668 originally bundled several *non-git* features (editor
gutter, markdown popout, toast restructure, auto-fetch, dir-caching, etc.).
Those were INTENTIONALLY NOT ported in this salvage, so this test deliberately
only asserts the additive Git-controls surface + its default-off wiring. It must
NOT assert the bundled features.
"""

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent
STATIC = REPO_ROOT / "static"


@pytest.fixture(scope="module")
def workspace_js():
    return (STATIC / "workspace.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def boot_js():
    return (STATIC / "boot.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def panels_js():
    return (STATIC / "panels.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ui_js():
    return (STATIC / "ui.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def i18n_js():
    return (STATIC / "i18n.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# workspace.js — the core git block exists with the expected entry points.
# ---------------------------------------------------------------------------

def test_workspace_js_defines_git_render_and_poll_functions(workspace_js):
    for fn in (
        "function _applyWorkspaceGitVisibility(",
        "function _installWorkspaceGitAutoRefresh(",
        "function renderGitBadge(",
        "function renderGitChanges(",
        "function _gitStatusForPath(",
        "async function refreshGitStatus(",
        "function switchWorkspacePanelTab(",
        "function renderGitBranchControl(",
        "async function refreshGitBranches(",
        "async function commitGitChanges(",
        "async function generateGitCommitMessage(",
        "async function runGitRemoteAction(",
    ):
        assert fn in workspace_js, f"missing git function: {fn}"


def test_workspace_js_git_state_seeded_on_S(ui_js):
    # S.git carries the git tab state; seeded in the S literal in ui.js.
    assert "git:{status:null,selectedTab:'files',selectedDiff:null,loading:false}" in ui_js


def test_workspace_js_remote_actions_hit_backend_git_routes(workspace_js):
    # Backend routes already shipped in master via #2625 — the UI calls them.
    for route in (
        "/api/git/status",
        "/api/git/diff",
        "/api/git/stage",
        "/api/git/unstage",
        "/api/git/discard",
        "/api/git/commit-selected",
        "/api/git/commit-message-selected",
        "/api/git/branches",
        "/api/git/stash-checkout",
    ):
        assert route in workspace_js, f"missing backend route call: {route}"
    # Fetch/Pull/Push are funneled through one guarded helper.
    assert "['fetch','pull','push'].includes(action)" in workspace_js
    assert "`/api/git/${action}`" in workspace_js


# ---------------------------------------------------------------------------
# index.html — the markup the JS drives exists.
# ---------------------------------------------------------------------------

def test_index_html_has_git_markup(index_html):
    for marker in (
        'id="btnWorkspaceChangesTab"',          # the Changes tab button
        'id="gitBranchControl"',                # branch switcher control
        'id="gitBranchMenu"',                   # branch dropdown menu
        'id="gitChangesView"',                  # file-change list view
        'id="gitChangesList"',
        'id="gitCommitBox"',                    # commit message + actions
        'id="gitCommitMessage"',
        'id="btnGitCommit"',
        'id="btnGitGenerateCommitMessage"',     # AI commit message
        'id="gitDiffView"',                     # split/diff surface
        'id="settingsWorkspaceGitEnabled"',     # the opt-in toggle
    ):
        assert marker in index_html, f"missing index.html markup: {marker}"


def test_index_html_changes_tab_wires_switch_handler(index_html):
    assert 'onclick="switchWorkspacePanelTab(\'changes\')"' in index_html


# ---------------------------------------------------------------------------
# Settings wiring — boot.js / panels.js push the toggle into the visibility gate.
# ---------------------------------------------------------------------------

def test_boot_js_wires_setting_to_visibility(boot_js):
    assert "window._workspaceGitEnabled=!!s.workspace_git_enabled;" in boot_js
    assert "_applyWorkspaceGitVisibility()" in boot_js


def test_panels_js_persists_and_applies_toggle(panels_js):
    assert "settingsWorkspaceGitEnabled" in panels_js
    assert "payload.workspace_git_enabled=workspaceGitEnabledCb.checked" in panels_js
    assert "body.workspace_git_enabled=!!window._workspaceGitEnabled;" in panels_js
    assert "_applyWorkspaceGitVisibility()" in panels_js


# ---------------------------------------------------------------------------
# ui.js — tree git badges live behind the flag (additive, default-off).
# ---------------------------------------------------------------------------

def test_ui_js_tree_git_badge_block_present_and_gated(ui_js):
    assert "if(window._workspaceGitEnabled){" in ui_js
    assert "_gitStatusForPath" in ui_js
    assert "file-git-status" in ui_js


# ---------------------------------------------------------------------------
# i18n — the additive keys exist (sampled). 60 keys x 14 locales were added.
# ---------------------------------------------------------------------------

def test_i18n_has_git_keys(i18n_js):
    for key in (
        "settings_label_workspace_git_enabled:",
        "settings_desc_workspace_git_enabled:",
        "git_changes:",
        "git_commit:",
        "git_commit_message:",
    ):
        assert key in i18n_js, f"missing i18n key: {key}"
