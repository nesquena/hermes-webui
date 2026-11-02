"""#8506: hovering an expanded transparent-stream row should brighten it.

Before, hovering an expanded row dropped it to the plain hover tint, so it
got lighter than its open tint.
"""
import re
from pathlib import Path

STYLE_CSS = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text(encoding="utf-8")


def _mix_percent(selector_block: str) -> int:
    match = re.search(r"var\(--surface-subtle\) (\d+)%", selector_block)
    assert match, selector_block
    return int(match.group(1))


def _rule(selector: str) -> str:
    start = STYLE_CSS.index(selector)
    return STYLE_CSS[start:STYLE_CSS.index("}", start) + 1]


def test_hovering_an_expanded_row_brightens_it():
    expanded = _mix_percent(_rule('.transparent-event-row[data-expanded="1"]{'))
    expanded_hover = _mix_percent(_rule('.transparent-event-row[data-expanded="1"]:hover,'))
    assert expanded_hover > expanded
    # The expanded-hover rule must come after the generic hover rule to win.
    assert STYLE_CSS.index('.transparent-event-row[data-expanded="1"]:hover,') > STYLE_CSS.index(
        ".transparent-event-row:hover,\n.transparent-event-row:focus-within{"
    )
