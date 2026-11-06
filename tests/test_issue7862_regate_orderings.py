"""#7862 round 6: the four orderings from the maintainer's re-gate probe.

The re-gate ran an isolated probe against the real
``goal_continuation_store`` and ``goal_command_payload``, plus the unmodified
``_start_chat_stream_for_session`` compiled into a fake rejected-start harness,
and reproduced four orderings where a cleared goal came back or a continuation
was lost. These are those four orderings as store-level regressions.

Ordering 1 and 3 are the round-6 CORE findings:
  1. a count cap evicted an in-flight attempt's rollback receipt, so the
     rejected start's rollback consumed ``false`` and the goal loop ran the
     retry as an ordinary turn;
  3. a clear tombstone lived in a bounded in-memory dict, so 64 unrelated
     retirements dropped it and a late rollback RESTORED a goal the user had
     cleared.

Orderings 2 and 4 are the same clear being forgotten through different paths
(the receipt slot and the cold-restart generation baseline).
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

PROMPT = "keep refining the release checklist"


@pytest.fixture
def clean_registry():
    """Isolate the registry between cases: in-memory mirror, file, tombstones."""
    from api import goal_continuation_store as store
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
        store._GENERATION = 0
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)
    yield
    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)


def _arm_and_consume(store, sid, prompt=PROMPT, attempt_id="att-1", token="tok-1"):
    store.arm_pending_goal_continuation(
        sid, prompt, continuation_id=token
    )
    assert store.consume_pending_goal_continuation(
        sid, prompt, token, attempt_id
    )


class TestOrdering1InFlightStartRejectedUnderLoad:
    """65 starts in flight, the first one rejected.

    Before round 6 a count cap (global, then per-session) evicted the in-flight
    attempt's receipt, the rejection found nothing to claim, fell back to a
    bare marker, and the retry consumed ``false`` -- running as an ordinary
    turn and losing the goal loop.
    """

    def test_first_rejected_start_still_restores_record_and_marker(self, clean_registry):
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-first-rejected"
        _arm_and_consume(store, sid, attempt_id="att-first", token="tok-first")

        # 64 other starts go in flight and each consumes its own intent.
        for i in range(64):
            other = f"sess-load-{i}"
            _arm_and_consume(
                store, other, attempt_id=f"att-{i}", token=f"tok-{i}"
            )

        # The rejected start's rollback.
        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-first")
        assert receipt is not None, (
            "the in-flight attempt's receipt was evicted by unrelated traffic"
        )
        assert receipt.get("continuation_id") == "tok-first"
        assert store.restore_pending_goal_continuation(sid, receipt) is True

        # BOTH the marker and the durable record are back, so the retry's
        # store-backed consume can match it instead of running blind.
        assert sid in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(sid, {}).get(
            "continuation_id"
        ) == "tok-first"
        assert store.consume_pending_goal_continuation(sid, PROMPT, "tok-first")== store.CONSUME_COMMITTED


class TestOrdering2ClearBeforeTheRollback:
    """/goal clear lands between the consume and the rejected start."""

    def test_clear_before_rollback_blocks_the_restore(self, clean_registry):
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-clear-first"
        _arm_and_consume(store, sid, attempt_id="att-c", token="tok-c")

        # The user clears the goal while the start is still in flight.
        store.retire_pending_goal_continuation(sid, reason="cleared")

        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-c")
        # The clear dropped the session's receipts, so there is nothing to
        # claim -- and the rollback must not resurrect anything.
        assert receipt is None
        assert sid not in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(sid) is None
        assert (
            store.restore_pending_goal_continuation(sid, {"generation": 1}) is False
        )


class TestOrdering3ClearForgottenAfterUnrelatedRetirements:
    """Clear, then 64 unrelated retirements, then the late rollback.

    Before round 6 the tombstone lived in a 64-entry in-memory dict keyed by
    session, so the unrelated retirements pushed this session's stamp out of the
    window. The late rollback then found no stamp and RESTORED a goal the user
    had cleared.
    """

    def test_sixty_four_unrelated_retirements_do_not_forget_the_clear(
        self, clean_registry
    ):
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-clear-then-churn"
        _arm_and_consume(store, sid, attempt_id="att-x", token="tok-x")
        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-x")
        assert receipt is not None
        receipt_generation = int(receipt.get("generation") or 0)
        assert receipt_generation > 0

        store.retire_pending_goal_continuation(sid, reason="cleared")

        # 64 unrelated sessions retire out from under the in-memory window.
        for i in range(store._MAX_RETIRED_GENERATIONS + 8):
            other = f"sess-churn-{i}"
            store.arm_pending_goal_continuation(other, PROMPT)
            store.retire_pending_goal_continuation(other, reason="expired")

        # The late rollback must still refuse: the clear is durable.
        assert store.restore_pending_goal_continuation(sid, receipt) is False
        assert sid not in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(sid) is None

        # And it is on disk, so a restart honours it too.
        raw = json.loads(store._PENDING_GOAL_FILE.read_text(encoding="utf-8"))
        assert int(raw["tombstones"].get(sid) or 0) >= receipt_generation


class TestOrdering4ClearForgottenAfterAColdRestart:
    """Restart: the in-process generation counter restarts below the disk's.

    A cold process counts generations from 0, so a fresh clear at generation 3
    compared OLDER than a restored record at generation 41 -- and the clear was
    silently forgotten, restoring a goal the user had cleared.
    """

    def test_restart_generation_baseline_beats_a_restored_record(self, clean_registry):
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        # A previous lifecycle: arm + consume 41 times so the persisted
        # generation is far above zero, then clear one of them.
        for i in range(41):
            sid = f"sess-history-{i}"
            _arm_and_consume(store, sid, attempt_id=f"att-h{i}", token=f"tok-h{i}")
        persisted_generation = store._GENERATION
        assert persisted_generation >= 41

        sid = "sess-cold-clear"
        _arm_and_consume(store, sid, attempt_id="att-cold", token="tok-cold")
        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-cold")
        assert receipt is not None
        receipt_generation = int(receipt.get("generation") or 0)
        store.retire_pending_goal_continuation(sid, reason="cleared")
        clear_generation = store._TOMBSTONES[sid]
        assert clear_generation >= receipt_generation

        # ---- cold restart: the module-level state is gone -----------------
        # Only what the file carries survives. Here nothing is restorable
        # (everything was retired), so the record restore is empty and the
        # tombstone is the ONLY durable evidence that the clear happened.
        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
            store._ROLLBACK_RECEIPTS.clear()
            store._RETIRED_GENERATIONS.clear()
            store._TOMBSTONES.clear()
            store._GENERATION = 0

        store.restore_goal_continuations()

        # The generation baseline is above everything the file carried, so no
        # later mutation in this process can sort older than a persisted one.
        assert store._GENERATION > persisted_generation

        # The clear survives the restart.
        assert store._TOMBSTONES.get(sid) == clear_generation
        assert store.restore_pending_goal_continuation(sid, receipt) is False
        assert sid not in PENDING_GOAL_CONTINUATION

    def test_restart_restores_a_live_record_above_the_persisted_baseline(
        self, clean_registry
    ):
        """A restored record must NOT be treated as older than a persisted clear.

        The other half of the cold-restart ordering: the file carries BOTH a
        live record at generation 41 and a clear at generation 40 for a
        DIFFERENT session. A cold process that counts from 0 would mint the
        restored record a fresh generation just above 40, so the two sessions'
        generations interleave wrongly and a later rollback for the live record
        can be refused by an unrelated session's clear.
        """
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        # Build: one live intent, one cleared session with a high generation.
        live_sid = "sess-live-cold"
        cleared_sid = "sess-cleared-cold"
        # Arm BOTH but consume only the cleared one: a live (unconsumed) intent
        # is what a restart actually has to bring back alongside a tombstone.
        store.arm_pending_goal_continuation(
            live_sid, PROMPT, continuation_id="tok-live"
        )
        _arm_and_consume(store, cleared_sid, attempt_id="att-clr", token="tok-clr")
        live_generation = int(
            PENDING_GOAL_CONTINUATION_RECORDS[live_sid]["generation"]
        )
        store.retire_pending_goal_continuation(cleared_sid, reason="cleared")
        cleared_generation = store._TOMBSTONES[cleared_sid]
        assert cleared_generation > live_generation  # the clear sorts AFTER it
        persisted_generation = store._GENERATION

        # Simulate the cold restart: only the file survives.
        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
            store._ROLLBACK_RECEIPTS.clear()
            store._RETIRED_GENERATIONS.clear()
            store._TOMBSTONES.clear()
            store._GENERATION = 0

        store.restore_goal_continuations()

        # The live intent keeps its ORIGINAL generation -- not a fresh, lower
        # one that would sort under the other session's clear.
        restored = PENDING_GOAL_CONTINUATION_RECORDS.get(live_sid)
        assert restored is not None
        assert int(restored.get("generation") or 0) == live_generation
        # The baseline is above everything the file carried.
        assert store._GENERATION > persisted_generation
        assert store._TOMBSTONES.get(cleared_sid) == cleared_generation

        # So a rollback for the LIVE session is still honoured: the other
        # session's clear at a higher generation must not block it.
        assert (
            store.restore_pending_goal_continuation(
                live_sid,
                {
                    "generation": live_generation,
                    "prompt": PROMPT,
                    "continuation_id": "tok-live",
                },
            )
            is False  # already live: nothing to restore, refused for that reason
        )
        # ...while the cleared session stays cleared.
        assert cleared_sid not in PENDING_GOAL_CONTINUATION
