"""Round-16 UX re-gate of PR #6836 (maintainer review 5482068105 @
2026-10-11T05:45:57Z, anchored ``f882c410``).

The review moved ONE item up from a follow-up to a fix, because the r7 scope
split removed the last scroll cue:

    "[UX] Pin the dialog's header and footer so Save/Cancel stay visible on long
    lists.  The whole dialog is the scroll container, so with many workspaces
    Save and Cancel fall below the dialog's edge, and the Add row clips flush
    against the border.  ...  At r7 the half-visible auto-assign checkbox cued
    the scroll; that cue is gone, and the app's thin overlay scrollbar is hidden
    on many platforms.  Save is hidden from 9 workspaces on desktop 1440x900,
    7 on phone 390x844, and 2 in landscape 844x390.
    Fix (two CSS rules, no markup change):
      .project-bindings-dialog{display:flex;flex-direction:column;overflow:hidden;max-height:80vh;}
      .project-bindings-body{overflow:auto;min-height:0;flex:1 1 auto;}"

plus a nit: the ``.project-bindings-body .app-dialog-actions`` rule is dead
because the actions live on the dialog, not in the body — removed here.

The guards below are static (the repo's CI has no browser), and they are written
whitespace-proof on purpose: the 6775 P2 lesson (Greptile 2026-10-10T08:06:14Z)
is that a bare substring assertion stays GREEN for every reformatting of the
same declaration.  ``min-height:0`` in particular is the whole reason the body
can shrink inside the flex column at all — ``min-height: auto`` (the flex-item
default) is exactly the "cannot shrink, so the dialog scrolls again" bug — so it
is checked with the same zero-length regex the round-12 guard uses.

The real-browser counterpart (the ten-workspace dialog on desktop and phone,
Save/Cancel inside the dialog and clickable, the body scrolling to its end with
the title pinned) is ``D:/tmp/regate1010/probe_pinned_footer_1011.py``: it is not
a pytest gate because the repo's CI has no browser, but it was run against this
head on a real Chromium and is RED on the pre-fix stylesheet.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")

# A zero length, any spelling: 0 / 0px / 0.0 / 0% / 0em / 0rem ... A value that
# merely starts with 0 (0.5px, 020px) is NOT a zero and must not match.
_ZERO_LENGTH = r"0(?:\.0+)?(?:px|%|em|rem|ch|ex|vw|vh|pt|pc|cm|mm|in)?"


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


def _has(block: str, prop: str, value: str) -> bool:
    """True when *block* declares ``prop: value``, whitespace tolerant."""
    pattern = rf"(?<![\w-]){re.escape(prop)}\s*:\s*{value}\s*(?:[;}}]|$)"
    return bool(re.search(pattern, block, re.IGNORECASE))


# --------------------------------------------------------------------------
# The fix: the dialog is a flex column, the body is the only scroller
# --------------------------------------------------------------------------


def test_dialog_is_a_flex_column_that_never_scrolls_itself():
    block = _rule(".project-bindings-dialog")
    assert _has(block, "display", r"flex\b"), block
    assert _has(block, "flex-direction", r"column\b"), block
    # The dialog itself must NOT be the scroll container any more: with
    # overflow:auto the header and the actions scroll away with the body.
    assert _has(block, "overflow", r"hidden\b"), block
    assert not re.search(r"(?<![\w-])overflow\s*:\s*(?:auto|scroll)\b", block), block
    assert _has(block, "max-height", r"80vh\b"), block


def test_body_is_the_only_scroller_and_can_shrink_inside_the_column():
    block = _rule(".project-bindings-body")
    assert _has(block, "overflow", r"auto\b"), block
    # min-height:0 is the load-bearing declaration: a flex item defaults to
    # min-height:auto, which refuses to shrink below its content, so the body
    # would push the actions out of the dialog again.
    assert re.search(
        rf"(?<![\w-])min-height\s*:\s*{_ZERO_LENGTH}\s*(?:[;}}]|$)", block
    ), block
    assert _has(block, "flex", r"1\s+1\s+auto\b"), block


def test_zero_min_height_guard_is_whitespace_proof():
    """The min-height guard must reject non-zero values (6775 P2 lesson)."""
    zero = ["min-height:0", "min-height: 0", "min-height :0;", "min-height:0px}",
            "min-height:0.0", "min-height:0%"]
    nonzero = ["min-height:1px", "min-height:auto", "min-height:100%",
               "min-height:0.5px", "max-height:0", "min-width:0", "min-height:020px"]
    pattern = rf"(?<![\w-])min-height\s*:\s*{_ZERO_LENGTH}\s*(?:[;}}]|$)"
    for text in zero:
        assert re.search(pattern, text), text
    for text in nonzero:
        assert not re.search(pattern, text), text


def test_dead_actions_rule_is_gone():
    """The nit: the actions are a dialog child, so that descendant rule is dead."""
    assert ".project-bindings-body .app-dialog-actions" not in STYLE_CSS


# --------------------------------------------------------------------------
# Why the rule can be dead: the actions really are outside the body
# --------------------------------------------------------------------------


def test_actions_row_is_a_sibling_of_the_body_in_the_shipped_dialog():
    assert "dialog.appendChild(btnRow)" in SESSIONS_JS.replace(" ", "")
    assert "body.appendChild(btnRow)" not in SESSIONS_JS
    # Order matters for the pinning: header, then the scrolling body, then the
    # actions, all three direct children of the dialog.
    assert (
        SESSIONS_JS.index("dialog.appendChild(header)")
        < SESSIONS_JS.index("dialog.appendChild(body)")
        < SESSIONS_JS.index("dialog.appendChild(btnRow)")
    )


def test_the_r7_long_path_fix_survives_this_edit():
    """Control: the sibling rule the previous UX gate signed off is untouched."""
    block = _rule(".project-bindings-ws-add .project-bindings-combo")
    assert re.search(
        rf"(?<![\w-])min-width\s*:\s*{_ZERO_LENGTH}\s*(?:[;}}]|$)", block
    ), block
    assert _has(block, "flex", r"1\b"), block
