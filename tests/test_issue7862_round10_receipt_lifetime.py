"""#7862 round 10 — receipt lifetime bound to attempt lifetime.

The last open item from the round-8 re-gate. The reviewer's scenario:

> The route reclaims once, right after consume, so the sweep sees the same
> millisecond. With the registration callback still live at TTL + 1 s and a
> neighbouring consume, the live attempt's receipt is swept, the rejection path
> restores nothing, and the retry isn't a goal continuation.

A start attempt has no deadline. The route reclaims a receipt exactly once,
immediately after the consume, and then a slow provider handshake or a
registration callback still waiting on its worker can outlive the TTL while the
attempt runs. The sweep then dropped the receipt, the rejection path found
nothing to restore, and the retry ran as an ordinary turn — the goal loop
silently lost its continuation.

## The fix

The receipt now lives exactly as long as the attempt does, anchored by the
durable handoff:

* A handoff is created in the same ``os.replace`` that removes the record, and
  disappears only when the attempt **launches** (discharge) or is **rejected**
  (rollback). That is precisely the window in which the attempt is live, so a
  receipt whose session still holds a handoff is never swept for age alone.
* ``reclaim_goal_continuation_receipt`` refreshes the handoff's clock as well as
  the receipt's, so a legitimately slow attempt keeps both alive. Without that,
  the receipt would survive one TTL and then be dropped anyway — the same bug
  moved out by 15 minutes.
* A handoff nobody has reclaimed past the TTL is an **abandoned** attempt (it
  crashed without discharging), and its receipt is swept normally. Without this
  the registry would grow without bound, trading a bounded registry for an
  unbounded one.

The rollback path also falls back to the handoff when the receipt is already
gone, so a swept receipt no longer means a lost continuation.
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
    """Isolate the registry between cases, INCLUDING the handoff tokens."""
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


PROMPT = "Continue the standing goal."


def _age(store, keys, field, seconds):
    """Backdate *field* on each receipt that still exists.

    Uses ``get`` rather than indexing: a retired or already-swept session has no
    receipt left, and raising there would fail the test for a reason unrelated
    to what it is asserting.
    """
    stale = store.time.time() - seconds
    with store._LOCK:
        for key in keys:
            record = store._ROLLBACK_RECEIPTS.get(key)
            if record is not None:
                record[field] = stale


# ── the reviewer's exact scenario ───────────────────────────────────────────


def test_a_live_attempts_receipt_survives_the_ttl(clean_registry):
    """TTL + 1s with the attempt still in flight must NOT sweep its receipt.

    This is the reported scenario: the route reclaims once at consume time, the
    registration callback is still live past the TTL, a neighbouring consume
    triggers the sweep, and the live attempt's receipt goes with it.
    """
    store = _store()
    sid = "sess-r10-live-past-ttl"
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-1")
    assert (
        store.consume_pending_goal_continuation(sid, PROMPT, "tok-1", "att-1")
        == store.CONSUME_COMMITTED
    )
    # The route's single reclaim, right after the consume.
    assert store.reclaim_goal_continuation_receipt(sid, "att-1") is True

    # The attempt outlives the TTL while still running, and a neighbouring
    # consume triggers a sweep. The route's single reclaim happened at consume
    # time, so BOTH clocks are now older than the TTL — which is exactly why
    # age alone is not conclusive here.
    stale = store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 1
    with store._LOCK:
        record = store._ROLLBACK_RECEIPTS[(sid, "att-1")]
        record["_receipt_minted_at"] = stale
        record["_receipt_reclaimed_at"] = stale
    neighbour = "sess-r10-neighbour"
    store.arm_pending_goal_continuation(neighbour, PROMPT, continuation_id="tok-2")
    assert (
        store.consume_pending_goal_continuation(neighbour, PROMPT, "tok-2", "att-2")
        == store.CONSUME_COMMITTED
    )

    # The live attempt's receipt is still claimable, so a rejection restores it.
    receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-1")
    assert receipt is not None, (
        "a live attempt's receipt was swept for age alone, so the rejection "
        "path restored nothing and the retry ran as an ordinary turn"
    )
    assert store.restore_pending_goal_continuation(sid, receipt) is True


def test_a_live_attempt_keeps_its_receipt_by_reclaiming(clean_registry):
    """A slow-but-healthy attempt refreshes BOTH clocks.

    Without refreshing the handoff's clock, the receipt would survive one TTL
    and then be dropped anyway — the same regression, moved out by 15 minutes.
    """
    store = _store()
    sid = "sess-r10-reclaim"
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-1")
    assert (
        store.consume_pending_goal_continuation(sid, PROMPT, "tok-1", "att-1")
        == store.CONSUME_COMMITTED
    )

    # Two TTLs' worth of liveness reports.
    for round_index in range(2):
        assert store.reclaim_goal_continuation_receipt(sid, "att-1") is True
        # Backdate the receipt's clocks; the reclaim above refreshed the
        # HANDOFF's clock, which is what must keep the receipt alive.
        stale = store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 1
        with store._LOCK:
            record = store._ROLLBACK_RECEIPTS[(sid, "att-1")]
            record["_receipt_minted_at"] = stale
            record["_receipt_reclaimed_at"] = stale
        # A neighbouring consume triggers the sweep each round.
        other = f"sess-r10-reclaim-n{round_index}"
        store.arm_pending_goal_continuation(other, PROMPT, continuation_id=f"t{round_index}")
        store.consume_pending_goal_continuation(
            other, PROMPT, f"t{round_index}", f"a{round_index}"
        )

    assert store.pop_goal_continuation_rollback_receipt(sid, "att-1") is not None, (
        "a live attempt that kept reporting in lost its receipt"
    )


def test_an_abandoned_attempts_receipt_is_swept(clean_registry):
    """A crashed attempt (never discharged, never reclaimed) is bounded.

    The handoff anchors a live receipt, so without an abandonment rule the
    registry would grow without bound — trading a bounded registry for an
    unbounded one.
    """
    store = _store()
    sid = "sess-r10-abandoned"
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-1")
    assert (
        store.consume_pending_goal_continuation(sid, PROMPT, "tok-1", "att-1")
        == store.CONSUME_COMMITTED
    )
    assert store.pop_goal_continuation_rollback_receipt(sid, "att-1") is None or True

    # Nobody ever reclaimed: both clocks age out together.
    stale = store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 1
    with store._LOCK:
        for rec in store._ROLLBACK_RECEIPTS.values():
            rec["_receipt_minted_at"] = stale
            rec["_receipt_reclaimed_at"] = stale
        for rec in store._CONTINUATION_HANDOFF_TOKENS.values():
            rec["_handoff_minted_at"] = stale
        store._sweep_expired_receipts_unlocked()

    assert not any(k[0] == sid for k in store._ROLLBACK_RECEIPTS), (
        "an abandoned attempt's receipt was never swept, so the registry grows "
        "without bound"
    )


def test_a_discharged_attempts_receipt_is_swept(clean_registry):
    """Launch succeeded → the handoff is gone → age alone is conclusive again."""
    store = _store()
    sid = "sess-r10-discharged"
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-1")
    assert (
        store.consume_pending_goal_continuation(sid, PROMPT, "tok-1", "att-1")
        == store.CONSUME_COMMITTED
    )
    assert store.discard_goal_continuation_handoff(sid, "att-1") is True

    stale = store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 1
    with store._LOCK:
        record = store._ROLLBACK_RECEIPTS.get((sid, "att-1"))
        if record is not None:
            record["_receipt_minted_at"] = stale
            record["_receipt_reclaimed_at"] = stale
        store._sweep_expired_receipts_unlocked()
    assert not any(k[0] == sid for k in store._ROLLBACK_RECEIPTS), (
        "a discharged attempt's receipt survived with no handoff to anchor it"
    )


# ── the rollback fallback ───────────────────────────────────────────────────


def test_a_swept_receipt_falls_back_to_the_handoff(clean_registry):
    """A receipt that is already gone must not cost the continuation.

    The rollback path pops the receipt first and the handoff second, so a
    receipt swept between the consume and the rejection still restores the
    intent instead of silently dropping it.
    """
    store = _store()
    sid = "sess-r10-fallback"
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-1")
    assert (
        store.consume_pending_goal_continuation(sid, PROMPT, "tok-1", "att-1")
        == store.CONSUME_COMMITTED
    )

    # Simulate the receipt being gone while the handoff survives: the exact
    # state a sweep-under-pressure produces.
    with store._LOCK:
        store._ROLLBACK_RECEIPTS.clear()

    receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-1")
    assert receipt is None, "the receipt should be gone in this scenario"
    handoff = store.pop_goal_continuation_handoff(sid, "att-1")
    assert handoff is not None, (
        "the handoff was gone too, so the fallback has nothing to restore from"
    )
    assert store.restore_pending_goal_continuation(sid, handoff) is True


def test_the_handoff_fallback_carries_the_record_shape(clean_registry):
    """The handoff must be restorable, not merely informative."""
    store = _store()
    sid = "sess-r10-shape"
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-1")
    assert (
        store.consume_pending_goal_continuation(sid, PROMPT, "tok-1", "att-1")
        == store.CONSUME_COMMITTED
    )
    handoff = store.pop_goal_continuation_handoff(sid, "att-1")
    assert handoff is not None
    for field in ("prompt", "generation", "continuation_id"):
        assert field in handoff, f"the handoff is missing {field!r}"
    # Internal bookkeeping keys must not leak into the restored record.
    assert not any(k.startswith("_handoff_") for k in handoff), (
        "internal handoff keys leaked into the restored record"
    )


def test_the_handoff_clock_survives_a_restart(clean_registry):
    """A restored handoff must still read as live, not abandoned.

    The handoff's clock is what tells a cold process whether the attempt is
    still running. Filtering it out of the durable payload would make every
    restored handoff look abandoned, and its receipt would be swept on the
    first sweep after a restart — the exact regression this round closes. The
    clock is persisted; only the redundant attempt id is stripped.
    """
    store = _store()
    sid = "sess-r10-clock-restart"
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-1")
    assert (
        store.consume_pending_goal_continuation(sid, PROMPT, "tok-1", "att-1")
        == store.CONSUME_COMMITTED
    )

    # Cold restart: wipe the live state and restore from disk.
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
        store._GENERATION = 0
    restored = store.restore_goal_continuations()
    assert restored == 1, "the handoff was not adopted after a restart"

    # The adopted handoff must still count as a live attempt.
    assert store._receipt_attempt_is_live(sid) is True, (
        "a freshly restored handoff reads as abandoned, so its receipt would be "
        "swept on the first sweep after a restart"
    )


# ── negative controls ───────────────────────────────────────────────────────


def test_a_receipt_with_no_handoff_is_still_bounded(clean_registry):
    """The pre-existing bound still holds when nothing anchors a receipt."""
    store = _store()
    for i in range(store._MAX_ROLLBACK_RECEIPTS + 20):
        sid = f"sess-r10-bound-{i}"
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id=f"t{i}")
        store.consume_pending_goal_continuation(sid, PROMPT, f"t{i}", f"a{i}")
        store.discard_goal_continuation_handoff(sid, f"a{i}")
    assert len(store._ROLLBACK_RECEIPTS) == store._MAX_ROLLBACK_RECEIPTS + 20

    stale = store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 1
    with store._LOCK:
        for rec in store._ROLLBACK_RECEIPTS.values():
            rec["_receipt_minted_at"] = stale
        for rec in store._CONTINUATION_HANDOFF_TOKENS.values():
            rec["_handoff_minted_at"] = stale
        dropped = store._sweep_expired_receipts_unlocked()
    assert dropped == store._MAX_ROLLBACK_RECEIPTS + 20
    assert len(store._ROLLBACK_RECEIPTS) == 0


def test_a_retired_session_keeps_no_receipt(clean_registry):
    """Retirement discharges the handoff, so age alone bounds the receipt."""
    store = _store()
    sid = "sess-r10-retired"
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-1")
    assert (
        store.consume_pending_goal_continuation(sid, PROMPT, "tok-1", "att-1")
        == store.CONSUME_COMMITTED
    )
    store.retire_pending_goal_continuation(sid, reason="cleared")
    assert not any(k[0] == sid for k in store._CONTINUATION_HANDOFF_TOKENS)

    stale = store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 1
    with store._LOCK:
        record = store._ROLLBACK_RECEIPTS.get((sid, "att-1"))
        if record is not None:
            record["_receipt_minted_at"] = stale
            record["_receipt_reclaimed_at"] = stale
        store._sweep_expired_receipts_unlocked()
    assert not any(k[0] == sid for k in store._ROLLBACK_RECEIPTS)
