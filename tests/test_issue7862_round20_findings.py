"""#7862 re-gate round 20 (review 5719342806, the two open compositions).

The maintainer's CHANGES_REQUESTED blocked release on two compositions of ONE
requirement: an admission has exactly one live owner, and every path that could
start a worker — or undo one — must prove ownership against the durable state
instead of against this process's memory.

1. **A prior merge can turn a revoked consume into the ordinary-start success
   path.** ``record_goal_continuation_stream()`` answers True when the local
   handoff key is absent on entry, which conflates "genuinely ordinary, no
   consume" with "consumed and subsequently revoked". Rounds 17 and 18 closed
   the reachable shapes of that fast path (the live receipt, and the route's
   own ``consumed`` provenance once the receipt is swept). What was still open
   is the composition: A consumes, B takes over after the lease window, an
   unrelated transaction in A's process merges away A's revoked handoff, and
   THEN A's rejected start is compensated. The bind refusal and the
   compensation were each pinned on their own; nobody had pinned the second
   one landing on top of the first, which is the only order a real rejected
   start produces.

2. **Refusal rollback can remove the successor attempt's handoff.** The
   compensation validates against the current attempt ownership, but it then
   mutated the SESSION's whole handoff table. The guard is what kept that
   honest; the mutation itself was still a retirement's shape, not a
   compensation's. This round scopes the spend to the losing attempt's own
   custody so the guard and the mutation agree instead of the guard carrying
   the whole requirement alone.

Two invariant cases pin the residual holes the maintainer's schedule names but
that the reachable paths cannot produce on their own:

* an **adoption conversion anchor** is not custody. ``restore_goal_continuations``
  keeps the in-memory token of a handoff it converted into a record (the
  receipt's liveness anchor needs it), keeps its ``_handoff_stream_id`` (the
  conversion copies the durable entry verbatim and strips that field only from
  the restored record), and the reconciliation deliberately preserves it. The
  stream-identity comparison in ``goal_continuation_attempt_owns_admission``
  therefore read "still ours" for an admission a restore had already spent —
  the one ownership consumer that disagreed with the rollback restore's own
  guard, which skips a marked token for exactly this reason.

* the rollback's handoff spend is keyed by (session, attempt), like the
  admission it settles.

The out-of-process schedules reuse the round-16 separate-process harness
verbatim — real interpreters, real module-level registry memory, a shared
durable file and the real ``fcntl.flock`` — because a single interpreter cannot
model two live owners. The worker's wall clock is shiftable so elapsed
admission time is simulatable without sleeping through a 15-minute lease.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

PROMPT = "keep refining the release checklist"

REPO_ROOT = Path(__file__).resolve().parents[1]

from api.goal_continuation_store import _ROLLBACK_RECEIPT_TTL_SECONDS as _TTL  # noqa: E402

# The round-16 separate-process harness and the round-17 command extensions
# (``consume_restored`` / ``rollback`` / ``route_launch_regen``) are reused
# verbatim: the maintainer asked to KEEP that design.
from tests.test_issue7862_round17_findings import (  # noqa: E402
    _Worker as _Round17Worker,
    _wait_for_parked,
    _wait_for_stream_release,
)


class _Worker(_Round17Worker):
    """One live WebUI process with the round-17 command extensions."""


@pytest.fixture
def workers(request):
    """Track every live worker so a failing assertion still reaps them."""
    tmp_path = request.getfixturevalue("tmp_path")
    opened: list[_Worker] = []

    def _open(name, clock_offset=0.0):
        worker = _Worker(tmp_path, name, clock_offset=clock_offset)
        opened.append(worker)
        return worker

    yield _open
    for worker in opened:
        worker.close()


@pytest.fixture
def clean_registry():
    """Isolate the registry between cases (the round-18 shape)."""
    from api import goal_continuation_store as store
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_PROMPTS,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    def _reset():
        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_PROMPTS.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
            store._LAST_LOAD_ERROR = None
            store._LAST_WRITE_ERROR = None
            store._RETIRED_LOG.clear()
            store._ROLLBACK_RECEIPTS.clear()
            store._RETIRED_GENERATIONS.clear()
            store._TOMBSTONES.clear()
            store._CONTINUATION_HANDOFF_TOKENS.clear()
            store._ADOPTED_HANDOFF_SESSIONS.clear()
            store._GENERATION = 0
            store._RESTORE_WRITE_FAILED_SESSIONS.clear()
            store._DEFERRED_HANDOFF_RECOVERY.clear()
            store._SPENT_GENERATIONS.clear()
        timer = getattr(store, "_DEFERRED_RECOVERY_TIMER", None)
        store._DEFERRED_RECOVERY_TIMER = None
        if timer is not None and hasattr(timer, "cancel"):
            try:
                timer.cancel()
            except Exception:
                pass
        store._PENDING_GOAL_FILE.unlink(missing_ok=True)

    _reset()
    yield store
    _reset()


def _arm_and_consume(store, sid: str, attempt: str, token: str) -> str:
    """Arm an intent and spend it as one start attempt, for real."""
    assert store.arm_pending_goal_continuation(sid, PROMPT, continuation_id=token), (
        "the intent could not be armed"
    )
    outcome = store.consume_pending_goal_continuation(sid, PROMPT, token, attempt)
    assert outcome == store.CONSUME_COMMITTED, (
        f"the admission did not commit: {outcome}"
    )
    return token


def _conversion_anchor(store, sid: str, attempt: str, generation: int) -> None:
    """Model the anchor ``restore_goal_continuations()`` leaves behind.

    An adoption is a CONVERSION, not a copy: the durable handoff is consumed,
    the in-memory token survives (it anchors the rollback receipt's liveness)
    and is marked, and the closing snapshot withholds the marked entry so the
    next cold restore cannot adopt it a second time. A token that does not
    exist yet is created in the shape the conversion produces; a token that
    does exist is only marked, so its stream identity — the field the
    conversion copies verbatim and strips only from the restored record — is
    preserved exactly as production leaves it.

    The withholding snapshot is committed here too, because the merge treats a
    key disk carries as authoritative: without it the very next transaction
    would replace the marked token with its unmarked durable copy.
    """
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    with store._LOCK:
        token = store._CONTINUATION_HANDOFF_TOKENS.get((sid, attempt))
        if token is None:
            token = {
                "prompt": PROMPT,
                "generation": generation,
                "created_at": store.time.time(),
                "reason": "goal_continue",
                "continuation_id": "",
            }
            store._CONTINUATION_HANDOFF_TOKENS[(sid, attempt)] = token
        token["_handoff_adopted_into_record"] = True
        if "_handoff_minted_at" not in token:
            token["_handoff_minted_at"] = store.time.time()
        store._ADOPTED_HANDOFF_SESSIONS.add(sid)
        assert store._write_registry_unlocked(
            PENDING_GOAL_CONTINUATION_RECORDS, context="round20 conversion anchor"
        ), "the conversion could not be made durable"


# ── finding 1: the pre-bind merge, followed by the compensation ────────────


class TestAPreBindMergeThenCompensationKeepsTheSuccessor:
    """The maintainer's exact schedule, in the order a real rejected start
    produces it.

    A consumes; its ownership lease expires; a cold peer takes the admission
    over and spends the restored intent as attempt B; an UNRELATED registry
    write in A's process merges A's revoked handoff away; and only then does
    A's rejected start land — the bind first, the compensation second. The
    bind refusal was pinned on its own (round 17) and the compensation was
    pinned on its own (rounds 17/18); this is the composition, where the
    compensation runs against a registry the merge has already reconciled.
    """

    def _takeover_and_spend(self, workers, sid: str, continuation_id: str):
        owner = workers("owner")
        admitted = owner.call("arm_consume", sid=sid, prompt=PROMPT, attempt="att-A")
        assert admitted["outcome"] == "committed", (
            "the real admission did not commit: the schedule cannot start"
        )
        assert admitted["continuation_id"], "the consume minted no continuation token"

        # The owner never renews: it is gone. Once its lease expires a cold
        # peer legitimately takes the admission over.
        owner.call("set_clock", offset=_TTL + 120)
        peer = workers("peer", clock_offset=_TTL + 120)
        assert peer.call("restore")["restored"] == 1, (
            "an abandoned admission was not recovered: the takeover half of "
            "the schedule cannot start"
        )
        # B spends the restored intent as ITS OWN start attempt.
        consumed = peer.call(
            "consume_restored",
            sid=sid,
            prompt=PROMPT,
            continuation_id=admitted["continuation_id"],
            attempt="att-B",
        )
        assert consumed["outcome"] == "committed", (
            "the successor did not consume the restored intent: the "
            "compensation half of the schedule cannot start"
        )
        return owner, peer

    def test_the_pre_bind_merge_refuses_the_bind_and_the_rollback(self, workers):
        sid = "sess-r20-pre-bind-merge"
        owner, peer = self._takeover_and_spend(workers, sid, "1" * 32)
        try:
            # An UNRELATED registry write in the owner's process: its
            # reconciliation drops the locally cached copy of an admission the
            # durable table no longer owns. This is the merge the maintainer's
            # schedule puts BEFORE the bind.
            assert owner.call("peer_arm", index=1)["ok"] is True
            dump = owner.call("dump")
            assert f"{sid}\x00att-A" not in dump["memory_handoffs"], (
                "the unrelated transaction did not merge the revoked handoff "
                "away: the schedule cannot exercise the pre-bind fast path"
            )

            # The bind is the launch fence. A missing local key must not read
            # as "genuinely ordinary, no consume" for an attempt whose consume
            # committed: the receipt it minted is the provenance, and the
            # route launches on this Boolean.
            assert (
                owner.call("bind", sid=sid, attempt="att-A", stream="stream-A")["ok"]
                is False
            ), (
                "the missing-key fast path answered True for a consumed and "
                "revoked admission after a prior merge: the route walks the "
                "ordinary path into thr.start() and a second goal worker runs "
                "for one intent (finding 1)"
            )

            # Now the compensation, in the only order a rejected start
            # produces: the bind refused, so the start is rolled back. B has
            # consumed the record, so the pending slot is empty and every
            # in-memory check would pass — the restore must still refuse,
            # because B owns this session's admission now.
            rolled = owner.call("rollback", sid=sid, attempt="att-A")
            assert rolled["accepted"] is False, (
                "the compensation recreated the pending intent beside the "
                "successor's running admission: the goal loop now has a "
                "duplicate continuation (finding 2)"
            )
            dump = peer.call("dump")
            assert sid not in dump["records"], (
                "the rollback recreated the pending intent the successor had "
                "already spent (finding 2)"
            )
            assert f"{sid}\x00att-B" in dump["handoffs"], (
                "the rollback's handoff deletion removed the SUCCESSOR "
                "attempt's durable admission: its crash-recovery evidence is "
                "gone (finding 2)"
            )
            assert f"{sid}\x00att-B" in dump["memory_receipts"], (
                "the successor's rollback receipt was disturbed by another "
                "attempt's compensation (finding 2)"
            )
        finally:
            for worker in (peer, owner):
                worker.close()

    def test_a_never_consumed_attempt_still_binds_lock_free(self, workers):
        """Control: the genuinely ordinary path stays True.

        An attempt that consumed nothing has no receipt, so the fast path must
        keep answering True without touching the cross-process lock — the
        round-15 BRICK contract this provenance check must not regress.
        """
        owner = workers("owner")
        try:
            assert (
                owner.call(
                    "bind", sid="sess-r20-ordinary", attempt="att-none", stream="s"
                )["ok"]
                is True
            ), (
                "an attempt with no consume lost its lock-free ordinary bind: "
                "the provenance check over-refuses"
            )
        finally:
            owner.close()


# ── finding 1 (b): the post-bind pause, with the successor's spend ─────────


class TestAPostBindPauseWithASuccessorThatAlreadySpentTheIntent:
    """The successful-bind-to-worker-release window, at the route level, with
    the step the round-17 route test stops short of.

    The real regeneration route consumes the intent, binds the admission's
    stream identity durably — renewing the ownership lease — and then releases
    its file transaction before the session save, the workspace work and the
    release of its gated worker. Round 17 parked there and let a peer take the
    admission over; this schedule also lets that peer SPEND the restored
    intent as attempt B before A resumes, so the compensation that follows the
    refusal runs against a registry where the pending slot is empty and the
    successor owns the admission. Age without revoking the old invocation is
    not a fence through launch, and a refusal that leaves a duplicate
    continuation behind is not a closure either.
    """

    def test_a_takeover_after_the_bind_refuses_and_rolls_back_cleanly(self, workers):
        sid = "sess-r20-post-bind-pause"
        late = _TTL + 120
        owner = workers("owner")
        peer = None
        try:
            state_root = owner.state_dir
            gate = state_root / "r20-regen-gate"
            parked = state_root / "r20-regen-parked"
            gate.write_text("hold\n", encoding="utf-8")
            cont_id = "6" * 32
            assert (
                owner.call("arm", sid=sid, prompt=PROMPT, continuation_id=cont_id)["ok"]
                is True
            )

            started = owner.call(
                "route_launch_regen",
                sid=sid,
                prompt=PROMPT,
                continuation_id=cont_id,
                workspace=str(state_root / "ws-owner"),
                gate=str(gate),
                parked=str(parked),
                gate_timeout=90,
            )
            assert started["started"] is True
            _wait_for_parked(owner, parked)
            assert owner.call("route_result")["done"] is False

            # The owner never renews again: its lease expires and a cold peer
            # legitimately takes the admission over — AFTER the bind.
            peer = workers("peer", clock_offset=late)
            assert peer.call("restore")["restored"] == 1, (
                "an abandoned admission was not recovered: the takeover half "
                "of the schedule cannot start"
            )
            # B spends the restored intent as ITS OWN start attempt.
            consumed = peer.call(
                "consume_restored",
                sid=sid,
                prompt=PROMPT,
                continuation_id=cont_id,
                attempt="att-B",
            )
            assert consumed["outcome"] == "committed", (
                "the successor did not consume the restored intent: the "
                "compensation half of the schedule cannot start"
            )

            # A resumes: the pre-release ownership revalidation must refuse.
            gate.unlink(missing_ok=True)
            deadline = time.time() + 60
            result = None
            while time.time() < deadline:
                reply = owner.call("route_result")
                if reply.get("done"):
                    result = reply["result"]
                    break
                time.sleep(0.05)
            assert result is not None, "the paused regeneration route never finished"

            stream_id = str(result.get("stream_id") or "")
            assert stream_id, f"the regeneration route returned no stream: {result}"
            assert not result.get("workers"), (
                "the gated worker EXECUTED after a peer took the admission "
                "over and spent the intent: the bind renewed a timestamp, but "
                "age without revoking the old invocation is not a fence "
                "through launch (finding 1)"
            )
            assert result.get("exception") == "_GoalContinuationCommitFailed", (
                f"the route acknowledged a taken-over admission: {result}"
            )
            _wait_for_stream_release(owner, stream_id)

            # The refusal's compensation must not resurrect the intent beside
            # the successor's running admission, and must not spend B's
            # custody either.
            dump = peer.call("dump")
            assert sid not in dump["records"], (
                "the rollback recreated the pending intent the successor had "
                "already spent (finding 2)"
            )
            assert f"{sid}\x00att-B" in dump["handoffs"], (
                "the rollback's handoff deletion removed the SUCCESSOR "
                "attempt's durable admission: its crash-recovery evidence is "
                "gone (finding 2)"
            )
            assert f"{sid}\x00att-B" in dump["memory_receipts"], (
                "the successor's rollback receipt was disturbed by another "
                "attempt's compensation (finding 2)"
            )
        finally:
            for worker in (peer, owner):
                if worker is not None:
                    worker.close()


# ── finding 2: the compensation spends only the losing attempt's custody ───


class TestTheRollbackSpendsOnlyTheLosingAttemptsCustody:
    """The admission is keyed by (session, attempt), so the compensation that
    settles it has to be keyed the same way.

    A rejected start lost ONE admission. The restore's admission guard already
    proves no other attempt owns this session's admission before it lands, so
    the only tokens left are the losing attempt's own custody plus any
    conversion anchors a restore left behind — and the mutation used to delete
    the session's whole handoff table anyway. Scoping the spend to the losing
    attempt makes the guard and the mutation agree instead of leaving the
    guard to carry the requirement alone.
    """

    def test_a_successful_rollback_leaves_another_attempts_anchor_alone(
        self, clean_registry
    ):
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        store = clean_registry
        sid = "sess-r20-custody-scope"
        token = "2" * 32
        generation = 7
        _arm_and_consume(store, sid, "att-A", token)
        with store._LOCK:
            generation = int(
                (store._ROLLBACK_RECEIPTS[(sid, "att-A")] or {}).get("generation") or 0
            )
        assert generation, "the consume stamped no generation on the receipt"

        # The conversion anchor a restore leaves behind for ANOTHER attempt of
        # the same session. It is the one token shape the admission guard lets
        # through, because a marked token is explicitly not custody.
        _conversion_anchor(store, sid, "att-B", generation)
        assert (
            store._CONTINUATION_HANDOFF_TOKENS[(sid, "att-A")].get(
                "_handoff_adopted_into_record"
            )
            is None
        ), "the losing attempt's own custody must not be pre-marked"

        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-A")
        assert receipt is not None and receipt.get("attempt_id") == "att-A"
        assert store.restore_pending_goal_continuation(sid, receipt) is True, (
            "the rollback refused its own rejected start: the retry loop "
            "cannot recover the continuation"
        )

        with store._LOCK:
            tokens = {
                key: dict(value)
                for key, value in store._CONTINUATION_HANDOFF_TOKENS.items()
                if key[0] == sid
            }
        assert (sid, "att-A") not in tokens, (
            "the losing attempt's own custody was not spent: a successful "
            "retry followed by a restart restores the continuation a second "
            "time (round 9)"
        )
        assert (sid, "att-B") in tokens, (
            "the rollback spent custody belonging to ANOTHER attempt: the "
            "session-wide handoff deletion is a retirement's mutation, not a "
            "compensation's (finding 2)"
        )
        assert tokens[(sid, "att-B")].get("_handoff_adopted_into_record") is True, (
            "the other attempt's conversion anchor was rewritten"
        )
        assert sid in store._ADOPTED_HANDOFF_SESSIONS, (
            "the withheld set was cleared while a token is still withheld "
            "from disk on this session's behalf"
        )
        # The restore itself still landed, and the intent is claimable again.
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, token, "att-A2")
            == store.CONSUME_COMMITTED
        ), "the restored intent is no longer consumable"

    def test_a_legacy_record_without_an_attempt_still_spends_the_session(
        self, clean_registry
    ):
        """Control: with no attempt identity to scope to, the spend keeps the
        session-wide shape rather than leaving a spent handoff claimable."""
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        store = clean_registry
        sid = "sess-r20-legacy-custody"
        # A record with no attempt identity: the pre-round-17 shape, where the
        # receipt carried nothing to key a scoped spend on.
        legacy = {
            "prompt": PROMPT,
            "generation": 909,
            "created_at": store.time.time(),
            "reason": "goal_continue",
            "continuation_id": "",
        }
        with store._LOCK:
            store._record_continuation_handoff_unlocked(sid, dict(legacy), "attempt")
        assert store.restore_pending_goal_continuation(sid, legacy) is True
        with store._LOCK:
            assert not any(
                key[0] == sid for key in store._CONTINUATION_HANDOFF_TOKENS
            ), "the legacy rollback left a claimable handoff behind"
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS


# ── finding 1 (b): an adoption conversion anchor is not custody ────────────


class TestAnAdoptionAnchorIsNotCustody:
    """The successful-bind-to-worker-release window, closed against the one
    takeover shape the merge cannot show the owner.

    A peer's adoption converts the owner's handoff into a claimable record and
    withholds the durable copy, so the owner's own reconciliation drops its
    local token and the pre-release ownership check refuses. When the
    conversion happens somewhere the token SURVIVES marked, the token keeps
    the stream identity the conversion copied verbatim — and the
    stream-identity comparison then read "still ours" for an admission a
    restore had already spent. A marked token is not custody: the rollback
    restore's own guard already skips one for exactly this reason, so this
    ownership check has to agree.
    """

    def test_a_converted_admission_is_not_owned_by_its_old_attempt(
        self, clean_registry
    ):
        store = clean_registry
        sid = "sess-r20-anchor-not-custody"
        token = "4" * 32
        _arm_and_consume(store, sid, "att-A", token)
        assert (
            store.record_goal_continuation_stream(sid, "att-A", "stream-A") is True
        ), "the admission's stream identity could not be bound"

        with store._LOCK:
            bound = store._CONTINUATION_HANDOFF_TOKENS[(sid, "att-A")]
            generation = int(bound.get("generation") or 0)
            assert bound.get("_handoff_stream_id") == "stream-A"

        # A restore converts this admission into a claimable record: the token
        # stays, keeps its stream identity, and is marked.
        _conversion_anchor(store, sid, "att-A", generation)
        # The adoption re-arms the intent the conversion restored.
        assert store.arm_pending_goal_continuation(
            sid, PROMPT, continuation_id=token
        ), "the adoption's restored record could not be re-armed"

        assert (
            store.goal_continuation_attempt_owns_admission(sid, "att-A", "stream-A")
            is False
        ), (
            "an admission a restore converted into a claimable record still "
            "answered as owned: the route releases its worker for a turn the "
            "record already describes, and the continuation runs twice "
            "(finding 1)"
        )

    def test_a_live_unconverted_admission_is_still_owned(self, clean_registry):
        """Control: the fence must not over-refuse the ordinary owner."""
        store = clean_registry
        sid = "sess-r20-anchor-control"
        token = "5" * 32
        _arm_and_consume(store, sid, "att-A", token)
        assert store.record_goal_continuation_stream(sid, "att-A", "stream-A") is True
        assert (
            store.goal_continuation_attempt_owns_admission(sid, "att-A", "stream-A")
            is True
        ), "the ownership fence refused an admission nothing had taken over"
        assert (
            store.goal_continuation_attempt_owns_admission(sid, "att-A", "stream-B")
            is False
        ), "a different stream identity was accepted as the same admission"
