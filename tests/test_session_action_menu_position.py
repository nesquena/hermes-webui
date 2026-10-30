"""Behavioural tests for the session action menu position (#1764, #3223).

The shared conversation-actions menu opens from the three-dot trigger button and
from a right-click on the session row. Right-click must place the menu at the
pointer — routed through the project picker's recorded context point, the only
pointer evidence in ``static/sessions.js`` — and keep it glued to that spot in
the row while the sidebar scrolls. Button opens stay anchored to their anchor,
and the zero-sized-anchor fallback belongs to ``_projectPickerAnchorRect``
(covered in ``test_project_picker_viewport_position.py``).
"""

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _block(start_marker: str, end_marker: str) -> str:
    start = SESSIONS_JS.find(start_marker)
    assert start != -1, f"{start_marker!r} not found"
    end = SESSIONS_JS.find(end_marker, start)
    assert end != -1, f"{end_marker!r} not found after {start_marker!r}"
    return SESSIONS_JS[start:end]


def _position_source() -> str:
    return _block("function _positionSessionActionMenu(", "\nfunction ")


def _record_point_source() -> str:
    return _block("function _recordProjectPickerContextPoint(", "\n// An open picker follows a resize")


def _open_menu_block() -> str:
    return _block("function _openSessionActionMenu(", "document.addEventListener('click'")


def _run_node(script: str):
    assert NODE is not None, "node is required for the behavioural harness"
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# A normal desktop row: the anchor (.session-actions) has a real rect, the row
# a second one. Window and menu stubs match test_project_picker_viewport_position.
_HARNESS = r"""
const window = {innerWidth: 768, innerHeight: 620,
  visualViewport: {offsetTop: 0, offsetLeft: 0, width: 768, height: 620}};
let _sessionActionMenu = {style: {}, scrollWidth: 180, offsetHeight: 140};
const rowRect = {top: 380, bottom: 428, left: 16, right: 374, width: 358, height: 48};
const row = {isConnected: true, getBoundingClientRect: () => rowRect};
const anchorEl = {
  isConnected: true,
  getBoundingClientRect: () => ({top: 300, bottom: 330, left: 340, right: 374, width: 34, height: 30}),
  closest: selector => selector === '.session-child-session-fork,.session-item' ? row : null,
};
"""


def test_context_menu_places_at_the_pointer_on_a_normal_row():
    """Right-click on a row with a visible ⋮ anchor must still open at the
    pointer — the point-anchored rect, left edge at the click."""
    script = (
        _HARNESS
        + _position_source()
        + r"""
_sessionActionMenu._pointAnchoredFromPointer = true;
anchorEl._projectPickerContextPoint = {clientX: 250, clientY: 300, rowOffsetX: 234, rowOffsetY: 20};
_positionSessionActionMenu(anchorEl);
console.log(JSON.stringify({left: _sessionActionMenu.style.left, top: _sessionActionMenu.style.top}));
"""
    )
    data = _run_node(script)
    # rowRect.left + rowOffsetX = 250, rowRect.top + rowOffsetY = 400 → under
    # the pointer, 7px below its y.
    assert data == {"left": "250px", "top": "407px"}, (
        f"a context-menu open must sit at the pointer; got {data}"
    )


def test_pointer_menu_follows_the_row_when_the_sidebar_scrolls():
    """Scroll repositioning reuses the stored row offsets: the menu keeps its
    spot in the row instead of jumping to the button or closing (#5347)."""
    script = (
        _HARNESS
        + _position_source()
        + r"""
_sessionActionMenu._pointAnchoredFromPointer = true;
anchorEl._projectPickerContextPoint = {clientX: 250, clientY: 300, rowOffsetX: 234, rowOffsetY: 20};
_positionSessionActionMenu(anchorEl);
// The sidebar scrolls: the row moves up 160px, the scroll handler calls the
// same reposition entry point again.
rowRect.top = 220; rowRect.bottom = 268;
_positionSessionActionMenu(anchorEl);
console.log(JSON.stringify({left: _sessionActionMenu.style.left, top: _sessionActionMenu.style.top}));
"""
    )
    data = _run_node(script)
    assert data == {"left": "250px", "top": "247px"}, (
        f"the pointer menu must follow its row on scroll; got {data}"
    )


def test_pointer_menu_clamps_to_the_viewport_edge():
    script = (
        _HARNESS
        + _position_source()
        + r"""
_sessionActionMenu._pointAnchoredFromPointer = true;
anchorEl._projectPickerContextPoint = {clientX: 740, clientY: 600};
_positionSessionActionMenu(anchorEl);
console.log(JSON.stringify({left: _sessionActionMenu.style.left, top: _sessionActionMenu.style.top}));
"""
    )
    data = _run_node(script)
    # Left clamps to innerWidth-menuW-margin (540); the 140px menu flips above
    # the click point (600-140-6) instead of running off the bottom.
    assert data == {"left": "540px", "top": "454px"}, (
        f"the pointer menu must clamp inside the viewport; got {data}"
    )


