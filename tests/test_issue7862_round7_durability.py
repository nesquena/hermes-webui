"""#7862 round 7: the three residuals from the Oct 5 re-gate probe.

The re-gate ran against the reviewed head ``b87974433e91`` and reproduced
three durable-outcome orderings. These are those three orderings as
regressions, tied to the real ``goal_continuation_store`` and (for finding 2)
the real ``_start_chat_stream_for_session`` source.

1. A failed registry write still acknowledged success: a consume whose
   ``os.replace`` failed returned an admitted stream, and ``/goal clear``
   still returned ``ok:true``, while the original registry bytes remained
   claimable on disk.
2. Consume committed removal before a durable pending-start handoff existed:
   injecting process loss at the pre-pending seam left no worker, no
   ``pending_user_message``, zero records on cold restore, and a matching
   retry classified ``goal_related:false``.
3. Elapsed age was treated as evidence that an attempt ended: a still-live
   registration callback's receipt was swept by simulated elapsed wall time,
   so the rejection could not restore intent and the retry became an
   ordinary turn.

Findings 1 and 3 are store/route level; finding 2 is a route-ordering
regression that pins the real source, because the seam it guards is a
crash boundary that cannot be exercised in-process by design.

No cross-PR symbol collision with #7855: the durable handoff token is named
``_CONTINUATION_HANDOFF_TOKENS`` / ``_arm_continuation_handoff_unlocked``,
which is distinct from #7855's ``PENDING_GOAL_CONTINUATION_RECORDS``-family
naming.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

PROMPT = "keep refining the release checklist"


@pytest.fixture
def clean_registry(monkeypatch):
    """Isolate the registry between cases: memory, file, tombstones, receipts.

    Also stubs ``api.goals.GoalManager``: CI installs no hermes-agent, so the
    native import fails there and ``GoalManager`` is ``None`` — which makes
    ``goal_command_payload`` bail out at ``_manager()`` with
    ``error="unavailable"`` before it ever reaches the clear/retire path these
    cases exercise. The module attribute is exported precisely so tests can
    replace it, so stub it with the same shape the real one exposes.
    """
    from api import goal_continuation_store as store
    from api import goals as goals_module
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    class _StubGoalManager:
        """Minimal stand-in: only what the clear path touches."""

        def __init__(self, *args, **kwargs):
            self._state = None

        def has_goal(self):
            return False

        def pause(self, *args, **kwargs):
            return None

        def resume(self, *args, **kwargs):
            return None

        def clear(self, *args, **kwargs):
            return None

        def state(self, *args, **kwargs):
            return self._state

    monkeypatch.setattr(goals_module, "GoalManager", _StubGoalManager)

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
    yield
    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)


def _arm_and_consume(store, sid, prompt=PROMPT, attempt_id="att-1", token="tok-1"):
    store.arm_pending_goal_continuation(sid, prompt, continuation_id=token)
    assert store.consume_pending_goal_continuation(sid, prompt, token, attempt_id)


def _break_replace(monkeypatch, store):
    """Make every durable snapshot fail at the ``os.replace`` boundary.

    This is the maintainer's "selective failure of the real registry
    replacement": the temp write and fsync still happen, so the failure is
    specifically the atomic swap that durability depends on.
    """

    def _boom(src, dst, *args, **kwargs):
        raise OSError("simulated failed registry replacement")

    monkeypatch.setattr(store.os, "replace", _boom)


# ---------------------------------------------------------------------------
# Finding 1: commit failure must propagate, not be acknowledged as success.
# ---------------------------------------------------------------------------


class TestFailedRegistryWriteIsNotAcknowledged:
    """A failed ``os.replace`` must rescind the acknowledgement."""

    def test_consume_refuses_when_the_record_cannot_be_removed(
        self, clean_registry, monkeypatch
    ):
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-admit-fail"
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-f1")
        assert sid in PENDING_GOAL_CONTINUATION

        _break_replace(monkeypatch, store)

        admitted = store.consume_pending_goal_continuation(
            sid, PROMPT, "tok-f1", "att-fail-1"
        )
        assert admitted == store.CONSUME_COMMIT_FAILED, (
            "an admitted consume reported success while its durable removal failed"
        )

        # The intent is STILL live, because the record is still claimable.
        assert sid in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(sid, {}).get("prompt") == PROMPT

        # ...and no rollback receipt was minted for a consume that never landed.
        assert (
            store.pop_goal_continuation_rollback_receipt(sid, "att-fail-1") is None
        )

        # The failure is observable, not swallowed.
        assert store.durability_diagnostics()["last_write_error"]

        # Cold restore after the failed replacement restores the STALE record:
        # that is exactly the residue the refusal exists to avoid spending.
        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
            store._GENERATION = 0
        store.restore_goal_continuations()
        assert sid in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(sid, {}).get("prompt") == PROMPT

    def test_clear_does_not_claim_success_when_the_retire_cannot_commit(
        self, clean_registry, monkeypatch
    ):
        """#7862 round 7 finding 1, second half: the real clear function."""
        from api import goal_continuation_store as store

        sid = "sess-clear-fail"

        # Arm durably first (the snapshot still works), then break the writer.
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-c1")
        assert store.retire_pending_goal_continuation(sid, reason="consumed") is True
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-c2")

        _break_replace(monkeypatch, store)

        from api.goals import goal_command_payload

        payload = goal_command_payload(sid, "clear")
        assert payload.get("ok") is False, (
            "/goal clear reported ok:true while the durable retirement failed"
        )
        assert payload.get("error") == "continuation_retire_failed"

        # The old claimable record is still on disk, so the clear did NOT take
        # effect durably -- the error payload must say so rather than lie.
        raw = json.loads(store._PENDING_GOAL_FILE.read_text(encoding="utf-8"))
        assert raw["records"].get(sid, {}).get("prompt") == PROMPT

        # A cold restore brings the stale record back, which is what makes the
        # success claim unsafe.
        with store._LOCK:
            store._PENDING_GOAL_FILE.write_text("", encoding="utf-8")  # keep file present
        with store._LOCK:
            store._RETIRED_LOG.clear()
        assert store.durability_diagnostics()["last_write_error"] is not None

    def test_clear_still_reports_success_on_a_durable_retire(
        self, clean_registry
    ):
        """The happy path is unaffected: a committed clear is still ok:true."""
        from api import goal_continuation_store as store

        sid = "sess-clear-ok"
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-ok")

        from api.goals import goal_command_payload

        payload = goal_command_payload(sid, "clear")
        assert payload.get("ok") is True
        assert payload.get("action") == "clear"
        assert store.retire_pending_goal_continuation(sid, reason="consumed") is not None

        with store._LOCK:
            store._RETIRED_LOG.clear()

    def test_retire_returns_the_durable_outcome(self, clean_registry, monkeypatch):
        """The mutator itself must return the commit outcome, not None."""
        from api import goal_continuation_store as store

        sid = "sess-retire-flag"
        store.arm_pending_goal_continuation(sid, PROMPT)

        _break_replace(monkeypatch, store)
        assert store.retire_pending_goal_continuation(sid, reason="cleared") is False

    def test_arm_returns_the_durable_outcome(self, clean_registry, monkeypatch):
        from api import goal_continuation_store as store

        _break_replace(monkeypatch, store)
        assert store.arm_pending_goal_continuation("sess-arm-fail", PROMPT) is False


