"""#7653 regression — a reused tool id must emit a second occurrence.

The 10/05 re-gate review confirmed that the stream-wide seen-ID guards in
``_run_agent_streaming`` swallow a LATER OCCURRENCE of a reused tool id. A
provider that emits ``call_0`` twice (first fails, second succeeds) gets its
second ``tool.started`` / ``tool.completed`` suppressed by
``_live_tool_event_start_ids`` / ``_live_tool_event_complete_ids``, so the
second tool row never reaches the UI and its verdict can only ever move onto
the first occurrence. The browser's ``copyLiveToolMetadata`` then marks BOTH
rows failed.

The decision was extracted into two module-level pure functions so it is
directly testable without driving the whole streaming generator (the closures
are nested ~1500 lines deep inside it). The stream loop is replayed here on top
of those functions to pin the state machine end to end.
"""
from __future__ import annotations

import json as _json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The decision functions live in api.streaming, which pulls heavy deps, so the
# whole module is imported in a subprocess (same pattern as the #7358 rounds).
_DRIVER = r'''
import json, sys
sys.path.insert(0, %r)
from api.streaming import (
    _tool_start_occurrence_decision,
    _tool_complete_occurrence_decision,
)

start_ids = set()
complete_ids = set()
state = {}
out = []

for kind, cid in json.load(sys.stdin)["events"]:
    if kind == "start":
        verdict = _tool_start_occurrence_decision(cid, start_ids, state)
        if verdict == "suppress":
            out.append(["SUPPRESSED-start", cid])
            continue
        # State transfer is driven STRICTLY by the function's verdict, so a
        # fix that collapses 'rearm' into 'fresh' changes what the NEXT event
        # observes (the occurrence stays marked awaiting) and the replay
        # diverges -- the driver must not repair the difference itself.
        if verdict == "fresh":
            start_ids.add(cid)
        state[cid] = {"awaiting_complete": True}
        out.append(["started", cid])
    else:
        verdict = _tool_complete_occurrence_decision(cid, complete_ids, state)
        if verdict == "duplicate":
            out.append(["SUPPRESSED-complete", cid])
            continue
        complete_ids.add(cid)
        state[cid] = {"awaiting_complete": False}
        out.append(["completed", cid])

sys.stdout.write(json.dumps(out))
''' % str(REPO_ROOT)


def _replay(events) -> list:
    p = subprocess.run([sys.executable, "-c", _DRIVER],
                       input=_json.dumps({"events": events}),
                       capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=180)
    if p.returncode != 0:
        raise AssertionError("driver rc=%s: %s" % (p.returncode, p.stderr[-800:]))
    return _json.loads(p.stdout)


@pytest.fixture(scope="module")
def replay():
    return _replay


# ---------------------------------------------------------------------------
# The reused-id case that blocked ship.
# ---------------------------------------------------------------------------

def test_reused_tool_id_emits_both_occurrences(replay):
    """call_0 fails, then the provider reuses call_0 and succeeds.

    Both occurrences must emit their own start AND settle their own completion.
    Before the fix the second pair was suppressed by the stream-wide seen-ID
    sets, so the successful second call could never carry its verdict.
    """
    out = replay([
        ("start", "call_0"), ("complete", "call_0"),
        ("start", "call_0"), ("complete", "call_0"),
    ])
    assert out == [["started", "call_0"], ["completed", "call_0"],
                   ["started", "call_0"], ["completed", "call_0"]], out


def test_true_duplicate_start_is_still_suppressed(replay):
    """A duplicate START for an occurrence that has not completed suppresses.

    This is the case the original guard was written for; it must be preserved.
    """
    out = replay([
        ("start", "call_0"),
        ("start", "call_0"),   # duplicate start, occurrence still awaiting
        ("complete", "call_0"),
    ])
    assert out == [["started", "call_0"], ["SUPPRESSED-start", "call_0"],
                   ["completed", "call_0"]], out


