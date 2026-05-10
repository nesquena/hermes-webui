"""
Regression test for PR #1071: approval card must render above the queue flyout.

Both `.approval-card` and `.queue-card` are siblings inside `.composer-flyout`
and share the same absolute positioning slot just above the composer. When
both are visible at the same time (queue flyout open + tool approval card
sliding up) the approval card MUST win the stacking order so its security-
relevant Allow / Deny buttons stay clickable.

The old CSS had `.queue-card { z-index: 2 }` and no z-index on
`.approval-card.visible`, so the queue card painted on top and blocked the
approval buttons. The fix raises `.approval-card.visible` to z-index 3.

This test pins the invariant: approval-card.visible z-index must be strictly
greater than queue-card z-index.
"""

import re
from pathlib import Path

CSS = Path("static/style.css").read_text(encoding="utf-8")


def _z_index_of(selector_regex: str) -> int | None:
    m = re.search(selector_regex + r"\s*\{[^}]*z-index:(\d+)", CSS)
    return int(m.group(1)) if m else None


def test_approval_card_visible_outranks_queue_card():
    queue_z = _z_index_of(r"\.queue-card")
    approval_visible_z = _z_index_of(r"\.approval-card\.visible")
    assert queue_z is not None, ".queue-card must declare a z-index"
    assert approval_visible_z is not None, (
        ".approval-card.visible must declare a z-index — without it, the approval "
        "buttons get covered by the queue flyout (PR #1071)"
    )
    assert approval_visible_z > queue_z, (
        f".approval-card.visible z-index ({approval_visible_z}) must be strictly "
        f"greater than .queue-card z-index ({queue_z}) so approval buttons "
        f"remain clickable when both flyouts are open."
    )


def test_approval_card_visible_outranks_terminal_card():
    terminal_z = _z_index_of(r"\.composer-terminal-panel")
    approval_visible_z = _z_index_of(r"\.approval-card\.visible")
    assert terminal_z is not None, ".composer-terminal-panel must declare a z-index"
    assert approval_visible_z is not None
    assert approval_visible_z > terminal_z, (
        f".approval-card.visible z-index ({approval_visible_z}) must stay above "
        f".composer-terminal-panel z-index ({terminal_z}) so approval controls "
        f"remain clickable when the terminal flyout is open."
    )
