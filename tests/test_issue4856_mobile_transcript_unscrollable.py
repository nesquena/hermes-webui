"""Regression test for #4856: mobile transcript collapses and won't scroll.

Root cause (proven empirically via headless Chrome mobile emulation):

  The mobile `.messages-inner` rule carried `overflow-x:hidden` (added in the
  #4583/#4584 batch, v0.51.576 — the user's bisected "first broken" version).
  Per the CSS overflow spec, when one axis is `hidden` and the other is
  `visible`, the `visible` axis is coerced to `auto`. So `overflow-x:hidden`
  silently turned `overflow-y` into `auto`, making `.messages-inner` a SCROLL
  CONTAINER.

  A scroll container's `min-height:auto` resolves to `0` (not `min-content`).
  `.messages-inner` is a flex item (default `flex-shrink:1`) inside the
  `.messages` column flexbox, so with min-height 0 it collapsed to the
  scroller's height (~681px) instead of growing to its content (~8251px). The
  transcript then overflowed the INNER (which clipped it) rather than the
  `.messages` scroller — so the scroller had nothing to scroll, `scrollTop`
  was pinned at 0, and every open/send/receive left the reader stuck at the
  top with no way to scroll down. Desktop escaped the collapse; mobile didn't.

Fix: `overflow-x:clip` clips horizontal overflow (the original #4553/#4583
intent) WITHOUT coercing `overflow-y` or creating a scroll container, so
`min-height:auto` stays `min-content` and the inner grows to its full height.

These are source-level guards; the runtime collapse is layout/viewport
specific and not reproducible in headless CI without a full browser.
"""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(encoding="utf-8")


def _mobile_messages_inner_rule() -> str:
    media = re.search(r"@media\(max-width:640px\)\{", CSS)
    assert media, "@media(max-width:640px) block not found"
    window = CSS[media.start(): media.start() + 8000]
    m = re.search(r"\.messages-inner\{([^}]*)\}", window)
    assert m, ".messages-inner rule not found in mobile media block"
    return m.group(0)


def test_mobile_inner_does_not_become_scroll_container():
    """overflow-x:hidden coerces overflow-y:auto, making the inner a scroll
    container whose min-height:auto resolves to 0 — the #4856 collapse. The
    mobile inner must use overflow-x:clip instead."""
    rule = _mobile_messages_inner_rule()
    assert "overflow-x:clip" in rule, (
        "mobile .messages-inner must use overflow-x:clip so it clips horizontal "
        "overflow without becoming an unscrollable scroll container (#4856)"
    )
    assert "overflow-x:hidden" not in rule, (
        "mobile .messages-inner must NOT use overflow-x:hidden — it coerces "
        "overflow-y to auto and collapses the transcript (#4856 regression)"
    )


def test_mobile_inner_must_not_set_overflow_y_auto_or_scroll():
    """Belt-and-suspenders: an explicit overflow-y:auto/scroll on the inner
    would reintroduce the scroll-container collapse even with overflow-x:clip."""
    rule = _mobile_messages_inner_rule()
    assert "overflow-y:auto" not in rule and "overflow-y:scroll" not in rule, (
        "mobile .messages-inner must not be a scroll container on the Y axis "
        "(would re-trigger the #4856 min-height:0 flex collapse)"
    )


def test_messages_scroller_keeps_overflow_y_auto():
    """The actual scroller is .messages, not .messages-inner — confirm it
    still owns vertical scrolling so the transcript scrolls there."""
    m = re.search(r"\.messages\{[^}]*\}", CSS)
    assert m, ".messages rule not found"
    assert "overflow-y:auto" in m.group(0), (
        ".messages must remain the overflow-y:auto scroll container (#4856)"
    )
