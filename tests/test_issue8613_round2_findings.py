"""#8613 round 2 (maintainer, 2026-10-31): the three blocking findings of the
release gate on ``ad11863b``.

The gate re-ran the whole suite against the installed Agent's real
``MemoryManager``, a provider with a real ``httpx.Client`` and the real eviction
entry point, and reported that the round-1 fixes had opened three new holes.
Each test here reproduces ONE of them on the current head, so the fix can be
proved by the same test turning green.

A. Work holds keyed by ``id(agent)`` lose a save after ID reuse.
B. The waiting agent's entry is pruned during the fence wait.
C. Lock-order inversion between the holder lock and ``Future._condition``.

The A and B tests reuse the production constructor
(``construct_profile_scoped_agent``) and the real eviction entry point, so the
holder table is populated exactly the way a turn populates it. The oracle in A
is the SURVIVING save: ``httpx`` refuses a request on a closed client, so a
release that ran under a queued save fails it.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 P0 module)
from __future__ import annotations

import gc
import threading
import time

import pytest

from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401 (fixtures)
    _key,
    hc,
    homes,
)
from tests.test_issue8613_provider_instance_holders import (  # noqa: F401
    PROVIDER_NAME,
    SharedInstanceProvider,
    _agent_home,
    _build,
    _fresh_instance,
    _pause_saves,
    _queue_paused_save,
    _shared_agent,
    holder_registry,
)

import api.memory_profile_scope as scope
import api.streaming as streaming

mm_mod = pytest.importorskip("agent.memory_manager")
if not callable(getattr(getattr(mm_mod, "MemoryManager", None), "release_soft", None)):
    pytest.skip("installed Agent predates MemoryManager.release_soft", allow_module_level=True)


# ── A. work holds keyed by id(agent) lose a save after ID reuse ──────────────
#
# CPython reuses the id() of a collected object. The work hold of a departed
# agent is stored under its id(), so a LATER agent that lands on the same id()
# inherits the hold: `_last_provider_holder_locked` skips `entry.work[pid]` as
# its own work (`if pid == agent_pid: continue`) and the release closes the
# shared client under the DEPARTED agent's queued save.
#
# The id reuse itself is the allocator's business, not the code's — so the test
# drives the state the reuse PRODUCES rather than the allocator: the departed
# agent's queued work is recorded against the departing agent, and the
# surviving agent then behaves as if it were that same agent. Whether the
# surviving agent's id() literally equals the departed one (the allocator's
# timing) or not, the hold must belong to the DEPARTED agent's manager — never
# to whichever agent happens to be released next.


def _rekey_the_departed_hold_onto(entry, survivor) -> None:
    """Make the surviving agent carry the departed agent's work hold.

    This is the state CPython's id() reuse produces: the hold is filed under
    the id the surviving agent now carries, so the surviving agent's own proof
    walks over it as if the queued work were its own.
    """
    holds = list(entry.work.items())
    assert holds, "the departed agent's queued save is not held"
    for pid, manager_ref in holds:
        entry.work[id(survivor)] = manager_ref
        del entry.work[pid]


def test_a_reused_agent_id_does_not_lose_the_departed_agents_pending_save(
    hc, homes, holder_registry
):
    """A (Codex, r2). A's queued save must survive B's release even when B
    happens to carry A's recycled ``id()``.

    The hold table keys a departed agent's queued work by ``id(agent)``. When a
    later agent is allocated at the same address — which CPython does as soon as
    A's slot is free — the hold is now filed under B's id, and B's own proof
    skips it as "its own work". The release then closes the shared client while
    A's save is still queued, and A's save fails with httpx's own error.

    The fix keys and compares the work holds by the MANAGER, not by the agent
    id, so the hold belongs to A's manager and B's proof counts it.
    """
    provider = _pause_saves(SharedInstanceProvider())
    departed = [_shared_agent(homes["alpha"], provider, "8613-r2-id-a")]
    survivor = _shared_agent(homes["alpha"], provider, "8613-r2-id-b")

    # A's turn ends with a background save queued on A's own manager worker.
    _queue_paused_save(departed[0], provider, "the departed agent's turn", hc)

    # A is dropped: the weak-reference callback moves its queued work into the
    # hold table.
    del departed[0]
    departed.clear()
    gc.collect()

    entry = scope._provider_holders[id(provider)]
    assert entry.work, "the departed agent's queued save is not held"

    # The allocator hands a new agent the address A just freed: the hold is
    # then filed under the surviving agent's id, and the surviving agent's own
    # proof walks over it as if the queued work were its own.
    _rekey_the_departed_hold_onto(entry, survivor)

    # B is the last RECORDED holder. Its proof must still count A's queued work.
    ok, why = scope.agent_is_last_provider_holder(survivor)
    assert ok is False and "queued memory work" in why, (
        f"the departed agent's queued save was not counted: {why!r}"
    )

    # ORACLE: B's release must not close the client A's save still needs.
    assert streaming._close_evicted_agent_at_session_boundary("8613-r2-id-b", survivor) is True
    assert provider.shutdowns == 0, "the provider was torn down under a pending save"
    assert provider.client.is_closed is False

    provider.save_resume.set()
    assert provider.save_landed.wait(30), "the pending save never completed"
    assert provider.writes == ["the departed agent's turn"]


def test_a_work_hold_is_not_shared_between_two_agents_with_one_manager(
    hc, homes, holder_registry
):
    """A (Codex, r2), the other half. Two agents of one profile share the
    INSTANCE but not the MANAGER: each owns its own ``MemoryManager``. A work
    hold must therefore never be transferred from one agent's id to another's —
    the hold belongs to the manager whose worker runs the save.

    This pins the fix's shape: the hold table keys a queued save by the manager
    it belongs to, so a hold can only be found (and only be removed) through
    that manager.
    """
    provider = _pause_saves(SharedInstanceProvider())
    first = _shared_agent(homes["alpha"], provider, "8613-r2-mgr-a")
    second = _shared_agent(homes["alpha"], provider, "8613-r2-mgr-b")
    assert first._memory_manager is not second._memory_manager

    _queue_paused_save(first, provider, "the first agent's turn", hc)

    del first
    gc.collect()

    entry = scope._provider_holders[id(provider)]
    assert entry.work, "the departed agent's queued save is not held"

    # Every work hold is keyed by the manager that owns the queued save. The
    # second agent's manager is NOT among them, so its proof counts the hold
    # as another agent's work.
    held_managers = [ref() for ref in entry.work.values()]
    assert second._memory_manager not in held_managers

    ok, why = scope.agent_is_last_provider_holder(second)
    assert ok is False and "queued memory work" in why, why

    assert streaming._close_evicted_agent_at_session_boundary("8613-r2-mgr-b", second) is True
    assert provider.shutdowns == 0, "the provider was torn down under a pending save"
    assert provider.client.is_closed is False

    provider.save_resume.set()
    assert provider.save_landed.wait(30), "the pending save never completed"
    assert provider.writes == ["the first agent's turn"]


# ── B. the waiting agent's entry is pruned during the fence wait ─────────────
#
# `_claim_last_held_teardown` removes the evicting agent from `entry.holders`
# BEFORE it may `cond.wait()`. While it waits the entry has no holders, no
# teardown flag and no work — and all three prune sites delete every such entry
# PROCESS-WIDE, not just their own. When the wait ends the proof answers "not
# tracked as held by any agent": the client stays open and untracked, which is
# the #8613 leak again, now confined to the wait window.


def test_the_fenced_entry_survives_an_unrelated_eviction_during_the_wait(
    hc, homes, holder_registry
):
    """B (advisor, r2). A release that waits for a same-profile construction must
    keep its own entry in the holder table while it waits.

    The advisor reproduced it two ways, with no timeout involved: an ordinary
    owned eviction of an unrelated agent on another worker during the wait, and
    any registered agent being garbage-collected during the wait.
    """
    provider = SharedInstanceProvider()
    fenced = _shared_agent(homes["alpha"], provider, "8613-r2-prune-fenced")

    started = threading.Event()
    proceed = threading.Event()

    def pause_before_memory():
        started.set()
        assert proceed.wait(30), "the fenced construction was never resumed"

    def build_other():
        _build(homes["alpha"], _fresh_instance, "8613-r2-prune-builder",
               before_init=pause_before_memory)

    builder = threading.Thread(target=build_other, name="8613-r2-prune-build", daemon=True)
    builder.start()
    assert started.wait(30), "the construction never started"

    # The release claims the instance and waits for that construction.
    claim_result: dict = {}

    def claim():
        ok, why, claimed = scope._claim_last_held_teardown(fenced)
        claim_result.update(ok=ok, why=why, claimed=claimed)

    claimer = threading.Thread(target=claim, name="8613-r2-prune-claim", daemon=True)
    claimer.start()

    # Wait until the claim is really waiting on the fence: the entry has lost
    # its holder (the claim drops the agent before it waits) and the table still
    # holds the entry.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if scope._provider_holders.get(id(provider)) is not None:
            entry = scope._provider_holders[id(provider)]
            if not entry.holders and not entry.teardown:
                break
        time.sleep(0.01)
    else:  # pragma: no cover - the claim never reached its wait
        proceed.set()
        pytest.fail("the claim never reached the fence wait")

    # An unrelated eviction runs on another worker during the wait. Its prune
    # must not delete the WAITING agent's entry.
    other_provider = SharedInstanceProvider()
    unrelated = _shared_agent(homes["beta"], other_provider, "8613-r2-prune-unrelated")
    assert streaming._close_evicted_agent_at_session_boundary(
        "8613-r2-prune-unrelated", unrelated
    ) is True

    entry = scope._provider_holders.get(id(provider))
    assert entry is not None, (
        "the waiting agent's entry was pruned during the fence wait — the "
        "release will answer 'not tracked as held by any agent' and leak the client"
    )

    proceed.set()
    builder.join(30)
    claimer.join(30)
    assert not builder.is_alive() and not claimer.is_alive()

    assert claim_result.get("ok") is True, claim_result.get("why")
    # The claim proved the entry survived the wait; the release itself is the
    # caller's job (the test drives the teardown the way the eviction path
    # does).
    agent = fenced
    claimed = claim_result.get("claimed") or []
    try:
        agent._memory_manager.release_soft()
    finally:
        scope._settle_last_held_teardown(agent, claimed, released=True)
    assert provider.shutdowns == 1, "the fenced release never ran"
    assert provider.client.is_closed is True


def test_the_fenced_entry_survives_a_garbage_collection_during_the_wait(
    hc, homes, holder_registry
):
    """B (advisor, r2), the GC variant: a registered agent collected during the
    wait must not prune the waiting agent's entry either.
    """
    provider = SharedInstanceProvider()
    fenced = _shared_agent(homes["alpha"], provider, "8613-r2-prune-gc-fenced")

    started = threading.Event()
    proceed = threading.Event()

    def pause_before_memory():
        started.set()
        assert proceed.wait(30), "the fenced construction was never resumed"

    def build_other():
        _build(homes["alpha"], _fresh_instance, "8613-r2-prune-gc-builder",
               before_init=pause_before_memory)

    builder = threading.Thread(target=build_other, name="8613-r2-prune-gc-build", daemon=True)
    builder.start()
    assert started.wait(30), "the construction never started"

    claim_result: dict = {}

    def claim():
        ok, why, claimed = scope._claim_last_held_teardown(fenced)
        claim_result.update(ok=ok, why=why, claimed=claimed)

    claimer = threading.Thread(target=claim, name="8613-r2-prune-gc-claim", daemon=True)
    claimer.start()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        entry = scope._provider_holders.get(id(provider))
        if entry is not None and not entry.holders and not entry.teardown:
            break
        time.sleep(0.01)
    else:  # pragma: no cover - the claim never reached its wait
        proceed.set()
        pytest.fail("the claim never reached the fence wait")

    # A registered agent is garbage-collected during the wait: its weakref
    # callback runs the prune for the whole table.
    collected = [_shared_agent(homes["alpha"], _fresh_instance(), "8613-r2-prune-gc-victim")]
    del collected[0]
    collected.clear()
    gc.collect()

    entry = scope._provider_holders.get(id(provider))
    assert entry is not None, (
        "the waiting agent's entry was pruned when another agent was collected"
    )

    proceed.set()
    builder.join(30)
    claimer.join(30)
    assert not builder.is_alive() and not claimer.is_alive()

    assert claim_result.get("ok") is True, claim_result.get("why")
    agent = fenced
    claimed = claim_result.get("claimed") or []
    try:
        agent._memory_manager.release_soft()
    finally:
        scope._settle_last_held_teardown(agent, claimed, released=True)
    assert provider.shutdowns == 1, "the fenced release never ran"


# ── the maintainer's non-blocking note: the fence wait stalls a lock holder ──
#
# The fence wait (bounded, 15 s) exists so a release does not close an instance
# a same-profile construction is about to acquire. The compression-migration
# close runs inside the per-session agent lock, so waiting there holds that
# lock and stalls the user's next turn. That close must therefore REFUSE the
# release right away — the provider keeps its worker until the next session
# boundary — instead of waiting.


def test_a_release_that_must_not_block_refuses_instead_of_waiting(
    hc, homes, holder_registry
):
    """The maintainer's note on the fence wait: a caller that holds a lock and
    must not block refuses the release instead of waiting for the
    construction.

    The oracle is the WAIT ITSELF: with a same-profile construction in
    flight, the claim would otherwise sit on the fence for its full 15 s
    budget.
    """
    provider = SharedInstanceProvider()
    agent = _shared_agent(homes["alpha"], provider, "8613-r2-nowait")

    started = threading.Event()
    proceed = threading.Event()

    def pause_before_memory():
        started.set()
        assert proceed.wait(30), "the fenced construction was never resumed"

    builder = threading.Thread(
        target=lambda: _build(
            homes["alpha"], _fresh_instance, "8613-r2-nowait-builder",
            before_init=pause_before_memory,
        ),
        name="8613-r2-nowait-build",
        daemon=True,
    )
    builder.start()
    assert started.wait(30), "the construction never started"

    # The construction is in flight, so the release is fenced.
    began = time.monotonic()
    with scope.release_without_fence_wait():
        ok, why, claimed = scope._claim_last_held_teardown(agent)
    waited = time.monotonic() - began

    assert ok is False, "the release must be refused, not granted"
    assert "acquired by" in why, why
    assert waited < 5, f"the release waited {waited:.1f}s for the fence"

    # The instance is untouched: no client was closed under a live agent.
    assert provider.shutdowns == 0
    assert provider.client.is_closed is False

    proceed.set()
    builder.join(30)
    assert not builder.is_alive()


def test_the_fence_wait_still_applies_to_a_release_that_may_wait(
    hc, homes, holder_registry
):
    """The refusal above is opt-IN: an ordinary release still waits for the
    construction, and runs the teardown once it resolves. Without this, the
    note's fix would silently disable the fence everywhere."""
    provider = SharedInstanceProvider()
    agent = _shared_agent(homes["alpha"], provider, "8613-r2-wait-ok")

    started = threading.Event()
    proceed = threading.Event()

    def pause_before_memory():
        started.set()
        assert proceed.wait(30), "the fenced construction was never resumed"

    builder = threading.Thread(
        target=lambda: _build(
            homes["alpha"], _fresh_instance, "8613-r2-wait-ok-builder",
            before_init=pause_before_memory,
        ),
        name="8613-r2-wait-ok-build",
        daemon=True,
    )
    builder.start()
    assert started.wait(30), "the construction never started"

    result: dict = {}

    def claim():
        ok, why, claimed = scope._claim_last_held_teardown(agent)
        result.update(ok=ok, why=why, claimed=claimed)

    claimer = threading.Thread(target=claim, name="8613-r2-wait-ok-claim", daemon=True)
    claimer.start()

    # The claim is waiting on the fence.
    time.sleep(0.5)
    assert claimer.is_alive(), "the ordinary release did not wait for the construction"

    proceed.set()
    builder.join(30)
    claimer.join(30)
    assert not builder.is_alive() and not claimer.is_alive()
    assert result.get("ok") is True, result.get("why")

    claimed = result.get("claimed") or []
    try:
        agent._memory_manager.release_soft()
    finally:
        scope._settle_last_held_teardown(agent, claimed, released=True)
    assert provider.shutdowns == 1, "the ordinary release never ran"


