"""#8506: hovering a transparent-stream row should not flash.

Faded rows used to replace the row's whole transition list with just
`opacity`, so the hover background snapped in one frame. And hovering an
expanded row made it lighter than its open tint.
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


def test_faded_rows_keep_background_and_border_transitions():
    # No faded-row rule may narrow the transition list; the base row's list applies.
    for rule in re.findall(r"\.transparent-event-row\[data-transparent-fade[^{]*\{[^}]*\}", STYLE_CSS):
        assert "transition" not in rule, rule
    base = _rule(".transparent-event-row{\n  margin:")
    assert "transition:background .18s ease,border-color .18s ease,opacity .22s ease;" in base


def test_hovering_an_expanded_row_brightens_it():
    expanded = _mix_percent(_rule('.transparent-event-row[data-expanded="1"]{'))
    expanded_hover = _mix_percent(_rule('.transparent-event-row[data-expanded="1"]:hover,'))
    assert expanded_hover > expanded
    # The expanded-hover rule must come after the generic hover rule to win.
    assert STYLE_CSS.index('.transparent-event-row[data-expanded="1"]:hover,') > STYLE_CSS.index(
        ".transparent-event-row:hover,\n.transparent-event-row:focus-within{"
    )
