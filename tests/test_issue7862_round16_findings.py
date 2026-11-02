"""#7862 re-gate (2026-10-29, review 5701146411): the four remaining
transaction gaps in the durable goal-continuation registry.

The maintainer's CHANGES_REQUESTED at head ``33eecc61501550b8f130cca482fb502baf3efbb3``
confirmed round 15 GREEN and blocked release on what a STATIC source trace
found still open:

1. **A TTL is still being used as proof that an admission owner died.**
   ``restore_goal_continuations`` skips a handoff only while
   ``_handoff_minted_at`` is fresh; once it is older than the reclaim window a
   cold restore converts it back into a claimable record even though its owner
   is still inside the admission. The route explicitly allows an admission to
   outlive the TTL, and ``reclaim_goal_continuation_receipt`` refreshed only
   LOCAL dictionaries — not a durable ownership fence. A's later stream
   binding trusts its local handoff, so the takeover is never revoked and the
   route launches a second goal worker.

2. **Startup and stale peers can still erase or republish another attempt's
   handoff.** ``restore_goal_continuations`` deliberately does not run the
   generic merge, so a fresh foreign handoff takes ``continue`` without
   entering the local table — and the same restore's closing snapshot
   serialises only the local table, erasing it from disk. ``_merge_disk_state_
   unlocked`` only ADDS handoff keys missing locally: it neither adopts an
   updated stream binding for an existing key nor removes a locally retained
   key that another process discharged, so a stale peer republishes it.

3. **The new failed-restore exemption can bypass a later clear.** A failed
   rollback snapshot adds the session to ``_RESTORE_WRITE_FAILED_SESSIONS``.
   If a peer then retires that session at a newer generation, the next merge
   imports the tombstone but unconditionally retains the dirty record, and an
   unrelated successful snapshot writes that old record alongside the newer
   tombstone. Plain-record restoration checks deleted-session membership but
   not the registry retirement tombstone.

4. **No-goal deletion checks only the local cache.**
   ``session_has_registry_state`` examines this process's records, handoffs
   and receipts; a process that has not observed a peer's durable state
   answers False, and the delete route then acknowledges retirement without
   consulting or retiring that state.

Every schedule below runs REAL independent processes (separate interpreters,
separate module-level registry memory, a shared durable file and the real
``fcntl.flock``) talking over stdin/stdout, because the round-15 helpers swap
globals inside ONE interpreter and cannot exercise independent custody. The
worker's wall clock is shiftable so elapsed admission time is simulatable
without sleeping through a 15-minute TTL.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

PROMPT = "keep refining the release checklist"

REPO_ROOT = Path(__file__).resolve().parents[1]

# The reclaim window the registry uses as the admission lease. Read from the
# real module so the schedules track the production constant.
from api.goal_continuation_store import _ROLLBACK_RECEIPT_TTL_SECONDS as _TTL  # noqa: E402

# ── the out-of-process worker ─────────────────────────────────────────────

_WORKER_SOURCE = r'''
"""#7862 round-16 out-of-process registry worker.

A REAL second WebUI process: its own interpreter, its own module-level
registry memory, its own file descriptors. The parent drives it over
stdin/stdout so two LIVE processes can hold custody of the same durable
registry at the same time -- exactly what the round-15 in-interpreter global
swap cannot model.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ["GC_WORKER_REPO"])

PROMPT = "keep refining the release checklist"

_OFFSET = float(os.environ.get("GC_CLOCK_OFFSET") or 0.0)
_real_time = time.time


class _Clock:
    """Shift the store's wall clock so elapsed admission time is simulatable."""

    def time(self):
        return _real_time() + _OFFSET

    def sleep(self, seconds):
        time.sleep(seconds)


from api import goal_continuation_store as store  # noqa: E402

store.time = _Clock()


_STATE = {}


def _reply(**payload):
    sys.stdout.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    sys.stdout.flush()


def _key(sid, attempt):
    return f"{sid}\u0000{attempt}"


def _dump():
    records, tombstones, handoffs = store._load_file_raw()
    return {
        "records": sorted(records),
        "tombstones": {str(k): int(v) for k, v in tombstones.items()},
        "handoffs": {_key(*k): v for k, v in handoffs.items()},
        "memory_handoffs": sorted(_key(*k) for k in store._CONTINUATION_HANDOFF_TOKENS),
        "memory_receipts": sorted(_key(*k) for k in store._ROLLBACK_RECEIPTS),
    }


