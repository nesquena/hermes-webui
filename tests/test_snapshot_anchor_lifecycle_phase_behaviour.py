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


# A row that already carries a canonical ``source_event_type`` short-circuits
# at the top of _sourceEventTypeForSnapshotAnchorRow() — this is the shape the
# live SSE path journals for a real compression envelope. A bare
# phase-only row with NO canonical type is exactly the untrustworthy shape the
# phantom-divider bug relied on (see the negative parametrization above), so
# only rows with an explicit canonical source_event_type OR positive text cues
# are legitimate compression rows.
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
            {"role": "lifecycle", "source_event_type": "compressing", "text": "anything"},
            "compressing",
            "canonical source_event_type",
        ),
        ({"role": "lifecycle", "status": "done", "text": "Context auto-compressed"}, "compressed", "auto-compressed"),
        ({"role": "lifecycle", "status": "done", "text": "Compression finished"}, "compressed", "finished"),
        (
            {"role": "lifecycle", "source_event_type": "compressed", "text": "anything"},
            "compressed",
            "canonical source_event_type",
        ),
    ],
)
def test_genuine_compression_rows_still_classify(row, expected, label):
    """Real compression lifecycle rows keep painting the divider.

    Compression identity comes from either a canonical ``source_event_type``
    (the live SSE path stamps it there when the compressing/compressed event
    is journaled) or from a positive text cue that mirrors the Python
    authority ``_is_agent_compression_start_status()``. A bare ``phase``
    with no canonical source is not evidence — a generic in-progress
    lifecycle row also carries ``phase='running'``, and the phantom-divider
    bug this test file exists to lock out was exactly that misread.
    """
    assert _classify(row) == expected, f"{label} must classify as {expected}"


def test_phase_only_compressing_row_is_not_a_compression_start():
    """A bare ``phase='compressing'`` with no canonical source_event_type is
    not evidence that compression started.

    ``_is_agent_compression_start_status()`` in api/streaming.py rejects any
    row that lacks a canonical envelope; the JS snapshot classifier makes
    the same call for the START marker. A genuine start arrives with a
    canonical ``source_event_type`` and returns at the top of the classifier
    before any phase inference — so reaching the phase branch means the row
    carried no canonical type, and phase alone must not paint a
    "Compressing context" divider.
    """
    assert _classify({"role": "lifecycle", "phase": "compressing", "text": "anything"}) == ""


def test_skipping_notice_is_not_a_compression_start():
    """"Skipping preflight compression..." must never look like a live start."""
    row = {"role": "lifecycle", "status": "running", "text": "Skipping preflight compression: cooldown active"}
    assert _classify(row) == ""


def test_preflight_only_row_is_not_a_compression_start():
    """Preflight announces intent, not that compaction ran.

    ``_is_agent_compression_start_status()`` in api/streaming.py excludes
    preflight on purpose: the later authoritative "Compacting context" marker is
    the signal that compression actually proceeded. The client snapshot path must
    make the same call, or a preflight-only turn paints a divider on replay that
    the live SSE path never painted.
    """
    row = {
        "role": "lifecycle",
        "status": "running",
        "text": "📦 Preflight compression: ~101,000 tokens >= 96,000 threshold. This may take a moment.",
    }
    assert _classify(row) == ""


def test_js_compression_cues_match_python_contract():
    """The JS positive cue set must not drift from the Python authority."""
    streaming_src = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    contract = streaming_src.split("def _is_agent_compression_start_status")[1].split("\ndef ")[0]
    # Every text cue the Python contract accepts, phrased as an agent emitter
    # would, must classify as a compression start on the snapshot path too.
    for cue, sample in (
        ("compacting context", "Compacting context — summarizing earlier conversation"),
        ("pre-api compression:", "Pre-API compression: ~900,000 tokens near the limit"),
        ("context too large", "Context too large (~1,000,000 tokens) — compressing (1/3)"),
        ("compression attempt", "Compression attempt 2 of 3"),
    ):
        assert cue in contract, f"{cue!r} vanished from the Python contract"
        assert _classify({"role": "lifecycle", "status": "running", "text": sample}) == "compressing", (
            f"JS lost the {cue!r} cue that api/streaming.py still accepts"
        )
    # Preflight is excluded on both sides.
    assert "intentionally excluded" in contract


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


# ── Live/replay parity: real producer -> real JS classifier ─────────────────


def _journal_scene_rows(tmp_path, monkeypatch, events):
    """Write ``events`` with the real journal writer, return the real scene rows."""
    from api import models, routes
    from api.run_journal import RunJournalWriter

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)

    session_id = "compressparity"
    stream_id = "stream-compress-parity"
    writer = RunJournalWriter(session_id, stream_id, session_dir=session_dir)
    for name, payload in events:
        writer.append_sse_event(name, payload)

    snapshot = routes._run_journal_live_snapshot(stream_id)
    assert snapshot is not None
    scene = snapshot["anchor_activity_scene"]
    assert scene["version"] == "activity_scene_v1"
    return scene["activity_rows"], stream_id


