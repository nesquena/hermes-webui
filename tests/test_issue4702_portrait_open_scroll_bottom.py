"""Regression test for #4702 (sibling #4701).

iOS Safari in PORTRAIT resolves its dynamic toolbar height *after* first paint.
When the toolbar collapses, the #messages scroller grows (clientHeight increases),
which fires a native scroll event with a DECREASED scrollTop even though the user
never scrolled. Before the fix that reflow was misread as an upward scroll
(`movedUp`), which falsely set `_messageUserUnpinned=true; _scrollPinned=false` on
a freshly-opened session — stranding portrait readers at the top, and the late
ResizeObserver settle then self-cancelled because of the false unpin.

The listener transition is exercised in Node with the repository's existing
scroll-listener runtime harness; source guards remain for the programmatic-write
and ResizeObserver integration points.
"""
import pathlib

import pytest

from tests.test_issue4295_scroll_pin_reentry import NODE, _run_scroll_listener

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


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_portrait_history_open_stays_pinned_at_bottom_after_viewport_growth():
    """A loaded long transcript remains pinned when portrait chrome settles.

    The first frame models the history tail after opening; the second models
    iOS increasing clientHeight and clamping scrollTop before the listener runs.
    """
    frames = [
        {"scrollTop": 6500, "scrollHeight": 7000, "clientHeight": 500},
        {"scrollTop": 6350, "scrollHeight": 7000, "clientHeight": 650},
    ]
    state = _run_scroll_listener(frames)
    assert state["_scrollPinned"] is True
    assert state["_messageUserUnpinned"] is False
    assert frames[-1]["scrollHeight"] - frames[-1]["scrollTop"] - frames[-1]["clientHeight"] == 0


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
