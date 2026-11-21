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
against the CSS ``min-height`` (44px -> a ceiling of ``ceil(44)+1 = 45px``)
instead of the composer's natural ONE-ROW height - ``line-height + vertical
padding + borders``.

Which configurations that broke is a property of the stylesheet, so the numbers
below are DERIVED from ``static/style.css`` by ``tests/_composer_metrics.py``
rather than typed into a fixture (an earlier revision of this module hardcoded
the 18px numbers and mislabelled them as the stock font size):

    composer config          one-row box   pre-fix ceiling   skip fired?
    default 16px                   44px            45px        yes (0.6px margin)
    data-font-size=large (18px)    48px            45px        NO - dead code
    data-font-size=xlarge (20px)   51px            45px        NO - dead code
    skin graphite (14px/1.45)      44px            45px        yes

So the fix is font-size independent: it derives the skip ceiling from the same
computed styles the guard already reads, and therefore also covers the smaller
configurations (which only cleared the old ceiling by 0.6px, or purely because
``min-height`` floored the box).

Fix: accept the natural one-row height (computed from line-height + padding +
borders) as the skip ceiling, failing closed to the old min-height-only
behaviour whenever those computed values are not strict px values (so a
percentage / ``normal`` line-height cannot wrongly enable the skip).

These tests exercise the REAL ``autoResize()`` body extracted from messages.js in
a node sandbox whose ``getComputedStyle`` returns the requested composer's real
computed styles. ``test_skip_is_reachable_in_every_composer_configuration`` is
red on the pre-fix tree for the 18px/20px configurations and green after it.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._composer_metrics import (
    COMPOSER_CONFIGS,
    CONFIG_DEFAULT,
    CONFIG_LARGE,
    CONFIG_SKIN,
    CONFIG_SMALL,
    CONFIG_XLARGE,
)

ROOT = Path(__file__).parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _autoresize_body() -> str:
    start = MESSAGES_JS.find("function autoResize(")
    assert start != -1
    end = MESSAGES_JS.find("function scheduleComposerAutoResize(", start)
    assert end > start
    return MESSAGES_JS[start:end]


