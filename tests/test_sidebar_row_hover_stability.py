"""Hovering a sidebar row must not reflow it.

Hovering reveals the row's action button and hides its timestamp. Both used to
change layout: the timestamp left the flow entirely (``display:none``), which
pulled the title chips and the done marker to the right, and the row grew 32px
of ``padding-right`` for the button, which pushed them back to the left. The
row therefore jittered sideways every time the pointer crossed it.

The fix reserves the button's lane structurally — rows whose timestamp is
replaced by a state indicator keep the padding — and hides the timestamp with
``visibility`` so its box holds the lane while the button is over it.
"""

import re
from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")

# Declaration blocks only: a selector may not contain braces, so media
# wrappers and at-rules never swallow a rule's body.
RULES = [
    (selector.strip(), body)
    for selector, body in re.findall(r"([^{}]*)\{([^{}]*)\}", CSS)
]

# Anything that makes the action button (or the timestamp's replacement)
# visible by pointer or keyboard. None of these may move the row's content.
POINTER_STATES = (":hover", ":has(:focus-visible)", ".menu-open")


def test_the_parser_sees_the_session_row_rules():
    assert any(".session-item" in selector for selector, _body in RULES)


def test_pointer_states_never_change_session_row_padding():
    # The 40px lane is the reservation this row's action button needs; a
    # pointer state that grants it reflows the row. (Touch layouts thin the
    # lane to 6px and keep the button display:none, so they are out of scope.)
    offenders = [
        selector
        for selector, body in RULES
        if ".session-item" in selector
        and any(state in selector for state in POINTER_STATES)
        and "padding-right:40px" in body
    ]
    assert not offenders, (
        "hover/focus/menu-open changes the session row's padding, which reflows "
        "the title chips and the done marker under the pointer: "
        + "; ".join(offenders)
    )


def test_rows_without_a_timestamp_still_reserve_the_button_lane():
    reserves = [
        selector
        for selector, body in RULES
        if ".session-item.streaming" in selector and "padding-right:40px" in body
    ]
    assert reserves, (
        "a streaming/unread row has no timestamp, so it needs the 40px "
        "action-button lane reserved by a class rule instead of by :hover"
    )


def test_timestamp_is_hidden_rather_than_removed_on_hover_and_focus():
    checked = 0
    for selector, body in RULES:
        if ".session-item" not in selector or ".session-time" not in selector:
            continue
        if not any(state in selector for state in POINTER_STATES):
            continue
        checked += 1
        assert "visibility:hidden" in body, (
            f"{selector} must hide the timestamp with visibility so its box keeps "
            "the action button's lane; removing it reflows the row"
        )
        assert "display:none" not in body, (
            f"{selector} still removes the timestamp from the flow"
        )
    assert checked, "no timestamp-hiding rule found — did the row markup change?"