# ── C. lock-order inversion between the holder lock and Future._condition ────
#
# `_manager_has_outstanding_work` calls `future.done()`, which takes the
# future's condition lock. It is called with `_provider_holders_lock` held. The
# writer thread holds that same condition inside `Future.set_result`, and
# allocates there, so a gen-0 collection can run inside it; if that collection
# frees a registered agent caught in a reference cycle, the weakref callback
# `_forget_agent_holder` blocks on the holder lock while the eviction thread
# holding the holder lock blocks on the condition — a deadlock.


def test_the_holder_lock_is_never_taken_with_a_futures_condition_lock_held(
    hc, homes, holder_registry
):
    """C (advisor, r2). The eviction thread must not block on a future's
    condition while it holds the holder lock, and the GC callback must not
    block on the holder lock while it holds that condition.

    The advisor reproduced the deadlock deterministically: one thread holds the
    future's condition and runs ``gc.collect()`` on a dropped cyclic agent, the
    other holds the holder lock and calls ``_manager_has_outstanding_work``.

    The schedule here is strictly ordered, so the deadlock is deterministic
    rather than a race:

    1. the writer takes the future's condition and announces it;
    2. the evictor takes the holder lock and calls the work check — which,
       pre-fix, wants the future's condition the writer holds;
    3. the writer, still holding that condition, collects a registered agent
       caught in a reference cycle: its weakref callback wants the holder lock
       the evictor holds.

    Each thread now waits for the other, and every later memory-bearing
    construction blocks in ``_reserve_acquisition`` until restart.

    The fix reads the future's ``_state`` instead of calling ``done()``, so no
    thread ever waits for the other.
    """
    from concurrent.futures import Future  # noqa: PLC0415

    provider = SharedInstanceProvider()
    agent = _shared_agent(homes["alpha"], provider, "8613-r2-lock-a")

    # A future that is NOT done, held by the agent's manager: the real
    # `concurrent.futures.Future` shape the deadlock needs.
    pending = Future()
    manager = agent._memory_manager
    manager._background_futures = {pending: "write"}

    cond = pending._condition
    cond_held = threading.Event()
    lock_held = threading.Event()

    def writer():
        """Holds the future's condition, then collects a registered agent."""
        with cond:
            cond_held.set()
            assert lock_held.wait(30), "the eviction thread never took the holder lock"
            # A registered agent caught in a reference cycle: only the cyclic
            # collector can free it, so its weakref callback runs inside the
            # gc.collect() below — while this thread holds the condition.
            cyclic = _shared_agent(homes["alpha"], _fresh_instance(), "8613-r2-lock-cyclic")
            type(cyclic).built.clear()
            cycle: list = [cyclic]
            cyclic.cycle_ref = cycle
            del cyclic, cycle
            gc.collect()

    def evictor():
        """Holds the holder lock and asks whether the manager has work."""
        assert cond_held.wait(30), "the writer thread never took the condition"
        with scope._provider_holders_lock:
            lock_held.set()
            scope._manager_has_outstanding_work(manager)

    writer_thread = threading.Thread(target=writer, name="8613-r2-lock-writer", daemon=True)
    evictor_thread = threading.Thread(target=evictor, name="8613-r2-lock-evictor", daemon=True)
    writer_thread.start()
    evictor_thread.start()

    writer_thread.join(20)
    evictor_thread.join(20)

    assert not writer_thread.is_alive(), (
        "the writer thread deadlocked: the GC callback wanted the holder lock "
        "while the eviction thread waited on the future's condition"
    )
    assert not evictor_thread.is_alive(), (
        "the eviction thread deadlocked on a future's condition while it held "
        "the holder lock"
    )


