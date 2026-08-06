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
    return (REPO / rel).read_text(encoding='utf-8')


class TestSyncWorkspaceDisplaysFallback:
    """syncWorkspaceDisplays must show default workspace when no session."""

    def test_uses_profile_default_workspace_as_fallback(self):
        src = read('static/panels.js')
        m = re.search(r'function syncWorkspaceDisplays\(\)\{.*?\n\}', src, re.DOTALL)
        assert m, "syncWorkspaceDisplays not found"
        fn = m.group(0)
        assert '_profileDefaultWorkspace' in fn, (
            "syncWorkspaceDisplays must read S._profileDefaultWorkspace as fallback "
            "when no active session is present"
        )

    def test_has_workspace_not_has_session_for_chip_disable(self):
        src = read('static/panels.js')
        m = re.search(r'function syncWorkspaceDisplays\(\)\{.*?\n\}', src, re.DOTALL)
        assert m
        fn = m.group(0)
        # composerChip.disabled must use hasWorkspace, not hasSession
        assert 'composerChip.disabled=!hasWorkspace' in fn or \
               'composerChip.disabled = !hasWorkspace' in fn, (
            "composerChip.disabled must use !hasWorkspace (not !hasSession) so the chip "
            "is enabled on the blank new-chat page when a default workspace is configured"
        )
        assert 'composerChip.disabled=!hasSession' not in fn, (
            "composerChip.disabled must not use !hasSession — this was the regression"
        )


class TestBootJsProfileDefaultWorkspace:
    """boot.js must read default_workspace from /api/settings into S._profileDefaultWorkspace."""

    def test_boot_reads_default_workspace_from_settings(self):
        src = read('static/boot.js')
        assert '_profileDefaultWorkspace' in src, (
            "boot.js must set S._profileDefaultWorkspace from the /api/settings "
            "default_workspace field so it is available before any session is created"
        )

    def test_boot_sets_profile_default_workspace_in_settings_block(self):
        """The settings block (lines ~758-800 in boot.js) must set
        S._profileDefaultWorkspace from the /api/settings response."""
        src = read('static/boot.js')
        # Find the settings fetch and the _profileDefaultWorkspace ASSIGNMENT
        # (the if(s.default_workspace) line, not usages elsewhere in the file)
        settings_idx = src.find("await api('/api/settings')")
        assert settings_idx != -1, "await api('/api/settings') not found in boot.js"
        # Find the assignment specifically — it uses 's.default_workspace'
        ws_assign_idx = src.find('S._profileDefaultWorkspace=s.default_workspace')
        assert ws_assign_idx != -1, "S._profileDefaultWorkspace assignment not found in boot.js"
        # The assignment must be in the same settings-fetch block (within a few hundred chars)
        assert abs(ws_assign_idx - settings_idx) < 1000, (
            "S._profileDefaultWorkspace must be set in the same settings-fetch block"
        )

    def test_boot_sets_profile_default_workspace_from_profile_active(self):
        """Profile active bootstrap must override settings with p.default_workspace (#5169)."""
        src = read('static/boot.js')
        active_idx = src.find("api('/api/profile/active'")
        assert active_idx != -1, "/api/profile/active fetch not found in boot.js"
        block = src[active_idx:active_idx + 1200]
        assert 'p.default_workspace' in block, (
            "boot.js must read default_workspace from /api/profile/active response"
        )
        assert '_profileDefaultWorkspace' in block, (
            "boot.js must assign S._profileDefaultWorkspace from profile active bootstrap"
        )
        assert re.search(
            r"if\s*\(\s*p\.default_workspace\s*\)\s*S\._profileDefaultWorkspace\s*=\s*p\.default_workspace",
            block,
        ), (
            "boot.js must set S._profileDefaultWorkspace when p.default_workspace is present"
        )


