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
    # Tolerate either single-line or multi-line if/else formatting.
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
    assert "_nearBottomCount>=2" not in upward_branch, (
        "The upward branch must not gate unpinning on hysteresis — that "
        "was the bug (#1731)."
    )


def test_upward_motion_only_unpins_after_recent_user_intent():
    """Layout/programmatic scrollTop decreases must not masquerade as user scroll-up.

    Long-session windowing can preserve/restore scroll positions while the live
    stream is growing. If a plain scrollTop decrease always clears
    ``_scrollPinned``, the viewport can be visually at bottom while the state says
    "not pinned", so streaming stops auto-following. Explicit wheel/touch upward
    input must still unpin immediately; passive layout movement must not.
    """
    assert "let _lastMessageUpwardIntentMs=" in UI_JS, (
        "ui.js must track recent upward wheel/touch intent inside #messages so "
        "programmatic/layout scroll changes do not permanently unpin streaming."
    )
    assert "function _recentMessageUpwardIntent()" in UI_JS, (
        "ui.js must expose a recent upward transcript intent helper."
    )
    block = _scroll_listener_block()
    moved_idx = block.index("const movedUp=")
    moved_expr = block[moved_idx : block.find(";", moved_idx)]
    assert "_recentMessageUpwardIntent()" in moved_expr, (
        "movedUp must require recent wheel/touch upward intent, not only a "
        "scrollTop decrease caused by DOM/layout changes."
    )


def test_wheel_touch_upward_intent_is_recorded_inside_messages():
    """Wheel/touch gestures inside #messages must mark real upward user intent."""
    fn_start = UI_JS.index("function _recordNonMessageScrollIntent")
    fn_end = UI_JS.index("function _recentNonMessageScrollIntent", fn_start)
    fn = UI_JS[fn_start:fn_end]
    assert "_lastMessageUpwardIntentMs=performance.now()" in fn, (
        "_recordNonMessageScrollIntent must timestamp real upward transcript "
        "wheel/touch gestures before clearing _scrollPinned."
    )
    assert "e.deltaY<0" in fn and "e.type==='touchmove'" in fn, (
        "Both wheel-up and touchmove gestures inside #messages should count as "
        "user upward intent."
    )


def test_downward_path_preserves_macos_momentum_hysteresis():
    """Downward / stationary motion must still go through the original
    hysteresis re-pin path so the #1360 macOS trackpad momentum protection
    is preserved.
    """
    block = _scroll_listener_block()
    else_idx = block.index("else", block.index("if(movedUp)"))
    # End of else branch is at the next btn lookup line.
    end_idx = block.index("const btn=", else_idx)
    downward_branch = block[else_idx:end_idx]

    assert "_nearBottomCount=nearBottom?_nearBottomCount+1:0" in downward_branch, (
        "Downward path must keep incrementing the near-bottom counter so "
        "the macOS momentum re-pin guard still applies (#1360)."
    )
    assert "_scrollPinned=_nearBottomCount>=2" in downward_branch, (
        "Downward path must keep the >=2 hysteresis re-pin requirement "
        "(#1360)."
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
