# Focused static coverage for the workspace-panel Context tab and the
# "Goal finish" button (feature: context brief in the workspace panel).
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
WORKSPACE = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
UI = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_workspace_context_tab_markup_present():
    assert 'id="workspaceContextTab"' in INDEX
    assert "switchWorkspacePanelTab('context')" in INDEX
    assert 'id="workspaceContextPanel"' in INDEX
    assert 'data-i18n="tab_context"' in INDEX


def test_switch_workspace_panel_tab_handles_context():
    assert "tab === 'context' ? 'context'" in WORKSPACE
    assert "$('workspaceContextTab')" in WORKSPACE
    assert "$('workspaceContextPanel')" in WORKSPACE
    assert "loadWorkspaceContextBrief()" in WORKSPACE


def test_workspace_brief_loader_reuses_shared_loader():
    assert "async function loadWorkspaceContextBrief(force)" in PANELS
    assert "_loadBriefInto($('workspaceContextPanel'), force)" in PANELS
    assert "async function _loadBriefInto(panel, force)" in PANELS
    assert "renderContextBrief(data && data.brief, panel)" in PANELS
    # brief payload retained on the host element for the goal button
    assert "panel._briefData = brief" in PANELS


def test_loader_uses_per_panel_sequence():
    # Two visible panels must not cancel each other's in-flight responses.
    assert "panel._briefReqSeq = (panel._briefReqSeq || 0) + 1" in PANELS
    assert "_contextBriefReqSeq" not in PANELS


def test_context_tab_hides_file_chrome_via_css():
    # Without a data-active-tab="context" rule the file tree stayed visible
    # under the brief (independent review finding).
    assert '.rightpanel[data-active-tab="context"]' in STYLE
    rule = STYLE.split('.rightpanel[data-active-tab="context"]', 1)[1]
    assert "#wsEmptyState" in rule
    assert ".file-tree" in rule


def test_goal_finish_button_and_handler():
    assert PANELS.count("context_goal_finish") >= 2  # both llm variants
    assert "async function _contextBriefGoalFinish(btn)" in PANELS
    assert "'/api/goal'" in PANELS
    assert "it.status === 'pending' || it.status === 'in_progress'" in PANELS
    # non-blocking app dialog, never the browser-native confirm
    assert "showConfirmDialog({" in PANELS
    assert "window.confirm" not in PANELS
    assert "context_goal_finish_none" in PANELS


def test_goal_finish_guards_stale_session():
    # Never post todos from a stale panel into a different conversation.
    body = PANELS.split("async function _contextBriefGoalFinish", 1)[1]
    assert "host.dataset.briefSid !== sid" in body
    assert "context_goal_finish_stale" in body


def test_goal_finish_i18n_all_locales():
    for key in (
        "context_goal_finish:",
        "context_goal_finish_prefix:",
        "context_goal_finish_confirm:",
        "context_goal_finish_none:",
        "context_goal_finish_started:",
        "context_goal_finish_stale:",
    ):
        assert I18N.count(key) >= 3, key  # en + fr + zh-Hant


def test_goal_finish_styles():
    assert ".ctx-brief-actions" in STYLE
    assert ".ctx-brief-btn-goal" in STYLE


def test_workspace_context_refresh_hooks():
    # Non-forced refresh: the briefLoaded guard dedupes (no POST per render).
    assert UI.count("loadWorkspaceContextBrief()") >= 2
    assert "loadWorkspaceContextBrief(true)" not in UI