class TestPromptNewFileNoSession:
    """promptNewFile/promptNewFolder must auto-create a session on blank page."""

    def test_prompt_new_file_auto_creates_session(self):
        src = read('static/ui.js')
        m = re.search(r'async function promptNewFile\([^)]*\)\{.*?\n\}', src, re.DOTALL)
        assert m, "promptNewFile not found"
        fn = m.group(0)
        # Must have auto-create path (not just early return when no session)
        assert '_profileDefaultWorkspace' in fn, (
            "promptNewFile must read S._profileDefaultWorkspace to auto-create "
            "a session when called on the blank new-chat page"
        )
        assert 'session/new' in fn, (
            "promptNewFile must call /api/session/new to create a session "
            "bound to the default workspace when S.session is null"
        )

    def test_prompt_new_folder_auto_creates_session(self):
        src = read('static/ui.js')
        m = re.search(r'async function promptNewFolder\([^)]*\)\{.*?\n\}', src, re.DOTALL)
        assert m, "promptNewFolder not found"
        fn = m.group(0)
        assert '_profileDefaultWorkspace' in fn, (
            "promptNewFolder must read S._profileDefaultWorkspace for auto-create path"
        )
        assert 'session/new' in fn, (
            "promptNewFolder must call /api/session/new to create session on blank page"
        )

    def test_prompt_new_file_still_returns_early_without_default(self):
        """If no default workspace, the function should return early (not crash)."""
        src = read('static/ui.js')
        m = re.search(r'async function promptNewFile\([^)]*\)\{.*?\n\}', src, re.DOTALL)
        assert m
        fn = m.group(0)
        # Must have a guard for empty workspace
        assert "if(!ws) return" in fn or "if(!ws)return" in fn, (
            "promptNewFile must return early if no default workspace is configured"
        )


class TestWorkspaceSwitcherBlankPage:
    """Opus review Q6: workspace switcher dropdown must not silently fail on blank page."""

    def test_switch_to_workspace_auto_creates_session(self):
        src = read('static/panels.js')
        m = re.search(r'async function switchToWorkspace\(.*?\n\}', src, re.DOTALL)
        assert m, "switchToWorkspace not found"
        fn = m.group(0)
        assert '_profileDefaultWorkspace' in fn or 'session/new' in fn, (
            "switchToWorkspace must auto-create session on blank page (Opus Q6 fix)"
        )
        assert 'session/new' in fn, (
            "switchToWorkspace must call /api/session/new when S.session is null"
        )

    def test_switch_to_workspace_keeps_busy_guard_after_blank_page_create(self):
        src = read('static/panels.js')
        start = src.find('async function switchToWorkspace(')
        assert start != -1, "switchToWorkspace not found"
        fn = src[start:src.find('async function toggleWorktreePanel', start)]
        assert "t('workspace_busy_switch')" in fn, (
            "switchToWorkspace must keep the busy-session workspace switch toast"
        )
        blank_create = fn.index("api('/api/session/new'")
        busy_guard = fn.index('if(S.busy)')
        update_call = fn.index("api('/api/session/update'")
        assert blank_create < busy_guard < update_call, (
            "switchToWorkspace must auto-create blank-page sessions before the busy guard, "
            "then return before workspace update while busy"
        )

    def test_prompt_workspace_path_auto_creates_session(self):
        src = read('static/panels.js')
        m = re.search(r'async function promptWorkspacePath\(\)\{.*?\n\}', src, re.DOTALL)
        assert m, "promptWorkspacePath not found"
        fn = m.group(0)
        assert 'session/new' in fn, (
            "promptWorkspacePath must call /api/session/new when S.session is null"
        )

    def test_sync_workspace_displays_dropdown_close_uses_has_workspace(self):
        src = read('static/panels.js')
        m = re.search(r'function syncWorkspaceDisplays\(\)\{.*?\n\}', src, re.DOTALL)
        assert m, "syncWorkspaceDisplays not found"
        fn = m.group(0)
        # Line 555: dropdown force-close must use hasWorkspace, not hasSession
        assert '!hasWorkspace && composerDropdown' in fn or '!hasWorkspace&&composerDropdown' in fn, (
            "syncWorkspaceDisplays must use !hasWorkspace (not !hasSession) to decide "
            "whether to force-close the dropdown (Opus Q6 fix)"
        )
        assert '!hasSession && composerDropdown' not in fn, (
            "Regression guard: !hasSession for dropdown close must be removed"
        )


