"""
Regression tests for the workspace panel persisting across page reload on
empty-session and no-session boot paths.

Two boot paths previously dropped the workspace panel even when the user had
explicitly opened it before reloading:

  1. Ephemeral-session guard added in #1182: when the restored session has
     0 messages, boot clears localStorage and shows the empty state. This
     path was calling ``syncWorkspacePanelState()`` without first restoring
     ``_workspacePanelMode`` from localStorage.
  2. No-saved-session path: a fresh page load with no localStorage session
     also went straight to ``syncWorkspacePanelState()`` without restoring
     the panel preference.

Both paths force-closed the panel because ``syncWorkspacePanelState()``
unconditionally set ``_workspacePanelMode='closed'`` whenever ``S.session``
was null — even when the user's preference was 'open'.

Fix verified by these tests:

  - ``syncWorkspacePanelState`` checks ``_workspacePanelMode==='preview'``
    BEFORE force-closing, so 'browse' mode is preserved without a session.
  - Both boot paths read the panel pref from localStorage and set
    ``_workspacePanelMode='browse'`` before calling sync.
  - ``canBrowse`` and ``openWorkspacePanel()`` include
    ``S._profileDefaultWorkspace`` so the toggle stays enabled.
"""
import pathlib

REPO = pathlib.Path(__file__).parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


# ── 1. syncWorkspacePanelState preserves browse mode without a session ──────


class TestSyncStateNoSession:
    def test_preview_mode_without_session_force_closes(self):
        """A 'preview' panel needs file content from a session — close it
        when there's no session."""
        idx = BOOT_JS.find("function syncWorkspacePanelState()")
        body = BOOT_JS[idx:idx + 800]
        assert "_workspacePanelMode==='preview'" in body, (
            "syncWorkspacePanelState must check _workspacePanelMode==='preview' "
            "before force-closing on no-session boot"
        )
        assert "_setWorkspacePanelMode('closed')" in body, (
            "syncWorkspacePanelState still must close 'preview' mode without a session"
        )

    def test_browse_mode_calls_sync_ui_instead_of_force_close(self):
        """For 'browse' mode without a session, syncWorkspacePanelUI() should
        run so the panel renders its 'no workspace' or default-workspace state
        rather than being force-closed."""
        idx = BOOT_JS.find("function syncWorkspacePanelState()")
        body = BOOT_JS[idx:idx + 800]
        # The else branch (browse / closed mode without session) calls UI sync
        assert "syncWorkspacePanelUI()" in body, (
            "syncWorkspacePanelState must call syncWorkspacePanelUI() in the "
            "no-session, non-preview branch so 'browse' mode is preserved"
        )


# ── 2. Both boot paths restore panelPref before sync ────────────────────────


class TestBootPathsRestorePanelPref:
    PREF_PATTERN = "hermes-webui-workspace-panel-pref"

    def test_ephemeral_path_restores_panel_pref(self):
        """The empty-session guard (#1182) must read panelPref before
        calling syncWorkspacePanelState()."""
        # Find the ephemeral guard — it's marked by message_count===0 check
        eph_idx = BOOT_JS.find("(S.session.message_count||0) === 0")
        assert eph_idx > 0, "Empty-session guard not found in boot IIFE"
        # The next syncWorkspacePanelState() call after this point is in the ephemeral path
        sync_idx = BOOT_JS.find("syncWorkspacePanelState()", eph_idx)
        assert sync_idx > 0, "syncWorkspacePanelState call not found in ephemeral path"
        # panelPref must be read between the guard and the sync call
        block = BOOT_JS[eph_idx:sync_idx]
        assert self.PREF_PATTERN in block, (
            "Ephemeral-session boot path must read 'hermes-webui-workspace-panel-pref' "
            "from localStorage before calling syncWorkspacePanelState()"
        )
        assert "_workspacePanelMode='browse'" in block or "_workspacePanelMode = 'browse'" in block, (
            "Ephemeral-session path must set _workspacePanelMode='browse' "
            "when the pref is 'open'"
        )

    def test_no_session_path_restores_panel_pref(self):
        """The fresh-load (no localStorage session) path must read panelPref
        before calling syncWorkspacePanelState()."""
        # Find the comment marker that precedes the no-session path
        marker = "no saved session"
        m_idx = BOOT_JS.find(marker)
        assert m_idx > 0, "no-saved-session path not found"
        # syncWorkspacePanelState should appear shortly after
        sync_idx = BOOT_JS.find("syncWorkspacePanelState()", m_idx)
        assert sync_idx > 0, "syncWorkspacePanelState() not found after no-saved-session marker"
        block = BOOT_JS[m_idx:sync_idx]
        assert self.PREF_PATTERN in block, (
            "No-saved-session boot path must read 'hermes-webui-workspace-panel-pref' "
            "before calling syncWorkspacePanelState()"
        )
        assert "_workspacePanelMode='browse'" in block or "_workspacePanelMode = 'browse'" in block, (
            "No-saved-session path must set _workspacePanelMode='browse' "
            "when the pref is 'open'"
        )


# ── 3. Toggle button stays enabled when profile default workspace exists ────


class TestToggleStaysEnabledWithProfileWorkspace:
    def test_can_browse_includes_profile_default_workspace(self):
        """The toggle button's enabled state (canBrowse) must be true when
        S._profileDefaultWorkspace is set, even with no active session."""
        idx = BOOT_JS.find("const canBrowse=")
        assert idx > 0, "canBrowse declaration not found in syncWorkspacePanelUI"
        line = BOOT_JS[idx:idx + 200].split("\n", 1)[0]
        assert "_profileDefaultWorkspace" in line, (
            "canBrowse must include S._profileDefaultWorkspace so the toggle "
            "button stays enabled when a profile workspace is configured"
        )

    def test_open_workspace_panel_allows_browse_with_profile_workspace(self):
        """openWorkspacePanel('browse') must not return early when
        S._profileDefaultWorkspace is set, otherwise clicking the toggle
        won't open the panel even though canBrowse said it should."""
        idx = BOOT_JS.find("function openWorkspacePanel(")
        body = BOOT_JS[idx:idx + 600]
        # The early-return guard should include the profile-workspace check
        assert "_profileDefaultWorkspace" in body, (
            "openWorkspacePanel must include S._profileDefaultWorkspace in its "
            "early-return guard so users can open the panel via the toggle "
            "button when a profile workspace is configured"
        )
