"""Tests for #804 — blank new-chat page loses default workspace binding

Fixes:
- syncWorkspaceDisplays() uses S._profileDefaultWorkspace as fallback when no session
- composerChip.disabled uses hasWorkspace (not hasSession) so chip is enabled on blank page
- boot.js reads default_workspace from /api/settings and sets S._profileDefaultWorkspace
- promptNewFile/promptNewFolder auto-create a session bound to default workspace
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


class TestSyncWorkspaceDisplaysFallback:
    """syncWorkspaceDisplays must show default workspace when no session."""

    def test_uses_profile_default_workspace_as_fallback(self):
        src = read("static/panels.js")
        m = re.search(r"function syncWorkspaceDisplays\(\)\{.*?\n\}", src, re.DOTALL)
        assert m, "syncWorkspaceDisplays not found"
        fn = m.group(0)
        assert "_profileDefaultWorkspace" in fn, (
            "syncWorkspaceDisplays must read S._profileDefaultWorkspace as fallback "
            "when no active session is present"
        )

    def test_has_workspace_not_has_session_for_chip_disable(self):
        src = read("static/panels.js")
        m = re.search(r"function syncWorkspaceDisplays\(\)\{.*?\n\}", src, re.DOTALL)
        assert m
        fn = m.group(0)
        # composerChip.disabled must use hasWorkspace, not hasSession
        assert (
            "composerChip.disabled=!hasWorkspace" in fn
            or "composerChip.disabled = !hasWorkspace" in fn
        ), (
            "composerChip.disabled must use !hasWorkspace (not !hasSession) so the chip "
            "is enabled on the blank new-chat page when a default workspace is configured"
        )
        assert "composerChip.disabled=!hasSession" not in fn, (
            "composerChip.disabled must not use !hasSession — this was the regression"
        )


class TestBootJsProfileDefaultWorkspace:
    """boot.js must read default_workspace from /api/settings into S._profileDefaultWorkspace."""

    def test_boot_reads_default_workspace_from_settings(self):
        src = read("static/boot.js")
        assert "_profileDefaultWorkspace" in src, (
            "boot.js must set S._profileDefaultWorkspace from the /api/settings "
            "default_workspace field so it is available before any session is created"
        )

    def test_boot_sets_profile_default_workspace_in_settings_block(self):
        """The settings block (lines ~758-800 in boot.js) must set
        S._profileDefaultWorkspace from the /api/settings response."""
        src = read("static/boot.js")
        # Find the settings fetch and the _profileDefaultWorkspace ASSIGNMENT
        # (the if(s.default_workspace) line, not usages elsewhere in the file)
        settings_idx = src.find("await api('/api/settings')")
        assert settings_idx != -1, "await api('/api/settings') not found in boot.js"
        # Find the assignment specifically — it uses 's.default_workspace'
        ws_assign_idx = src.find("S._profileDefaultWorkspace=s.default_workspace")
        assert ws_assign_idx != -1, (
            "S._profileDefaultWorkspace assignment not found in boot.js"
        )
        # The assignment must be in the same settings-fetch block (within a few hundred chars)
        assert abs(ws_assign_idx - settings_idx) < 1000, (
            "S._profileDefaultWorkspace must be set in the same settings-fetch block"
        )


class TestPromptNewFileNoSession:
    """promptNewFile/promptNewFolder must auto-create a session on blank page."""

    def test_prompt_new_file_auto_creates_session(self):
        src = read("static/ui.js")
        m = re.search(r"async function promptNewFile\(\)\{.*?\n\}", src, re.DOTALL)
        assert m, "promptNewFile not found"
        fn = m.group(0)
        # Must have auto-create path (not just early return when no session)
        assert "_profileDefaultWorkspace" in fn, (
            "promptNewFile must read S._profileDefaultWorkspace to auto-create "
            "a session when called on the blank new-chat page"
        )
        assert "session/new" in fn, (
            "promptNewFile must call /api/session/new to create a session "
            "bound to the default workspace when S.session is null"
        )

    def test_prompt_new_folder_auto_creates_session(self):
        src = read("static/ui.js")
        m = re.search(r"async function promptNewFolder\(\)\{.*?\n\}", src, re.DOTALL)
        assert m, "promptNewFolder not found"
        fn = m.group(0)
        assert "_profileDefaultWorkspace" in fn, (
            "promptNewFolder must read S._profileDefaultWorkspace for auto-create path"
        )
        assert "session/new" in fn, (
            "promptNewFolder must call /api/session/new to create session on blank page"
        )

    def test_prompt_new_file_still_returns_early_without_default(self):
        """If no default workspace, the function should return early (not crash)."""
        src = read("static/ui.js")
        m = re.search(r"async function promptNewFile\(\)\{.*?\n\}", src, re.DOTALL)
        assert m
        fn = m.group(0)
        # Must have a guard for empty workspace
        assert "if(!ws) return" in fn or "if(!ws)return" in fn, (
            "promptNewFile must return early if no default workspace is configured"
        )


class TestWorkspaceSwitcherBlankPage:
    """Opus review Q6: workspace switcher dropdown must not silently fail on blank page."""

    def test_switch_to_workspace_auto_creates_session(self):
        src = read("static/panels.js")
        m = re.search(r"async function switchToWorkspace\(.*?\n\}", src, re.DOTALL)
        assert m, "switchToWorkspace not found"
        fn = m.group(0)
        assert "_profileDefaultWorkspace" in fn or "session/new" in fn, (
            "switchToWorkspace must auto-create session on blank page (Opus Q6 fix)"
        )
        assert "session/new" in fn, (
            "switchToWorkspace must call /api/session/new when S.session is null"
        )

    def test_prompt_workspace_path_auto_creates_session(self):
        src = read("static/panels.js")
        m = re.search(
            r"async function promptWorkspacePath\(\)\{.*?\n\}", src, re.DOTALL
        )
        assert m, "promptWorkspacePath not found"
        fn = m.group(0)
        assert "session/new" in fn, (
            "promptWorkspacePath must call /api/session/new when S.session is null"
        )

    def test_sync_workspace_displays_dropdown_close_uses_has_workspace(self):
        src = read("static/panels.js")
        m = re.search(r"function syncWorkspaceDisplays\(\)\{.*?\n\}", src, re.DOTALL)
        assert m, "syncWorkspaceDisplays not found"
        fn = m.group(0)
        # Line 555: dropdown force-close must use hasWorkspace, not hasSession
        assert (
            "!hasWorkspace && composerDropdown" in fn
            or "!hasWorkspace&&composerDropdown" in fn
        ), (
            "syncWorkspaceDisplays must use !hasWorkspace (not !hasSession) to decide "
            "whether to force-close the dropdown (Opus Q6 fix)"
        )
        assert "!hasSession && composerDropdown" not in fn, (
            "Regression guard: !hasSession for dropdown close must be removed"
        )
