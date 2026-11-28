"""#7862 round 21 (R5): a peer-persisted continuation must not be rejected.

Greptile finding (https://github.com/nesquena/hermes-webui/pull/7862#discussion_r4607836750):

    "Valid continuations get rejected. With two WebUI processes sharing
    state, process A can save a new continuation while process B still has
    no local marker. Process B then rejects the valid token here before
    ``consume_pending_goal_continuation()`` can reload the saved record.
    Even ``/goal resume`` on B returns the token from disk without adding the
    local marker, so the instructed ``continue`` fails too.

    Let the store's locked read decide whether the token matches instead of
    rejecting it from the local set first."

The schedule is cross-process by nature, so it is driven with the round-16
out-of-process harness: two REAL interpreters, two independent module-level
registry memories, ONE shared durable state directory, and the REAL
``fcntl.flock``. A single interpreter cannot model two live owners.

The route under test is the REAL chat-start entry point
(``api.routes._start_chat_stream_for_session``), driven with a fake worker
(``_run_agent_streaming`` is stubbed, no real target and no real tool runs).

The contract that must hold at the same time:

* a valid continuation persisted by the peer is admitted (at most ONE worker,
  with ``goal_related=True``);
* an already-consumed / invalid continuation is still refused (single
  execution right);
* an ordinary message is not refused by a stale state.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The round-16 out-of-process harness is reused verbatim: the maintainer
# asked to KEEP that design (real interpreters, real module-level registry
# memory, a shared durable file, the real flock).
from tests.test_issue7862_round16_findings import (  # noqa: E402
    _WORKER_SOURCE as _WORKER_SOURCE_R16,
    _Worker as _Round16Worker,
)

PROMPT = "keep refining the release checklist"


_WORKER_SOURCE_R21 = _WORKER_SOURCE_R16.replace(
    '''            elif cmd == "dump":''',
    '''            elif cmd == "forget_local":
                # Drop this process's IN-MEMORY view of the pending marker
                # and record, WITHOUT touching the durable file. This is the
                # state a peer is in after the other process persisted a
                # continuation but before this one observed it: the durable
                # registry holds the record, this process's local set does
                # not.
                from api.config import (
                    PENDING_GOAL_CONTINUATION,
                    PENDING_GOAL_CONTINUATION_RECORDS,
                )

                with store._LOCK:
                    PENDING_GOAL_CONTINUATION.discard(args["sid"])
                    PENDING_GOAL_CONTINUATION_RECORDS.pop(args["sid"], None)
                _reply(
                    local_marker=args["sid"] in PENDING_GOAL_CONTINUATION,
                    local_record=args["sid"] in PENDING_GOAL_CONTINUATION_RECORDS,
                )
            elif cmd == "dump":''',
)
assert _WORKER_SOURCE_R21 != _WORKER_SOURCE_R16, (
    "the round-16 worker dispatch was not found: the forget_local command "
    "would silently never run"
)


class _Worker(_Round16Worker):
    """One live WebUI process holding its own view of the registry."""

    def _worker_source(self) -> str:
        return _WORKER_SOURCE_R21


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


def test_a_peer_persisted_continuation_is_admitted_by_the_real_route(workers):
    """R5: process B must admit the continuation process A persisted.

    The exact schedule Greptile describes: A saves a new continuation, B
    still has no local marker, and B's chat start carries the valid token.
    B must NOT refuse it -- the store's locked read has to decide.
    """
    sid = "sess-r21-peer-route"
    cont_id = "a" * 32

    worker_a = workers("A")
    worker_b = workers("B")

    # Process A persists the continuation durably.
    assert worker_a.call(
        "arm", sid=sid, prompt=PROMPT, continuation_id=cont_id
    )["ok"], "process A could not persist the continuation"

    # Process B never observed it: its own memory has no marker and no
    # record. This is the state Greptile's finding is about.
    forgotten = worker_b.call("forget_local", sid=sid)
    assert forgotten.get("local_marker") is False, (
        "process B still holds the local marker; the schedule needs B to "
        "have no local view"
    )
    assert forgotten.get("local_record") is False, (
        "process B still holds the local record; the schedule needs B to "
        "have no local view"
    )

    # The durable state is shared: B sees A's record on disk.
    dump_a = worker_a.call("dump")
    assert sid in dump_a["records"], (
        "process A did not persist the continuation durably"
    )
    dump_b = worker_b.call("dump")
    assert sid in dump_b["records"], (
        "the durable registry is not shared between the two processes"
    )

    # Process B drives the REAL chat-start route with the valid token.
    workspace = Path(worker_b.state_dir) / "ws-b"
    result = worker_b.call(
        "route_launch",
        sid=sid,
        prompt=PROMPT,
        workspace=str(workspace),
        continuation_id=cont_id,
        gate=str(Path(worker_b.state_dir) / "gate"),
        gate_timeout=5,
    )
    assert result.get("started"), (
        f"process B could not start the route launch: {result}"
    )

    # The route must finish without the unmatched-token refusal.
    deadline = time.time() + 30
    outcome = {}
    while time.time() < deadline:
        reply = worker_b.call("route_result")
        if reply.get("done"):
            outcome = reply.get("result") or {}
            break
        time.sleep(0.05)

    assert outcome, "process B's route launch never finished"
    assert "exception" not in outcome, (
        "process B refused a valid continuation that process A had "
        f"persisted: {outcome.get('exception')}"
    )
    assert outcome.get("status") == 200, (
        f"process B's chat start did not succeed: {outcome}"
    )
    workers_launched = outcome.get("workers") or []
    assert len(workers_launched) == 1, (
        f"expected exactly one worker, got {len(workers_launched)}: "
        f"{outcome}"
    )
    assert workers_launched[0].get("goal_related") is True, (
        "the admitted continuation launched as an ordinary turn "
        f"(goal_related={workers_launched[0].get('goal_related')!r})"
    )


def test_an_already_consumed_continuation_is_still_refused(workers):
    """R5 (single execution right): a spent continuation stays refused.

    "Valid continuations get rejected" must not become "everything is
    accepted": a token whose record is gone must still be refused, and the
    refusal must not launch a second worker.
    """
    sid = "sess-r21-peer-spent"
    cont_id = "b" * 32

    worker_a = workers("A")
    worker_b = workers("B")

    assert worker_a.call(
        "arm", sid=sid, prompt=PROMPT, continuation_id=cont_id
    )["ok"]

    # Process A spends it.
    spent = worker_a.call(
        "arm_consume", sid=sid, prompt=PROMPT, attempt="att-A"
    )
    assert spent.get("outcome") == "committed", (
        f"process A could not spend the continuation: {spent}"
    )

    # Process B now tries the SAME token. The record is gone.
    workspace = Path(worker_b.state_dir) / "ws-b2"
    result = worker_b.call(
        "route_launch",
        sid=sid,
        prompt=PROMPT,
        workspace=str(workspace),
        continuation_id=cont_id,
        gate=str(Path(worker_b.state_dir) / "gate"),
        gate_timeout=5,
    )
    assert result.get("started"), f"process B could not start: {result}"

    deadline = time.time() + 30
    outcome = {}
    while time.time() < deadline:
        reply = worker_b.call("route_result")
        if reply.get("done"):
            outcome = reply.get("result") or {}
            break
        time.sleep(0.05)

    assert outcome, "process B's route launch never finished"
    assert outcome.get("exception") == "_GoalContinuationTokenUnmatched", (
        "a continuation another process already spent was not refused: "
        f"{outcome}"
    )
    assert not (outcome.get("workers") or []), (
        "the refused continuation still launched a worker: "
        f"{outcome.get('workers')}"
    )


def test_an_ordinary_message_is_not_refused_by_a_stale_state(workers):
    """R5 (counter-control): an ordinary turn stays an ordinary turn.

    The peer persisted a continuation for another session; this process's
    ordinary message about a DIFFERENT session must not be refused.
    """
    worker_a = workers("A")
    worker_b = workers("B")

    sid_peer = "sess-r21-peer-other"
    cont_id = "c" * 32
    assert worker_a.call(
        "arm", sid=sid_peer, prompt=PROMPT, continuation_id=cont_id
    )["ok"]

    sid_own = "sess-r21-peer-own"
    workspace = Path(worker_b.state_dir) / "ws-b3"
    result = worker_b.call(
        "route_launch",
        sid=sid_own,
        prompt="just an ordinary question",
        workspace=str(workspace),
        continuation_id="",
        gate=str(Path(worker_b.state_dir) / "gate"),
        gate_timeout=5,
    )
    assert result.get("started"), f"process B could not start: {result}"

    deadline = time.time() + 30
    outcome = {}
    while time.time() < deadline:
        reply = worker_b.call("route_result")
        if reply.get("done"):
            outcome = reply.get("result") or {}
            break
        time.sleep(0.05)

    assert outcome, "process B's route launch never finished"
    assert "exception" not in outcome, (
        f"an ordinary message was refused by a stale peer state: {outcome}"
    )
    assert outcome.get("status") == 200, (
        f"the ordinary chat start did not succeed: {outcome}"
    )
    workers_launched = outcome.get("workers") or []
    assert len(workers_launched) == 1, (
        f"expected exactly one worker, got {len(workers_launched)}: {outcome}"
    )
    assert workers_launched[0].get("goal_related") is not True, (
        "an ordinary message was launched as a goal continuation: "
        f"{workers_launched[0].get('goal_related')!r}"
    )