def _do_delete_route(sid, session_dir):
    """Run the REAL /api/session/delete handler in this process."""
    import api.models as models
    import api.routes as routes
    from types import SimpleNamespace

    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    models.SESSION_DIR = session_dir
    routes.SESSION_DIR = session_dir
    index = session_dir / "_index.json"
    models.SESSION_INDEX_FILE = index
    routes.SESSION_INDEX_FILE = index
    models.SESSIONS.clear()
    models.Session(session_id=sid, title="Session").save(
        touch_updated_at=False, skip_index=True
    )

    routes._check_csrf = lambda _handler: True
    routes.read_body = lambda _handler: {"session_id": sid}
    routes._lookup_cli_session_metadata = lambda value: {}
    routes._is_messaging_session_id = lambda value: False
    routes._session_is_subagent_view_only = lambda value: False
    captured = {}
    routes.j = (
        lambda _h, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        )
        or True
    )
    handler = type(
        "H",
        (),
        {
            "command": "POST",
            "wfile": None,
            "send_response": lambda self, status: None,
            "send_header": lambda self, key, value: None,
            "end_headers": lambda self: None,
        },
    )()
    routes.handle_post(handler, SimpleNamespace(path="/api/session/delete"))
    return captured


def main():
    state_dir = os.environ["GC_WORKER_STATE_DIR"]
    os.makedirs(os.path.join(state_dir, "sessions"), exist_ok=True)
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
            cmd = request.get("cmd")
            args = request.get("args") or {}
            if cmd == "exit":
                return
            elif cmd == "set_clock":
                global _OFFSET
                _OFFSET = float(args["offset"])
                _reply(offset=_OFFSET)
            elif cmd == "arm":
                _reply(
                    ok=bool(
                        store.arm_pending_goal_continuation(
                            args["sid"],
                            args["prompt"],
                            continuation_id=args.get("continuation_id", ""),
                        )
                    )
                )
            elif cmd == "arm_consume":
                from api import goals

                cont_id = goals.register_pending_goal_continuation(
                    args["sid"], args["prompt"]
                )
                outcome = store.consume_pending_goal_continuation(
                    args["sid"], args["prompt"], cont_id, args["attempt"]
                )
                _reply(continuation_id=cont_id, outcome=outcome)
            elif cmd == "reclaim":
                _reply(
                    ok=bool(
                        store.reclaim_goal_continuation_receipt(
                            args["sid"], args["attempt"]
                        )
                    )
                )
            elif cmd == "bind":
                _reply(
                    ok=bool(
                        store.record_goal_continuation_stream(
                            args["sid"], args["attempt"], args["stream"]
                        )
                    )
                )
            elif cmd == "discharge":
                _reply(
                    ok=bool(
                        store.discard_goal_continuation_handoff(
                            args["sid"], args["attempt"]
                        )
                    )
                )
            elif cmd == "peer_arm":
                # An UNRELATED registry write by this process: what a real
                # peer does while another process holds an admission.
                _reply(
                    ok=bool(
                        store.arm_pending_goal_continuation(
                            f"sess-r16-peer-write-{args['index']}",
                            PROMPT,
                            continuation_id=f"{args['index']:032d}",
                        )
                    )
                )
            elif cmd == "restore":
                _reply(restored=int(store.restore_goal_continuations()))
            elif cmd == "retire":
                _reply(
                    ok=bool(
                        store.retire_pending_goal_continuation(
                            args["sid"], reason="clear"
                        )
                    )
                )
            elif cmd == "has_state":
                _reply(has_state=bool(store.session_has_registry_state(args["sid"])))
            elif cmd == "dump":
                _reply(**_dump())
            elif cmd == "rollback_enospc":
                receipt = store.pop_goal_continuation_rollback_receipt(
                    args["sid"], args["attempt"]
                )
                if receipt is None:
                    receipt = store.pop_goal_continuation_handoff(
                        args["sid"], args["attempt"]
                    )
                real_replace = os.replace

                def _boom(src, dst):
                    if "pending_goal_continuations" in str(dst):
                        raise OSError("disk full (simulated)")
                    return real_replace(src, dst)

                os.replace = _boom
                try:
                    accepted = bool(
                        store.restore_pending_goal_continuation(args["sid"], receipt)
                    )
                finally:
                    os.replace = real_replace
                _reply(
                    accepted=accepted,
                    receipt_generation=int((receipt or {}).get("generation") or 0),
                    dirty=sorted(store._RESTORE_WRITE_FAILED_SESSIONS),
                )
            elif cmd == "route_launch":
                # Drive the REAL chat-start route for a continuation whose
                # admission this process holds. ``append_turn_journal_event``
                # is the hook the route calls AFTER the consume and BEFORE the
                # stream binding, so blocking there models an owner paused
                # inside its admission while a peer acts on the registry.
                import threading

                import api.models as models
                import api.routes as routes
                import api.turn_journal as turn_journal

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
                    messages=[{"role": "user", "content": "earlier", "timestamp": 1}],
                )
                session.save()

                captured = []
                routes._agent_runtime_barrier_response = lambda **kw: None
                routes.webui_gateway_chat_enabled = lambda _cfg: False

                def _recorder(session_id, msg, model, workspace, stream_id, attachments, **kwargs):
                    captured.append(kwargs)

                routes._run_agent_streaming = _recorder

                gate = Path(args["gate"])
                real_append = turn_journal.append_turn_journal_event

                def _gated_append(*a, **kw):
                    # Pause inside the admission: the consume has committed,
                    # the stream identity is not bound yet.
                    deadline = time.time() + float(args.get("gate_timeout", 60))
                    while gate.exists() and time.time() < deadline:
                        time.sleep(0.02)
                    return real_append(*a, **kw)

                turn_journal.append_turn_journal_event = _gated_append

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
                        )
                        outcome["status"] = int(response.get("_status", 200) or 200)
                    except Exception as exc:
                        outcome["exception"] = type(exc).__name__
                    finally:
                        turn_journal.append_turn_journal_event = real_append
                        outcome["workers"] = list(captured)
                        _STATE["route_outcome"] = outcome

                _STATE["route_thread"] = threading.Thread(target=_launch, daemon=True)
                _STATE["route_thread"].start()
                _reply(started=True)
            elif cmd == "route_result":
                thread = _STATE.get("route_thread")
                if thread is None:
                    _reply(error="no route launch was started")
                elif thread.is_alive():
                    _reply(done=False)
                else:
                    thread.join(timeout=30)
                    _reply(done=True, result=_STATE.get("route_outcome") or {})
            elif cmd == "release_gate":
                Path(args["gate"]).unlink(missing_ok=True)
                _reply(released=True)
            elif cmd == "delete_route":
                captured = _do_delete_route(args["sid"], state_dir)
                _reply(
                    payload=captured.get("payload"),
                    status=captured.get("status"),
                    **_dump(),
                )
            else:
                _reply(error="unknown command " + str(cmd))
        except Exception as exc:  # surface it, never hang the parent
            _reply(error=f"{type(exc).__name__}: {exc}")


