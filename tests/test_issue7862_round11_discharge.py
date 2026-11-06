"""#7862 re-gate CORE: a failed durable discharge after a successful launch.

The Oct 10 re-gate's last CORE: ``discard_goal_continuation_handoff()``
returns ``False`` when the registry replacement fails, but the two launch
paths (the worker-thread start and the regeneration start) ignore it, so a
launch that succeeded while its discharge write failed returns a stream
with ``goal_related=true`` and a cold restore still recovers the
already-admitted continuation.

The reviewer's exact fix: persist the admission with its attempt/stream
identity BEFORE worker execution, handle the failed settlement in both
launch paths, and reconcile recovery against that persisted identity
instead of restoring admitted turns as pending intent.

That is what this module pins, in two halves:

* **Store half** — the handoff token (which IS the durable admission,
  written in the same snapshot as the consume) learns the stream identity
  of the attempt it admits. A stream id recorded on the token is what
  makes a failed discharge RECONCILABLE rather than merely visible: the
  stream is real, the turn is running, and a later cold restore must be
  able to see that this handoff's attempt already launched under that
  stream instead of re-adopting it as interrupted intent.
* **Route half** — both launch paths must (a) record the stream id on the
  admission before the worker starts, and (b) consume the discharge's
  boolean instead of dropping it: a failed settlement after a live launch
  has to be reported (and reconciled), not acknowledged as a clean 200.

An unrelated detail that matters for the store half: the stream id must
NOT be confused with the handoff's liveness clock. A handoff with a
stream id is no longer merely "an interrupted start that may be
re-adopted" — it is "an admitted start that ran". Adoption therefore
refuses tokens carrying a stream id, so a discharge that failed forever
cannot resurrect a continuation whose turn already executed.

RED control: each case fails on the pre-fix store/routes (the stream id
does not exist; the discharge return value is dropped).
"""

from __future__ import annotations

import re as _re

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

PROMPT = "keep refining the release checklist"
TOKEN = "tok-discharge-1"


@pytest.fixture
def clean_registry(monkeypatch):
    """Isolate the registry between cases (same shape as round 7/11)."""
    from api import goal_continuation_store as store
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    def _reset():
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
            store._ADOPTED_HANDOFF_SESSIONS.clear()
            store._GENERATION = 0
        store._PENDING_GOAL_FILE.unlink(missing_ok=True)

    _reset()
    yield
    _reset()


def _cold_restore(store):
    """Simulate the cold-process half of a restart.

    ``restore_goal_continuations`` skips a handoff whose token is already in
    memory (the warm path: the same process still owns the admission), so a
    cold-restore case must first drop the in-memory half — exactly what a new
    process starts with. Only the durable file survives a restart.
    """
    with store._LOCK:
        store._CONTINUATION_HANDOFF_TOKENS.clear()
        store._ADOPTED_HANDOFF_SESSIONS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._GENERATION = 0
    return store.restore_goal_continuations()


def _arm_and_consume(store, sid, attempt_id, token=TOKEN):
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id=token)
    assert (
        store.consume_pending_goal_continuation(sid, PROMPT, token, attempt_id)
        == store.CONSUME_COMMITTED
    )


def _age_handoffs(store, sid):
    """Age all of *sid*'s durable handoffs past the owner-liveness window.

    #7862 re-gate round 15 (CORE finding 2): adoption is gated on PROOF that
    the owning process is gone — a freshly minted handoff is left alone on a
    cold restore (its process may be mid-admission), so these crash scenarios
    first age the durable liveness clock the way a crashed owner that stopped
    reclaiming would.
    """
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    with store._LOCK:
        for key in [k for k in store._CONTINUATION_HANDOFF_TOKENS if k[0] == sid]:
            store._CONTINUATION_HANDOFF_TOKENS[key]["_handoff_minted_at"] = (
                store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 60
            )
    with store._LOCK:
        store._write_registry_unlocked(PENDING_GOAL_CONTINUATION_RECORDS, context="aging")


def _break_replace(monkeypatch, store):
    def _boom(src, dst, *args, **kwargs):
        raise OSError("simulated failed registry replacement")

    monkeypatch.setattr(store.os, "replace", _boom)


# ---------------------------------------------------------------------------
# Store half: the admission learns its stream identity.
# ---------------------------------------------------------------------------


