"""#7653 re-gate round 14 — the recovery-metadata starvation (item 4).

The 10-19 re-gate (item 4) found both live-metadata consumers starved of
their own row::

    Settled rows are [old@1, new@4], and the live mirror holds only `new`
    (duration 8, started_at 2000). When `new` is processed, its own row is
    already used, so it gets nothing. PR: `new` gets `null/null` in both
    consumers.

Root cause: the pairing ran per row in order, so an earlier settled row
whose id has no live row (or no occurrence-exact match) consumed the later
row's own live row through the NAME fallback. The reviewer's fix: reserve
every explicit id/occurrence match across ALL settled rows FIRST, then run
the name fallback over the live rows that are left.

This file drives the REAL ``_mergeSettledToolCallsWithLiveMetadata``
(static/messages.js) and the REAL reservation helper +
``copyLiveToolMetadata`` (static/ui.js) with the reviewer's shape.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def _function_block(src: str, header: str) -> str:
    start = src.find(header)
    assert start != -1, f"{header!r} not found"
    brace = src.find("{", start)
    assert brace != -1, f"no body for {header!r}"
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError(f"{header!r} did not close")


def _run_node(script: str, payload: dict) -> dict:
    assert NODE, "node not on PATH"
    harness = (
        "var PAYLOAD = JSON.parse(require('fs').readFileSync(0, 'utf8') || '{}');\n"
        + script
    )
    result = subprocess.run(
        [NODE, "-e", harness],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "NODE_CHANNEL_FD": ""},
    )
    # node crashes with the Hermes-injected NODE_CHANNEL_FD; strip it.
    if result.returncode != 0 and "NODE_CHANNEL_FD" not in result.stderr:
        pass
    assert result.returncode == 0, f"node failed:\n{result.stderr[:800]}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# messages.js consumer — the real _mergeSettledToolCallsWithLiveMetadata.
# ---------------------------------------------------------------------------


def _merge_slice(raw_calls, live):
    fn = _function_block(
        _read("static/messages.js"),
        "function _mergeSettledToolCallsWithLiveMetadata(",
    )
    script = f"""
const S = {{ toolCalls: PAYLOAD.live }};
{fn}
process.stdout.write(JSON.stringify(_mergeSettledToolCallsWithLiveMetadata(PAYLOAD.rawCalls)));
"""
    return _run_node(script, {"rawCalls": raw_calls, "live": live})


# The reviewer's starvation shape: two DISTINCT ids, both terminal ``bash``
# calls; the live mirror holds ONLY the newer row, stream-born (no
# ``assistant_msg_idx``), so occurrence-exact fails even for its own id.
_STARVED_RAW = [
    {"name": "bash", "tid": "call_1", "assistant_msg_idx": 1},
    {"name": "bash", "tid": "call_2", "assistant_msg_idx": 4},
]
_STARVED_LIVE = [
    {"name": "bash", "tid": "call_2", "duration": 8.0, "started_at": 2000},
]


def test_messages_merge_current_row_keeps_its_own_live_metadata():
    """The reviewer's item-4 repro: with the mirror holding only ``new``'s
    row, ``new`` must still receive its own duration/started_at and ``old``
    must not steal it through the name fallback."""
    out = _merge_slice(_STARVED_RAW, _STARVED_LIVE)
    by_tid = {r.get("tid"): r for r in out}
    assert by_tid["call_2"].get("duration") == 8.0, (
        "the current call was starved of its own live row — the name "
        f"fallback consumed it first: {by_tid['call_2']!r}"
    )
    assert by_tid["call_2"].get("started_at") == 2000
    assert by_tid["call_1"].get("duration") is None, (
        f"the older call must not borrow the newer row's metadata: "
        f"{by_tid['call_1']!r}"
    )


def test_messages_merge_metadata_reservation_never_transfers_a_verdict():
    """The metadata reservation is presentation-only: an OWNED row whose
    occurrence-exact match failed takes duration/started_at but NOT the live
    row's failure verdict (round-12 pin, unchanged)."""
    live = [
        {"name": "bash", "tid": "call_2", "duration": 8.0,
         "started_at": 2000, "is_error": True},
    ]
    out = _merge_slice(_STARVED_RAW, live)
    by_tid = {r.get("tid"): r for r in out}
    assert by_tid["call_2"].get("duration") == 8.0
    assert by_tid["call_2"].get("is_error") is not True, (
        "the loosened id-own metadata match repainted the row failed — "
        "verdict transfer stays occurrence-scoped"
    )
    assert by_tid["call_1"].get("is_error") is not True


