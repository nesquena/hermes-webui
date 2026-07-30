"""Regression test for #4702 (sibling #4701).

iOS Safari in PORTRAIT resolves its dynamic toolbar height *after* first paint.
When the toolbar collapses, the #messages scroller grows (clientHeight increases),
which fires a native scroll event with a DECREASED scrollTop even though the user
never scrolled. Before the fix that reflow was misread as an upward scroll
(`movedUp`), which falsely set `_messageUserUnpinned=true; _scrollPinned=false` on
a freshly-opened session — stranding portrait readers at the top, and the late
ResizeObserver settle then self-cancelled because of the false unpin.

These are source-level guards (the runtime behavior is iOS-Safari-specific and not
reproducible in CI), mirroring the static-assertion style of
test_issue1360_streaming_scroll_hardening.py.
"""
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def test_client_height_seeded_with_scrolltop_on_programmatic_writes():
    """Every programmatic seed of `_lastScrollTop` must also seed
    `_lastMessageClientHeight`, else the FIRST native toolbar-collapse scroll event
    sees a null/stale prior height, `grew` is false, and the false-unpin still
    fires (Codex gate finding). The two must always be written together."""
    # No bare `_lastScrollTop=el.scrollTop;` without the height co-seed remains.
    assert "_lastScrollTop=el.scrollTop;\n" not in UI_JS, (
        "A programmatic _lastScrollTop=el.scrollTop write is missing the paired "
        "_lastMessageClientHeight=el.clientHeight seed (#4702)."
    )
    # The paired form is present at the programmatic-write sites.
    assert UI_JS.count("_lastScrollTop=el.scrollTop;_lastMessageClientHeight=el.clientHeight;") >= 4


def test_client_height_growth_guard_declared():
    """The scroller-height tracker must be declared so a toolbar-settle reflow can
    be distinguished from a real user scroll."""
    assert "let _lastMessageClientHeight=null;" in UI_JS


def test_moved_up_ignores_container_growth():
    """`movedUp` must be gated on `!grew` so a clientHeight increase (toolbar
    collapse) can't be misread as an upward user scroll (#4702)."""
    assert "const grew=_lastMessageClientHeight!==null&&el.clientHeight>_lastMessageClientHeight+1;" in UI_JS
    assert "const movedUp=!grew&&_lastScrollTop!==null&&top<_lastScrollTop-2;" in UI_JS
    # The height must be sampled every scroll event (so the next delta compares fresh).
    assert "_lastMessageClientHeight=el.clientHeight;" in UI_JS


def test_client_height_tracker_reset_on_session_switch():
    """A genuine session switch must reset the tracker so a stale cross-session
    height comparison can't suppress a real first scroll."""
    reset_idx = UI_JS.index("function _resetScrollDirectionTracker(){")
    body = UI_JS[reset_idx: reset_idx + 600]
    assert "_lastMessageClientHeight=null;" in body


def test_explicit_settle_observes_scroller_for_portrait_toolbar():
    """An explicit (open/user) settle must also observe the scroller itself, so a
    late portrait toolbar collapse re-anchors the bottom (#4702 defense-in-depth)."""
    assert "if(explicit&&observed!==el){ try{ ro.observe(el); }catch(_){ } }" in UI_JS