class TestWorkspaceDropdownBlankPageCurrentWs:
    """Blank-page workspace dropdown must highlight profile default (#5169)."""

    def test_render_workspace_dropdown_uses_profile_default_on_blank_page(self):
        src = read('static/panels.js')
        assert re.search(
            r"renderWorkspaceDropdownInto\([^,]+,\s*[^,]+,\s*"
            r"S\.session\?\.workspace\|\|S\._profileDefaultWorkspace\|\|data\.last\|\|''\)",
            src,
        ), (
            "renderWorkspaceDropdownInto must use session, profile default, or data.last "
            "as current workspace on blank page"
        )


class TestNewChatOnWorkspaceSwitchOptIn:
    """#5473 opt-in: switching to a DIFFERENT workspace starts a new chat instead
    of mutating the current session in place. Default OFF preserves shipped behavior."""

    def test_setting_registered_default_off(self):
        import api.config as c
        assert c._SETTINGS_DEFAULTS.get('new_chat_on_workspace_switch') is False, (
            "new_chat_on_workspace_switch must default to False (shipped in-place behavior)"
        )
        assert 'new_chat_on_workspace_switch' in c._SETTINGS_BOOL_KEYS, (
            "new_chat_on_workspace_switch must be a recognized boolean setting key"
        )

    def test_switch_to_workspace_has_gated_new_chat_branch(self):
        src = read('static/panels.js')
        start = src.find('async function switchToWorkspace(')
        assert start != -1
        fn = src[start:src.find('async function toggleWorktreePanel', start)]
        # The new-chat branch must be gated on the opt-in flag AND a different workspace.
        assert 'window._newChatOnWorkspaceSwitch===true' in fn, (
            "the new-chat branch must be gated on the default-off opt-in flag"
        )
        assert 'path!==S.session.workspace' in fn, (
            "the new-chat branch must only fire when the target workspace DIFFERS "
            "(same-workspace selection stays an in-place refresh/no-op)"
        )
        assert 'S.messages.length>0' in fn, (
            "the new-chat branch must only fire when the current conversation has messages"
        )
        assert 'newSession(false)' in fn, (
            "the new-chat branch must call newSession() to start the fresh chat"
        )
        # The branch must run BEFORE the in-place /api/session/update mutation.
        newchat_idx = fn.index('window._newChatOnWorkspaceSwitch===true')
        update_idx = fn.index("api('/api/session/update'")
        assert newchat_idx < update_idx, (
            "the opt-in new-chat branch must short-circuit before the in-place workspace update"
        )

    def test_boot_and_panels_wire_the_flag(self):
        boot = read('static/boot.js')
        panels = read('static/panels.js')
        assert 'window._newChatOnWorkspaceSwitch=!!s.new_chat_on_workspace_switch' in boot, (
            "boot.js must set window._newChatOnWorkspaceSwitch from the loaded settings"
        )
        assert 'settingsNewChatOnWorkspaceSwitch' in panels, (
            "panels.js must wire the settings checkbox (load + payload)"
        )
        assert 'payload.new_chat_on_workspace_switch' in panels, (
            "panels.js must include new_chat_on_workspace_switch in the autosave payload"
        )

    def test_settings_checkbox_and_i18n_present(self):
        html = read('static/index.html')
        assert 'id="settingsNewChatOnWorkspaceSwitch"' in html, (
            "the Settings checkbox for the opt-in must exist"
        )
        i18n = read('static/i18n.js')
        for key in (
            'settings_label_new_chat_on_workspace_switch',
            'settings_desc_new_chat_on_workspace_switch',
            'workspace_switched_new_chat',
        ):
            assert key in i18n, f"i18n key {key} must be defined"
