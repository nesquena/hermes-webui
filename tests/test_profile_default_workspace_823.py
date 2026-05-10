"""Tests for #823 — _profileDefaultWorkspace persists after newSession() (#804 follow-up)

Root cause: newSession() consumed S._profileDefaultWorkspace for the one-shot
profile-switch semantic (setting it to null after the first new session). This
caused the blank-page default workspace display to regress after any session
was created and then deleted.

Fix: introduce S._profileSwitchWorkspace as the dedicated one-shot flag for
profile-switch semantics; S._profileDefaultWorkspace is now persistent.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


class TestProfileDefaultWorkspacePersistence:
    """_profileDefaultWorkspace must NOT be nulled by newSession()."""

    def test_new_session_does_not_null_profile_default_workspace(self):
        src = read("static/sessions.js")
        m = re.search(r"async function newSession\(.*?\n\}", src, re.DOTALL)
        assert m, "newSession not found"
        fn = m.group(0)
        # The old consume pattern must be gone
        assert (
            "_profileDefaultWorkspace=null" not in fn
            and "_profileDefaultWorkspace = null" not in fn
        ), (
            "newSession must NOT null S._profileDefaultWorkspace — it is the persistent "
            "boot/settings default used for blank-page display after session delete (#823)"
        )

    def test_new_session_uses_dedicated_switch_workspace_flag(self):
        src = read("static/sessions.js")
        m = re.search(r"async function newSession\(.*?\n\}", src, re.DOTALL)
        assert m
        fn = m.group(0)
        assert "_profileSwitchWorkspace" in fn, (
            "newSession must read S._profileSwitchWorkspace for the one-shot "
            "profile-switch inherit, not S._profileDefaultWorkspace"
        )
        # It should null the switch flag, not the default
        assert (
            "_profileSwitchWorkspace=null" in fn
            or "_profileSwitchWorkspace = null" in fn
        ), "newSession must null S._profileSwitchWorkspace after consuming it"

    def test_new_session_still_inherits_default_workspace(self):
        """newSession must still pass a workspace to /api/session/new —
        now via the _profileSwitchWorkspace → current session → _profileDefaultWorkspace chain."""
        src = read("static/sessions.js")
        m = re.search(r"async function newSession\(.*?\n\}", src, re.DOTALL)
        assert m
        fn = m.group(0)
        # inheritWs must be computed and passed to /api/session/new
        assert "inheritWs" in fn or "inherit" in fn.lower(), (
            "newSession must compute an inheritWs from switch/current/default workspace"
        )
        assert "_profileDefaultWorkspace" in fn, (
            "newSession must fall through to S._profileDefaultWorkspace as last resort"
        )


class TestProfileSwitchWorkspaceSetter:
    """panels.js must set _profileSwitchWorkspace on profile switch."""

    def test_panels_sets_profile_switch_workspace(self):
        src = read("static/panels.js")
        # Find the profile-switch workspace block
        assert "S._profileSwitchWorkspace" in src, (
            "panels.js must set S._profileSwitchWorkspace during profile switch "
            "so newSession() can apply it to the first new session"
        )

    def test_panels_still_sets_profile_default_workspace(self):
        src = read("static/panels.js")
        assert "S._profileDefaultWorkspace = data.default_workspace" in src, (
            "panels.js must still set S._profileDefaultWorkspace (persistent default) "
            "alongside S._profileSwitchWorkspace"
        )

    def test_both_set_together_in_same_block(self):
        src = read("static/panels.js")
        default_pos = src.find("S._profileDefaultWorkspace = data.default_workspace")
        switch_pos = src.find("S._profileSwitchWorkspace = data.default_workspace")
        assert default_pos != -1, "S._profileDefaultWorkspace setter not found"
        assert switch_pos != -1, "S._profileSwitchWorkspace setter not found"
        # Both must be set within 200 chars of each other (same block)
        assert abs(default_pos - switch_pos) < 300, (
            "_profileDefaultWorkspace and _profileSwitchWorkspace must be set "
            "together in the same profile-switch workspace block"
        )

    def test_switch_to_workspace_clears_profile_switch_workspace(self):
        """Opus Q4: when the user manually changes workspace, the pending one-shot
        switch flag should be cleared so a subsequent newSession() inherits the
        user's explicit choice rather than the stale profile-switch default."""
        src = read("static/panels.js")
        m = re.search(r"async function switchToWorkspace\(.*?\n\}", src, re.DOTALL)
        assert m, "switchToWorkspace not found"
        fn = m.group(0)
        assert (
            "_profileSwitchWorkspace=null" in fn
            or "_profileSwitchWorkspace = null" in fn
        ), (
            "switchToWorkspace must null S._profileSwitchWorkspace after a manual switch "
            "so the next newSession() inherits the user's explicit workspace choice"
        )


class TestBlankPageAfterSessionDelete:
    """After all sessions are deleted, blank page must still show default workspace."""

    def test_sync_workspace_displays_reads_profile_default(self):
        """syncWorkspaceDisplays relies on S._profileDefaultWorkspace which must
        still be set after a session is created and deleted."""
        src = read("static/panels.js")
        m = re.search(r"function syncWorkspaceDisplays\(\)\{.*?\n\}", src, re.DOTALL)
        assert m, "syncWorkspaceDisplays not found"
        fn = m.group(0)
        assert "_profileDefaultWorkspace" in fn, (
            "syncWorkspaceDisplays must read S._profileDefaultWorkspace as fallback"
        )

    def test_prompt_new_file_reads_profile_default(self):
        """promptNewFile on blank page reads _profileDefaultWorkspace which must
        be non-null even after a newSession() + deleteSession() cycle."""
        src = read("static/ui.js")
        m = re.search(r"async function promptNewFile\(\)\{.*?\n\}", src, re.DOTALL)
        assert m, "promptNewFile not found"
        fn = m.group(0)
        assert "_profileDefaultWorkspace" in fn, (
            "promptNewFile must read S._profileDefaultWorkspace (must persist after newSession)"
        )
