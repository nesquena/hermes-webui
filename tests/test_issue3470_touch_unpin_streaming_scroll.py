"""Regression: mobile touch swipe-up must unpin streaming auto-scroll (#3470).

On touch devices the auto-scroll-during-streaming behavior could not be
dismissed: ``_recordNonMessageScrollIntent`` only set ``_messageUserUnpinned``
on the wheel path (``typeof e.deltaY === 'number'``), but touch events never
carry ``deltaY``, so a finger swipe-up never unpinned the stream and it kept
snapping to the bottom on every token.

The fix tracks ``_touchStartY`` on ``touchstart`` and, on ``touchmove``, treats
a finger that moved up by >8px as upward-scroll intent — the same effect as the
wheel ``deltaY < 0`` branch — setting ``_messageUserUnpinned=true`` /
``_scrollPinned=false``. ``_touchStartY`` is cleared on ``touchend`` /
``touchcancel`` and in ``_resetScrollDirectionTracker``.

These are source-structure assertions (the project has no JS test runtime),
matching the sibling sticky-unpin regressions (test_issue1731 / test_issue3250).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _record_intent_fn() -> str:
    start = UI_JS.index("function _recordNonMessageScrollIntent")
    end = UI_JS.index("function _recentNonMessageScrollIntent")
    return UI_JS[start:end]


def test_touchmove_branch_sets_user_unpinned():
    """The touchmove branch must set the same authoritative unpin flags the
    wheel deltaY<0 branch sets."""
    fn = _record_intent_fn()
    compact = fn.replace(" ", "").replace("\n", "")
    # The touchmove branch is gated on event type + a recorded start Y + a live touch point.
    assert "e.type==='touchmove'" in compact, "must handle the touchmove event type"
    assert "_touchStartY!==null" in compact, (
        "touchmove must compare against a recorded _touchStartY start position"
    )
    # Upward intent (finger moved up) flips the same flags as the wheel path.
    assert "_messageUserUnpinned=true" in compact, (
        "touch swipe-up must set _messageUserUnpinned (the authoritative unpin signal)"
    )
    assert "_scrollPinned=false" in compact, "touch swipe-up must clear _scrollPinned"


def test_touchmove_uses_correct_downward_finger_threshold():
    """Upward-scroll intent on touch = finger dragged DOWN the screen (dy>0),
    which scrolls content up into earlier history (scrollTop decreases) — the
    same direction the scroll listener's movedUp branch unpins on. A >8px
    deadzone avoids tap/jitter. (The original PR had the sign inverted —
    dy<-8 — which would have unpinned on a follow-the-stream upward swipe;
    Codex caught it. This test pins the corrected direction.)"""
    fn = _record_intent_fn().replace(" ", "").replace("\n", "")
    assert "_touchStartY" in fn and "clientY" in fn, (
        "touch delta must be computed from clientY vs _touchStartY"
    )
    assert "dy=e.touches[0].clientY-_touchStartY" in fn, "dy = current clientY - start clientY"
    assert "if(dy>8)" in fn, (
        "unpin must fire on dy>8 (finger moved DOWN -> scroll up into history); "
        "dy<-8 would be backwards and unpin while the user follows the stream"
    )
    assert "if(dy<-8)" not in fn, "the inverted dy<-8 sign must NOT be present"


def test_touchstart_records_start_y():
    """A touchstart listener must record the initial Y so touchmove has a baseline."""
    compact = UI_JS.replace(" ", "").replace("\n", "")
    assert "addEventListener('touchstart'" in compact or 'addEventListener("touchstart"' in compact, (
        "must register a touchstart listener to seed _touchStartY"
    )
    assert "_touchStartY=e.touches[0].clientY" in compact, (
        "touchstart must record the finger's start clientY into _touchStartY"
    )


def test_touch_end_and_cancel_clear_start_y():
    """_touchStartY must be cleared when the gesture ends so a later touchmove
    in a different gesture doesn't compare against a stale baseline."""
    compact = UI_JS.replace(" ", "").replace("\n", "")
    assert "addEventListener('touchend'" in compact or 'addEventListener("touchend"' in compact
    assert "addEventListener('touchcancel'" in compact or 'addEventListener("touchcancel"' in compact
    # Both handlers reset the baseline.
    assert compact.count("_touchStartY=null") >= 2, (
        "touchend and touchcancel (and the reset hook) must clear _touchStartY"
    )


def test_reset_scroll_direction_tracker_clears_touch_start_y():
    """Session switch / reset must also clear _touchStartY (no cross-chat bleed)."""
    start = UI_JS.index("function _resetScrollDirectionTracker")
    end = UI_JS.index("}", UI_JS.index("{", start))
    fn = UI_JS[start:end].replace(" ", "").replace("\n", "")
    assert "_touchStartY=null" in fn, (
        "_resetScrollDirectionTracker must clear _touchStartY on session switch"
    )
