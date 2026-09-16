"""Regression coverage for composer typing lag in long chat sessions.

Symptom: in a long session, typing becomes laggy - keystrokes echo with a
visible delay. Measured on a 758-message / 5 MB session (31k DOM nodes
rendered), with the browser's own Event Timing API (input -> next paint) at 6x
CPU throttle:

    transcript rendered        keystroke -> echo (median)
    full (36 rows) @6x         168ms  ->  136ms with this fix
    windowed (11 rows) @6x      96ms  ->   72ms with this fix
    floor (transcript detached) 72ms

Root cause: ``autoResize()`` in ``static/messages.js`` has a single-row fast path
that must skip the ``height:'auto'`` -> read ``scrollHeight`` -> restore round
trip. That round trip forces a SYNCHRONOUS layout of the whole document (the
transcript included), so its cost grows with the rendered transcript - exactly
the reported symptom. The guard that gates the skip compared ``el.offsetHeight``
against the CSS ``min-height``, but the composer's natural ONE-ROW height is
``line-height + vertical padding + borders`` ~48px while the CSS min-height is
44px. ``48 <= ceil(44)+1`` is false, so the skip never fired: every append
keystroke did the full round trip (measured in the live app: 2 ``style.height``
writes and the slow path on 8/8 keystrokes; 16 style writes per 8 keystrokes
before, 2 after).

Fix: accept the natural one-row height (computed from line-height + padding +
borders) as the skip ceiling, failing closed to the old min-height-only
behaviour whenever those computed values are not strict px values (so a
percentage / ``normal`` line-height cannot wrongly enable the skip).

These tests exercise the REAL ``autoResize()`` body extracted from messages.js in
a node sandbox whose ``getComputedStyle`` returns the composer's real computed
styles. ``test_natural_one_row_append_skips_the_height_round_trip`` is red on the
pre-fix tree and green after it.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")

# The composer's real computed styles (see ``textarea#msg`` in static/style.css):
# line-height 1.65 -> 29.7px, padding 12px/6px, no border, min-height 44px. The
# natural one-row border box is therefore 29.7+12+6 = 47.7 -> 48px.
COMPOSER_STYLE = {
    "minHeight": "44px",
    "lineHeight": "29.7px",
    "paddingTop": "12px",
    "paddingBottom": "6px",
    "borderTopWidth": "0px",
    "borderBottomWidth": "0px",
    "fontSize": "18px",
}
NATURAL_ROW = 48


def _autoresize_body() -> str:
    start = MESSAGES_JS.find("function autoResize(")
    assert start != -1
    end = MESSAGES_JS.find("function scheduleComposerAutoResize(", start)
    assert end > start
    return MESSAGES_JS[start:end]


def _run_autoresize(*, value: str, previous_value: str, box_height: int,
                    content_height: int, computed=None):
    """Run the real autoResize() against a faithful textarea stub.

    ``content_height`` is the height the content WANTS (1 row = 48px, 2 rows =
    68px); the stub models a real textarea, whose scrollHeight is the box height
    while the box is taller than the content and the content height once the box
    collapses to ``height:'auto'`` (one row).
    """
    node = shutil.which("node")
    assert node, "node is required for the autoResize harness"
    body = _autoresize_body()
    harness = textwrap.dedent(
        """
        let _composerAutoResizeRaf = 0;
        let _composerLastResizeValue = %(previous_value)r;
        let writes = 0, height = %(box_height)s;
        const NATURAL_ROW = %(natural_row)s;
        const CONTENT_H = %(content_height)s;
        const msg = {
          value: %(value)r,
          get offsetHeight() { return height; },
          get scrollHeight() { return height > NATURAL_ROW ? height : CONTENT_H; },
          style: {
            set height(v) { writes += 1; height = v === 'auto' ? NATURAL_ROW : parseInt(v, 10); },
            get height() { return height + 'px'; },
          },
        };
        const messages = { scrollTop: 0 };
        const $ = (id) => id === 'msg' ? msg : id === 'messages' ? messages : null;
        const COMPUTED = %(computed)s;
        function getComputedStyle() { return COMPUTED; }
        let sendUpdates = 0;
        function updateSendBtn() { sendUpdates += 1; }
        function _repinMessagesAfterComposerResize() {}
        %(autoresize)s
        autoResize();
        console.log(JSON.stringify({ writes, height, lastValue: _composerLastResizeValue, sendUpdates }));
        """
    ) % {
        "previous_value": previous_value,
        "value": value,
        "box_height": box_height,
        "content_height": content_height,
        "natural_row": NATURAL_ROW,
        "computed": json.dumps(computed if computed is not None else COMPOSER_STYLE),
        "autoresize": body,
    }
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_natural_one_row_append_skips_the_height_round_trip():
    """THE typing-lag regression: at the natural one-row height (48px) an append
    keystroke must NOT run the height round trip.

    Pre-fix the 48px offsetHeight was compared against the 44px CSS min-height
    (48 <= 45 is false), so every keystroke wrote style.height twice and forced a
    synchronous full-document reflow - the long-session lag.
    """
    out = _run_autoresize(
        value="a", previous_value="", box_height=NATURAL_ROW,
        content_height=NATURAL_ROW,
    )
    assert out["writes"] == 0, f"one-row append must skip the resize round trip; got {out}"
    assert out["height"] == NATURAL_ROW, out
    assert out["lastValue"] == "a", out
    assert out["sendUpdates"] == 1, "the primary-button refresh must still run"


def test_oversized_composer_still_remeasures_to_its_natural_height():
    """The skip must not preserve an oversized composer (the #5514 invariant).

    A three-row box holding a one-line value remeasures down to one row: its
    offsetHeight (176px) is far above the natural one-row ceiling (48px).
    """
    out = _run_autoresize(
        value="short prefix", previous_value="short prefi", box_height=176,
        content_height=NATURAL_ROW,
    )
    assert out["writes"] == 2, f"an oversized composer must remeasure; got {out}"
    assert out["height"] == NATURAL_ROW, out


def test_single_line_delete_still_runs_the_height_round_trip():
    """Shrinking values are not append-only and must remeasure."""
    out = _run_autoresize(
        value="a", previous_value="hello", box_height=100,
        content_height=NATURAL_ROW,
    )
    assert out["writes"] == 2, f"a delete must remeasure; got {out}"
    assert out["height"] == NATURAL_ROW, out


def test_multi_line_append_still_runs_the_height_round_trip():
    """A newline append is not a one-row append: it must measure and grow."""
    out = _run_autoresize(
        value="a\nb", previous_value="a", box_height=NATURAL_ROW,
        content_height=68,
    )
    assert out["writes"] == 2, f"newline growth must remeasure; got {out}"
    assert out["height"] == 68, out


def test_non_px_line_height_fails_closed_to_min_height_only():
    """Without strict px padding/line-height values the skip falls back to the
    pre-fix min-height-only semantics (a 48px box over a 44px min remeasures)."""
    out = _run_autoresize(
        value="a", previous_value="", box_height=NATURAL_ROW,
        content_height=NATURAL_ROW, computed={"minHeight": "44px"},
    )
    assert out["writes"] == 2, f"must fail closed to the full resize; got {out}"


def test_percentage_line_height_fails_closed():
    """A percentage line-height is not a strict px value and must not enable the
    skip (mirrors the #6349 min-height gate)."""
    out = _run_autoresize(
        value="a", previous_value="", box_height=NATURAL_ROW,
        content_height=NATURAL_ROW,
        computed={**COMPOSER_STYLE, "lineHeight": "165%"},
    )
    assert out["writes"] == 2, f"percentage line-height must fail closed; got {out}"


def test_fix_is_pinned_in_source():
    """Pin the guard shape so a refactor cannot silently drop the natural-row
    ceiling (and with it the typing fix) again."""
    body = _autoresize_body()
    assert "const _composerPx=(raw)=>" in body
    assert "const _minHeightRaw=_composerStyle?_composerStyle.minHeight:'';" in body
    assert "const _minHeight=_composerPx(_minHeightRaw);" in body
    assert "_lineHeight+(_composerPx(_composerStyle.paddingTop)||0)" in body
    assert "const _rowCeiling=Number.isFinite(_naturalRowHeight)&&Number.isFinite(_minHeight)?Math.max(_minHeight,_naturalRowHeight):_minHeight;" in body
    assert "const _isAtMinimumHeight=Number.isFinite(_rowCeiling)&&el.offsetHeight<=Math.ceil(_rowCeiling)+1;" in body
    assert "if(_isAppendOnly&&_fitsCurrentHeight&&_isAtMinimumHeight){" in body
    # The strict px gate is still in force (no lax parseFloat of a percentage).
    assert "parseFloat(getComputedStyle(el).minHeight)" not in body