def test_journaled_compression_events_survive_snapshot_replay(tmp_path, monkeypatch):
    """Canonical compressing/compressed journal events must reach the scene.

    The live SSE path paints the divider from these very events. If the snapshot
    producer drops them, a genuinely-compressed turn loses its divider on
    session-switch replay — the mirror image of the phantom-card bug.
    """
    rows, stream_id = _journal_scene_rows(
        tmp_path,
        monkeypatch,
        [
            ("token", {"text": "before compression"}),
            ("compressing", {"session_id": "compressparity", "message": "Compressing context"}),
            ("compressed", {"session_id": "compressparity", "message": "Compression finished"}),
            ("token", {"text": " after compression"}),
        ],
    )

    # Order relative to prose is preserved, and the prose runs stay split.
    assert [(r["role"], r["source_event_type"]) for r in rows] == [
        ("prose", "token"),
        ("lifecycle", "compressing"),
        ("lifecycle", "compressed"),
        ("prose", "token"),
    ]

    running, done = rows[1], rows[2]
    assert (running["status"], done["status"]) == ("running", "completed")
    assert running["kind"] == done["kind"] == "lifecycle_status"
    # Quiet lifecycle display hints.
    for row in (running, done):
        assert row["display_hint"] == "quiet_lifecycle_row"
        assert row["display_hints"]["compact_worklog"] == "quiet_lifecycle_row"
        assert row["display_hints"]["transparent_stream"] == "chronological_activity"
        # Event-envelope identity, not a synthesised guess.
        assert row["event_id"] == f"{stream_id}:{row['seq']}"
        assert row["identity"]["event_id"] == row["event_id"]
        assert row["run_id"] == row["identity"]["run_id"] == stream_id
        assert row["stream_id"] == stream_id
    assert running["seq"] < done["seq"]

    # Live/replay parity: the real JS classifier round-trips each row back to the
    # canonical source_event_type the live path used.
    assert _classify(running) == "compressing"
    assert _classify(done) == "compressed"


def test_journaled_compression_preserves_reasoning_order(tmp_path, monkeypatch):
    """Reasoning on both sides of compression remains chronologically split."""
    rows, _ = _journal_scene_rows(
        tmp_path,
        monkeypatch,
        [
            ("reasoning", {"text": "reasoning before"}),
            ("compressing", {"message": "Compressing context"}),
            ("compressed", {"message": "Compression finished"}),
            ("reasoning", {"text": "reasoning after"}),
        ],
    )

    assert [(row["role"], row["source_event_type"], row["text"]) for row in rows] == [
        ("thinking", "reasoning", "reasoning before"),
        ("lifecycle", "compressing", "Compressing context"),
        ("lifecycle", "compressed", "Compression finished"),
        ("thinking", "reasoning", "reasoning after"),
    ]


def test_reasoning_after_compression_stays_after_compression(tmp_path, monkeypatch):
    """Future reasoning must not flush ahead of a pending lifecycle boundary."""
    rows, _ = _journal_scene_rows(
        tmp_path,
        monkeypatch,
        [
            ("token", {"text": "progress"}),
            ("compressing", {"message": "Compressing context"}),
            ("reasoning", {"text": "reasoning after compression"}),
            ("tool", {"name": "next", "id": "tool-next"}),
        ],
    )

    assert [(row["role"], row["source_event_type"]) for row in rows] == [
        ("prose", "token"),
        ("lifecycle", "compressing"),
        ("thinking", "reasoning"),
        ("tool", "tool"),
    ]


def test_journaled_compression_preserves_tool_order(tmp_path, monkeypatch):
    """A canonical lifecycle boundary between tools stays between those tools."""
    rows, _ = _journal_scene_rows(
        tmp_path,
        monkeypatch,
        [
            ("tool", {"name": "first", "id": "tool-1"}),
            ("compressing", {"message": "Compressing context"}),
            ("tool", {"name": "second", "id": "tool-2"}),
        ],
    )

    assert [(row["role"], row["source_event_type"]) for row in rows] == [
        ("tool", "tool"),
        ("lifecycle", "compressing"),
        ("tool", "tool"),
    ]
    assert [rows[0]["tool"]["name"], rows[2]["tool"]["name"]] == ["first", "second"]


def test_generic_working_shell_row_replays_as_non_compression(tmp_path, monkeypatch):
    """The journal's own 'Working' shell must never replay as compression."""
    rows, _ = _journal_scene_rows(
        tmp_path,
        monkeypatch,
        [("state_saved", {"ok": True})],
    )

    assert len(rows) == 1
    shell = rows[0]
    assert shell["role"] == "lifecycle"
    assert shell["text"] == "Working"
    # Producer keeps the non-canonical marker; classifier ignores it and finds no cue.
    assert shell["source_event_type"] == "runtime_journal_snapshot"
    assert _classify(shell) == ""
