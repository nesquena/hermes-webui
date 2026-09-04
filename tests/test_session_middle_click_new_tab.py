"""Middle-click (auxclick button 1) on a sidebar session row opens that session
in a new browser tab instead of switching the current tab.

Today the top-level `.session-item` rows swallow non-left buttons: the
`onpointerup` guard returns early for `button !== 0` and there is no
`auxclick`/`mousedown` middle-button handler anywhere in sessions.js, so a
middle-click does nothing (or triggers browser autoscroll).

The fix wires `_openSessionUrlInNewTab(sid)` into a shared choke point used
by all three sidebar row kinds (top-level session rows, fork rows, and plain
child-session buttons) via both `auxclick` (primary) and `mousedown`
(prevents autoscroll) plus Ctrl/Cmd+click on the click path, while leaving
right-click (menu), select mode, rename, swipe, and single-tap behavior
untouched.
"""
from pathlib import Path

REPO = Path(__file__).parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")

ROW_KINDS = (
    ".session-item top-level row",
    "fork child row (.session-child-session-fork)",
    "plain child row (.session-child-session button)",
)


def test_new_tab_helper_exists():
    """A shared `_openSessionUrlInNewTab(sid)` helper builds the deep link."""
    assert "function _openSessionUrlInNewTab(" in SESSIONS_JS
    helper = SESSIONS_JS.split(
        "function _openSessionUrlInNewTab(")[1][:1200]
    assert "_sessionUrlForSid(" in helper
    assert "window.open(" in helper


def test_auxclick_wired_for_all_row_kinds():
    """Each row kind handles middle-click via the shared wiring helper."""
    # The auxclick/mousedown listeners live once in _wireSessionNewTabListeners;
    # every row kind (top-level .session-item, fork row, fork main button,
    # plain child button, lineage segment) must call the wirer.
    assert "_wireSessionNewTabListeners(el, ()=>s.session_id)" in SESSIONS_JS
    assert SESSIONS_JS.count("_wireSessionNewTabListeners(row, ()=>child.session_id)") == 2
    assert "_wireSessionNewTabListeners(mainBtn, ()=>child.session_id)" in SESSIONS_JS
    assert "_wireSessionNewTabListeners(row, ()=>seg.session_id)" in SESSIONS_JS
    assert SESSIONS_JS.count("_wireSessionNewTabListeners(") >= 6  # def + 5 call sites
    # All opens route through the two choke points with the concrete sid.
    assert "_openSessionUrlInNewTab(getSid())" in SESSIONS_JS
    assert "_openSessionUrlInNewTab(sid)" in SESSIONS_JS
    assert "_openSessionUrlInNewTab(childSession.session_id)" in SESSIONS_JS


def test_middle_mousedown_prevents_autoscroll():
    """`mousedown` on button 1 preventDefaults so the browser doesn't autoscroll."""
    assert "addEventListener('auxclick'" in SESSIONS_JS
    assert "addEventListener('mousedown'" in SESSIONS_JS
    idx = SESSIONS_JS.index("function _wireSessionNewTabListeners(")
    window = SESSIONS_JS[idx:idx + 2500]
    assert "button" in window and "1" in window
    assert "preventDefault()" in window
    # Ctrl/Cmd+click on the tap paths also routes to the new-tab opener.
    assert "_consumeSessionNewTabClick(e, child.session_id)" in SESSIONS_JS
    assert "_consumeSessionNewTabClick(e, s.session_id)" in SESSIONS_JS


def test_ctrl_click_opens_new_tab():
    """Ctrl/Cmd+left-click on a row opens the deep link in a new tab."""
    assert "e.ctrlKey||e.metaKey" in SESSIONS_JS.replace(" ", "")


def test_action_menu_and_select_mode_untouched():
    """New-tab must not fire from the ⋮ menu, checkboxes, or select mode."""
    assert "_isSessionActionTarget" in SESSIONS_JS
    assert "_sessionSelectMode" in SESSIONS_JS
    # The existing guards already cover these paths on the click/pointerup
    # flow; the shared choke point and the wirer must consult them too.
    consume_idx = SESSIONS_JS.index("function _consumeSessionNewTabClick(")
    consume = SESSIONS_JS[consume_idx:consume_idx + 1800]
    assert "_isSessionActionTarget" in consume
    assert "_sessionSelectMode" in consume
    assert "_renamingSid" in consume
    wire_idx = SESSIONS_JS.index("function _wireSessionNewTabListeners(")
    wire = SESSIONS_JS[wire_idx:wire_idx + 2500]
    assert "_isSessionActionTarget" in wire
    assert "session-actions" in wire


def test_openChildSession_new_tab_flag():
    """Child-row programmatic path supports open-in-new-tab without a same-tab switch."""
    idx = SESSIONS_JS.index("const openChildSession=async(childSession,")
    window = SESSIONS_JS[idx:idx + 400]
    assert "newTab" in window
    assert "_openSessionUrlInNewTab(childSession.session_id)" in window
