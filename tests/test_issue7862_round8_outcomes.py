"""#7862 review round 8 — the two CORE root causes.

Round 7 made a failed registry write stop acknowledging success inside the
store. The 2026-10-08 re-gate found that the two remaining CORE items are both
about *distinguishing failure kinds*, and both are silent data-loss paths:

1. **[CORE] The route still admits a failed commit as a plain turn.**
   ``consume_pending_goal_continuation`` returned a bare bool, so the route could
   not tell "this turn is not the continuation" from "this turn IS the
   continuation but the durable removal did not commit". Both were ``False``, so
   a failed commit was admitted as an ordinary turn — the turn was spent while
   the old registry bytes survived, and a cold restore brought back the unspent
   intent.

2. **[CORE] Removal still commits before a durable handoff exists.** Consume did
   TWO replaces: the record was removed at the first, the handoff added at the
   second. A process loss between them left ``records=[] handoffs=[]``, so cold
   restore restored nothing and the retry ran as an ordinary turn. When the
   second write failed, the handoff was dropped and the consume still returned
   True.

The store now answers with a three-state result, and the removal + handoff land
in ONE ``os.replace``.
"""

from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)


def _store():
    import api.goal_continuation_store as store

    return store


@pytest.fixture()
def clean_registry():
    """Isolate the registry between cases, INCLUDING the handoff tokens.

    Copied from the round-7 fixtures and extended: they never cleared
    ``_CONTINUATION_HANDOFF_TOKENS``, so a leaked token survived into every later
    snapshot in the same process and was masked during adoption by the in-memory
    presence skip (#7862 round 8, "smaller" finding 1).
    """
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._LAST_LOAD_ERROR = None
        store._LAST_WRITE_ERROR = None
        store._RETIRED_LOG.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
        store._GENERATION = 0
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)
    for tmp in store._PENDING_GOAL_FILE.parent.glob("pending_goal_continuations.*.tmp"):
        tmp.unlink(missing_ok=True)
    yield
    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)


# ── finding 1: failure kinds are distinguishable ────────────────────────────


def test_the_store_exposes_the_three_outcomes():
    store = _store()
    assert store.CONSUME_NOT_MATCHING == "not_matching"
    assert store.CONSUME_COMMITTED == "committed"
    assert store.CONSUME_COMMIT_FAILED == "commit_failed"
    # They must be mutually distinct, or the route cannot branch on them.
    assert len({store.CONSUME_NOT_MATCHING, store.CONSUME_COMMITTED, store.CONSUME_COMMIT_FAILED}) == 3


def test_a_non_matching_turn_reports_not_matching(clean_registry):
    """Leave the intent pending; this is not an error."""
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION

    sid = "sess-r8-nomatch"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")
    outcome = store.consume_pending_goal_continuation(sid, "unrelated chatter")
    assert outcome == store.CONSUME_NOT_MATCHING
    assert sid in PENDING_GOAL_CONTINUATION, (
        "a non-matching turn must leave the intent pending"
    )


def test_a_failed_commit_reports_commit_failed(clean_registry, monkeypatch):
    """The turn IS the continuation but nothing became durable."""
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = "sess-r8-commitfail"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")
    before = dict(PENDING_GOAL_CONTINUATION_RECORDS)

    monkeypatch.setattr(store, "_write_registry_unlocked", lambda *a, **k: False)
    outcome = store.consume_pending_goal_continuation(sid, "Continue the standing goal.")

    assert outcome == store.CONSUME_COMMIT_FAILED, (
        "a matched continuation whose durable removal failed must be reported as "
        f"a commit failure, not {outcome!r}"
    )
    # And the in-memory state must describe the PRE-consume world.
    assert sid in PENDING_GOAL_CONTINUATION
    assert dict(PENDING_GOAL_CONTINUATION_RECORDS) == before, (
        "a refused consume must restore the record it popped"
    )
    # No handoff may survive a refused consume.
    assert not [k for k in store._CONTINUATION_HANDOFF_TOKENS if k[0] == sid], (
        "a refused consume left a handoff behind, so a restart could adopt a "
        "record that was never durably removed"
    )
    # And no rollback receipt may survive either.
    assert store.pop_goal_continuation_rollback_receipt(sid) is None


def test_a_committed_consume_reports_committed(clean_registry):
    store = _store()
    sid = "sess-r8-committed"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")
    assert (
        store.consume_pending_goal_continuation(sid, "Continue the standing goal.")
        == store.CONSUME_COMMITTED
    )