def test_button_opens_stay_anchored_to_their_anchor():
    """Without a fresh context point the menu stays right-aligned to its
    anchor — the ⋮ button behaviour, unchanged by right-click positioning."""
    script = (
        _HARNESS
        + _position_source()
        + r"""
_positionSessionActionMenu(anchorEl);
console.log(JSON.stringify({left: _sessionActionMenu.style.left, top: _sessionActionMenu.style.top}));
"""
    )
    data = _run_node(script)
    assert data == {"left": "154px", "top": "336px"}, (
        f"button opens must stay anchored to the row; got {data}"
    )


def test_context_point_is_recorded_as_fresh_and_consumed_on_open():
    """_recordProjectPickerContextPoint marks the point as the invocation spot;
    _openSessionActionMenu consumes the flag so a later button open is not
    dragged back to an old click."""
    script = (
        _record_point_source()
        + r"""
const anchor = {closest: sel => sel === '.session-child-session-fork,.session-item'
  ? {isConnected: true, getBoundingClientRect: () => ({left: 16, top: 380})} : null};
_recordProjectPickerContextPoint(anchor, {clientX: 120, clientY: 80});
console.log(JSON.stringify(anchor._projectPickerContextPoint));
"""
    )
    data = _run_node(script)
    assert data == {
        "clientX": 120,
        "clientY": 80,
        "rowOffsetX": 104,
        "rowOffsetY": -300,
        "freshMenuAnchor": True,
    }, f"the recorded point must carry the offsets and its freshness; got {data}"

    open_block = _open_menu_block()
    assert "const openAtContextPoint=!!(contextPoint&&contextPoint.freshMenuAnchor);" in open_block
    assert "if(contextPoint) delete contextPoint.freshMenuAnchor;" in open_block
    assert "menu._pointAnchoredFromPointer=openAtContextPoint;" in open_block
    # A fresh right-click reopens at the new point instead of toggling closed.
    assert "_sessionActionAnchor===anchorEl && !openAtContextPoint){" in open_block


def test_right_click_keeps_masters_point_recording_and_open_form():
    """The row handlers stay on master's contract: record the context point,
    then open with the two-argument opener. No second pointer channel."""
    child_handler = _block("row.oncontextmenu=(e)=>{", "childList.appendChild(row);")
    assert "_recordProjectPickerContextPoint(actions,e);" in child_handler
    assert "_openSessionActionMenu(child, actions||row);" in child_handler
    row_handler = _block("el.oncontextmenu=(e)=>{", "// Use release events")
    assert "_recordProjectPickerContextPoint(actions,e);" in row_handler
    assert "_openSessionActionMenu(s, actions||el);" in row_handler

    assert "_sessionActionPointer" not in SESSIONS_JS
    assert "function _openSessionActionMenu(session, anchorEl){" in SESSIONS_JS
    assert "function _mountSessionActionMenu(menu, session, anchorEl){" in SESSIONS_JS


def _record_for(event_js: str):
    script = (
        _record_point_source()
        + r"""
const anchor = {closest: sel => sel === '.session-child-session-fork,.session-item'
  ? {isConnected: true, getBoundingClientRect: () => ({left: 16, top: 380})} : null};
const onTrigger = {closest: sel => sel === '.session-actions-trigger' ? {} : null};
const onTitle = {closest: () => null};
_recordProjectPickerContextPoint(anchor, """
        + event_js
        + r""");
console.log(JSON.stringify(anchor._projectPickerContextPoint));
"""
    )
    return _run_node(script)


def test_keyboard_context_menu_opens_on_the_button():
    """Shift+F10 / the ContextMenu key fire contextmenu with button -1 at the
    focused trigger's centre (Chromium 149). That is not a pointer position:
    the menu must open on the button as a click does, not over the chat pane."""
    data = _record_for("{clientX: 357, clientY: 315, button: -1, target: onTrigger}")
    assert "freshMenuAnchor" not in data, f"a keyboard open must not anchor at a point; got {data}"
    assert (data["clientX"], data["clientY"]) == (357, 315), "the point itself is still recorded"
    data = _record_for("{clientX: 200, clientY: 400, button: -1, target: onTitle}")
    assert "freshMenuAnchor" not in data, f"button -1 is a keyboard open wherever it lands; got {data}"


def test_right_click_on_the_trigger_opens_on_the_button():
    """A mouse right-click on the row's own trigger opens where a click on it
    does, instead of covering the trigger with the menu's corner."""
    data = _record_for("{clientX: 357, clientY: 315, button: 2, target: onTrigger}")
    assert "freshMenuAnchor" not in data, f"right-click on the trigger must open on the button; got {data}"


def test_right_click_on_the_row_still_opens_at_the_pointer():
    data = _record_for("{clientX: 120, clientY: 400, button: 2, target: onTitle}")
    assert data.get("freshMenuAnchor") is True, f"a row right-click must keep the pointer placement; got {data}"
