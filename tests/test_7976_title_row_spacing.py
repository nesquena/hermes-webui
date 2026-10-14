"""#7976 release-review pins for the sidebar title-row spacing (gate 2026-10-06).

1. The 24px title floor applies only beside a child chip, so a childless compressed row in
   Detailed density keeps master's layout and its non-shrinking prior-turns pill isn't clipped.
2. Rows that carry a child chip tighten their gaps and drop the chip margin, so with fork,
   worktree and project badges at a 180px sidebar the child status mark stays inside the
   .session-text clip.
The browser gates (tests/browser_child_chip_visibility.py) measure the geometry; these pins
keep the scoping from regressing silently.
"""
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(encoding="utf-8")


def test_title_floor_is_scoped_to_rows_with_a_child_chip():
    assert ".session-title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);user-select:none;}" in CSS
    assert ".session-title-row:has(.session-child-count) .session-title{min-width:24px;}" in CSS
    assert ".session-title{flex:1;min-width:24px;" not in CSS


def test_child_chip_rows_tighten_gaps_and_drop_the_chip_margin():
    assert ":root .session-item .session-text .session-title-row:has(.session-child-count){column-gap:2px;}" in CSS
    assert ".session-title-row .session-child-count{margin-left:0;}" in CSS
