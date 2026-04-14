"""
Tests for issue #424: workspace chip shows 'No active workspace' after profile switch
when a conversation is in progress.

These are static source-analysis tests that verify the fix is present in panels.js.
"""
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text()


class TestWorkspaceChipAfterProfileSwitch(unittest.TestCase):
    """Verify that switchToProfile() applies the profile default workspace
    to the new session when a conversation is in progress (fixes #424)."""

    def test_workspace_chip_updated_after_profile_switch(self):
        """After await newSession(false) in the sessionInProgress branch,
        the code must call updateWorkspaceChip() so the chip reflects the
        new profile's default workspace instead of showing 'No active workspace'."""
        # Find the sessionInProgress block
        idx = PANELS_JS.find('if (sessionInProgress)')
        self.assertGreater(idx, -1, "sessionInProgress branch must exist in panels.js")

        # Slice from that point to cover the relevant block
        block = PANELS_JS[idx:idx + 1000]

        # newSession(false) must be called first
        self.assertIn('await newSession(false)', block,
                      "sessionInProgress branch must call await newSession(false)")

        # The fix: updateWorkspaceChip() must be called after newSession(false)
        pos_new_session = block.find('await newSession(false)')
        pos_update_chip = block.find('updateWorkspaceChip()')
        self.assertGreater(pos_update_chip, -1,
                           "updateWorkspaceChip() must be called in the sessionInProgress branch")
        self.assertGreater(pos_update_chip, pos_new_session,
                           "updateWorkspaceChip() must be called AFTER newSession(false)")

    def test_profile_default_workspace_applied_to_new_session(self):
        """After newSession(false) the code must assign S._profileDefaultWorkspace
        to S.session.workspace so the session is correctly tagged."""
        idx = PANELS_JS.find('if (sessionInProgress)')
        self.assertGreater(idx, -1)
        block = PANELS_JS[idx:idx + 1000]

        # The fix block must set S.session.workspace from S._profileDefaultWorkspace
        self.assertIn('S.session.workspace = S._profileDefaultWorkspace', block,
                      "S.session.workspace must be set from S._profileDefaultWorkspace "
                      "in the sessionInProgress branch after newSession(false)")

    def test_api_session_update_called_for_new_session_workspace(self):
        """The fix must call /api/session/update to persist the workspace on the server."""
        idx = PANELS_JS.find('if (sessionInProgress)')
        self.assertGreater(idx, -1)
        block = PANELS_JS[idx:idx + 1000]

        # Must patch the session on the backend too
        self.assertIn('/api/session/update', block,
                      "The sessionInProgress branch must call /api/session/update "
                      "to persist the new workspace after newSession(false)")

    def test_update_workspace_chip_before_render_session_list(self):
        """updateWorkspaceChip() should be called before renderSessionList()
        so the chip is correct when the UI re-renders."""
        idx = PANELS_JS.find('if (sessionInProgress)')
        self.assertGreater(idx, -1)
        block = PANELS_JS[idx:idx + 1000]

        pos_chip = block.find('updateWorkspaceChip()')
        pos_render = block.find('await renderSessionList()')
        self.assertGreater(pos_chip, -1, "updateWorkspaceChip() must exist in block")
        self.assertGreater(pos_render, -1, "renderSessionList() must exist in block")
        self.assertLess(pos_chip, pos_render,
                        "updateWorkspaceChip() must be called before renderSessionList()")


if __name__ == '__main__':
    unittest.main()
