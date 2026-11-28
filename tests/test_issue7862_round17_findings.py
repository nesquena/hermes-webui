"""#7862 re-gate round 17 (review 5719342806): attempt-ownership provenance.

The maintainer's CHANGES_REQUESTED at head ``fd1683acfd062d437e82bfb642df77c28a193e81``
accepted the round-16 repairs as unchanged and blocked release on two
compositions a STATIC source trace found still open. Both are about ONE
requirement: an admission has exactly one live owner, and every path that
could start a worker — or undo one — must prove ownership against the durable
state instead of against this process's memory.

1. **A prior merge can turn a revoked consume into the ordinary-start success
   path.** ``record_goal_continuation_stream()`` returns True when the local
   handoff key is absent on entry, which conflates "genuinely ordinary, no
   consume" with "consumed and subsequently revoked". Source schedule: A
   consumes, B takes over after the lease window, an unrelated transaction (or
   the liveness renewal) in A's process merges away A's revoked handoff, then A
   binds — the missing-key fast path answers True and the route proceeds to
   ``thr.start()``. A missing local key must not erase the receipt's
   provenance. The same review also flags the successful-bind-to-worker-release
   gap: the regeneration path renews the lease and releases the file
   transaction before the session save / workspace work and the release of its
   gated worker, and ``_gated_worker`` does not revalidate ownership — age
   without revoking the old invocation is not a fence through launch.

2. **Refusal rollback can remove the successor attempt's handoff.** A consumes
   and pauses before bind; B adopts the expired handoff AND consumes the
   restored intent as attempt B; A resumes and its transactional bind correctly
   refuses; the compensation then pops A's still-local receipt and calls
   ``restore_pending_goal_continuation``. That restore's admission checks look
   at the pending-record table and the retirement generation — and B has
   consumed the record, so the pending slot is empty — while its closing
   session-wide handoff deletion removes B's admission too.

Every schedule below runs REAL independent processes (separate interpreters,
separate module-level registry memory, a shared durable file and the real
``fcntl.flock``), reusing the round-16 worker design: the maintainer asked for
the pre-bind-merge, post-bind-pause and B-adopts-then-consumes-before-A-rollback
compositions on top of it. The worker's wall clock is shiftable so elapsed
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

# The round-16 separate-process harness is reused verbatim: the maintainer
# asked to KEEP that design, so the worker program, the clock shift and the
# real chat-start route driver all come from there.
from tests.test_issue7862_round16_findings import (  # noqa: E402
    _WORKER_SOURCE,
    _Worker as _Round16Worker,
)

# ── the round-17 worker extensions ────────────────────────────────────────
#
# Injected into the round-16 dispatch, before its final ``else``. Three new
# commands:
#
# * ``consume_restored`` — a peer spends the record a takeover restored, as its
#   OWN start attempt (the composition the round-16 route test stops short of:
#   it lets B restore but never consume, so B's pending record masks the
#   compensation path).
# * ``rollback`` — the rejected-start compensation shape: pop this attempt's
#   receipt (handoff fallback included) and restore, exactly like the route's
#   ``restore_consumed_continuation_markers``.
# * ``route_launch_regen`` — drive the REAL regeneration chat-start route,
#   parked between the durable stream-identity binding and the release of its
#   gated worker (the successful-bind-to-worker-release window).
# * ``stream_state`` / ``stream_events`` — observe the launched stream: whether
#   its registries were released and which terminal events it published.

_EXTRA_COMMANDS = r'''            elif cmd == "consume_restored":
                outcome = store.consume_pending_goal_continuation(
                    args["sid"],
                    args["prompt"],
                    args.get("continuation_id", ""),
                    args["attempt"],
                )
                _reply(outcome=outcome)
            elif cmd == "rollback":
                receipt = store.pop_goal_continuation_rollback_receipt(
                    args["sid"], args["attempt"]
                )
                if receipt is None:
                    receipt = store.pop_goal_continuation_handoff(
                        args["sid"], args["attempt"]
                    )
                accepted = bool(
                    store.restore_pending_goal_continuation(args["sid"], receipt)
                )
                _reply(
                    accepted=accepted,
                    receipt_generation=int((receipt or {}).get("generation") or 0),
                )
            elif cmd == "route_launch_regen":
                # Drive the REAL regeneration chat-start route for a
                # continuation whose admission this process holds. The route
                # consumes the intent, binds the admission's stream identity
                # durably, saves the session and then parks at
                # ``set_last_workspace`` — AFTER the binding released its file
                # transaction and BEFORE the gated worker is released. That
                # is the window in which a peer can take the admission over.
                import threading

                import api.models as models
                import api.routes as routes
                from api.session_ops import plan_regeneration

                workspace = Path(args["workspace"])
                workspace.mkdir(parents=True, exist_ok=True)
                models.SESSION_DIR = Path(os.environ["GC_WORKER_STATE_DIR"]) / "sessions"
                routes.SESSION_DIR = models.SESSION_DIR
                index = models.SESSION_DIR / "_index.json"
                models.SESSION_INDEX_FILE = index
                routes.SESSION_INDEX_FILE = index
                session = models.Session(
                    session_id=args["sid"],
                    title="Goal continuation",
                    workspace=str(workspace),
                    model="test-model",
                    model_provider="test-provider",
                    messages=[
                        {"role": "user", "content": args["prompt"], "timestamp": 1},
                        {"role": "assistant", "content": "prior answer", "timestamp": 2},
                    ],
                )
                session.save()
                plan = plan_regeneration(session, lock_held=True)

                captured = []
                routes._agent_runtime_barrier_response = lambda **kw: None
                routes.webui_gateway_chat_enabled = lambda _cfg: False

                def _recorder(session_id, msg, model, workspace, stream_id, attachments, **kwargs):
                    captured.append(kwargs)

                routes._run_agent_streaming = _recorder
                real_prepare = routes._prepare_chat_start_session_for_stream
                stream_ids = []

                def _capturing_prepare(*a, **kw):
                    stream_ids.append(kw.get("stream_id"))
                    return real_prepare(*a, **kw)

                routes._prepare_chat_start_session_for_stream = _capturing_prepare

                gate = Path(args["gate"])
                parked = Path(args["parked"])
                real_set_last_workspace = routes.set_last_workspace

                def _gated_set_last_workspace(*a, **kw):
                    # Park after the stream identity was bound durably and
                    # before the gated worker is released.
                    parked.write_text("parked\n", encoding="utf-8")
                    deadline = time.time() + float(args.get("gate_timeout", 60))
                    while gate.exists() and time.time() < deadline:
                        time.sleep(0.02)
                    return real_set_last_workspace(*a, **kw)

                routes.set_last_workspace = _gated_set_last_workspace

                outcome = {}

                def _launch():
                    try:
                        response = routes._start_chat_stream_for_session(
                            session,
                            msg=args["prompt"],
                            workspace=str(workspace),
                            model="test-model",
                            model_provider="test-provider",
                            goal_continuation_id=args["continuation_id"],
                            regeneration=plan.turn,
                        )
                        outcome["status"] = int(response.get("_status", 200) or 200)
                        outcome["stream_id"] = response.get("stream_id")
                    except Exception as exc:
                        outcome["exception"] = type(exc).__name__
                    finally:
                        routes.set_last_workspace = real_set_last_workspace
                        routes._prepare_chat_start_session_for_stream = real_prepare
                        outcome["workers"] = list(captured)
                        outcome["stream_id"] = (
                            outcome.get("stream_id")
                            or (stream_ids[0] if stream_ids else None)
                        )
                        _STATE["route_outcome"] = outcome

                _STATE["route_thread"] = threading.Thread(target=_launch, daemon=True)
                _STATE["route_thread"].start()
                _reply(started=True)
            elif cmd == "stream_state":
                import api.routes as routes

                _reply(live=args["stream_id"] in routes.STREAMS)
'''

_DISPATCH_TAIL = '''            else:
                _reply(error="unknown command " + str(cmd))'''

_WORKER_SOURCE_R17 = _WORKER_SOURCE.replace(
    _DISPATCH_TAIL, _EXTRA_COMMANDS + _DISPATCH_TAIL
)
assert _WORKER_SOURCE_R17 != _WORKER_SOURCE, (
    "the round-16 worker dispatch tail was not found: the round-17 commands "
    "would silently never run"
)


class _Worker(_Round16Worker):
    """One live WebUI process with the round-17 command extensions."""

    def _worker_source(self) -> str:
        return _WORKER_SOURCE_R17


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


def _wait_for_parked(owner: _Worker, parked: Path, timeout: float = 30.0) -> None:
    """Wait until the route is parked between its binding and the release."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        assert owner.call("route_result")["done"] is False, (
            "the regeneration route finished before it parked: the schedule "
            "cannot exercise the successful-bind-to-worker-release window"
        )
        if parked.exists():
            return
        time.sleep(0.05)
    raise AssertionError("the regeneration route never parked after its binding")


