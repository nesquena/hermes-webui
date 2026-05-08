"""Regression coverage for settings navigation and master-detail refresh state."""
import pathlib
import re


REPO_ROOT = pathlib.Path(__file__).parent.parent
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


class TestSettingsNavigationGuard:
    """Leaving Settings through the rail must still honor save/discard semantics."""

    def test_switch_panel_checks_settings_guard(self):
        assert "if (!opts.bypassSettingsGuard && !_beforePanelSwitch(nextPanel)) return false;" in PANELS_JS, (
            "switchPanel() must consult the settings guard before leaving Settings "
            "so rail/sidebar navigation cannot bypass the unsaved-changes flow"
        )

    def test_dirty_settings_capture_requested_destination(self):
        m = re.search(
            r"function _beforePanelSwitch\(nextPanel\)\s*\{.*?"
            r"_pendingSettingsTargetPanel = nextPanel \|\| 'chat';.*?"
            r"_showSettingsUnsavedBar\(\);.*?"
            r"return false;",
            PANELS_JS,
            re.DOTALL,
        )
        assert m, (
            "_beforePanelSwitch() must remember the requested destination and "
            "block navigation while settings are dirty"
        )

    def test_hiding_settings_resumes_pending_target_with_bypass(self):
        m = re.search(
            r"function _hideSettingsPanel\(\)\s*\{.*?"
            r"const target = _consumeSettingsTargetPanel\('chat'\);.*?"
            r"switchPanel\(target, \{bypassSettingsGuard:true\}\);",
            PANELS_JS,
            re.DOTALL,
        )
        assert m, (
            "_hideSettingsPanel() must resume the pending target after save/discard "
            "instead of always falling back to chat"
        )

    def test_entering_settings_starts_fresh_preview_session(self):
        assert "if (prevPanel !== 'settings' && nextPanel === 'settings') _beginSettingsPanelSession();" in PANELS_JS, (
            "switchPanel() must snapshot the preview baseline when entering Settings "
            "through the main navigation"
        )


class TestMasterDetailRefreshClearsRemovedSelections:
    """Refreshes must not leave dead detail panes visible after a selection disappears."""

    def test_tasks_clear_empty_state_detail(self):
        assert "if (_cronMode !== 'create' && _cronMode !== 'edit') _clearCronDetail();" in PANELS_JS, (
            "loadCrons() must clear the detail pane when the jobs list becomes empty"
        )

    def test_tasks_clear_missing_selected_job(self):
        m = re.search(
            r"if \(_currentCronDetail && _cronMode !== 'create' && _cronMode !== 'edit'\) \{.*?"
            r"if \(refreshed\) _renderCronDetail\(refreshed\);\s*else _clearCronDetail\(\);",
            PANELS_JS,
            re.DOTALL,
        )
        assert m, (
            "loadCrons() must clear the detail pane when the selected job disappears "
            "during refresh"
        )

    def test_workspaces_clear_missing_selected_workspace(self):
        m = re.search(
            r"if \(_currentWorkspaceDetail && _workspaceMode !== 'create' && _workspaceMode !== 'edit'\) \{.*?"
            r"if \(refreshed\) _renderWorkspaceDetail\(refreshed\);\s*else _clearWorkspaceDetail\(\);",
            PANELS_JS,
            re.DOTALL,
        )
        assert m, (
            "renderWorkspacesPanel() must clear the detail pane when the selected "
            "workspace disappears during refresh"
        )

    def test_profiles_clear_empty_state_detail(self):
        assert "if (_profileMode !== 'create') _clearProfileDetail();" in PANELS_JS, (
            "loadProfilesPanel() must clear the detail pane when there are no profiles"
        )

    def test_profiles_clear_missing_selected_profile(self):
        m = re.search(
            r"if \(_currentProfileDetail && _profileMode !== 'create'\) \{.*?"
            r"if \(refreshed\) _renderProfileDetail\(refreshed, data.active\);\s*else _clearProfileDetail\(\);",
            PANELS_JS,
            re.DOTALL,
        )
        assert m, (
            "loadProfilesPanel() must clear the detail pane when the selected "
            "profile disappears during refresh"
        )
