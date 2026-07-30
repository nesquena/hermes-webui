"""Regression test for #4696 — eliminate the iOS tap delay on interactive buttons.

iOS Safari adds a ~300ms delay (and a double-tap "select then fire" behavior) on
tappable elements unless `touch-action:manipulation` is set. The fix adds that —
plus `-webkit-tap-highlight-color:transparent` — to interactive controls in the
touch/compact media block so the send button and friends fire on the first tap.
"""
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text(encoding="utf-8")


def test_interactive_buttons_have_touch_action_manipulation():
    # The rule must cover the common interactive selectors and set both
    # touch-action:manipulation (kills the 300ms tap delay) and a transparent
    # tap highlight. We assert the exact rule the fix introduced.
    rule = (
        "button,.icon-btn,.panel-icon-btn,.send-btn,.approval-btn,[onclick]"
        "{touch-action:manipulation;-webkit-tap-highlight-color:transparent;}"
    )
    assert rule in CSS, (
        "interactive controls must declare touch-action:manipulation to remove the "
        "iOS tap delay (#4696)"
    )