class TestTheAdmissionLearnsItsStreamIdentity:
    """A launched attempt's handoff must carry the stream it ran under."""

    def test_the_stream_id_is_recorded_on_the_admission(self, clean_registry):
        from api import goal_continuation_store as store

        sid, attempt = "sess-stream-1", "att-stream-1"
        _arm_and_consume(store, sid, attempt)
        store.record_goal_continuation_stream(sid, attempt, "stream-abc")
        with store._LOCK:
            token = store._CONTINUATION_HANDOFF_TOKENS[(sid, attempt)]
        assert token.get("_handoff_stream_id") == "stream-abc"

    def test_the_stream_id_survives_the_registry_snapshot(self, clean_registry):
        """The identity is durable, not just in-memory.

        This is the reviewer's "persist admission with its attempt/stream
        identity": a cold process must be able to read which stream an
        admitted attempt launched under.
        """
        from api import goal_continuation_store as store

        sid, attempt = "sess-stream-2", "att-stream-2"
        _arm_and_consume(store, sid, attempt)
        store.record_goal_continuation_stream(sid, attempt, "stream-def")

        records, _tombstones, handoffs = store._load_file_raw()
        assert (sid, attempt) in handoffs, "the admission must stay durable"
        assert handoffs[(sid, attempt)].get("_handoff_stream_id") == "stream-def"

    def test_a_stream_admitted_handoff_is_not_re_adopted_as_interrupted(
        self, clean_registry
    ):
        """An admission whose turn already RAN must not come back.

        The failed-discharge scenario: the launch succeeded, the discharge
        write failed, the process restarts. The handoff is still on disk,
        but it describes a stream that already executed the turn — adopting
        it would restore a continuation that was already spent, which is
        exactly the resurrection the reviewer reproduced (cold restore
        recovers 1 already-admitted continuation).

        #7862 re-gate round 3 (finding 5): the reconciliation now asks the
        durable run/turn journals whether the stream's worker actually ran,
        because an allocated stream id alone is NOT proof of execution (the
        route binds it before ``thr.start()``). This case therefore leaves the
        worker-side journal row a real launch would have written.
        """
        from api import goal_continuation_store as store
        from api.models import SESSION_DIR
        from api.run_journal import append_run_event

        sid, attempt = "sess-stream-3", "att-stream-3"
        _arm_and_consume(store, sid, attempt)
        store.record_goal_continuation_stream(sid, attempt, "stream-ghi")
        try:
            # The worker DID run: its run journal carries a row for the stream.
            append_run_event(
                sid, "stream-ghi", "start", {"meta": "test"}, session_dir=SESSION_DIR
            )

            # The owning process is proven gone (its liveness clock aged past
            # the reclaim window) before the restart reconciles the stream.
            _age_handoffs(store, sid)
            restored = _cold_restore(store)
            from api.config import PENDING_GOAL_CONTINUATION_RECORDS

            assert restored == 0
            assert sid not in PENDING_GOAL_CONTINUATION_RECORDS
            # ...and the spent handoff must not be left behind either.
            _records, _tombstones, handoffs = store._load_file_raw()
            assert (sid, attempt) not in handoffs
        finally:
            for sub in ("_run_journal", "_turn_journal"):
                target = SESSION_DIR / sub / sid
                try:
                    for p in target.rglob("*"):
                        if p.is_file():
                            p.unlink()
                    target.rmdir()
                except (FileNotFoundError, OSError):
                    pass

    def test_an_admission_without_a_stream_is_still_reconcilable(
        self, clean_registry
    ):
        """The pre-launch crash arm must keep working (round 7's contract)."""
        from api import goal_continuation_store as store

        sid, attempt = "sess-stream-4", "att-stream-4"
        _arm_and_consume(store, sid, attempt)
        # No stream recorded: the process died between consume and launch.
        # Its liveness clock goes stale (the owner is gone) before the
        # restart reconciles the admission.
        _age_handoffs(store, sid)
        restored = _cold_restore(store)
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        assert restored == 1
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS

    def test_a_discharged_admission_records_nothing_to_reconcile(
        self, clean_registry
    ):
        """The happy path is unchanged: a clean discharge leaves no trace."""
        from api import goal_continuation_store as store

        sid, attempt = "sess-stream-5", "att-stream-5"
        _arm_and_consume(store, sid, attempt)
        assert store.discard_goal_continuation_handoff(sid, attempt) is True
        _records, _tombstones, handoffs = store._load_file_raw()
        assert (sid, attempt) not in handoffs
        assert _cold_restore(store) == 0


# ---------------------------------------------------------------------------
# Route half: the launch paths must consume the settlement outcome.
# ---------------------------------------------------------------------------


def _routes_source() -> str:
    import api.routes as routes

    return _re.sub(r"\s+", " ", open(routes.__file__, encoding="utf-8").read())


def _first_match(text: str, pattern: str):
    return _re.search(pattern, text, _re.DOTALL)


class TestTheLaunchPathsHandleTheSettlement:
    """Both launch paths must act on the discharge outcome.

    The reviewer's probe: "selectively failing the registry replacement
    after captured worker launch still returns a stream with
    goal_related=true; cold restore brings back 1 continuation". The store
    already reports the failure; these pins keep the routes from dropping
    that report on the floor.
    """

    def test_the_worker_launch_path_records_the_stream_and_checks_the_discharge(
        self,
    ):
        src = _routes_source()
        body = _first_match(src, r"def _start_chat_stream_for_session\(")
        assert body is not None
        scope = src[body.start():body.start() + 60000]

        # (a) The stream identity is bound to the admission BEFORE the
        # worker starts, so a failed settlement is reconcilable by stream.
        record_m = _first_match(
            scope, r"record_goal_continuation_stream\(\s*s\.session_id\s*,"
            r"\s*goal_continuation_attempt_id\s*,\s*stream_id\s*,?\s*\)"
        )
        assert record_m is not None, (
            "the launch path must bind the stream id to the admission before "
            "the worker executes"
        )
        thr_m = _first_match(scope, r"thr\.start\(\)")
        assert thr_m is not None
        assert record_m.start() < thr_m.start(), (
            "the admission's stream identity must be recorded before the "
            "worker thread starts"
        )

        # (b) The discharge outcome is consumed, not dropped.
        discharge_m = _first_match(
            scope, r"discard_goal_continuation_handoff\(\s*s\.session_id\s*,"
            r"\s*goal_continuation_attempt_id\s*,?\s*\)"
        )
        assert discharge_m is not None
        after = scope[discharge_m.end():discharge_m.end() + 400]
        assert "if not" in after or "_discharged" in after, (
            "the launch path must act on the discharge outcome instead of "
            "dropping the boolean"
        )

    def test_the_regeneration_path_handles_the_settlement_too(self):
        src = _routes_source()
        body = _first_match(src, r"def _start_chat_stream_for_session\(")
        assert body is not None
        scope = src[body.start():body.start() + 60000]
        # The reviewer read the same unchecked settlement at the
        # regeneration launch; both call sites must handle it.
        assert scope.count("discard_goal_continuation_handoff(") >= 2, (
            "both launch paths (worker start and regeneration) must discharge "
            "the admission"
        )
