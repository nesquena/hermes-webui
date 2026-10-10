"""#7862 round 5: a rollback receipt must survive until its own attempt ends.

The round-4 fix (restore marker + record on a rejected chat start) left one
hole the maintainer's probe found, plus two Codex reports it passed along:

1. **CORE (maintainer-reproduced)** — receipts lived in a global
   ``deque(maxlen=64)`` and a SUCCESSFUL launch never discarded its receipt.
   Session A consumed (its start is about to be rejected), then 64 other
   sessions consumed AND launched normally, then A rolled back: the receipt was
   gone, the routes fallback restored a bare marker, and A's retry could not
   consume it — the goal loop died. Fixed by keying receipts per start attempt
   and discarding them as soon as a launch succeeds.

2. **CORE (Codex)** — a pending selected-text reply flushed into an automatic
   goal send alters the prompt, so the store's text match rejects it even
   though the continuation token is correct.

3. **SILENT (Codex)** — a late rejected-start rollback can restore an intent
   that ``/goal clear`` retired in the meantime, silently undoing the clear.
"""

import re
from pathlib import Path

import pytest


PROMPT = "Continue the standing goal, please."


@pytest.fixture(autouse=True)
def clean_registry():
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


class TestReceiptSurvivesUnrelatedTraffic:
    """Finding 1: unrelated successful starts must not evict a live receipt."""

    def test_inflight_receipt_survives_a_full_deque_of_other_sessions(self, clean_registry):
        """The maintainer's exact sequence, through the real store.

        A consumes and its start is about to be rejected. 64 other sessions
        then consume AND launch successfully. A's rollback must still find its
        receipt, and A's retry must still consume as a continuation.
        """
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        a = "sess-A"
        store.arm_pending_goal_continuation(a, PROMPT, continuation_id="tok-A")
        assert store.consume_pending_goal_continuation(a, PROMPT, "tok-A", "attempt-A")== store.CONSUME_COMMITTED

        # 64 unrelated sessions consume, then their launches succeed (which
        # discards their receipts). This is what used to push A out.
        for i in range(store._MAX_ROLLBACK_RECEIPTS + 8):
            sid = f"sess-other-{i}"
            store.arm_pending_goal_continuation(sid, PROMPT, continuation_id=f"tok-{i}")
            assert store.consume_pending_goal_continuation(sid, PROMPT, f"tok-{i}", f"att-{i}")
            store.discard_goal_continuation_rollback_receipt(sid, f"att-{i}")

        # A's receipt is STILL claimable — this is the assertion the old
        # global deque failed (pop returned None).
        receipt = store.pop_goal_continuation_rollback_receipt(a, "attempt-A")
        assert receipt is not None, "A's in-flight receipt was evicted by unrelated traffic"
        assert receipt.get("prompt") == PROMPT
        assert store.restore_pending_goal_continuation(a, receipt) is True
        assert a in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(a) is not None
        # And the retry consumes as the continuation (not a bare marker).
        assert store.consume_pending_goal_continuation(a, PROMPT, "tok-A", "attempt-A2")== store.CONSUME_COMMITTED

    def test_successful_launch_discards_its_receipt(self, clean_registry):
        """A launch that got past thr.start() has no rollback consumer."""
        from api import goal_continuation_store as store

        sid = "sess-success"
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-s")
        assert store.consume_pending_goal_continuation(sid, PROMPT, "tok-s", "att-s")== store.CONSUME_COMMITTED
        assert store.durability_diagnostics()["pending_rollback_receipts"] == 1
        store.discard_goal_continuation_rollback_receipt(sid, "att-s")
        assert store.durability_diagnostics()["pending_rollback_receipts"] == 0
        # Nothing left to claim, so a spurious rollback is a no-op.
        assert store.pop_goal_continuation_rollback_receipt(sid, "att-s") is None

    def test_discard_does_not_touch_another_attempts_receipt(self, clean_registry):
        """Discard is scoped to the attempt that succeeded."""
        from api import goal_continuation_store as store

        sid = "sess-two-attempts"
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-1")
        assert store.consume_pending_goal_continuation(sid, PROMPT, "tok-1", "att-1")== store.CONSUME_COMMITTED
        # A different attempt id must not claim it...
        assert store.pop_goal_continuation_rollback_receipt(sid, "att-other") is None
        # ...and the real owner still can.
        assert store.pop_goal_continuation_rollback_receipt(sid, "att-1") is not None

    def test_receipts_stay_bounded_without_evicting_a_live_attempt(self, clean_registry):
        """Bounded storage WITHOUT count-cap eviction of live attempts.

        The per-session cap is gone. It was the round-6 scenario: one session
        leaking many attempts evicted the in-flight one's receipt, and the
        rejected-start rollback then consumed ``false`` (running as an ordinary
        turn) instead of restoring the goal. The bound is now per-receipt TTL,
        which cannot touch an attempt that is still in flight.
        """
        from api import goal_continuation_store as store

        # One session leaking many attempts. NOTE: ``arm`` drops the session's
        # older receipts by design (a fresh intent supersedes the older one, so
        # its rollback would resurrect a superseded generation), so the count
        # ends at 1 -- that is not eviction, it is supersession.
        sid = "sess-leaky"
        for i in range(store._MAX_ROLLBACK_RECEIPTS_PER_SESSION + 6):
            store.arm_pending_goal_continuation(sid, PROMPT, continuation_id=f"tok-{i}")
            assert store.consume_pending_goal_continuation(sid, PROMPT, f"tok-{i}", f"att-{i}")
        assert [k for k in store._ROLLBACK_RECEIPTS if k[0] == sid] == [(sid, "att-9")]

        # The real in-flight pressure is 65 DISTINCT starts in flight (one chat
        # start per session, serialised by the session agent lock). None of
        # them may evict another, which is what the round-6 probe pins.
        for j in range(store._MAX_ROLLBACK_RECEIPTS + 40):
            other = f"sess-bulk-{j}"
            store.arm_pending_goal_continuation(other, PROMPT, continuation_id=f"tb-{j}")
            assert store.consume_pending_goal_continuation(
                other, PROMPT, f"tb-{j}", f"ab-{j}"
            )
        assert len(store._ROLLBACK_RECEIPTS) == store._MAX_ROLLBACK_RECEIPTS + 41
        # Every one of them is still claimable by its own attempt.
        assert store.pop_goal_continuation_rollback_receipt("sess-bulk-0", "ab-0") is not None
        assert store.pop_goal_continuation_rollback_receipt(
            "sess-bulk-64", "ab-64"
        ) is not None

        # Age everything past the TTL: the sweep bounds memory again.
        # #7862 round 10 (finding 3): the handoff now anchors a live receipt, so
        # it must be aged too. Leaving it fresh would pin every receipt as
        # "attempt in flight" forever, which is the unbounded registry the TTL
        # exists to prevent — an attempt that crashed without discharging is
        # exactly what this half of the test describes.
        with store._LOCK:
            for rec in store._ROLLBACK_RECEIPTS.values():
                rec["_receipt_minted_at"] = (
                    store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 1
                )
            for rec in store._CONTINUATION_HANDOFF_TOKENS.values():
                rec["_handoff_minted_at"] = (
                    store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 1
                )
            store._sweep_expired_receipts_unlocked()
        assert len(store._ROLLBACK_RECEIPTS) == 0

    def test_retire_drops_the_sessions_receipts(self, clean_registry):
        """A retired intent leaves no claimable receipt behind."""
        from api import goal_continuation_store as store

        sid = "sess-retire-drops"
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-r")
        assert store.consume_pending_goal_continuation(sid, PROMPT, "tok-r", "att-r")== store.CONSUME_COMMITTED
        store.retire_pending_goal_continuation(sid, reason="cleared")
        assert store.pop_goal_continuation_rollback_receipt(sid, "att-r") is None


