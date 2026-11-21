"""#7862 round 21 (end-to-end): the restart -> pending -> consume -> persist chain.

The unit tests pin each mechanism. This file pins the USER-VISIBLE chain
the mechanisms exist for, across a REAL process boundary:

    arm (process A)
      -> the process dies
      -> a cold process restores the pending continuation
      -> the browser's token is accepted and the intent is consumed
      -> the durable registry no longer holds the record
      -> a later restore does NOT bring it back
      -> an ordinary turn is still refused by neither the stale state nor
         a duplicate worker

The schedule uses the round-16 out-of-process harness: two real
interpreters, one shared durable state directory, the real ``flock``. No
real target and no real tool is run -- the store's own writer is the
production code under test.
"""

from __future__ import annotations

import pytest

# The round-17 worker is the round-16 worker plus the two commands this
# chain needs: ``consume_restored`` (spend a restored record as this
# process's own start attempt) and ``rollback`` (the rejected-start
# compensation). The maintainer asked to KEEP that harness design.
from tests.test_issue7862_round17_findings import _Worker as _Round17Worker  # noqa: E402

PROMPT = "keep refining the release checklist"


class _Worker(_Round17Worker):
    """One live WebUI process holding its own view of the registry."""


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


def test_a_pending_continuation_survives_a_restart_and_is_consumed_once(workers):
    """The whole chain, across a process boundary.

    This is the user-visible promise: a queued continuation survives the
    server restarting, the browser's token still works afterwards, the
    intent is consumed exactly once, and the durable registry ends up
    without it.
    """
    sid = "sess-r21-e2e"
    cont_id = "e" * 32

    # ── process A: the intent is created and persisted ────────────────────
    worker_a = workers("A")
    assert worker_a.call(
        "arm", sid=sid, prompt=PROMPT, continuation_id=cont_id
    )["ok"], "process A could not persist the continuation"

    dump_a = worker_a.call("dump")
    assert sid in dump_a["records"], (
        "the continuation was not persisted durably before the restart"
    )

    # ── process A dies; process B is a cold process ───────────────────────
    worker_a.close()
    worker_b = workers("B")

    # B's own memory has nothing (it never observed A).
    dump_b_before = worker_b.call("dump")
    assert sid in dump_b_before["records"], (
        "the durable state is not shared between the two processes, so the "
        "restart case cannot be exercised"
    )

    # B performs the startup restore (the production entry point).
    restored = worker_b.call("restore")
    assert restored["restored"] == 1, (
        f"the cold process did not restore the pending continuation: {restored}"
    )

    # ── the browser's token is accepted after the restart ─────────────────
    consumed = worker_b.call(
        "arm_consume", sid=sid, prompt=PROMPT, attempt="att-B"
    )
    assert consumed["outcome"] == "committed", (
        "the browser's token was refused after the restart: the queued turn "
        f"cannot run ({consumed})"
    )

    # ── the durable registry no longer holds it ───────────────────────────
    dump_b_after = worker_b.call("dump")
    assert sid not in dump_b_after["records"], (
        "the consumed continuation is still claimable on disk: a later "
        "restart would restore work that already ran"
    )

    # ── a later restore does NOT bring it back ────────────────────────────
    restored_again = worker_b.call("restore")
    assert restored_again["restored"] == 0, (
        "a later restore brought the consumed continuation back: completed "
        "work was resurrected"
    )
    dump_b_final = worker_b.call("dump")
    assert sid not in dump_b_final["records"], (
        "the consumed continuation came back after a later restore"
    )


def test_a_late_rollback_does_not_resurrect_completed_work(workers):
    """The counter-control: completed work stays completed.

    A rejected start rolls its intent back. That rollback must NOT bring
    back an intent another process already consumed -- the round-19
    "completed work returns after recovery" invariant, checked here on the
    same chain.
    """
    sid = "sess-r21-e2e-rollback"
    cont_id = "f" * 32

    worker_a = workers("A")
    worker_b = workers("B")

    assert worker_a.call(
        "arm", sid=sid, prompt=PROMPT, continuation_id=cont_id
    )["ok"]

    # Process B spends it (the successful turn).
    spent = worker_b.call("arm_consume", sid=sid, prompt=PROMPT, attempt="att-B")
    assert spent["outcome"] == "committed", (
        f"process B could not spend the continuation: {spent}"
    )

    # Process A's late rejected start rolls ITS attempt back.
    rollback = worker_a.call("rollback", sid=sid, attempt="att-A")
    assert rollback.get("accepted") is False, (
        "a late rollback re-armed an intent another process already consumed"
    )

    dump = worker_b.call("dump")
    assert sid not in dump["records"], (
        "a late rollback resurrected completed work: the durable registry "
        "holds the intent again"
    )

    # And a restore does not bring it back either.
    restored = worker_b.call("restore")
    assert restored["restored"] == 0, (
        "a restore resurrected the completed work: the intent came back"
    )


def test_an_ordinary_turn_after_a_restart_is_not_mistaken_for_the_continuation(
    workers,
):
    """The counter-control: an ordinary message stays ordinary.

    After a restart the pending intent is live again. An UNRELATED message
    must not be swallowed as the continuation (that would retire the intent
    and let the goal machinery queue another automatic continuation on top
    of an unrelated turn).
    """
    sid = "sess-r21-e2e-ordinary"
    cont_id = "0" * 32

    worker_a = workers("A")
    worker_b = workers("B")

    assert worker_a.call(
        "arm", sid=sid, prompt=PROMPT, continuation_id=cont_id
    )["ok"]
    assert worker_b.call("restore")["restored"] == 1

    # An unrelated turn, with NO token: the store's text comparison must
    # not match it, so the intent stays pending.
    outcome = worker_b.call(
        "consume_restored",
        sid=sid,
        prompt="what about the weather?",
        attempt="att-ordinary",
    )
    assert outcome["outcome"] == "not_matching", (
        "an ordinary turn was consumed as the continuation: the intent was "
        f"retired by an unrelated message ({outcome})"
    )

    dump = worker_b.call("dump")
    assert sid in dump["records"], (
        "the pending continuation was retired by an ordinary turn: the "
        "intent must survive an unrelated message"
    )
