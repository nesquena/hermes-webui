#!/usr/bin/env python3
"""Regression coverage for the browser lifecycle terminal-row parity gate.

The settle-vs-reload comparison used to strip the final line of the rendered
terminal row before diffing.  Because the renderer puts the clock in its own
``.agent-activity-status-time`` element, that made "settled row with clock" and
"reloaded row whose clock was dropped" compare equal, so a user-visible
regression slipped through the gate.

``_assert_terminal_parity`` instead requires:

* both terminal row IDs to be nonempty before comparing them (``rowId`` comes
  from ``row.getAttribute('data-anchor-row-id') || ''``, so two missing IDs
  compared equal would be vacuous), and
* the label text to be equal while the clock is nonempty on BOTH sides -- the
  clock *values* may legitimately differ across a minute boundary.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from browser_conversation_lifecycle import _assert_terminal_parity


TERMINAL_ERROR_TEXT = "Lifecycle gate encountered a terminal-side error."
ROW_ID = "terminal-row-1"


def _terminal_row(*, label, clock, row_id=ROW_ID):
    return {
        "role": "terminal",
        "rowId": row_id,
        "text": f"{label}\n{clock}" if clock else label,
        "label": label,
        "clock": clock,
    }


def test_terminal_parity_accepts_differing_clock_values():
    settled = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:14 PM")]
    reloaded = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:15 PM")]
    _assert_terminal_parity(settled, reloaded)


def test_terminal_parity_rejects_reload_that_dropped_the_clock():
    settled = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:14 PM")]
    reloaded = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="")]
    with pytest.raises(AssertionError) as excinfo:
        _assert_terminal_parity(settled, reloaded)
    assert "reloaded" in str(excinfo.value)


def test_terminal_parity_rejects_settled_that_dropped_the_clock():
    settled = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="")]
    reloaded = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:15 PM")]
    with pytest.raises(AssertionError):
        _assert_terminal_parity(settled, reloaded)


def test_terminal_parity_rejects_changed_label():
    settled = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:14 PM")]
    reloaded = [_terminal_row(label="A different terminal-side error.", clock="9:15 PM")]
    with pytest.raises(AssertionError) as excinfo:
        _assert_terminal_parity(settled, reloaded)
    assert "label" in str(excinfo.value)


def test_terminal_parity_rejects_empty_row_id_on_the_settled_side():
    settled = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:14 PM", row_id="")]
    reloaded = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:15 PM", row_id="")]
    with pytest.raises(AssertionError) as excinfo:
        _assert_terminal_parity(settled, reloaded)
    assert "rowId" in str(excinfo.value)


def test_terminal_parity_rejects_empty_row_id_on_the_reloaded_side():
    settled = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:14 PM", row_id="")]
    reloaded = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:15 PM")]
    with pytest.raises(AssertionError) as excinfo:
        _assert_terminal_parity(settled, reloaded)
    assert "rowId" in str(excinfo.value)


def test_terminal_parity_rejects_mismatched_row_ids():
    settled = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:14 PM", row_id="row-a")]
    reloaded = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:15 PM", row_id="row-b")]
    with pytest.raises(AssertionError) as excinfo:
        _assert_terminal_parity(settled, reloaded)
    assert "rowId" in str(excinfo.value)


def test_terminal_parity_requires_exactly_one_row_on_each_side():
    settled = [
        _terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:14 PM"),
        _terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:14 PM"),
    ]
    reloaded = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:15 PM")]
    with pytest.raises(AssertionError):
        _assert_terminal_parity(settled, reloaded)


def test_terminal_parity_rejects_two_empty_row_ids_comparing_equal():
    # The historical vacuous case: both sides lost the id entirely.
    settled = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:14 PM", row_id="")]
    reloaded = [_terminal_row(label=TERMINAL_ERROR_TEXT, clock="9:14 PM", row_id="")]
    with pytest.raises(AssertionError) as excinfo:
        _assert_terminal_parity(settled, reloaded)
    assert "vacuous" in str(excinfo.value)


# ── Snapshot-body escape guard ─────────────────────────────────────────────
# The browser snapshot is a NON-raw Python triple-quoted string handed to
# ``page.evaluate``. Any ``\n`` written inside it (comments included) is
# decoded by Python into a real newline before Playwright sees it, which
# broke the JS parse and reddened every lifecycle job on the #7792 head.
# These tests pin the evaluated body: it must parse as JavaScript, and it
# must not contain a newline smuggled in from a two-character source escape.

_LIFECYCLE_PY = pathlib.Path(__file__).resolve().parent / "browser_conversation_lifecycle.py"


def _evaluated_activity_snapshot_body() -> str:
    """Return the JS body of ``_activity_snapshot``'s ``page.evaluate`` call,
    decoded exactly once (the same Python decoding ``page.evaluate`` does)."""
    src = _LIFECYCLE_PY.read_text(encoding="utf-8")
    start_marker = "def _activity_snapshot(page) -> dict:"
    start = src.find(start_marker)
    assert start >= 0, "_activity_snapshot not found in browser_conversation_lifecycle.py"
    call = src.find('page.evaluate(', start)
    assert call >= 0, "page.evaluate call not found in _activity_snapshot"
    literal_start = src.find('"""', call)
    assert literal_start >= 0
    literal_start += 3
    literal_end = src.find('"""', literal_start)
    assert literal_end >= 0
    raw = src[literal_start:literal_end]
    # Simulate what Python hands to evaluate: the module compiles the
    # literal, decoding ``\n`` escapes into real newlines. Reproduce that
    # single decode on the extracted slice (without eval of code semantics)
    # by decoding the escape sequences Python would decode.
    return raw.encode("utf-8").decode("unicode_escape")


def test_activity_snapshot_body_has_no_smuggled_newline_in_comments():
    """A source-level ``\\n`` inside the embedded JS becomes a real newline
    after Python decodes the literal, which leaves bare text on its own
    line inside a ``//`` comment context and breaks the parse."""
    body = _evaluated_activity_snapshot_body()
    # The concrete regression: the decoded body must not contain a line that
    # starts with the clock fragment that used to be smuggled out.
    for line in body.split("\n"):
        stripped = line.strip()
        assert not stripped.startswith('9:14 PM"'), (
            "the embedded comment's \\n escape was decoded into a real newline; "
            "write \\\\n in the Python source so evaluate() receives backslash+n"
        )


def test_activity_snapshot_body_parses_as_javascript():
    """The evaluated snapshot body must be syntactically valid JavaScript
    (guards against any future escape/comment breakage in the literal)."""
    body = _evaluated_activity_snapshot_body()
    wrapped = "(function(){" + body + "})"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write("module.exports = " + wrapped + ";\n")
        tmp = f.name
    try:
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"embedded JS failed to parse: {r.stderr}"
    finally:
        os.unlink(tmp)
