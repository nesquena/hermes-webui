"""
Regression test for #workspace-files: workspace file tree must stay
visible across REPEATED blank-page reloads (not just the first one).

Bug shape: PR #1182's ephemeral guard removed the stored session ID from
localStorage when it detected a 0-message session. That made the FIRST
refresh work (loadSession → loadDir → files render, then guard fires and
clears the key), but the SECOND refresh fell into the "no saved session"
boot path which never calls loadDir() — file tree went blank.

Fix: keep the session ID in localStorage. Every refresh runs the same
path:
  loadSession() → loadDir() populates the workspace
  → ephemeral guard fires → S.session=null in memory only
  → workspace panel stays open with files visible

The session ID persisting in localStorage is harmless — server-side
``all_sessions()`` filters Untitled+0-message sessions so no phantom
sidebar entry appears, and ``newSession()`` overwrites the key when the
user actually creates a real session.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


def test_ephemeral_guard_does_not_remove_session_localstorage_key():
    """The empty-session guard block must NOT call
    localStorage.removeItem('hermes-webui-session') — that's exactly what
    breaks the second refresh."""
    # Find the guard block (message_count===0 check)
    guard_idx = BOOT_JS.find("(S.session.message_count||0) === 0")
    assert guard_idx > 0, "Empty-session guard block not found in boot IIFE"
    # The block runs until 'return;' that exits the IIFE early
    block_end = BOOT_JS.find("return;", guard_idx)
    assert block_end > guard_idx
    block = BOOT_JS[guard_idx:block_end]
    assert "removeItem('hermes-webui-session')" not in block, (
        "The empty-session guard must NOT remove 'hermes-webui-session' from "
        "localStorage. Removing it sends the next refresh into the no-saved-"
        "session boot path which never calls loadDir(), leaving the workspace "
        "file tree permanently blank (#workspace-files)."
    )
    assert 'removeItem("hermes-webui-session")' not in block, (
        "Same as above (double-quoted form)."
    )


def test_ephemeral_guard_still_clears_in_memory_session_state():
    """The guard MUST still clear ``S.session`` and ``S.messages`` in memory
    so the user isn't locked into an empty conversation. Only the
    localStorage cleanup is what was removed."""
    guard_idx = BOOT_JS.find("(S.session.message_count||0) === 0")
    block_end = BOOT_JS.find("return;", guard_idx)
    block = BOOT_JS[guard_idx:block_end]
    # Both in-memory clears must remain
    assert re.search(r"S\.session\s*=\s*null", block), (
        "Empty-session guard must still set S.session=null so the empty "
        "scratch-pad is not surfaced as the active conversation"
    )
    assert re.search(r"S\.messages\s*=\s*\[\]", block), (
        "Empty-session guard must still reset S.messages=[]"
    )


def test_ephemeral_guard_still_restores_panel_pref():
    """PR #1187's panel-pref restore must still happen in the same block —
    that's how the workspace panel stays visible on the empty-session
    refresh path."""
    guard_idx = BOOT_JS.find("(S.session.message_count||0) === 0")
    block_end = BOOT_JS.find("return;", guard_idx)
    block = BOOT_JS[guard_idx:block_end]
    assert "hermes-webui-workspace-panel-pref" in block, (
        "Empty-session guard must still read 'hermes-webui-workspace-panel-pref' "
        "from localStorage to keep the panel open across refreshes (#1187)"
    )
