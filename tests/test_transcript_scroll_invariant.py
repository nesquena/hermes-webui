"""Class-level regression guard for the #4856 mobile-transcript-collapse family.

#4856 was a long-lived, high-impact mobile bug: the transcript was completely
unscrollable on phones. It survived multiple "fixes" because the regressions
were guarded only by grepping for one exact CSS string. This test guards the
*invariant* that actually matters, so the whole CLASS of bug can't come back:

  THE INVARIANT
  -------------
  In the transcript layout, `.messages` is the scroll container (it owns
  `overflow-y:auto` + `min-height:0`). `.messages-inner` is its child flex item
  that must GROW to the transcript's full height so the content overflows the
  *scroller*, not the inner. If `.messages-inner` ever becomes a scroll
  container on the Y axis, its `min-height:auto` resolves to 0, it collapses
  inside the flex column, and the transcript becomes unscrollable.

  `.messages-inner` becomes a Y scroll container if it sets EITHER:
    - `overflow-y: auto|scroll|hidden|clip`, OR
    - `overflow-x: hidden|scroll|auto` while overflow-y is visible — because per
      the CSS overflow spec, when one axis is hidden/scroll/auto and the other
      is `visible`, the visible axis is computed to `auto`. (`overflow-x: clip`
      is the ONLY horizontal-clipping value that does NOT coerce overflow-y.)

So: every `.messages-inner` rule (base and every media query) may use
`overflow-x: clip` to clip horizontal overflow, but must never use
`overflow-x: hidden` (or any overflow-y scroll value). And `.messages` must
remain the real scroller.

This is a pure source-invariant test (no browser needed in CI). For a true
runtime check at a real viewport, use the private workspace tool
`scripts/layout_scroll_probe.py`.
"""
from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(encoding="utf-8")

# Values that, on overflow-x, coerce overflow-y -> auto (making a scroll
# container). `clip` and `visible` do not coerce.
_COERCING_OVERFLOW_X = ("hidden", "scroll", "auto")


def _all_rule_bodies(selector_substr: str) -> list[tuple[int, str]]:
    """Return (offset, rule_body) for every CSS rule whose selector contains
    selector_substr — across the base stylesheet AND all media queries."""
    out = []
    for m in re.finditer(r"([^{}]*\{[^{}]*\})", CSS):
        block = m.group(1)
        # selector is everything before the first '{'
        sel = block[: block.index("{")]
        if selector_substr in sel:
            out.append((m.start(), block))
    return out


def _decls(rule_body: str) -> dict[str, str]:
    inner = rule_body[rule_body.index("{") + 1 : rule_body.rindex("}")]
    decls = {}
    for part in inner.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            decls[k.strip().lower()] = v.strip().lower().replace("!important", "").strip()
    return decls


def test_messages_inner_never_becomes_a_y_scroll_container():
    """Every .messages-inner rule (base + every media query) must not turn the
    inner into a Y-axis scroll container — the #4856 collapse class."""
    rules = _all_rule_bodies(".messages-inner")
    assert rules, ".messages-inner rules not found in style.css"
    for _, body in rules:
        # Only consider rules whose selector is exactly/primarily .messages-inner
        sel = body[: body.index("{")]
        if ".messages-inner" not in sel:
            continue
        d = _decls(body)
        ox = d.get("overflow-x")
        oy = d.get("overflow-y")
        overflow = d.get("overflow")
        # Explicit overflow-y scroll value on the inner is forbidden.
        assert oy not in ("auto", "scroll", "hidden", "clip"), (
            f".messages-inner sets overflow-y:{oy} — that makes it a scroll "
            f"container, min-height:auto->0, and collapses the transcript (#4856). "
            f"Rule: {sel.strip()}"
        )
        # A shorthand `overflow:` that implies a scroll value is forbidden.
        if overflow is not None:
            assert overflow in ("visible", "clip visible", "visible clip"), (
                f".messages-inner sets overflow:{overflow} — risks the #4856 "
                f"scroll-container collapse. Rule: {sel.strip()}"
            )
        # overflow-x must not be a coercing value (hidden/scroll/auto): those
        # coerce overflow-y->auto when overflow-y is visible. clip is allowed.
        if ox is not None:
            assert ox not in _COERCING_OVERFLOW_X, (
                f".messages-inner sets overflow-x:{ox} — per the CSS spec this "
                f"coerces overflow-y to auto, making the inner an unscrollable "
                f"scroll container (the #4856 mobile-transcript collapse). Use "
                f"overflow-x:clip to clip horizontally without coercion. "
                f"Rule: {sel.strip()}"
            )


def test_messages_remains_the_transcript_scroll_container():
    """The transcript's real scroller is .messages: it must keep overflow-y:auto
    and min-height:0 (so it can shrink in the flex column and scroll)."""
    # Find the base `.messages{...}` rule directly (not .messages-inner / variants).
    base = re.search(r"(?<![\w.-])\.messages\{[^}]*\}", CSS)
    assert base, ".messages base rule not found"
    d = _decls(base.group(0))
    assert d.get("overflow-y") == "auto", (
        ".messages must keep overflow-y:auto — it is the transcript scroll "
        "container (#4856)."
    )
    assert d.get("min-height") == "0", (
        ".messages must keep min-height:0 so it can shrink within the flex "
        "column and actually scroll (without it, the column can't scroll) (#4856)."
    )


def test_overflow_x_clip_is_the_chosen_horizontal_clip():
    """Affirm the fix is in place: the mobile .messages-inner clips horizontally
    with `clip` (not `hidden`). Anchors the #4856/#4898 resolution."""
    mobile = re.search(r"@media\(max-width:640px\)\{", CSS)
    assert mobile, "@media(max-width:640px) block not found"
    window = CSS[mobile.start(): mobile.start() + 12000]
    m = re.search(r"\.messages-inner\{([^}]*)\}", window)
    assert m, "mobile .messages-inner rule not found"
    assert "overflow-x:clip" in m.group(0), (
        "mobile .messages-inner must use overflow-x:clip (the #4856 fix)"
    )