# ---------------------------------------------------------------------------
# Finding 2: the registry-to-session handoff must be crash-reconcilable.
# ---------------------------------------------------------------------------


class TestRegistryToSessionHandoffIsCrashReconcilable:
    """The consume must leave a durable handoff before the start is admitted.

    Today a consume deletes the durable record and keeps only an in-memory
    rollback receipt. If the process dies between that delete and
    ``_prepare_chat_start_session_for_stream`` writing ``pending_user_message``,
    a cold restore finds ZERO records: the intent was spent and nothing
    durable can resume it. The retry then runs as an ordinary turn.

    A durable handoff marker (minted by the consuming attempt, cleared by the
    launch that becomes durable) makes the seam crash-reconcilable: a cold
    restore can still re-dispatch the recorded continuation instead of losing
    it.
    """

    def test_a_consuming_attempt_leaves_a_durable_handoff_marker(
        self, clean_registry
    ):
        from api import goal_continuation_store as store

        sid = "sess-handoff"
        _arm_and_consume(store, sid, attempt_id="att-handoff", token="tok-ho")

        handoff = store.pop_goal_continuation_handoff(sid, "att-handoff")
        assert handoff is not None, (
            "the consuming attempt left no durable handoff for a cold restore"
        )
        assert handoff.get("prompt") == PROMPT
        assert handoff.get("attempt_id") == "att-handoff"
        assert handoff.get("continuation_id") == "tok-ho"

        # The handoff must survive a cold restart on its own, so a process
        # loss between the consume and the pending-start is recoverable.
        with store._LOCK:
            store._CONTINUATION_HANDOFF_TOKENS.clear()
            store._ROLLBACK_RECEIPTS.clear()
            store._GENERATION = 0
        restored = store.restore_goal_continuations()
        assert restored == 1
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        assert sid in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(sid, {}).get("prompt") == PROMPT
        assert (
            PENDING_GOAL_CONTINUATION_RECORDS.get(sid, {}).get("continuation_id")
            == "tok-ho"
        )

    def test_a_successful_launch_clears_the_handoff_marker(self, clean_registry):
        """The handoff is not a second intent: it must be discharged on launch."""
        from api import goal_continuation_store as store

        sid = "sess-handoff-discharge"
        _arm_and_consume(store, sid, attempt_id="att-hd", token="tok-hd")

        store.discard_goal_continuation_handoff(sid, "att-hd")

        # Nothing re-arms the intent that the successful launch already ran.
        with store._LOCK:
            store._GENERATION = 0
        assert store.restore_goal_continuations() == 0
        assert store.pop_goal_continuation_handoff(sid, "att-hd") is None

    def test_an_unrelated_attempt_cannot_claim_the_handoff(self, clean_registry):
        from api import goal_continuation_store as store

        sid = "sess-handoff-other"
        _arm_and_consume(store, sid, attempt_id="att-mine", token="tok-mine")

        assert store.pop_goal_continuation_handoff(sid, "att-someone-else") is None
        assert store.pop_goal_continuation_handoff(sid, "att-mine") is not None

    def test_the_route_keeps_the_handoff_inside_the_lock_before_pending(
        self, clean_registry
    ):
        """The route ordering must actually enable the durable handoff.

        This is the source-anchored half of finding 2: the consume stays
        before the pending-start, and the handoff minted by the consume is
        carried by the rejected-start rollback path, so a crash between the
        two leaves a durable owner instead of a live-only receipt.
        """
        src = _routes_source()
        # Scope to the one function where the ordering matters, so a match on an
        # unrelated definition (or another call site) cannot satisfy this.
        body = _first_match(src, r"def _start_chat_stream_for_session\(")
        assert body is not None
        scope = src[body.start():body.start() + 40000]
        consume_m = _first_match(
            scope,
            r"consume_pending_goal_continuation\(\s*s\.session_id,\s*msg,\s*"
            r"goal_continuation_id\s*,\s*goal_continuation_attempt_id\s*,?\s*\)",
        )
        assert consume_m is not None, "the match-gated consume call is gone"
        pending_m = _first_match(
            scope, r"_prepare_chat_start_session_for_stream\("
        )
        assert pending_m is not None
        assert consume_m.start() < pending_m.start(), (
            "consume must still precede the durable pending-start handoff"
        )

        # The launch path discharges the handoff, and the rejected path keeps
        # it so a later restore can adopt it. Compare in a comment-stripped view
        # so a large explanatory comment block cannot push the call out of the
        # window and make the check meaningless.
        stripped = "".join(
            line for line in scope.splitlines(keepends=True) if not line.lstrip().startswith("#")
        )
        thr_m = _first_match(stripped, r"thr\.start\(\)\s*")
        assert thr_m is not None
        tail = stripped[thr_m.end():thr_m.end() + 500]
        assert "discard_goal_continuation_handoff(" in tail, (
            "a successful launch must discharge the durable handoff"
        )
        # ...and it must come BEFORE the receipt discard, so a launch cannot
        # leave the turn's durable owner behind while already running it.
        assert (
            tail.index("discard_goal_continuation_handoff(")
            < tail.index("discard_goal_continuation_rollback_receipt(")
        ), "the handoff must be discharged with the receipt, not after it"

        # A held-live attempt must report liveness, so the sweep cannot age it
        # out while the start is still in flight.
        assert "reclaim_goal_continuation_receipt(" in scope, (
            "an admitted start must report itself live to the receipt sweep"
        )

    def test_the_rejected_start_rolls_back_to_a_durable_owner(
        self, clean_registry
    ):
        """Rejected start: the rollback must restore a MATCHABLE intent.

        The store-level contract behind the route ordering. With the handoff
        still live, the rollback restores marker + record together, so the
        retry consumes the continuation instead of running as an ordinary
        turn.
        """
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-handoff-rollback"
        _arm_and_consume(store, sid, attempt_id="att-hrb", token="tok-hrb")

        handoff = store.pop_goal_continuation_handoff(sid, "att-hrb")
        assert handoff is not None
        assert store.restore_pending_goal_continuation(sid, handoff) is True

        assert sid in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(sid, {}).get("prompt") == PROMPT
        # The retry matches on identity AND text.
        assert store.consume_pending_goal_continuation(sid, PROMPT, "tok-hrb")== store.CONSUME_COMMITTED