class TestLateRollbackCannotUndoRetirement:
    """Finding 3: a rollback must not resurrect a goal the user cleared."""

    def test_goal_clear_between_consume_and_rollback_blocks_restore(self, clean_registry):
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-cleared-midflight"
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-c")
        assert store.consume_pending_goal_continuation(sid, PROMPT, "tok-c", "att-c")== store.CONSUME_COMMITTED
        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-c")
        assert receipt is not None

        # The user clears the goal while the start is still in flight.
        store.retire_pending_goal_continuation(sid, reason="cleared")

        # The late rollback must NOT bring it back.
        assert store.restore_pending_goal_continuation(sid, receipt) is False
        assert sid not in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(sid) is None

    def test_a_fresh_arm_after_clear_is_still_rollbackable(self, clean_registry):
        """The guard must not break the normal rejected-start rollback."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-clear-then-arm"
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-old")
        assert store.consume_pending_goal_continuation(sid, PROMPT, "tok-old", "att-old")== store.CONSUME_COMMITTED
        store.retire_pending_goal_continuation(sid, reason="cleared")
        # The goal loop arms a NEW intent after the clear.
        store.arm_pending_goal_continuation(sid, "A fresh continuation.", continuation_id="tok-new")
        assert store.consume_pending_goal_continuation(sid, "A fresh continuation.", "tok-new", "att-new")== store.CONSUME_COMMITTED
        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-new")
        assert receipt is not None
        # Its rejected start rolls back normally.
        assert store.restore_pending_goal_continuation(sid, receipt) is True
        assert PENDING_GOAL_CONTINUATION_RECORDS[sid]["prompt"] == "A fresh continuation."


class TestRoutesAndFrontendShape:
    """The wiring that makes the store-level fixes reachable in production."""

    def test_routes_discards_receipt_after_a_successful_launch(self):
        """Every successful start path must drop its receipt.

        A receipt with no consumer is what filled the old global deque, so both
        the worker-thread path and the regeneration path must discard.
        """
        src = Path("api/routes.py").read_text(encoding="utf-8")
        # The discard is imported ...
        assert "discard_goal_continuation_rollback_receipt" in src
        # ... and called right after the launch on the thread path ...
        m = re.search(r"thr\.start\(\).*?discard_goal_continuation_rollback_receipt", src, re.DOTALL)
        assert m is not None, "no receipt discard after thr.start()"
        # ... and on the regeneration success path too.
        regen = re.search(
            r"restore_consumed_continuation_markers\(\)\s*\n\s*else:.*?"
            r"discard_goal_continuation_rollback_receipt",
            src,
            re.DOTALL,
        )
        assert regen is not None, "no receipt discard on the regeneration success path"

    def test_routes_passes_this_attempts_id_to_consume_and_rollback(self):
        """Consume and rollback must agree on the attempt id."""
        src = Path("api/routes.py").read_text(encoding="utf-8")
        consume = re.search(
            r"consume_pending_goal_continuation\(\s*s\.session_id,.*?\)", src, re.DOTALL
        )
        assert consume is not None
        assert "goal_continuation_attempt_id" in consume.group(0)
        pop = re.search(r"pop_goal_continuation_rollback_receipt\((.*?)\)", src, re.DOTALL)
        assert pop is not None
        assert "goal_continuation_attempt_id" in pop.group(0)

    def test_automatic_continuation_does_not_flush_pending_selections(self):
        """Finding 2: a tokenized automatic send must not absorb user selections.

        Flushing the pending selected-text blocks into the composer prepends
        them to the queued continuation prompt, so the store's text match
        rejects an otherwise-correct automatic turn.
        """
        src = Path("static/messages.js").read_text(encoding="utf-8")
        m = re.search(
            r"if\(!\(options&&options\.goal_continuation_id\)\)\s*"
            r"_flushSelectionBlocksToComposer\(\);",
            src,
        )
        assert m is not None, "tokenized automatic send still flushes pending selections"
        # Human sends must keep flushing (the selection blocks are theirs).
        assert src.count("_flushSelectionBlocksToComposer();") >= 1