def test_the_work_check_does_not_take_the_futures_condition_lock(hc, homes, holder_registry):
    """C (advisor, r2), the mechanism. Reading the manager's outstanding work
    must not acquire the future's condition lock: that is the edge that
    inverts the lock order against a writer inside ``Future.set_result``.

    A real ``Future``'s condition is a real lock, so the test can prove the
    lock is NOT taken — not merely that the run finished.
    """
    from concurrent.futures import Future  # noqa: PLC0415

    provider = SharedInstanceProvider()
    agent = _shared_agent(homes["alpha"], provider, "8613-r2-lock-b")

    pending = Future()
    manager = agent._memory_manager
    manager._background_futures = {pending: "write"}

    # Track every acquisition of the future's condition lock. ``Condition``
    # uses its own ``_lock``; ``done()`` takes it through ``__enter__``.
    taken: list[str] = []
    real_lock = pending._condition._lock

    class _TrackingLock:
        def __enter__(self):
            taken.append("acquire")
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

        def acquire(self, *args, **kwargs):  # pragma: no cover - not used here
            taken.append("acquire")
            return real_lock.acquire(*args, **kwargs)

        def release(self):  # pragma: no cover - not used here
            return real_lock.release()

    pending._condition._lock = _TrackingLock()

    assert scope._manager_has_outstanding_work(manager) is True
    assert not taken, "the future's condition lock was taken under the holder lock"

    # A finished future still answers without taking the lock.
    pending.set_result(None)
    taken.clear()
    assert scope._manager_has_outstanding_work(manager) is False
    assert not taken, "the future's condition lock was taken for a finished future"