main()
'''


class _Worker:
    """One live WebUI process holding its own view of the registry."""

    def _worker_source(self) -> str:
        """The program this process runs; subclasses may extend it."""
        return _WORKER_SOURCE

    def __init__(self, tmp_path, name, clock_offset=0.0):
        self.name = name
        self.state_dir = tmp_path / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
        script = tmp_path / f"gc_worker_{name}.py"
        script.write_text(self._worker_source(), encoding="utf-8")
        self._stderr_path = tmp_path / f"gc_worker_{name}.stderr"
        env = os.environ.copy()
        env["GC_WORKER_REPO"] = str(REPO_ROOT)
        env["GC_WORKER_STATE_DIR"] = str(self.state_dir)
        env["GC_CLOCK_OFFSET"] = repr(float(clock_offset))
        # A fresh, per-test state home: the worker resolves ``STATE_DIR`` from
        # it, so every schedule gets a pristine durable registry.
        env["HERMES_WEBUI_STATE_DIR"] = str(self.state_dir)
        env["HERMES_HOME"] = str(self.state_dir)
        env["HERMES_BASE_HOME"] = str(self.state_dir)
        env["HERMES_CONFIG_PATH"] = str(self.state_dir / "config.yaml")
        env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
        self._stderr = open(self._stderr_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
        )

    def call(self, cmd, **args):
        assert self.proc.poll() is None, (
            f"worker {self.name} exited before {cmd}: "
            f"{self._stderr_path.read_text(encoding='utf-8')[-2000:]}"
        )
        self.proc.stdin.write(json.dumps({"cmd": cmd, "args": args}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, (
            f"worker {self.name} closed stdout on {cmd}: "
            f"{self._stderr_path.read_text(encoding='utf-8')[-2000:]}"
        )
        reply = json.loads(line)
        assert "error" not in reply, f"worker {self.name} failed on {cmd}: {reply}"
        return reply

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.stdin.write(json.dumps({"cmd": "exit"}) + "\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=60)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        finally:
            try:
                self._stderr.close()
            except Exception:
                pass


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


# ── finding 1: a live owner paused past the TTL keeps its admission ───────


class TestALiveOwnerPausedPastTheTtl:
    """``restore_goal_continuations`` used the handoff's clock as proof that
    its owner died. The route allows an admission to outlive the reclaim
    window, and the reclaim refreshed only local dictionaries, so a cold
    restore took a LIVE owner's admission over and the route later launched a
    second goal worker for the same intent."""

    def test_a_live_owner_paused_past_the_ttl_keeps_its_admission(
        self, tmp_path, workers
    ):
        sid = "sess-r16-live-owner"
        late = _TTL + 120

        owner = workers("owner")
        peer = None
        try:
            admitted = owner.call(
                "arm_consume", sid=sid, prompt=PROMPT, attempt="att-A"
            )
            assert admitted["outcome"] == "committed", (
                "the real admission did not commit: the schedule cannot start"
            )

            # A is still inside its admission, but the wall clock has moved
            # past the reclaim window (a slow provider handshake, a
            # registration callback still waiting on its worker). A reports
            # itself live exactly as ``_start_chat_stream_for_session`` does.
            owner.call("set_clock", offset=late)
            assert owner.call("reclaim", sid=sid, attempt="att-A")["ok"] is True

            # Process B starts cold while A is still paused. B must NOT take
            # the admission over: A proved ownership by renewing its lease.
            peer = workers("peer", clock_offset=late)
            assert peer.call("restore")["restored"] == 0, (
                "a cold restore took over a LIVE owner's admission past the "
                "TTL: the reclaim renewed only local dictionaries, so the "
                "durable lease stayed stale and B converted A's handoff into "
                "a claimable record (finding 1)"
            )
            dump = peer.call("dump")
            assert f"{sid}\x00att-A" in dump["handoffs"], (
                "the live owner's admission evidence was erased from disk"
            )

            # A still owns the admission, so its stream binding still lands.
            assert (
                owner.call("bind", sid=sid, attempt="att-A", stream="stream-A")["ok"]
                is True
            )
        finally:
            for worker in (peer, owner):
                if worker is not None:
                    worker.close()


# ── finding 1: a takeover must REVOKE the attempt it displaced ────────────


class TestALeaseTakeoverRevokesTheDisplacedAttempt:
    """A cold restore that adopts an expired handoff removes it from the
    durable table, but the displaced attempt kept trusting its LOCAL copy: its
    stream binding succeeded and the route launched after that Boolean. A
    takeover must revoke the old attempt so it cannot bind or launch."""

    def test_a_taken_over_admission_cannot_bind_or_launch(self, tmp_path, workers):
        sid = "sess-r16-takeover"
        late = _TTL + 120

        owner = workers("owner")
        peer = None
        try:
            admitted = owner.call(
                "arm_consume", sid=sid, prompt=PROMPT, attempt="att-A"
            )
            assert admitted["outcome"] == "committed"

            # The owner never renews: it is gone. Once its lease expires a cold
            # peer legitimately takes the admission over.
            peer = workers("peer", clock_offset=late)
            assert peer.call("restore")["restored"] == 1, (
                "an abandoned admission was not recovered: the schedule cannot "
                "exercise the revocation"
            )
            dump = peer.call("dump")
            assert sid in dump["records"]
            assert f"{sid}\x00att-A" not in dump["handoffs"], (
                "the takeover did not consume the durable handoff"
            )

            # The displaced owner wakes up late and tries to bind its stream.
            owner.call("set_clock", offset=late)
            assert (
                owner.call("bind", sid=sid, attempt="att-A", stream="stream-A")["ok"]
                is False
            ), (
                "the displaced attempt still bound its stream identity: the "
                "route launches after that Boolean succeeds, so the takeover "
                "never revoked it and two goal workers ran for one intent "
                "(finding 1)"
            )

            # ... and it must not be able to discharge the peer's takeover.
            owner.call("discharge", sid=sid, attempt="att-A")
            dump = peer.call("dump")
            assert sid in dump["records"], (
                "the revoked attempt disturbed the record the takeover created"
            )
        finally:
            for worker in (peer, owner):
                if worker is not None:
                    worker.close()


class TestATakenOverAdmissionNeverLaunchesASecondWorker:
    """The route launches after the stream binding's Boolean succeeds, so the
    takeover has to be visible at THAT point: an owner paused inside its
    admission (the consume committed, the stream identity not yet bound) must
    find its admission gone and refuse the launch instead of running a second
    goal worker for one intent."""

    def test_the_route_refuses_to_launch_a_taken_over_admission(
        self, tmp_path, workers
    ):
        sid = "sess-r16-route-takeover"
        late = _TTL + 120
        gate = tmp_path / "r16-gate"
        gate.write_text("hold\n", encoding="utf-8")

        owner = workers("owner")
        peer = None
        try:
            cont_id = "a" * 32
            assert (
                owner.call(
                    "arm", sid=sid, prompt=PROMPT, continuation_id=cont_id
                )["ok"]
                is True
            )

            # The real chat-start route for this continuation: the route itself
            # consumes the intent (so the admission and its handoff are minted
            # inside this call) and then pauses at the gate, between the
            # consume and the stream binding.
            started = owner.call(
                "route_launch",
                sid=sid,
                prompt=PROMPT,
                continuation_id=cont_id,
                workspace=str(tmp_path / "ws-owner"),
                gate=str(gate),
                gate_timeout=90,
            )
            assert started["started"] is True
            # The route thread reaches the gate asynchronously: it consumes
            # the intent and mints the handoff before it parks at the gate.
            # A single poll therefore races the route thread — the launch is
            # not finished yet, but the handoff may not exist either. Wait for
            # the handoff to appear instead of snapshotting once, so the
            # schedule does not depend on how fast the worker thread runs.
            deadline = time.time() + 30
            minted = False
            while time.time() < deadline:
                assert owner.call("route_result")["done"] is False, (
                    "the route launch was not paused inside the admission: the "
                    "schedule cannot exercise the binding"
                )
                dump = owner.call("dump")
                if any(
                    k.startswith(f"{sid}\x00") for k in dump["memory_handoffs"]
                ):
                    minted = True
                    break
                time.sleep(0.05)
            assert minted, (
                "the paused route did not mint the admission handoff: the "
                "schedule cannot exercise the binding"
            )

            # The owner never renews, so its lease expires and a cold peer
            # legitimately takes the admission over.
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

            # The owner resumes and finishes its launch.
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
                "the route launched a SECOND goal worker for an admission "
                "another process had already taken over: the takeover never "
                "revoked the displaced attempt (finding 1)"
            )
            assert result.get("exception") or int(result.get("status") or 200) >= 400, (
                f"the route acknowledged a taken-over admission: {result}"
            )
        finally:
            for worker in (peer, owner):
                if worker is not None:
                    worker.close()


# ── finding 2 (a): startup must not erase a fresh foreign handoff ─────────


class TestStartupRestoreKeepsAFreshForeignHandoff:
    """``restore_goal_continuations`` deliberately does not run the generic
    merge, so a fresh foreign handoff takes ``continue`` without entering the
    local handoff table. If the same restore also recovers an unrelated record
    or drops another entry, its closing snapshot serialises only the local
    table and removes the skipped handoff from disk."""

    def test_a_fresh_foreign_handoff_survives_an_unrelated_startup_restore(
        self, tmp_path, workers
    ):
        sid_foreign = "sess-r16-foreign"
        sid_unrelated = "sess-r16-unrelated"

        first = workers("first")
        try:
            assert (
                first.call(
                    "arm",
                    sid=sid_unrelated,
                    prompt=PROMPT,
                    continuation_id="u" * 32,
                )["ok"]
                is True
            )
            admitted = first.call(
                "arm_consume", sid=sid_foreign, prompt=PROMPT, attempt="att-A"
            )
            assert admitted["outcome"] == "committed"
        finally:
            first.close()

        # Process B starts cold. Its startup restore recovers the unrelated
        # record AND meets the fresh foreign handoff.
        peer = workers("peer")
        try:
            assert peer.call("restore")["restored"] == 1, (
                "the unrelated record was not recovered: the closing snapshot "
                "that erases the foreign handoff never ran"
            )
            dump = peer.call("dump")
            assert f"{sid_foreign}\x00att-A" in dump["handoffs"], (
                "the startup restore erased a LIVE peer's handoff from disk "
                "while publishing an unrelated record: a crash of its owner "
                "then has no admission to recover (finding 2)"
            )
        finally:
            peer.close()


# ── finding 2 (b): a stale peer must not republish a discharged handoff ───


class TestAStalePeerCannotRepublishADischargedHandoff:
    """``_merge_disk_state_unlocked`` only ADDS handoff keys missing locally.
    It neither adopts an updated stream binding for an existing key nor
    removes a locally retained key that another process discharged, so a peer
    that cached the earlier unbound handoff republishes it during an unrelated
    write — losing the stream identity and resurrecting a launched turn."""

    def test_a_stale_peer_keeps_the_binding_and_honours_the_discharge(
        self, tmp_path, workers
    ):
        sid_owner = "sess-r16-stale-owner"

        owner = workers("owner")
        peer = workers("peer")
        try:
            admitted = owner.call(
                "arm_consume", sid=sid_owner, prompt=PROMPT, attempt="att-A"
            )
            assert admitted["outcome"] == "committed"

            # The peer observes the admission with an unrelated write, so its
            # memory carries a copy of the still-unbound handoff.
            assert peer.call("peer_arm", index=1)["ok"] is True
            dump = peer.call("dump")
            assert f"{sid_owner}\x00att-A" in dump["memory_handoffs"], (
                "the peer did not merge the owner's handoff: the stale-peer "
                "schedule cannot start"
            )

            # The owner binds its stream identity: the durable admission of a
            # launch that is about to happen.
            assert (
                owner.call("bind", sid=sid_owner, attempt="att-A", stream="stream-A")[
                    "ok"
                ]
                is True
            )

            # The peer's NEXT unrelated write must carry the stream identity,
            # not the stale unbound copy it cached.
            assert peer.call("peer_arm", index=2)["ok"] is True
            dump = peer.call("dump")
            entry = dump["handoffs"].get(f"{sid_owner}\x00att-A")
            assert entry is not None, (
                "the peer dropped the owner's handoff from its snapshot while "
                "the admission was still in flight"
            )
            assert entry.get("_handoff_stream_id") == "stream-A", (
                "the peer republished the STALE unbound handoff: the stream "
                "identity is lost, which bypasses the recovery reconciliation "
                "that depends on it (finding 2)"
            )

            # The launch succeeded, so the owner discharges the admission.
            assert owner.call("discharge", sid=sid_owner, attempt="att-A")["ok"] is True

            # The peer's next unrelated write must NOT resurrect it.
            assert peer.call("peer_arm", index=3)["ok"] is True
            dump = peer.call("dump")
            assert f"{sid_owner}\x00att-A" not in dump["handoffs"], (
                "the stale peer republished a handoff the owner had already "
                "bound and discharged: a cold restore would resurrect a "
                "continuation whose turn already ran (finding 2)"
            )
        finally:
            for worker in (peer, owner):
                worker.close()


# ── finding 3: a failed warm rollback cannot outlive a newer clear ────────


class TestAFailedWarmRollbackCannotOutliveAPeerClear:
    """A failed rollback snapshot left the record live in memory only. A peer
    that then retires the session at a newer generation had its tombstone
    imported while the dirty record was unconditionally retained, and an
    unrelated successful snapshot wrote that old record alongside the newer
    tombstone. Plain-record restoration also ignored the retirement authority
    the handoff adoption honours."""

    def test_a_failed_warm_rollback_is_fenced_by_the_peer_clear(
        self, tmp_path, workers
    ):
        sid = "sess-r16-dirty-restore"
        owner, peer = self._replay(workers, sid)
        try:
            # The owner's next unrelated transaction runs its merge.
            assert (
                owner.call(
                    "arm",
                    sid="sess-r16-dirty-other",
                    prompt=PROMPT,
                    continuation_id="o" * 32,
                )["ok"]
                is True
            )
            dump = owner.call("dump")
            assert sid not in dump["records"], (
                "the dirty restore outlived the peer's higher-generation "
                "clear: the merge retained it unconditionally and the "
                "unrelated snapshot republished it next to the newer "
                "tombstone (finding 3)"
            )
        finally:
            for worker in (peer, owner):
                worker.close()

    def test_a_cold_restore_refuses_a_record_the_registry_retired(
        self, tmp_path, workers
    ):
        """The same composition, one step further on: a cold process must not
        republish the cleared record either. Plain-record restoration checked
        deleted-session membership but not the registry retirement tombstone
        the handoff adoption honours."""
        sid = "sess-r16-dirty-restore"
        owner, peer = self._replay(workers, sid)
        try:
            assert (
                owner.call(
                    "arm",
                    sid="sess-r16-dirty-other",
                    prompt=PROMPT,
                    continuation_id="o" * 32,
                )["ok"]
                is True
            )
        finally:
            for worker in (peer, owner):
                worker.close()

        cold = workers("cold")
        try:
            # Control: the UNRELATED live intent is still recoverable, so the
            # assertion below is about the retired one and not about a restore
            # that recovers nothing at all.
            assert cold.call("restore")["restored"] == 1, (
                "the unrelated live intent was not recovered: the schedule "
                "cannot discriminate the retired record from a dead registry"
            )
            dump = cold.call("dump")
            assert sid not in dump["records"], (
                "plain-record restoration republished a record covered by the "
                "registry retirement tombstone: the cleared goal came back "
                "(finding 3)"
            )
            assert "sess-r16-dirty-other" in dump["records"]
        finally:
            cold.close()

    @staticmethod
    def _replay(workers, sid):
        """A failed warm rollback followed by a higher-generation peer clear."""
        owner = workers("owner")
        peer = None
        try:
            admitted = owner.call(
                "arm_consume", sid=sid, prompt=PROMPT, attempt="att-A"
            )
            assert admitted["outcome"] == "committed"

            # The rejected-start rollback lands while the snapshot cannot
            # commit (warm-path ENOSPC): the record is live in memory only.
            dirty = owner.call("rollback_enospc", sid=sid, attempt="att-A")
            assert dirty["accepted"] is True
            assert sid in dirty["dirty"], (
                "the rollback restore did not register the write failure: the "
                "exemption schedule cannot start"
            )

            # A peer clears the goal at a NEWER generation.
            peer = workers("peer")
            assert peer.call("retire", sid=sid)["ok"] is True
            dump = peer.call("dump")
            tombstone = int(dump["tombstones"].get(sid) or 0)
            assert tombstone > int(dirty["receipt_generation"]), (
                "the peer's clear was not minted above the generation the "
                "failed restore holds: the fence has nothing to compare"
            )
            return owner, peer
        except BaseException:
            for worker in (peer, owner):
                if worker is not None:
                    worker.close()
            raise


# ── finding 4: no-goal deletion must consult the durable registry ─────────


class TestNoGoalDeleteConsultsTheDurableRegistry:
    """``session_has_registry_state`` examined only this process's records,
    handoffs and receipts. A process that has not observed a peer's durable
    state answered False, and the delete route then acknowledged retirement
    without consulting or retiring that state — leaving the peer's private
    continuation prompt on disk."""

    def test_a_local_empty_delete_still_retires_peer_durable_state(
        self, tmp_path, workers
    ):
        sid = "sess-r16-peer-delete"

        # Another live process populates the durable registry.
        owner = workers("owner")
        try:
            admitted = owner.call(
                "arm_consume", sid=sid, prompt=PROMPT, attempt="att-A"
            )
            assert admitted["outcome"] == "committed"
        finally:
            owner.close()

        # This process has never observed that durable state.
        deleter = workers("deleter")
        try:
            assert deleter.call("has_state", sid=sid)["has_state"] is True, (
                "a peer-populated durable registry read as EMPTY from "
                "authoritative state: the no-goal delete would skip the "
                "retirement (finding 4)"
            )
            result = deleter.call("delete_route", sid=sid)
            payload = result.get("payload") or {}
            assert payload.get("ok") is True, (
                f"the delete failed: {payload}"
            )
            assert payload.get("continuation_retire_failed") is not True, (
                "the delete acknowledged retirement without retiring the "
                "peer's durable continuation state (finding 4)"
            )
            assert sid not in result["records"], (
                "the peer's durable continuation record survived the delete: "
                "its private continuation prompt is still claimable on disk "
                "(finding 4)"
            )
            assert f"{sid}\x00att-A" not in result["handoffs"], (
                "the peer's durable admission handoff survived the delete"
            )
        finally:
            deleter.close()