def test_the_outcome_strings_are_never_truthy_booleans():
    """Guard the trap: ``'commit_failed'`` is truthy.

    A caller that keeps the old ``if consume(...):`` shape would treat a failed
    commit as success. The constants must stay non-empty strings that are only
    ever compared, never coerced.
    """
    store = _store()
    assert bool(store.CONSUME_COMMIT_FAILED) is True, (
        "if this ever becomes falsy, ``if outcome:`` silently changes meaning"
    )


# ── finding 1 (route side): a failed commit refuses admission ───────────────


def test_the_route_branches_on_the_outcome():
    """The route must distinguish the three outcomes, not coerce to bool."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(
        encoding="utf-8"
    )
    assert "CONSUME_COMMITTED" in src, (
        "the route does not branch on the committed outcome"
    )
    assert "CONSUME_COMMIT_FAILED" in src, (
        "the route does not branch on the commit-failed outcome, so a failed "
        "commit is still admitted as an ordinary turn (#7862 round 8 finding 1)"
    )
    assert "_GoalContinuationCommitFailed" in src, (
        "the route has no dedicated failure type, so the generic "
        "``except Exception`` swallows the refusal"
    )


def test_the_refusal_escapes_the_generic_except():
    """The dedicated exception must be re-raised, not logged and ignored."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(
        encoding="utf-8"
    )
    idx = src.find("except _GoalContinuationCommitFailed:")
    assert idx > 0
    window = src[idx : idx + 120]
    assert "raise" in window, (
        "the dedicated exception is caught but not re-raised, so the refusal "
        "degrades into the old swallow-and-admit behaviour"
    )


# ── finding 2: one atomic replace, not two ─────────────────────────────────


def test_consume_writes_the_registry_exactly_once(clean_registry, monkeypatch):
    """The removal and its handoff must land in ONE os.replace.

    Round 7 wrote them in two passes, so a process loss between them left
    ``records=[] handoffs=[]``.
    """
    store = _store()
    sid = "sess-r8-atomic"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")

    calls = []
    real_write = store._write_registry_unlocked

    def _counting(records, *, context=""):
        calls.append(context)
        return real_write(records, context=context)

    monkeypatch.setattr(store, "_write_registry_unlocked", _counting)
    outcome = store.consume_pending_goal_continuation(
        sid, "Continue the standing goal.", "", "att-atomic"
    )
    assert outcome == store.CONSUME_COMMITTED
    assert len(calls) == 1, (
        f"consume wrote the registry {len(calls)} times ({calls!r}); the record "
        "removal and the handoff must be committed together or a crash between "
        "them strands the intent with no durable evidence"
    )
    assert "consume+handoff" in calls[0], (
        f"the single write is not the combined one: {calls[0]!r}"
    )


def test_a_successful_consume_leaves_both_halves_durable(clean_registry):
    """The committed snapshot has the record GONE and the handoff PRESENT."""
    import json
    from pathlib import Path

    store = _store()
    sid = "sess-r8-both"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")
    assert (
        store.consume_pending_goal_continuation(
            sid, "Continue the standing goal.", "", "att-both"
        )
        == store.CONSUME_COMMITTED
    )

    payload = json.loads(Path(store._PENDING_GOAL_FILE).read_text(encoding="utf-8"))
    assert sid not in (payload.get("records") or {}), (
        "the consumed record is still in the durable snapshot"
    )
    handoffs = payload.get("continuation_handoffs") or {}
    assert any(k.startswith(f"{sid}\u0000") for k in handoffs), (
        "the durable snapshot has no handoff for a consumed continuation, so a "
        "crash right after the consume loses the intent entirely"
    )


def test_the_failed_second_write_no_longer_returns_success(clean_registry, monkeypatch):
    """Round 7's second-write failure path returned True; that path is gone."""
    store = _store()
    sid = "sess-r8-nosecond"
    store.arm_pending_goal_continuation(sid, "Continue the standing goal.")
    monkeypatch.setattr(store, "_write_registry_unlocked", lambda *a, **k: False)
    outcome = store.consume_pending_goal_continuation(
        sid, "Continue the standing goal.", "", "att-nosecond"
    )
    assert outcome == store.CONSUME_COMMIT_FAILED, (
        "a consume whose only write failed must refuse, not report success"
    )
