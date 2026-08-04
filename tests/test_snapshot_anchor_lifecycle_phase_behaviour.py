"""Behavioural tests for _sourceEventTypeForSnapshotAnchorRow() in static/messages.js.

Regression cover for the phantom "Compressing context" divider.

``_sourceEventTypeForSnapshotAnchorRow()`` classified a lifecycle row by
``phase = row.phase || row.status``. The compression branches led with a bare
``phase==='running'`` (and ``phase==='done'``/``'completed'``) test, so EVERY
generic in-progress lifecycle row matched before any text check ran. A row of
``{role:'lifecycle', status:'running', text:'Working'}`` — persisted in real
session activity scenes — classified as ``compressing`` and painted a
"Compressing context" divider on turns where no compression happened. The
settled counterpart painted a false "Context auto-compressed" divider.

The pre-existing guard in test_auto_compression_card.py
(``test_snapshot_anchor_hydration_does_not_invent_compressing_rows``) asserts on
source SUBSTRINGS only, so it passed throughout: the phrases it greps for were
all still present. These tests EXECUTE the real function via node instead, which
is the only way to catch a mis-ordered branch. Prefer adding cases here over
extending the substring test.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS_PATH = REPO_ROOT / "static" / "messages.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# Extract the single function under test out of messages.js (which is a browser
# script, not a module) and evaluate it standalone. The helper is self-contained:
# it only reads properties off the row argument, so no DOM/global stubs are
# needed.
_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const name = '_sourceEventTypeForSnapshotAnchorRow';
const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
if (start < 0) throw new Error(name + ' not found in messages.js');
let i = src.indexOf('{', start);
let depth = 1;
i++;
while (depth > 0 && i < src.length) {
  if (src[i] === '{') depth++;
  else if (src[i] === '}') depth--;
  i++;
}
eval(src.slice(start, i));
const row = JSON.parse(process.argv[2]);
process.stdout.write(String(_sourceEventTypeForSnapshotAnchorRow(row)));
"""


def _classify(row: dict) -> str:
    """Run the real JS helper on ``row`` and return its source_event_type."""
    import json

    proc = subprocess.run(
        [NODE, "--eval", _DRIVER_SRC, "--", str(MESSAGES_JS_PATH), json.dumps(row)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    return proc.stdout.strip()


# ── Rows that must NOT paint a compression divider ──────────────────────────


@pytest.mark.parametrize(
    "row,label",
    [
        # The exact shape persisted in real session activity scenes.
        ({"role": "lifecycle", "status": "running", "text": "Working"}, "running/Working"),
        ({"role": "lifecycle", "phase": "running", "text": "Thinking"}, "running/Thinking"),
        ({"role": "lifecycle", "status": "running", "text": ""}, "running/empty text"),
        ({"kind": "lifecycle_status", "status": "running", "label": "Waiting"}, "kind form"),
        # Settled counterparts must not paint "Context auto-compressed".
        ({"role": "lifecycle", "status": "done", "text": "Working"}, "done/Working"),
        ({"role": "lifecycle", "phase": "completed", "text": "Thinking"}, "completed/Thinking"),
        ({"role": "lifecycle", "status": "done", "text": ""}, "done/empty text"),
    ],
)
def test_generic_lifecycle_rows_do_not_invent_compression(row, label):
    """A lifecycle row with no compression cue in its TEXT is not compression.

    ``phase``/``status`` describe whether the row is in progress, not what the
    work is. Only the text identifies compression.
    """
    assert _classify(row) == "", f"{label} must not classify as compression"


# ── Rows that must still paint a compression divider ────────────────────────


@pytest.mark.parametrize(
    "row,expected,label",
    [
        (
            {"role": "lifecycle", "phase": "running", "text": "Compacting context — summarizing earlier conversation"},
            "compressing",
            "agent COMPACTION_STATUS",
        ),
        (
            {"role": "lifecycle", "status": "running", "text": "Pre-API compression: ~900,000 tokens near the limit"},
            "compressing",
            "pre-API compression",
        ),
        (
            {"role": "lifecycle", "status": "running", "text": "Context too large (~1,000,000 tokens) — compressing (1/3)"},
            "compressing",
            "context too large",
        ),
        (
            {"role": "lifecycle", "status": "running", "text": "Preflight compression: ~800,000 tokens >= threshold"},
            "compressing",
            "preflight compression",
        ),
        ({"role": "lifecycle", "phase": "compressing", "text": "anything"}, "compressing", "explicit phase"),
        ({"role": "lifecycle", "status": "done", "text": "Context auto-compressed"}, "compressed", "auto-compressed"),
        ({"role": "lifecycle", "status": "done", "text": "Compression finished"}, "compressed", "finished"),
        ({"role": "lifecycle", "phase": "compressed", "text": "anything"}, "compressed", "explicit phase"),
    ],
)
def test_genuine_compression_rows_still_classify(row, expected, label):
    """Real compression lifecycle rows keep painting the divider."""
    assert _classify(row) == expected, f"{label} must classify as {expected}"


def test_skipping_notice_is_not_a_compression_start():
    """"Skipping preflight compression..." must never look like a live start."""
    row = {"role": "lifecycle", "status": "running", "text": "Skipping preflight compression: cooldown active"}
    assert _classify(row) == ""


# ── Non-lifecycle roles keep their existing mapping ─────────────────────────


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"role": "terminal", "status": "running", "text": "x"}, ""),
        ({"role": "terminal", "status": "error", "text": "x"}, "error"),
        ({"role": "terminal", "status": "cancelled", "text": "x"}, "cancel"),
        ({"role": "terminal", "status": "finished", "text": "x"}, "done"),
        ({"role": "tool", "status": "running"}, "tool"),
        ({"role": "tool", "status": "completed"}, "tool_complete"),
        ({"role": "prose", "text": "hello"}, "token"),
        ({"role": "thinking", "text": "hmm"}, "reasoning"),
    ],
)
def test_non_lifecycle_roles_unchanged(row, expected):
    assert _classify(row) == expected


def test_explicit_source_event_type_wins():
    """An explicit source_event_type is passed through untouched."""
    row = {"role": "lifecycle", "status": "running", "text": "Working", "source_event_type": "token"}
    assert _classify(row) == "token"