# ---------------------------------------------------------------------------
# Finding 3: age alone is not evidence that an attempt ended.
# ---------------------------------------------------------------------------


class TestElapsedAgeIsNotAttemptCompletion:
    """A held-live registration callback must not have its receipt swept.

    The sweep asserted that a receipt older than the TTL cannot belong to a
    live request. The route has no corresponding deadline, so a long-lived
    registration callback (or any attempt still inside its start) keeps its
    receipt claimable no matter how much wall time passes.
    """

    def test_a_held_live_attempt_keeps_its_receipt_across_elapsed_time(
        self, clean_registry, monkeypatch
    ):
        """A live attempt's registration callback outlives the TTL.

        The reviewer's scenario: the route's registration callback is still
        live, simulated elapsed wall time plus another consume sweeps its
        receipt, subsequent rejection cannot restore intent, and the matching
        retry becomes an ordinary turn. The route now reports the attempt live
        on admission; here the attempt reports it again while it is still
        holding its start, which the sweep must honour.
        """
        from api import goal_continuation_store as store

        sid = "sess-held-live"
        _arm_and_consume(store, sid, attempt_id="att-live", token="tok-live")

        # Simulate elapsed wall time far past the sweep's TTL.
        real_time = store.time.time
        monkeypatch.setattr(store.time, "time", lambda: real_time() + 10_000_000)

        # The attempt is STILL LIVE: it reports itself, exactly as
        # ``_start_chat_stream_for_session`` does while the start is in flight.
        assert store.reclaim_goal_continuation_receipt(sid, "att-live") is True

        # A neighbouring consume runs the sweep (that is when it fires).
        other = "sess-sweep-neighbour"
        store.arm_pending_goal_continuation(other, PROMPT, continuation_id="tok-n")
        assert store.consume_pending_goal_continuation(other, PROMPT, "tok-n", "att-n")

        # The held attempt's receipt is STILL claimable.
        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-live")
        assert receipt is not None, (
            "an attempt reporting itself live had its receipt swept on age alone"
        )
        assert receipt.get("continuation_id") == "tok-live"

        # So its rejection can still restore intent, and the retry consumes.
        assert store.restore_pending_goal_continuation(sid, receipt) is True
        assert store.consume_pending_goal_continuation(sid, PROMPT, "tok-live")== store.CONSUME_COMMITTED

    def test_a_live_attempt_is_reclaimed_not_rejected_after_a_sweep(
        self, clean_registry, monkeypatch
    ):
        """The reclaim side: the attempt OWNS the receipt, not the clock.

        A sweep may only conclude the attempt ended once it has stopped
        reporting. Here the owning attempt explicitly reclaims a receipt the TTL
        considered expired, and that reclaim is still honoured.
        """
        from api import goal_continuation_store as store

        sid = "sess-reclaim"
        _arm_and_consume(store, sid, attempt_id="att-reclaim", token="tok-rc")

        real_time = store.time.time
        monkeypatch.setattr(store.time, "time", lambda: real_time() + 10_000_000)

        # The attempt declares itself still live by reclaiming its slot.
        assert store.reclaim_goal_continuation_receipt(sid, "att-reclaim") is True

        # A neighbouring consume runs the TTL sweep; it must not touch the
        # reclaimed slot.
        other = "sess-reclaim-neighbour"
        store.arm_pending_goal_continuation(other, PROMPT, continuation_id="tok-rn")
        assert store.consume_pending_goal_continuation(other, PROMPT, "tok-rn", "att-rn")

        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-reclaim")
        assert receipt is not None
        assert store.restore_pending_goal_continuation(sid, receipt) is True

    def test_a_receipt_whose_attempt_is_over_is_still_bounded(
        self, clean_registry, monkeypatch
    ):
        """The bound is not removed: an abandoned receipt still goes.

        This is the negative control that discriminates "live" from "old": a
        receipt whose attempt has stopped reporting and which nobody reclaims is
        dropped, so storage stays bounded. Without this control, "never sweep"
        would also pass, which is not the fix.
        """
        from api import goal_continuation_store as store

        for i in range(store._MAX_ROLLBACK_RECEIPTS + 20):
            leaked = f"sess-abandoned-{i}"
            store.arm_pending_goal_continuation(leaked, PROMPT)
            assert store.consume_pending_goal_continuation(
                leaked, PROMPT, "", f"att-dead-{i}"
            )

        real_time = store.time.time
        monkeypatch.setattr(store.time, "time", lambda: real_time() + 10_000_000)

        # A neighbouring consume runs the sweep. Nobody reclaims the abandoned
        # receipts, so they are dropped -- the bound round 6 established holds.
        other = "sess-abandoned-neighbour"
        store.arm_pending_goal_continuation(other, PROMPT, continuation_id="tok-an")
        assert store.consume_pending_goal_continuation(other, PROMPT, "tok-an", "att-an")

        assert len(store._ROLLBACK_RECEIPTS) == 1  # only the live neighbour's own

    def test_reclaim_is_refused_for_an_unknown_attempt(self, clean_registry):
        from api import goal_continuation_store as store

        assert store.reclaim_goal_continuation_receipt("sess-none", "att-none") is False
        store.arm_pending_goal_continuation("sess-known", PROMPT)
        assert store.reclaim_goal_continuation_receipt("sess-known", "att-x") is False


def _routes_source() -> str:
    from pathlib import Path

    return Path("api/routes.py").read_text(encoding="utf-8")


def _first_match(src: str, pattern: str):
    import re

    return re.search(pattern, src, re.DOTALL)