def test_messages_merge_both_rows_present_is_unchanged():
    """Control: the round-12 shape (both live rows present) keeps each row's
    own metadata — the two-pass reservation did not regress it."""
    live = [
        {"name": "bash", "tid": "call_2", "duration": 8.0, "started_at": 2000},
        {"name": "bash", "tid": "call_1", "duration": 5.0, "started_at": 1000},
    ]
    out = _merge_slice(_STARVED_RAW, live)
    by_tid = {r.get("tid"): r for r in out}
    assert by_tid["call_2"].get("duration") == 8.0
    assert by_tid["call_1"].get("duration") == 5.0


# ---------------------------------------------------------------------------
# ui.js consumer — the real reservation helper + copyLiveToolMetadata.
# ---------------------------------------------------------------------------


def _run_ui(candidates, by_tid, live):
    reserve_fn = _function_block(
        _read("static/ui.js"), "function _reserveLiveToolMetadataForCandidates(",
    )
    copy_fn = _function_block(_read("static/ui.js"), "const copyLiveToolMetadata=")
    script = f"""
const _persistedIsErrorByTid = {{}};
const liveMetadataByTid = new Map(Object.entries(PAYLOAD.byTid));
const usedLiveToolMetadata = new Set();
const liveToolMetadata = PAYLOAD.live;
{reserve_fn}
{copy_fn}
const _cands = PAYLOAD.candidates;
const _reserved = _reserveLiveToolMetadataForCandidates(_cands, liveMetadataByTid, usedLiveToolMetadata);
const out = _cands.map(c => copyLiveToolMetadata(
  Object.assign({{}}, c.next), c.name, c.tid, _reserved.get(c) || null));
process.stdout.write(JSON.stringify(out));
"""
    return _run_node(script, {"candidates": candidates, "byTid": by_tid, "live": live})


def test_ui_consumer_current_row_keeps_its_own_live_metadata():
    """The reviewer's item-4 repro through the render consumer: the mirror
    holds only ``new``'s row, so ``new`` must be reserved it before the name
    fallback can hand it to ``old``."""
    out = _run_ui(
        candidates=[
            {"next": {"name": "bash", "tid": "call_1",
                      "assistant_msg_idx": 1, "done": True}, "name": "bash",
             "tid": "call_1"},
            {"next": {"name": "bash", "tid": "call_2",
                      "assistant_msg_idx": 4, "done": True}, "name": "bash",
             "tid": "call_2"},
        ],
        by_tid={"call_2": [{"tc": {"name": "bash", "tid": "call_2",
                                   "duration": 8.0, "started_at": 2000},
                            "idx": 0}]},
        live=[{"name": "bash", "tid": "call_2", "duration": 8.0,
               "started_at": 2000}],
    )
    by_tid = {r.get("tid"): r for r in out}
    assert by_tid["call_2"].get("duration") == 8.0, (
        "ui.js consumer starved the current row of its own live metadata: "
        f"{by_tid['call_2']!r}"
    )
    assert by_tid["call_2"].get("started_at") == 2000
    assert by_tid["call_1"].get("duration") is None, (
        f"the older row borrowed the newer row's metadata: {by_tid['call_1']!r}"
    )


def test_ui_consumer_occurrence_exact_still_transfers_the_verdict():
    """Control: when both sides name the owning assistant index the pairing
    is occurrence-exact and the verdict transfer still runs (the two-pass
    reservation did not close the exact-match verdict path)."""
    out = _run_ui(
        candidates=[
            {"next": {"name": "bash", "tid": "call_1",
                      "assistant_msg_idx": 1, "done": True}, "name": "bash",
             "tid": "call_1"},
            {"next": {"name": "bash", "tid": "call_2",
                      "assistant_msg_idx": 4, "done": True}, "name": "bash",
             "tid": "call_2"},
        ],
        by_tid={
            "call_1": [{"tc": {"name": "bash", "tid": "call_1",
                               "assistant_msg_idx": 1, "is_error": True,
                               "duration": 5.0}, "idx": 0}],
            "call_2": [{"tc": {"name": "bash", "tid": "call_2",
                               "assistant_msg_idx": 4, "duration": 8.0},
                        "idx": 1}],
        },
        live=[
            {"name": "bash", "tid": "call_1", "assistant_msg_idx": 1,
             "is_error": True, "duration": 5.0},
            {"name": "bash", "tid": "call_2", "assistant_msg_idx": 4,
             "duration": 8.0},
        ],
    )
    by_tid = {r.get("tid"): r for r in out}
    assert by_tid["call_1"].get("is_error") is True, (
        f"the occurrence-exact verdict transfer stopped working: "
        f"{by_tid['call_1']!r}"
    )
    assert by_tid["call_1"].get("duration") == 5.0
    assert by_tid["call_2"].get("duration") == 8.0
    assert by_tid["call_2"].get("is_error") is not True