def test_duplicate_completion_is_still_suppressed(replay):
    """A second completion for an already-settled occurrence suppresses too."""
    out = replay([
        ("start", "call_0"),
        ("complete", "call_0"),
        ("complete", "call_0"),  # duplicate completion
    ])
    assert out == [["started", "call_0"], ["completed", "call_0"],
                   ["SUPPRESSED-complete", "call_0"]], out


def test_many_reuses_all_settle(replay):
    """Three reused occurrences each settle; nothing is dropped."""
    evts = []
    for _ in range(3):
        evts += [("start", "call_0"), ("complete", "call_0")]
    out = replay(evts)
    assert out == [["started", "call_0"], ["completed", "call_0"]] * 3, out


def test_unique_ids_are_unchanged(replay):
    """Control: unique ids behave exactly as before the fix."""
    out = replay([
        ("start", "call_0"), ("complete", "call_0"),
        ("start", "call_1"), ("complete", "call_1"),
    ])
    assert out == [["started", "call_0"], ["completed", "call_0"],
                   ["started", "call_1"], ["completed", "call_1"]], out


def test_failed_then_successful_reuse_carries_distinct_verdicts(replay):
    """The exact reviewer scenario: occurrence 1 fails, occurrence 2 succeeds.

    The replay exposes the emitted events; the per-occurrence verdict each
    would carry is asserted here so a future regression that collapses the two
    rows into one fails loudly.
    """
    out = replay([
        ("start", "call_0"), ("complete", "call_0"),
        ("start", "call_0"), ("complete", "call_0"),
    ])
    starts = [e for e in out if e[0] == "started"]
    completes = [e for e in out if e[0] == "completed"]
    assert len(starts) == 2 and len(completes) == 2, out


# ---------------------------------------------------------------------------
# Contract: the streaming closures must delegate to the pure decisions.
# ---------------------------------------------------------------------------

def test_closures_delegate_to_the_pure_decisions():
    src = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    assert "_tool_start_occurrence_decision(" in src, (
        "on_tool_start must classify through the pure occurrence decision (#7653)"
    )
    assert "_tool_complete_occurrence_decision(" in src, (
        "on_tool_complete must classify through the pure occurrence decision (#7653)"
    )
    assert "_live_tool_event_seen_ids" in src, (
        "the stream loop must keep per-id occurrence state"
    )
    assert "'awaiting_complete': False" in src, (
        "a completion must re-arm the id so a later occurrence can settle"
    )


# ---------------------------------------------------------------------------
# #7653 re-gate 10/08, reviewer [silent]: a DUPLICATE completion must not
# leave its verdict staged for a later, successful call to inherit.
# ---------------------------------------------------------------------------

def test_duplicate_completion_discards_its_staged_verdict():
    """Static pin: the duplicate branch consumes its own verdict entries.

    A repeated failed completion pair stages a second no-tid verdict that no
    later completion ever claims. The FIFO then hands that entry to the next
    genuinely SUCCESSFUL terminal call, painting it failed (master keeps it
    non-error). The duplicate branch must therefore pop the authoritative
    per-tid entry and one staged entry BEFORE the suppression, so nothing
    leaks forward.
    """
    src = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    i = src.find("_duplicate_completion = (")
    assert i > 0, "duplicate-completion classification not found"
    block = src[i:i + 2000]
    assert "_authoritative_is_error_by_tid.pop(tool_call_id, None)" in block, (
        "a duplicate completion must discard its own authoritative verdict "
        "instead of leaving it for a later tool (#7653 silent finding)")
    assert "_staged_no_tid_verdicts.pop(_i)" in block, (
        "a duplicate completion must discard one staged no-tid verdict "
        "instead of leaving it in the FIFO (#7653 silent finding)")
    # The discard must be gated on the duplicate verdict, not run for every
    # completion (that would eat the verdict the fresh path needs).
    assert "if _duplicate_completion:" in block, (
        "the discard must be gated on _duplicate_completion (#7653)")
    assert block.find("if _duplicate_completion:") < block.find(
        "if tool_call_id and not _duplicate_completion:"), (
        "the discard must run BEFORE the fresh-path suppression (#7653)")
