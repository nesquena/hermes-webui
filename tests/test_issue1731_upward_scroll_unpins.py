"""Regression tests for #1731: small upward scrolls during streaming.

The pre-fix scroll listener applied hysteresis symmetrically: an upward
scroll that landed inside the 250px near-bottom zone still reported
``nearBottom = true``, so ``_nearBottomCount`` kept incrementing and
``_scrollPinned`` stayed true. The next streaming token then snapped
the user back to the bottom. The user effectively had to escape the
250px zone in a single fling to get unpinned.

The fix tracks ``_lastScrollTop`` and unpins immediately when the user
explicitly scrolls upward, bypassing the hysteresis counter for the
unpin path while preserving it for the re-pin path (which is what the
#1360 macOS momentum protection actually needs).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _scroll_listener_block() -> str:
    """Return the rAF callback inside the messages scroll listener."""
    anchor = "el.addEventListener('scroll'"
    start = UI_JS.index(anchor)
    raf_start = UI_JS.index("requestAnimationFrame", start)
    brace = UI_JS.index("{", raf_start)
    depth = 0
    for i in range(brace, len(UI_JS)):
        ch = UI_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[brace : i + 1]
    raise AssertionError("scroll listener rAF callback not found")


def test_scroll_listener_tracks_last_scroll_top():
    """The listener must remember the previous scrollTop to detect direction."""
    assert "let _lastScrollTop=" in UI_JS, (
        "Direction detection requires a closure-scoped _lastScrollTop "
        "tracker (#1731)."
    )

    block = _scroll_listener_block()
    assert "_lastScrollTop=top" in block, (
        "The rAF callback must update _lastScrollTop after each sample so "
        "the next sample can compare against it (#1731)."
    )


def test_scroll_listener_detects_upward_motion():
    """An upward scroll (scrollTop decreased) must be detected explicitly."""
    block = _scroll_listener_block()
    assert "movedUp" in block, (
        "The rAF callback must compute a movedUp flag from scrollTop "
        "direction so explicit upward scrolls bypass the hysteresis "
        "counter (#1731)."
    )
    # The threshold must be more than zero so a single-pixel jitter (e.g. a
    # browser rounding rAF reflow) doesn't unpin, but small enough that a
    # real wheel/trackpad up-tick is caught.
    assert "_lastScrollTop-2" in block or "top<_lastScrollTop -" in block, (
        "Upward detection must allow a small (~2px) tolerance against "
        "sub-pixel scroll noise (#1731)."
    )


def test_upward_scroll_unpins_immediately_without_hysteresis():
    """Upward motion sets _scrollPinned=false and resets the counter, no count needed."""
    block = _scroll_listener_block()
    if_idx = block.index("if(movedUp)")
    else_idx = block.find("else", if_idx)
    assert else_idx > if_idx, "upward / downward branches not found (#1731)"
    upward_branch = block[if_idx:else_idx]

    assert "_scrollPinned=false" in upward_branch, (
        "Upward scroll must set _scrollPinned=false immediately so the "
        "next streaming token does not re-snap to bottom (#1731)."
    )
    assert "_nearBottomCount=0" in upward_branch, (
        "Upward scroll must reset _nearBottomCount so a subsequent "
        "downward motion has to clear the hysteresis fresh (#1731)."
    )
    assert "_messageUserUnpinned=true" in upward_branch, (
        "Upward scroll must set the sticky manual-unpin flag."
    )


def test_upward_motion_unpins_on_scroll_top_delta_without_intent_timeout():
    """Scrollbar / keyboard upward scroll must unpin without a wheel intent window."""
    block = _scroll_listener_block()
    moved_idx = block.index("const movedUp=")
    moved_expr = block[moved_idx : block.find(";", moved_idx)]
    assert "_recentMessageUpwardIntent()" not in moved_expr, (
        "movedUp must use scrollTop direction only; sticky unpin replaces the #3250 timeout."
    )
    assert "_lastScrollTop-2" in moved_expr or "top<_lastScrollTop -" in moved_expr


def test_wheel_touch_upward_intent_unpins_immediately_inside_messages():
    """Wheel/touch up inside #messages must unpin before the scroll listener runs."""
    fn_start = UI_JS.index("function _recordNonMessageScrollIntent")
    fn_end = UI_JS.index("function _recentNonMessageScrollIntent", fn_start)
    fn = UI_JS[fn_start:fn_end]
    assert "_messageUserUnpinned=true" in fn.replace(" ", "")
    assert "e.deltaY<0" in fn and "e.type==='touchmove'" in fn


def test_downward_path_preserves_macos_momentum_hysteresis():
    """Downward motion into the near-bottom zone re-follows with hysteresis (#1360)."""
    block = _scroll_listener_block()
    assert "elseif(movedDown&&nearBottom)" in block.replace(" ", ""), (
        "Explicit downward scroll into the near-bottom zone must be the re-follow path "
        "after a sticky manual unpin."
    )
    assert "if(_nearBottomCount>=2)" in block, (
        "Re-follow still requires two consecutive near-bottom samples."
    )


def test_repin_threshold_is_still_250px():
    """The 250px near-bottom dead zone is locked in by #1360 / #677 and must
    stay. Direction detection is the new lever, not threshold relaxation.
    """
    block = _scroll_listener_block()
    assert "clientHeight<250" in block, (
        "The 250px re-pin dead zone must remain — #1360 / #677 require it "
        "for macOS small-window + trackpad momentum cases. The #1731 fix "
        "uses direction detection, not threshold changes."
    )


def test_programmatic_scroll_guard_still_skips_listener():
    """Programmatic scrolls must continue to short-circuit the listener so
    they don't pollute _lastScrollTop. (We bail before scheduling the rAF.)
    """
    anchor = "el.addEventListener('scroll'"
    start = UI_JS.index(anchor)
    brace = UI_JS.index("{", start)
    end = UI_JS.index("})", brace)
    listener = UI_JS[brace:end]

    bail_idx = listener.index("if(_programmaticScroll) return")
    raf_idx = listener.index("requestAnimationFrame")
    assert bail_idx < raf_idx, (
        "The _programmaticScroll guard must run before requestAnimationFrame "
        "so programmatic scrollToBottom() calls never update _lastScrollTop "
        "and never spuriously unpin (#1731)."
    )
