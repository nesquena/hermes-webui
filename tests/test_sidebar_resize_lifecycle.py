"""Sidebar resize lifecycle: pointer-capture fallback and teardown (#7954).

Focuses on the regression path where ``setPointerCapture`` is unavailable or
throws: the drag must keep working through the document-level fallback and
must end cleanly on release outside the handle. The positive controls the fix
must keep working (capture success, ``lostpointercapture``, window ``blur``,
touch pointers, storage-denied date-group toggle) live here too.

Runs the REAL resize and collapse-state code extracted from ``static/boot.js``
and ``static/sessions.js`` in a browser harness, in the same style as
``test_send_key_preference_live_update.py``.
"""

from pathlib import Path
import json

import pytest

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - dependency optional
    sync_playwright = None


REPO = Path(__file__).parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _require_playwright():
    if sync_playwright is None:
        pytest.skip("playwright is unavailable; run `playwright install chromium`")
    return sync_playwright


def _extract_braced(src: str, start_marker: str) -> str:
    """Return the full ``{...}`` block that follows ``start_marker``."""
    start = src.index(start_marker)
    brace_at = src.index("{", start)
    depth = 0
    for i in range(brace_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unterminated block after {start_marker!r}")


def _extract_init_resize() -> str:
    return _extract_braced(
        BOOT_JS, "function initResize(handleId, targetEl, edge, minW, maxW, storageKey)"
    )


def _extract_collapse_state_block() -> str:
    # The seeding block alone is not enough: _groupCollapsed/_saveCollapsed are
    # declared right after it in sessions.js, and the toggle mirrors them.
    marker = "if(!window.__hermesDateGroupCollapsed)"
    start = SESSIONS_JS.index(marker)
    tail = SESSIONS_JS[start:]
    save_marker = "const _saveCollapsed=()=>"
    save_at = tail.index(save_marker)
    save_block = _extract_braced(tail, save_marker)
    return tail[: save_at + len(save_block)] + ";"


HARNESS_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>resize lifecycle harness</title></head>
<body>
<div id="sidebar" style="width: 360px; height: 300px;"></div>
<div id="sidebarResize" style="width: 5px; height: 300px;"></div>
<script>
window.$ = sel => document.querySelector(sel);
window._syncWorkspacePanelInlineWidth = () => {};

__INIT_RESIZE__

__COLLAPSE_STATE__

// Mirrors hdr.onclick (static/sessions.js): flip in-memory state, persist
// best-effort, then re-render from the in-memory authority.
window.__toggleGroup = (label) => {
  const state = window.__hermesDateGroupCollapsed;
  _pending.add(label);
  state[label] = !state[label];
  _saveCollapsed();
  return state[label];
};
window.__reseedCollapseState = () => {
  __COLLAPSE_STATE__
};
// Mirrors the render's visibility rule (static/sessions.js): a group body is
// hidden exactly when its collapse flag is truthy. Builds rowsPerGroup rows
// per group so visible session-row counts can be asserted.
window.__renderGroups = (labels, rowsPerGroup) => {
  const host = document.getElementById('sidebar');
  host.innerHTML = '';
  let visible = 0;
  for (const label of labels) {
    const body = document.createElement('div');
    body.className = 'session-date-body';
    for (let i = 0; i < rowsPerGroup; i++) {
      const row = document.createElement('div');
      row.className = 'session-row';
      body.appendChild(row);
    }
    const collapsed = Boolean(window.__hermesDateGroupCollapsed[label]);
    if (collapsed) body.style.display = 'none';
    host.appendChild(body);
    if (!collapsed) visible += rowsPerGroup;
  }
  return visible;
};
</script>
</body></html>
"""


def _build_harness_html() -> str:
    collapse = _extract_collapse_state_block()
    return HARNESS_HTML.replace("__INIT_RESIZE__", _extract_init_resize()).replace(
        "__COLLAPSE_STATE__", collapse
    )


STATE_JS = """() => ({
    width: document.getElementById('sidebar').style.width,
    dragging: document.getElementById('sidebarResize').classList.contains('dragging'),
    resizing: document.body.classList.contains('resizing'),
    stored: (() => { try { return localStorage.getItem('hermes-sidebar-w'); } catch (e) { return 'THROWS'; } })(),
})"""


def _boot_resize(page, *, capture="ok"):
    """Wire initResize against the harness DOM.

    capture='ok'    -> setPointerCapture succeeds (handle keeps receiving events)
    capture='throw' -> setPointerCapture throws (fallback branch must engage)
    """
    page.evaluate(
        """(capture) => {
        if (capture === 'throw') {
            Element.prototype.setPointerCapture = function() { throw new Error('capture unavailable'); };
        } else {
            Element.prototype.setPointerCapture = function(id) { this.__capturedId = id; };
            Element.prototype.releasePointerCapture = function() {};
        }
        localStorage.removeItem('hermes-sidebar-w');
        initResize('#sidebarResize', document.getElementById('sidebar'), 'right', 180, 420, 'hermes-sidebar-w');
    }""",
        capture,
    )


def _pointer(page, type_, *, target="#sidebarResize", x=100, pointer_id=1, pointer_type="mouse"):
    page.evaluate(
        """({type, target, x, pointerId, pointerType}) => {
            const el = document.querySelector(target);
            el.dispatchEvent(new PointerEvent(type, {
                bubbles: true, cancelable: true,
                clientX: x, clientY: 50,
                pointerId: pointerId, pointerType: pointerType,
            }));
        }""",
        {
            "type": type_,
            "target": target,
            "x": x,
            "pointerId": pointer_id,
            "pointerType": pointer_type,
        },
    )


@pytest.fixture
def page():
    _require_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context()
        pg = context.new_page()
        import tempfile

        tmp = Path(tempfile.mkstemp(suffix=".html")[1])
        tmp.write_text(_build_harness_html(), encoding="utf-8")
        pg.goto(tmp.as_uri())
        pg.wait_for_load_state("domcontentloaded")
        yield pg
        browser.close()


def test_capture_success_drag_resizes_and_persists(page):
    """Positive control: working capture resizes, ends on release, persists."""
    _boot_resize(page, capture="ok")
    _pointer(page, "pointerdown", x=100)
    state = page.evaluate(STATE_JS)
    assert state["dragging"] and state["resizing"]

    _pointer(page, "pointermove", x=150)  # +50px
    state = page.evaluate(STATE_JS)
    assert state["width"] == "410px"

    _pointer(page, "pointerup", x=150)
    state = page.evaluate(STATE_JS)
    assert not state["dragging"] and not state["resizing"]
    assert state["stored"] == "410"


def test_capture_failure_falls_back_and_still_ends(page):
    """The #7954 regression: a thrown setPointerCapture must not stall the drag.

    Move and release happen OUTSIDE the 5px handle, which only the
    document-level fallback can see.
    """
    _boot_resize(page, capture="throw")
    _pointer(page, "pointerdown", x=100)
    state = page.evaluate(STATE_JS)
    assert state["dragging"] and state["resizing"]

    _pointer(page, "pointermove", target="#sidebar", x=150)  # outside the handle
    state = page.evaluate(STATE_JS)
    assert state["width"] == "410px", "fallback must resize while the pointer is outside the handle"

    _pointer(page, "pointerup", target="body", x=150)  # release outside too
    state = page.evaluate(STATE_JS)
    assert not state["dragging"], "handle.dragging must clear on an outside release"
    assert not state["resizing"], "body.resizing must clear on an outside release"
    assert state["stored"] == "410", "width must persist at the shared end path"

    _pointer(page, "pointermove", target="#sidebar", x=300)  # drag is over
    state = page.evaluate(STATE_JS)
    assert state["width"] == "410px", "a later move must have no effect"
    assert not state["dragging"] and not state["resizing"]


def test_lostpointercapture_ends_drag(page):
    """Positive control: capture revoked by the platform ends the drag."""
    _boot_resize(page, capture="ok")
    _pointer(page, "pointerdown", x=100)
    _pointer(page, "pointermove", x=150)
    page.evaluate(
        "() => document.getElementById('sidebarResize')"
        ".dispatchEvent(new PointerEvent('lostpointercapture', {bubbles: true, pointerId: 1}))"
    )
    state = page.evaluate(STATE_JS)
    assert not state["dragging"] and not state["resizing"]
    assert state["stored"] == "410"


def test_window_blur_ends_drag(page):
    """Positive control: losing the window mid-drag ends the drag."""
    _boot_resize(page, capture="ok")
    _pointer(page, "pointerdown", x=100)
    _pointer(page, "pointermove", x=150)
    page.evaluate("() => window.dispatchEvent(new Event('blur'))")
    state = page.evaluate(STATE_JS)
    assert not state["dragging"] and not state["resizing"]
    assert state["stored"] == "410"


def test_touch_pointer_does_not_start_resize(page):
    """Positive control: touch pointers are not panel-resize drags."""
    _boot_resize(page, capture="ok")
    _pointer(page, "pointerdown", x=100, pointer_type="touch")
    state = page.evaluate(STATE_JS)
    assert not state["dragging"] and not state["resizing"]
    _pointer(page, "pointermove", x=150, pointer_type="touch")
    state = page.evaluate(STATE_JS)
    assert state["width"] == "360px"


def test_date_group_toggle_survives_storage_denial(page):
    """Positive control: a throwing localStorage write cannot undo the toggle (#7953)."""
    page.evaluate(
        """() => {
        Storage.prototype.setItem = function() { throw new DOMException('denied', 'QuotaExceededError'); };
        window.__hermesDateGroupCollapsed = undefined;
        window.__reseedCollapseState();
    }"""
    )
    on = page.evaluate("() => window.__toggleGroup('YESTERDAY')")
    assert on is True
    reseeded = page.evaluate(
        "() => { window.__reseedCollapseState(); return window.__hermesDateGroupCollapsed['YESTERDAY']; }"
    )
    assert reseeded is True, "re-seeding must not clobber the in-memory state from storage"


def test_other_pointer_cancel_does_not_end_drag(page):
    """Regression (Oct 2 re-gate): a pointercancel from an unrelated pointer
    must not end the active resize.

    Reviewer's sequence: capture failing, pointer 1 drags to +20px, pointer 2
    (a pen or touch contact elsewhere) is cancelled, then pointer 1 moves to
    +50px and releases — the drag must survive the foreign cancel.
    """
    _boot_resize(page, capture="throw")
    _pointer(page, "pointerdown", x=100, pointer_id=1)
    _pointer(page, "pointermove", target="#sidebar", x=120, pointer_id=1)
    state = page.evaluate(STATE_JS)
    assert state["width"] == "380px"

    _pointer(page, "pointercancel", target="#sidebar", x=120, pointer_id=2)
    state = page.evaluate(STATE_JS)
    assert state["dragging"], "another pointer's cancel must not clear handle.dragging"
    assert state["resizing"], "another pointer's cancel must not clear body.resizing"
    assert state["width"] == "380px"

    _pointer(page, "pointermove", target="#sidebar", x=150, pointer_id=1)
    _pointer(page, "pointerup", target="body", x=150, pointer_id=1)
    state = page.evaluate(STATE_JS)
    assert not state["dragging"] and not state["resizing"]
    assert state["width"] == "410px", "the drag must resume and finish after the foreign cancel"
    assert state["stored"] == "410"


def test_other_pointer_cancel_ignored_with_capture(page):
    """The pointer-id guard applies on the capture-success path too."""
    _boot_resize(page, capture="ok")
    _pointer(page, "pointerdown", x=100, pointer_id=1)
    _pointer(page, "pointermove", x=150, pointer_id=1)
    _pointer(page, "pointercancel", x=150, pointer_id=2)  # on the handle
    state = page.evaluate(STATE_JS)
    assert state["dragging"] and state["resizing"]
    assert state["width"] == "410px"
    _pointer(page, "pointerup", x=150, pointer_id=1)
    state = page.evaluate(STATE_JS)
    assert not state["dragging"] and not state["resizing"]
    assert state["stored"] == "410"


def test_active_pointer_cancel_still_ends_drag(page):
    """The active pointer's own cancel must still end the drag."""
    _boot_resize(page, capture="throw")
    _pointer(page, "pointerdown", x=100, pointer_id=1)
    _pointer(page, "pointermove", target="#sidebar", x=150, pointer_id=1)
    _pointer(page, "pointercancel", target="#sidebar", x=150, pointer_id=1)
    state = page.evaluate(STATE_JS)
    assert not state["dragging"] and not state["resizing"]
    assert state["stored"] == "410"


def test_successful_cross_tab_change_wins_over_released_local(page):
    """Oct 3 re-gate: a local override is released once its snapshot is
    successfully written, so another tab's newer successful choice wins.

    A collapses YESTERDAY (write succeeds, override released). B reopens it
    (write succeeds). A re-renders and must show YESTERDAY expanded with its
    rows visible. A then toggles the unrelated OLDER group; the saved JSON
    must preserve B's newer YESTERDAY:false.
    """
    # A collapses YESTERDAY; the write succeeds and releases the override.
    assert page.evaluate("() => window.__toggleGroup('YESTERDAY')") is True
    assert json.loads(page.evaluate("() => localStorage.getItem('hermes-date-groups-collapsed')")) == {"YESTERDAY": True}
    assert page.evaluate("() => window.__hermesDateGroupPending.size") == 0, (
        "a successful write must release the pending override"
    )
    assert page.evaluate("() => window.__renderGroups(['YESTERDAY'], 6)") == 0, (
        "a collapsed group must hide its session rows"
    )

    # B reopens YESTERDAY (a successful write to shared storage).
    page.evaluate("() => localStorage.setItem('hermes-date-groups-collapsed', JSON.stringify({YESTERDAY: false}))")

    # A re-renders: the released key adopts B's newer choice.
    page.evaluate("() => window.__reseedCollapseState()")
    assert page.evaluate("() => window.__hermesDateGroupCollapsed['YESTERDAY']") is False, (
        "after a successful write released the key, a newer stored choice must win"
    )
    assert page.evaluate("() => window.__renderGroups(['YESTERDAY'], 6)") == 6, (
        "the adopted reopen must make the group's session rows visible again"
    )

    # A toggles the unrelated OLDER group; B's YESTERDAY:false is preserved.
    page.evaluate("() => window.__toggleGroup('OLDER')")
    stored = json.loads(page.evaluate("() => localStorage.getItem('hermes-date-groups-collapsed')"))
    assert stored == {"YESTERDAY": False, "OLDER": True}, (
        "an unrelated local toggle must not overwrite another tab's newer successful choice"
    )


def test_cleared_storage_expands_adopted_non_pending_group(page):
    """Oct 3 re-gate: a valid empty/cleared snapshot removes adopted
    (non-pending) keys, so a group another tab expanded is no longer hidden.
    """
    page.evaluate("() => localStorage.setItem('hermes-date-groups-collapsed', JSON.stringify({TODAY: true}))")
    page.evaluate("() => window.__reseedCollapseState()")
    assert page.evaluate("() => window.__hermesDateGroupCollapsed['TODAY']") is True
    assert page.evaluate("() => window.__renderGroups(['TODAY'], 4)") == 0

    # Another tab clears storage entirely (a valid empty snapshot).
    page.evaluate("() => localStorage.removeItem('hermes-date-groups-collapsed')")
    page.evaluate("() => window.__reseedCollapseState()")
    assert page.evaluate("() => window.__hermesDateGroupCollapsed['TODAY']") in (None, False), (
        "a valid cleared snapshot must remove an adopted (non-pending) collapse key"
    )
    assert page.evaluate("() => window.__renderGroups(['TODAY'], 4)") == 4, (
        "the cleared key must make the group's session rows visible again"
    )


def test_malformed_or_unavailable_storage_preserves_state(page):
    """Oct 3 re-gate controls: a malformed or unavailable read must not
    clobber the current in-memory state or drop adopted collapses.
    """
    page.evaluate("() => localStorage.setItem('hermes-date-groups-collapsed', JSON.stringify({TODAY: true}))")
    page.evaluate("() => window.__reseedCollapseState()")
    assert page.evaluate("() => window.__hermesDateGroupCollapsed['TODAY']") is True

    # Malformed JSON: state preserved.
    page.evaluate("() => localStorage.setItem('hermes-date-groups-collapsed', '{not-valid-json')")
    page.evaluate("() => window.__reseedCollapseState()")
    assert page.evaluate("() => window.__hermesDateGroupCollapsed['TODAY']") is True, (
        "a malformed read must preserve the current state"
    )

    # Unavailable storage: state preserved.
    page.evaluate(
        "() => { Object.defineProperty(Storage.prototype, 'getItem',"
        " {value: function(){ throw new Error('denied'); }, configurable: true}); }"
    )
    page.evaluate("() => window.__reseedCollapseState()")
    assert page.evaluate("() => window.__hermesDateGroupCollapsed['TODAY']") is True, (
        "an unavailable read must preserve the current state"
    )
