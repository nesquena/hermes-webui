"""Round-12 UX re-gate of PR #6836 (maintainer review 5480716877 @
2026-10-10T20:33:37Z, anchored ``8f757aa1``).

The one remaining finding:

    "[UX MUST-FIX] Picking a long saved path pushes "Add" outside the dialog
    ``static/style.css:2278``
    ``.project-bindings-ws-add .project-bindings-combo{flex:1}`` has no
    ``min-width:0``, so after you pick a saved workspace whose name plus path is
    long (roughly 45+ characters), the trigger grows to its content and pushes
    the Add button and chevron past the dialog's right edge.  Measured: the
    dialog's scrollWidth is 559 against a clientWidth of 458 on desktop, tablet
    and landscape, and 557 against 364 on phone.  The Add button sits at
    x996-1050 while the dialog ends at x950.  ...
    Fix: add ``min-width:0`` to that rule.  The trigger's existing ellipsis then
    applies.  A test that picks a long path and checks that the Add button's
    right edge is inside the dialog would pin it."

Fix: ``min-width:0`` on that rule.  The guard below is deliberately
whitespace-proof: the 6775 lesson (Greptile P2 2026-10-10T08:06:14Z) was that a
bare ``"min-width:0" in rule`` assertion stays GREEN for ``min-width: 0`` /
``min-width :0`` / ``min-width:0px``, i.e. it cannot catch the regression it
exists for.  ``_pins_zero_min_width`` matches every zero spelling and rejects
non-zero lengths, so the desktop/phone column-overflow regression cannot walk
back in disguised.

The real-browser counterpart (Add button's right edge inside the dialog at
1440x900 / 390x844 / 844x390 / 820x1180 with a long path picked) is the
maintainer's own screenshot pass; it was reproduced locally with the shipped
dialog and is not a pytest gate here because the repo's CI has no browser.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")

# min-width set to a zero length: `0`, `0px`, `0.0`, `0%`, `0em`, ... A value
# that merely starts with 0 (0.5px) is NOT a zero and must not match.
_ZERO_MIN_WIDTH = re.compile(
    r"min-width\s*:\s*0(?:\.0+)?(?:px|%|em|rem|ch|ex|vw|vh|pt|pc|cm|mm|in)?\s*(?:[;}]|$)"
)


def _pins_zero_min_width(text: str) -> bool:
    """True when *text* sets ``min-width`` to a zero length, any spelling."""
    return bool(_ZERO_MIN_WIDTH.search(text))


def _rule(selector: str) -> str:
    """Return the declaration block of the FIRST rule whose selector matches.

    Brace-depth aware, so a comment or a nested at-rule inside the block cannot
    truncate the slice early.
    """
    start = STYLE_CSS.find(selector)
    assert start >= 0, f"{selector} selector not found in style.css"
    open_brace = STYLE_CSS.find("{", start + len(selector) - 1)
    assert open_brace >= 0, f"{selector} rule did not open"
    depth = 0
    for idx in range(open_brace, len(STYLE_CSS)):
        ch = STYLE_CSS[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return STYLE_CSS[start : idx + 1]
    raise AssertionError(f"{selector} rule did not close")


# --------------------------------------------------------------------------
# The fix itself
# --------------------------------------------------------------------------


def test_add_row_combo_opts_out_of_the_flex_min_width_auto_clamp():
    """The add-row combo is the flex item, so IT must carry min-width:0."""
    rule = _rule(".project-bindings-ws-add .project-bindings-combo{")
    assert "flex:1" in rule, (
        "The add-row combo should keep flex:1 (it takes the row's spare width)."
    )
    assert "min-width:auto" not in rule.replace(" ", ""), (
        "The combo must not re-assert min-width:auto; that is the clamp that "
        "made the Add button overflow the dialog."
    )
    assert _pins_zero_min_width(rule), (
        "`.project-bindings-ws-add .project-bindings-combo` must set "
        "min-width:0, otherwise a long picked path grows the trigger to its "
        "min-content width, pushes the Add button (and chevron) past the "
        "dialog's right edge, and a click meant for Add hits the overlay "
        "instead - which closes the dialog and drops every unsaved change "
        "(maintainer UX must-fix 2026-10-10T20:33:37Z)."
    )


def test_the_add_row_is_still_a_flex_row_with_an_unshrinkable_button():
    """Pin the geometry contract the fix depends on."""
    row = _rule(".project-bindings-ws-add{")
    assert "display:flex" in row, "The add row must stay a flex row."
    assert "align-items:center" in row, "The Add button should stay centred."

    btn = _rule(".project-bindings-ws-add .ws-add-btn{")
    assert "flex:0 0 auto" in btn, (
        "The Add button must stay a fixed-size flex item - the combo is the "
        "only child that may shrink."
    )


def test_the_shrunk_trigger_still_ellipsizes_its_label():
    """min-width:0 only helps if the trigger's own label can truncate."""
    trigger = _rule(".project-bindings-combo-trigger .combo-trigger-name{")
    assert "overflow:hidden" in trigger
    assert "text-overflow:ellipsis" in trigger
    assert "white-space:nowrap" in trigger
    assert _pins_zero_min_width(trigger), (
        "The trigger's name span must keep its own min-width:0 so it ellipsizes "
        "instead of forcing the trigger wider than its flex item."
    )


# --------------------------------------------------------------------------
# Guard the guard: the whitespace/spelling blindness that bit PR #6775
# --------------------------------------------------------------------------


def test_zero_min_width_guard_accepts_every_zero_spelling():
    spellings = [
        "min-width:0",
        "min-width: 0",
        "min-width :0",
        "min-width:0px",
        "min-width: 0.0",
        "min-width:0%;",
        "min-width:0em}",
        "flex:1;min-width:0;margin-bottom:0;",
        "min-width:0vw;",
    ]
    for text in spellings:
        assert _pins_zero_min_width(text), f"{text!r} must count as a zero min-width"


def test_zero_min_width_guard_rejects_non_zero_and_lookalikes():
    controls = [
        "min-width:1px",
        "min-width:auto",
        "min-width:100%",
        "min-width:0.5px",
        "min-width:10px;",
        "max-width:0",
        "min-height:0",
        "min-width:020px;",
        "flex:1;margin-bottom:0;",
    ]
    for text in controls:
        assert not _pins_zero_min_width(text), f"{text!r} must NOT count as a zero min-width"
