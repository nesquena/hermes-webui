"""Regression tests for #5637: mobile scroll jump-back (two linked causes).

Real-device instrumentation (Android Chrome) traced the mobile scroll
jump-back to two causes:

1. Primary (CSS): the ``@media (pointer: coarse)`` block set a single flat
   ``content-visibility: auto; contain-intrinsic-size: auto 1px`` on every
   ``.msg-row``. Off-screen assistant rows (which can be multi-thousand px:
   tool-result / long-answer) then reserved only ~1px each, so ``scrollHeight``
   collapsed by tens of thousands of px on a long transcript, the browser
   force-clamped ``scrollTop``, and the viewport jumped to the top. A flat
   value cannot square "reserve tall rows" with "keep scrollHeight stable for
   iOS flick momentum" (the reason 1px was chosen), so content-visibility is
   now gated to the short, size-predictable user rows only; assistant rows keep
   their real rendered height.

2. Secondary (JS): ``_restoreMessageScrollSnapshotSameFrame`` fell back to an
   ABSOLUTE ``snapshot.top`` when the semantic anchor restore failed. During
   streaming, the live activity-scene refresh fires this every tick; for a
   reader scrolled up into history (unpinned), snapping to a stale absolute top
   nudges the viewport backward by an amount that grows with ``scrollHeight``.
   The fix holds position (no scrollTop write) for the unpinned/anchor-failed
   case and lets the browser's own scroll anchoring keep the reader put.

Both tests fail on the pre-fix tree and pass only on the fixed tree.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _coarse_pointer_block() -> str:
    """Return the @media (pointer: coarse) block body from style.css."""
    anchor = "@media (pointer: coarse)"
    start = STYLE_CSS.index(anchor)
    brace = STYLE_CSS.index("{", start)
    depth = 0
    for i in range(brace, len(STYLE_CSS)):
        ch = STYLE_CSS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return STYLE_CSS[brace : i + 1]
    raise AssertionError("@media (pointer: coarse) block not found")


def _restore_same_frame_fn() -> str:
    """Return the _restoreMessageScrollSnapshotSameFrame function body."""
    anchor = "function _restoreMessageScrollSnapshotSameFrame"
    start = UI_JS.index(anchor)
    brace = UI_JS.index("{", start)
    depth = 0
    for i in range(brace, len(UI_JS)):
        ch = UI_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[start : i + 1]
    raise AssertionError("_restoreMessageScrollSnapshotSameFrame not found")


def test_content_visibility_not_flat_on_all_msg_rows():
    """content-visibility:auto must NOT be applied to a bare .msg-row selector.

    A flat `.msg-row { content-visibility: auto; contain-intrinsic-size: auto 1px }`
    collapses tall assistant rows' off-screen height and drives the scrollHeight
    clamp jump-back (#5637). The rule must be scoped so tall assistant rows are
    not off-screen-skipped with a tiny flat estimate.
    """
    block = _coarse_pointer_block()
    # The exact pre-fix line: content-visibility on an unqualified .msg-row.
    assert ".msg-row { content-visibility: auto; contain-intrinsic-size: auto 1px" not in block, (
        "Flat content-visibility on every .msg-row is the #5637 primary cause; "
        "it must be scoped away from tall (assistant) rows."
    )


def test_content_visibility_scoped_to_user_rows():
    """content-visibility should be retained for the short user rows only."""
    block = _coarse_pointer_block()
    assert 'content-visibility: auto' in block, (
        "The iOS off-screen-skip optimization should still apply to short rows."
    )
    assert '.msg-row[data-role="user"]' in block, (
        "content-visibility:auto must be scoped to user rows (size-predictable), "
        "not applied flat to assistant rows that can be multi-thousand px (#5637)."
    )


def test_restore_same_frame_holds_position_for_unpinned_reader():
    """The absolute-top fallback must be skipped for an unpinned reader.

    When the reader is scrolled up (userUnpinned) and the anchor restore failed,
    _restoreMessageScrollSnapshotSameFrame must NOT write an absolute snapshot.top
    (it goes stale and nudges the viewport backward as scrollHeight grows, #5637).
    It should hold position and return before the el.scrollTop write.
    """
    fn = _restore_same_frame_fn()
    assert "snapshot.userUnpinned===true&&snapshot.pinned!==true" in fn, (
        "The fallback must guard the unpinned/anchor-failed case (#5637)."
    )
    # The guard must sit BEFORE the absolute-top scrollTop write and short-circuit it.
    guard_idx = fn.index("snapshot.userUnpinned===true&&snapshot.pinned!==true")
    # the absolute-top write in the fallback
    write_idx = fn.index("el.scrollTop=Math.max(0,Math.min(target,maxTop))")
    assert guard_idx < write_idx, (
        "The unpinned guard must precede (and return before) the absolute-top "
        "scrollTop write so it is never reached for an unpinned reader (#5637)."
    )
    # Between the guard and the write there must be an early return.
    between = fn[guard_idx:write_idx]
    assert "return;" in between, (
        "The unpinned guard must `return` before the absolute-top write (#5637)."
    )