def _wait_for_stream_release(owner: _Worker, stream_id: str, timeout: float = 20.0) -> None:
    """Wait until the gated worker released the stream's launch registries."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if owner.call("stream_state", stream_id=stream_id)["live"] is False:
            return
        time.sleep(0.05)
    raise AssertionError(
        "the stream registries were never released: the refused launch did "
        "not compensate its own registration"
    )


# ── finding 1 (a): a revoked admission merged away BEFORE the bind ─────────


class TestARevokedAdmissionMergedAwayBeforeTheBind:
    """The missing-key fast path in ``record_goal_continuation_stream`` answers
    True for an attempt whose handoff is gone, which is also the shape of
    "consumed and then revoked": a peer takeover (or discharge) removed the
    durable handoff, and an earlier transaction's reconciliation already
    dropped the local copy. The receipt the consume minted is the provenance a
    missing local key must not erase, so the bind must refuse exactly like the
    in-transaction refusal — otherwise the route walks the ordinary path into
    ``thr.start()`` and a second goal worker runs for one intent.
    """

    def _takeover(self, workers, sid: str):
        """A consumes; its lease expires; a cold peer takes the admission over."""
        owner = workers("owner")
        admitted = owner.call("arm_consume", sid=sid, prompt=PROMPT, attempt="att-A")
        assert admitted["outcome"] == "committed", (
            "the real admission did not commit: the schedule cannot start"
        )
        # The owner never renews: it is gone. Once its lease expires a cold
        # peer legitimately takes the admission over.
        owner.call("set_clock", offset=_TTL + 120)
        peer = workers("peer", clock_offset=_TTL + 120)
        assert peer.call("restore")["restored"] == 1, (
            "an abandoned admission was not recovered: the schedule cannot "
            "exercise the revocation"
        )
        dump = peer.call("dump")
        assert sid in dump["records"]
        assert f"{sid}\x00att-A" not in dump["handoffs"], (
            "the takeover did not consume the durable handoff"
        )
        return owner, peer

    def test_a_renewal_that_merges_away_the_revoked_admission_blocks_the_bind(
        self, workers
    ):
        sid = "sess-r17-renewal-merge"
        owner, peer = self._takeover(workers, sid)
        try:
            # The liveness renewal runs its reconciliation: the durable table
            # no longer owns this admission, so the local copy is merged away.
            # (Its own return stays True — it is a liveness report, not a
            # launch fence; the bind is the fence.)
            assert owner.call("reclaim", sid=sid, attempt="att-A")["ok"] is True
            dump = owner.call("dump")
            assert f"{sid}\x00att-A" not in dump["memory_handoffs"], (
                "the renewal did not merge the revoked handoff away: the "
                "schedule cannot exercise the pre-bind fast path"
            )
            # The consume's receipt is still live: that is the provenance.
            assert f"{sid}\x00att-A" in dump["memory_receipts"], (
                "the consume left no rollback receipt: the provenance the "
                "bind must read is gone"
            )
            assert (
                owner.call("bind", sid=sid, attempt="att-A", stream="stream-A")["ok"]
                is False
            ), (
                "the missing-key fast path answered True for a consumed and "
                "revoked admission: the route launches after that Boolean, so "
                "a second goal worker runs for one intent (finding 1)"
            )
        finally:
            for worker in (peer, owner):
                worker.close()

    def test_an_unrelated_write_that_merges_away_the_revoked_admission_blocks_the_bind(
        self, workers
    ):
        sid = "sess-r17-unrelated-merge"
        owner, peer = self._takeover(workers, sid)
        try:
            # An UNRELATED registry write in the owner's process: the same
            # reconciliation runs and drops the revoked local handoff.
            assert owner.call("peer_arm", index=1)["ok"] is True
            dump = owner.call("dump")
            assert f"{sid}\x00att-A" not in dump["memory_handoffs"], (
                "the unrelated transaction did not merge the revoked handoff "
                "away: the schedule cannot exercise the pre-bind fast path"
            )
            assert (
                owner.call("bind", sid=sid, attempt="att-A", stream="stream-A")["ok"]
                is False
            ), (
                "the missing-key fast path answered True after an unrelated "
                "transaction merged the revoked admission away: the route "
                "launches after that Boolean, so a second goal worker runs "
                "for one intent (finding 1)"
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
                    "bind", sid="sess-r17-ordinary", attempt="att-none", stream="s"
                )["ok"]
                is True
            ), (
                "an attempt with no consume lost its lock-free ordinary bind: "
                "the provenance check over-refuses"
            )
        finally:
            owner.close()


# ── finding 1 (b): a takeover AFTER the bind never releases the worker ─────


class TestATakeoverAfterTheBindStopsTheGatedWorker:
    """The regeneration route renews the ownership lease when it binds the
    stream identity, then releases the file transaction before the session
    save / workspace work and the release of its gated worker. A peer can take
    the admission over inside that window, and ``_gated_worker`` used to
    execute anyway: age without revoking the old invocation is not a fence
    through launch.
    """

    def test_a_takeover_after_the_bind_never_releases_the_worker(self, workers):
        sid = "sess-r17-post-bind"
        late = _TTL + 120
        owner = workers("owner")
        peer = None
        try:
            # The gate and the parked marker live beside the worker's state
            # dir, so both processes can see them.
            state_root = owner.state_dir
            gate = state_root / "r17-regen-gate"
            parked = state_root / "r17-regen-parked"
            gate.write_text("hold\n", encoding="utf-8")
            cont_id = "c" * 32
            assert (
                owner.call("arm", sid=sid, prompt=PROMPT, continuation_id=cont_id)[
                    "ok"
                ]
                is True
            )

            # The real regeneration route for this continuation: the route
            # consumes the intent, binds the admission's stream identity
            # durably, then parks between that binding and the release.
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
            result_peek = owner.call("route_result")
            assert result_peek["done"] is False

            # The owner never renews again, so its lease expires and a cold
            # peer legitimately takes the admission over — AFTER the bind.
            peer = workers("peer", clock_offset=late)
            assert peer.call("restore")["restored"] == 1, (
                "an abandoned admission was not recovered: the takeover half "
                "of the schedule cannot start"
            )
            dump = peer.call("dump")
            assert sid in dump["records"]
            assert not any(
                k.startswith(f"{sid}\x00") for k in dump["handoffs"]
            ), "the takeover did not consume the durable admission handoff"

            # The owner resumes: the route finishes and releases its worker.
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
                "over: the bind renewed a timestamp, but age without revoking "
                "the old invocation is not a fence through launch (finding 1)"
            )
            assert result.get("exception") == "_GoalContinuationCommitFailed", (
                f"the route acknowledged a taken-over admission: {result}"
            )
            # The refusal happened before the worker was released, so the
            # launch-phase registries were compensated away exactly like a
            # failed start — no stream is left registered for a turn that
            # must not run.
            _wait_for_stream_release(owner, stream_id)
        finally:
            for worker in (peer, owner):
                if worker is not None:
                    worker.close()


# ── finding 2: B adopts AND consumes before A's rollback lands ─────────────


class TestARefusedBindsRollbackKeepsTheSuccessorsAdmission:
    """One step past the round-16 route test: B adopts the expired handoff AND
    consumes the restored intent as attempt B. A resumes, its transactional
    bind correctly refuses, and the compensation pops A's still-local receipt
    and restores. The pending-record table is empty (B consumed it), so the
    restore's admission checks all pass — and its session-wide handoff
    deletion removes B's admission too. The restore must validate against the
    CURRENT attempt ownership and mutate only the losing attempt's custody.
    """

    def test_a_store_rollback_refuses_while_a_successor_owns_the_admission(
        self, workers
    ):
        sid = "sess-r17-rollback-store"
        owner = workers("owner")
        peer = None
        try:
            admitted = owner.call(
                "arm_consume", sid=sid, prompt=PROMPT, attempt="att-A"
            )
            assert admitted["outcome"] == "committed"

            # The owner never renews: B takes the admission over.
            owner.call("set_clock", offset=_TTL + 120)
            peer = workers("peer", clock_offset=_TTL + 120)
            assert peer.call("restore")["restored"] == 1

            # B consumes the restored intent as ITS OWN attempt.
            consumed = peer.call(
                "consume_restored",
                sid=sid,
                prompt=PROMPT,
                continuation_id=admitted["continuation_id"],
                attempt="att-B",
            )
            assert consumed["outcome"] == "committed", (
                "the successor did not consume the restored intent: the "
                "compensation schedule cannot start"
            )
            dump = peer.call("dump")
            assert sid not in dump["records"], (
                "the successor's consume left the record claimable"
            )
            assert f"{sid}\x00att-B" in dump["handoffs"], (
                "the successor's admission handoff is not durable"
            )

            # A's rejected start rolls back: its receipt describes an intent
            # the successor now owns, so the restore must REFUSE.
            rolled = owner.call("rollback", sid=sid, attempt="att-A")
            assert rolled["accepted"] is False, (
                "the rollback restored an intent a successor attempt had "
                "already consumed: the goal loop now has a duplicate pending "
                "continuation beside a running admission (finding 2)"
            )
            dump = peer.call("dump")
            assert sid not in dump["records"], (
                "the refused rollback still recreated the pending intent"
            )
            assert f"{sid}\x00att-B" in dump["handoffs"], (
                "the rollback's session-wide handoff deletion removed the "
                "SUCCESSOR attempt's admission: its crash-recovery evidence "
                "is gone (finding 2)"
            )
            assert f"{sid}\x00att-B" in dump["memory_receipts"], (
                "the successor's rollback receipt was disturbed"
            )
        finally:
            for worker in (peer, owner):
                if worker is not None:
                    worker.close()

    def test_the_route_rollback_keeps_the_successors_admission(self, workers):
        sid = "sess-r17-rollback-route"
        late = _TTL + 120
        owner = workers("owner")
        peer = None
        gate_holder = owner.state_dir
        gate = gate_holder / "r17-rollback-gate"
        try:
            cont_id = "d" * 32
            assert (
                owner.call("arm", sid=sid, prompt=PROMPT, continuation_id=cont_id)[
                    "ok"
                ]
                is True
            )
            gate.write_text("hold\n", encoding="utf-8")

            # The real chat-start route consumes the intent (minting the
            # admission inside this call) and parks at the gate, between the
            # consume and the stream binding.
            started = owner.call(
                "route_launch",
                sid=sid,
                prompt=PROMPT,
                continuation_id=cont_id,
                workspace=str(gate_holder / "ws-owner"),
                gate=str(gate),
                gate_timeout=90,
            )
            assert started["started"] is True
            deadline = time.time() + 30
            minted = False
            while time.time() < deadline:
                assert owner.call("route_result")["done"] is False, (
                    "the route launch was not paused inside the admission: "
                    "the schedule cannot exercise the binding"
                )
                dump = owner.call("dump")
                if any(k.startswith(f"{sid}\x00") for k in dump["memory_handoffs"]):
                    minted = True
                    break
                time.sleep(0.05)
            assert minted, (
                "the paused route did not mint the admission handoff: the "
                "schedule cannot exercise the binding"
            )

            # B takes the admission over AND consumes the restored intent as
            # attempt B — the step the round-16 route test stops short of.
            peer = workers("peer", clock_offset=late)
            assert peer.call("restore")["restored"] == 1
            consumed = peer.call(
                "consume_restored",
                sid=sid,
                prompt=PROMPT,
                continuation_id=cont_id,
                attempt="att-B",
            )
            assert consumed["outcome"] == "committed", (
                "the successor did not consume the restored intent: the "
                "compensation schedule cannot start"
            )

            # A resumes: its transactional bind refuses, and the route's
            # compensation rolls the rejected start back.
            assert owner.call("release_gate", gate=str(gate))["released"] is True
            deadline = time.time() + 60
            result = None
            while time.time() < deadline:
                reply = owner.call("route_result")
                if reply.get("done"):
                    result = reply["result"]
                    break
                time.sleep(0.05)
            assert result is not None, "the paused route launch never finished"
            assert not result.get("workers"), (
                "the route launched a goal worker for an admission another "
                "process had already taken over and consumed (finding 2)"
            )
            assert result.get("exception") or int(result.get("status") or 200) >= 400, (
                f"the route acknowledged a taken-over admission: {result}"
            )

            # The compensation must not have touched the successor's custody.
            dump = peer.call("dump")
            assert f"{sid}\x00att-B" in dump["handoffs"], (
                "the refused bind's rollback deleted the SUCCESSOR attempt's "
                "durable admission: its crash-recovery evidence is gone "
                "(finding 2)"
            )
            assert sid not in dump["records"], (
                "the rollback recreated the pending intent beside the "
                "successor's running admission (finding 2)"
            )
            assert f"{sid}\x00att-B" in dump["memory_receipts"], (
                "the successor's rollback receipt was disturbed"
            )
        finally:
            for worker in (peer, owner):
                if worker is not None:
                    worker.close()