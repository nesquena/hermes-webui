"""Regression tests for #1694 root boot policy around saved running sessions.

The active pane is only a projection. A root `/` tab restored from
``localStorage['hermes-webui-session']`` should not automatically project into a
saved session that is still running, because that makes the new tab inherit the
running pane's busy/stream state even though the user did not explicitly open
that session.

Explicit `/session/<sid>` reload remains different: it should still restore and
reattach to the requested running session.
"""

from pathlib import Path


REPO = Path(__file__).parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


def _boot_saved_session_block() -> str:
    marker = "const urlSession="
    start = BOOT_JS.find(marker)
    assert start > 0, "boot saved-session restore block not found"
    end_marker = "// no saved session"
    end = BOOT_JS.find(end_marker, start)
    assert end > start, "no-saved-session marker not found after restore block"
    return BOOT_JS[start:end]


def test_root_boot_distinguishes_url_session_from_localstorage_saved_session():
    """Root restore and explicit URL restore must be separate decisions."""
    block = _boot_saved_session_block()
    assert "const savedLocal=" in block, (
        "boot must keep the localStorage session separate from urlSession so "
        "root `/` policy can differ from explicit `/session/<sid>` reload"
    )
    compact = block.replace(" ", "")
    assert "constsaved=urlSession||savedLocal" in compact, (
        "boot should still prefer explicit URL sessions over saved localStorage sessions"
    )


def test_root_saved_running_session_is_checked_before_load_session_projection():
    """A saved running localStorage session should be detected before loadSession()."""
    block = _boot_saved_session_block()
    guard = "!urlSession&&savedLocal"
    guard_pos = block.replace(" ", "").find(guard)
    load_pos = block.find("await loadSession(saved, {preserveActiveInput:true})")
    assert guard_pos >= 0, (
        "root `/` boot must have a !urlSession && savedLocal guard for saved "
        "running sessions before projecting them into the active pane"
    )
    assert load_pos >= 0, "loadSession(saved) call not found"
    assert guard_pos < load_pos, (
        "saved running-session root guard must run before loadSession(saved), "
        "otherwise loadSession already projects the session into the active pane"
    )
    assert "_savedSessionSidebarOnlyState" in block, (
        "boot should delegate the saved-running metadata check to a named helper"
    )


def test_saved_running_session_helper_uses_metadata_only_and_runtime_markers():
    """The helper should inspect metadata without loading messages or attaching SSE."""
    helper_idx = BOOT_JS.find("async function _savedSessionSidebarOnlyState")
    assert helper_idx > 0, "saved-running root policy helper not found"
    helper = BOOT_JS[helper_idx:helper_idx + 1200]
    assert "/api/session?session_id=" in helper, (
        "helper should inspect session metadata via /api/session before deciding"
    )
    assert "messages=0" in helper, "helper must avoid loading full messages"
    assert "resolve_model=0" in helper, "helper must avoid unnecessary model resolution"
    assert "active_stream_id" in helper, "helper must treat active_stream_id as running"
    assert "pending_user_message" in helper, "helper must treat pending_user_message as running"
    assert "session.archived" in helper, (
        "helper must skip auto-opening archived localStorage sessions so root "
        "boot lands on the empty state instead of reopening archived chats"
    )
    assert "sidebarOnly:archived||running" in helper.replace(" ", ""), (
        "helper must report the sidebar-only decision without conflating archived "
        "sessions with running sessions"
    )
    assert "loadSession(" not in helper, (
        "helper must not call loadSession(), because that would already project "
        "the saved session into the active pane"
    )


def test_root_saved_running_sidebar_only_path_renders_empty_state_and_sidebar():
    """Skipping projection should still leave the app usable and sidebar visible."""
    block = _boot_saved_session_block()
    helper_pos = block.find("_savedSessionSidebarOnlyState")
    render_pos = block.find("await renderSessionList()", helper_pos)
    empty_pos = block.find("$('emptyState').style.display=''", helper_pos)
    return_pos = block.find("return;", helper_pos)
    assert helper_pos >= 0, "saved-running helper call not found"
    assert empty_pos > helper_pos, "sidebar-only path must show the empty state"
    assert render_pos > helper_pos, "sidebar-only path must render the session list"
    assert return_pos > render_pos, "sidebar-only path should return before loadSession(saved)"


def test_root_archived_saved_session_clears_stale_localstorage_pointer():
    """Archived root-restore skips projection and clears stale saved-session state."""
    block = _boot_saved_session_block()
    helper_pos = block.find("_savedSessionSidebarOnlyState")
    clear_guard = "if(savedSidebarOnlyState.archived)"
    guard_pos = block.find(clear_guard, helper_pos)
    clear_pos = block.find("localStorage.removeItem('hermes-webui-session')", guard_pos)
    render_pos = block.find("await renderSessionList()", helper_pos)
    load_pos = block.find("await loadSession(saved, {preserveActiveInput:true})")
    assert guard_pos > helper_pos, "archived sidebar-only path must be distinguished"
    assert clear_pos > guard_pos, "archived saved session must clear stale localStorage pointer"
    assert clear_pos < render_pos, "stale pointer should be cleared before the sidebar-only return"
    assert render_pos < load_pos, "archived saved session must return before loadSession(saved)"