def _run_autoresize(config, *, value: str, previous_value: str, box_height: int,
                    content_height: int, computed=None):
    """Run the real autoResize() against a faithful textarea stub.

    ``box_height`` is the composer's current offsetHeight and ``content_height``
    the height its content WANTS (one row = the config's one-row box, two rows =
    ``config.content_two_rows``); the stub models a real textarea, whose
    scrollHeight is the box height while the box is taller than the content and
    the content height once the box collapses to ``height:'auto'``.
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
        "natural_row": config.offset_height,
        "computed": json.dumps(config.computed if computed is None else computed),
        "autoresize": body,
    }
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("config", COMPOSER_CONFIGS, ids=lambda c: c.name)
def test_skip_is_reachable_in_every_composer_configuration(config):
    """THE typing-lag regression: at its natural one-row height an append
    keystroke must NOT run the height round trip, in every configuration the
    stylesheet can produce.

    Pre-fix the one-row box was compared against the 44px CSS min-height (a
    45px ceiling). That still cleared the 16px default (44px) by 0.6px, but the
    18px/20px ``data-font-size`` composers sit at 48px/51px, where the skip was
    dead code and every keystroke forced a synchronous full-document reflow.
    """
    out = _run_autoresize(
        config, value="a", previous_value="", box_height=config.offset_height,
        content_height=config.offset_height,
    )
    assert out["writes"] == 0, f"one-row append must skip the resize round trip; got {out}"
    assert out["height"] == config.offset_height, out
    assert out["lastValue"] == "a", out
    assert out["sendUpdates"] == 1, "the primary-button refresh must still run"


def test_the_regression_was_font_size_dependent_not_stock():
    """Grounding for this fix, straight from the stylesheet.

    This is the check the first revision of this module failed: it asserted a
    48px "stock" composer, which is the 18px ``data-font-size=large`` box. The
    real default (16px) already reached the old ceiling, so the fix is about
    making the skip font-size independent - not about a default the reporter
    never ran.
    """
    assert CONFIG_DEFAULT.offset_height <= CONFIG_DEFAULT.old_ceiling, (
        "the stylesheet default is expected to have reached the pre-fix ceiling"
    )
    assert CONFIG_LARGE.offset_height > CONFIG_LARGE.old_ceiling
    assert CONFIG_XLARGE.offset_height > CONFIG_XLARGE.old_ceiling
    # ...and after the fix every configuration's one-row box clears the ceiling,
    # including the two that were dead and the two that were already fine.
    for config in COMPOSER_CONFIGS:
        assert config.skip_is_reachable, f"{config.name} must reach the skip after the fix"
    assert CONFIG_SMALL.offset_height == CONFIG_SMALL.min_height, (
        "the small configuration's box is floored by min-height, not by its text"
    )
    assert CONFIG_SKIN.offset_height == CONFIG_SKIN.min_height


def test_typed_word_appends_skip_in_the_reported_configuration():
    """The reported lag configuration (18px composer) must skip on EVERY append
    keystroke, not just the first: the real typing loop is '' -> 'h' -> 'he' ...
    """
    previous = ""
    for letter in "hello":
        value = previous + letter
        out = _run_autoresize(
            CONFIG_LARGE, value=value, previous_value=previous,
            box_height=CONFIG_LARGE.offset_height,
            content_height=CONFIG_LARGE.offset_height,
        )
        assert out["writes"] == 0, f"append {value!r} must skip; got {out}"
        assert out["lastValue"] == value, out
        previous = value


def test_oversized_composer_still_remeasures_to_its_natural_height():
    """The skip must not preserve an oversized composer (the #5514 invariant).

    A composer several rows tall holding a one-line value remeasures down to one
    row: its offsetHeight is far above the natural one-row ceiling.
    """
    out = _run_autoresize(
        CONFIG_LARGE, value="short prefix", previous_value="short prefi",
        box_height=CONFIG_LARGE.oversized_box, content_height=CONFIG_LARGE.offset_height,
    )
    assert out["writes"] == 2, f"an oversized composer must remeasure; got {out}"
    assert out["height"] == CONFIG_LARGE.offset_height, out


def test_single_line_delete_still_runs_the_height_round_trip():
    """Shrinking values are not append-only and must remeasure."""
    out = _run_autoresize(
        CONFIG_LARGE, value="a", previous_value="hello",
        box_height=CONFIG_LARGE.oversized_box, content_height=CONFIG_LARGE.offset_height,
    )
    assert out["writes"] == 2, f"a delete must remeasure; got {out}"
    assert out["height"] == CONFIG_LARGE.offset_height, out


def test_multi_line_append_still_runs_the_height_round_trip():
    """A newline append is not a one-row append: it must measure and grow."""
    out = _run_autoresize(
        CONFIG_LARGE, value="a\nb", previous_value="a",
        box_height=CONFIG_LARGE.offset_height,
        content_height=CONFIG_LARGE.content_two_rows,
    )
    assert out["writes"] == 2, f"newline growth must remeasure; got {out}"
    assert out["height"] == CONFIG_LARGE.content_two_rows, out


def test_non_px_computed_values_fail_closed_to_min_height_only():
    """Without strict px padding/line-height values the skip falls back to the
    pre-fix min-height-only semantics, i.e. the 18px one-row box remeasures."""
    out = _run_autoresize(
        CONFIG_LARGE, value="a", previous_value="",
        box_height=CONFIG_LARGE.offset_height,
        content_height=CONFIG_LARGE.offset_height,
        computed={"minHeight": "44px"},
    )
    assert out["writes"] == 2, f"must fail closed to the full resize; got {out}"


def test_percentage_line_height_fails_closed():
    """A percentage line-height is not a strict px value and must not enable the
    skip (mirrors the #6349 min-height gate)."""
    out = _run_autoresize(
        CONFIG_LARGE, value="a", previous_value="",
        box_height=CONFIG_LARGE.offset_height,
        content_height=CONFIG_LARGE.offset_height,
        computed={**CONFIG_LARGE.computed, "lineHeight": "165%"},
    )
    assert out["writes"] == 2, f"percentage line-height must fail closed; got {out}"


def test_percentage_min_height_fails_closed():
    """A percentage min-height must not be read as a pixel number by
    ``parseFloat`` (``parseFloat('50%') === 50``) - the #6349 property, still in
    force after this change: no strict px min-height means no reachable ceiling."""
    out = _run_autoresize(
        CONFIG_DEFAULT, value="a", previous_value="",
        box_height=CONFIG_DEFAULT.offset_height,
        content_height=CONFIG_DEFAULT.offset_height,
        computed={**CONFIG_DEFAULT.computed, "minHeight": "50%"},
    )
    assert out["writes"] == 2, f"percentage min-height must fail closed; got {out}"
