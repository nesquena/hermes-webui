"""Regression tests for the session action menu position (#1764, #3223).

The shared conversation-actions menu is opened from two places: the three-dot
trigger button, and a right-click on the session row. Right-click must place the
menu at the pointer (clamped to the viewport). When the anchor is a hidden
``.session-actions`` wrapper (zero-size rect), positioning must fall back to the
visible session row instead of landing at ``0,0``.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _sessions_block(start_marker: str, end_marker: str) -> str:
    start = SESSIONS_JS.find(start_marker)
    assert start != -1, f"{start_marker!r} not found"
    end = SESSIONS_JS.find(end_marker, start)
    assert end != -1, f"{end_marker!r} not found after {start_marker!r}"
    return SESSIONS_JS[start:end]


def test_position_session_action_menu_supports_pointer_and_zero_rect_fallback():
    body = _sessions_block(
        "function _positionSessionActionMenu(anchorEl, pointer){",
        "function _buildSessionAction(",
    )
    assert "Number.isFinite(pointer.clientX)" in body
    assert "Number.isFinite(pointer.clientY)" in body
    assert "!rect.width && !rect.height" in body
    assert "anchorEl.closest('.session-item,.session-child-session')" in body


def test_session_row_context_menu_uses_row_anchor_and_pointer_event():
    context_menu = _sessions_block("el.oncontextmenu=(e)=>{", "// Use release events")
    assert SESSIONS_JS.count("el.oncontextmenu=(e)=>{") == 1
    assert "_openSessionActionMenu(s, el, e);" in context_menu
    assert "_openSessionActionMenu(s, actions||el);" not in context_menu


def test_fork_child_context_menu_uses_row_anchor_and_pointer_event():
    fork_block = _sessions_block("row.oncontextmenu=(e)=>{", "childList.appendChild(row);")
    assert "_openSessionActionMenu(child, row, e);" in fork_block
    assert "_openSessionActionMenu(child, actions||row);" not in fork_block


def test_mount_and_open_session_action_menu_track_pointer_state():
    assert "let _sessionActionPointer = null;" in SESSIONS_JS
    assert "function _mountSessionActionMenu(menu, session, anchorEl, pointerEvent){" in SESSIONS_JS
    assert "_sessionActionPointer = pointerEvent || null;" in SESSIONS_JS
    assert "function _openSessionActionMenu(session, anchorEl, pointerEvent){" in SESSIONS_JS
    assert "_positionSessionActionMenu(_sessionActionAnchor, _sessionActionPointer);" in SESSIONS_JS
    assert "_sessionActionPointer = null;" in SESSIONS_JS
