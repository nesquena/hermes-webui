"""#8613: an unowned eviction refused only because memory work is still queued
must RETAIN its cleanup intent and release the provider once that work drains,
without another agent reaching a session boundary.

Review 5722370551 (nesquena-hermes, 2026-10-31): "Also retain the
outstanding-work release intent until a last-holder save drains; requiring a
third agent's later build/evict to trigger cleanup leaves the original leak
unresolved. Preserve the newly fixed save-safety and unknown-owner
no-session-end boundaries."

The release must still never close the instance under a live holder, a
construction that is acquiring it, or a save that is still queued, and it must
run exactly once. The oracles are the provider's own counters and client state
(``shutdowns``, ``client.is_closed``) plus every queued save landing.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 P0 module)
from __future__ import annotations

import gc
import random
import threading
import time

import pytest

from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401 (fixtures)
    _key,
    hc,
    homes,
)
from tests.test_issue8613_provider_instance_holders import (  # noqa: F401
    SharedInstanceProvider,
    _build,
    _pause_saves,
    _queue_paused_save,
    _shared_agent,
    holder_registry,
    surviving_save,
)

import api.memory_profile_scope as scope
import api.streaming as streaming

mm_mod = pytest.importorskip("agent.memory_manager")
if not callable(getattr(getattr(mm_mod, "MemoryManager", None), "release_soft", None)):
    pytest.skip("installed Agent predates MemoryManager.release_soft", allow_module_level=True)


class RecordingProvider(SharedInstanceProvider):
    """Records the context-local profile home each shutdown ran under, and any
    session end (an unknown owner must never get one)."""

    def __init__(self):
        super().__init__()
        self.shutdown_homes: list[str] = []
        self.session_ends = 0

    def shutdown(self):
        import hermes_constants  # noqa: PLC0415 - the hc fixture installs it

        self.shutdown_homes.append(str(hermes_constants.get_hermes_home()))
        super().shutdown()

    def on_session_end(self, messages):
        self.session_ends += 1


def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _released_exactly_once(provider, timeout: float = 10.0) -> None:
    assert _wait_for(lambda: provider.shutdowns >= 1, timeout), (
        "the provider was never released after the queued work drained: the cleanup "
        "intent was dropped, and only another agent's session boundary would release it"
    )
    time.sleep(0.2)  # a second (double) release would land here
    assert provider.shutdowns == 1, f"released {provider.shutdowns} times"
    assert provider.client.is_closed is True
    assert _wait_for(lambda: id(provider) not in scope._provider_holders, 5), (
        "the released instance stayed in the holder table"
    )


def test_a_departed_agents_drained_save_releases_the_provider_without_another_boundary(
    hc, homes, holder_registry
):
    """A's save is queued; A is evicted (B still holds the instance) and then B,
    the last recorded holder, is evicted: refused because A's save still writes
    through the instance. Once that save lands the provider must be released —
    no third agent's build/evict may be needed."""
    provider = _pause_saves(RecordingProvider())
    first = _shared_agent(homes["alpha"], provider, "8613-pd-a")
    second = _shared_agent(homes["alpha"], provider, "8613-pd-b")
    _queue_paused_save(first, provider, "agent A's turn", hc)

    assert streaming._close_evicted_agent_at_session_boundary("8613-pd-a", first) is True
    assert streaming._close_evicted_agent_at_session_boundary("8613-pd-b", second) is True
    assert provider.shutdowns == 0, "the provider was torn down under a pending save"
    assert provider.client.is_closed is False

    provider.save_resume.set()
    assert provider.save_landed.wait(30), "the pending save never completed"
    assert provider.writes == ["agent A's turn"]
    _released_exactly_once(provider)
    assert provider.session_ends == 0, "an unknown owner got a session end"
    assert provider.shutdown_homes == [str(homes["alpha"])], provider.shutdown_homes


def test_a_sole_agents_own_drained_save_releases_the_provider_without_another_boundary(
    hc, homes, holder_registry
):
    """The sole holder is evicted on a path that must not wait (the per-session
    lock is held) while its OWN save is queued: the release is refused so the save
    lands — and then the provider must be released, in its bound profile home,
    without a session end."""
    provider = _pause_saves(RecordingProvider())
    agent = _shared_agent(homes["alpha"], provider, "8613-pd-sole")
    _queue_paused_save(agent, provider, "sole save", hc)

    with scope.release_without_fence_wait():
        assert streaming._close_evicted_agent_at_session_boundary("8613-pd-sole", agent) is True
    assert provider.shutdowns == 0 and provider.client.is_closed is False

    provider.save_resume.set()
    assert provider.save_landed.wait(30), "the pending save never completed"
    assert provider.writes == ["sole save"]
    _released_exactly_once(provider)
    assert provider.session_ends == 0, "an unknown owner got a session end"
    assert provider.shutdown_homes == [str(homes["alpha"])], provider.shutdown_homes


def test_a_collected_agents_drained_save_releases_the_provider_without_another_boundary(
    hc, homes, holder_registry
):
    """The GC variant: A is collected (not evicted) with its save queued, B is
    evicted and refused. The drained save must release the provider."""
    provider = _pause_saves(RecordingProvider())
    gone = [_shared_agent(homes["alpha"], provider, "8613-pd-gc-a")]
    second = _shared_agent(homes["alpha"], provider, "8613-pd-gc-b")
    _queue_paused_save(gone[0], provider, "the collected agent's turn", hc)
    gone.clear()
    gc.collect()

    assert streaming._close_evicted_agent_at_session_boundary("8613-pd-gc-b", second) is True
    assert provider.shutdowns == 0 and provider.client.is_closed is False

    provider.save_resume.set()
    assert provider.save_landed.wait(30), "the pending save never completed"
    assert provider.writes == ["the collected agent's turn"]
    _released_exactly_once(provider)


def test_a_retained_intent_never_releases_under_a_new_live_holder(hc, homes, holder_registry):
    """The intent is retained, then a new agent of the profile acquires the same
    instance before the save drains. The drained save must NOT close the client
    under that agent; its own boundary releases it, exactly once."""
    provider = _pause_saves(RecordingProvider())
    sole = _shared_agent(homes["alpha"], provider, "8613-pd-t-a")
    _queue_paused_save(sole, provider, "first save", hc)
    with scope.release_without_fence_wait():
        assert streaming._close_evicted_agent_at_session_boundary("8613-pd-t-a", sole) is True
    assert provider.shutdowns == 0

    newcomer = _shared_agent(homes["alpha"], provider, "8613-pd-t-b")
    provider.save_resume.set()
    assert provider.save_landed.wait(30)
    time.sleep(0.5)  # a (wrong) deferred release would land here
    assert provider.shutdowns == 0, "the provider was released under a live holder"
    assert surviving_save(newcomer, "newcomer save") is provider
    assert provider.writes == ["first save", "newcomer save"]

    assert streaming._close_evicted_agent_at_session_boundary("8613-pd-t-b", newcomer) is True
    _released_exactly_once(provider)


def test_a_retained_intent_waits_for_a_construction_that_acquires_the_instance(
    hc, homes, holder_registry
):
    """The save drains while a same-profile construction holds its acquisition
    reservation. The deferred release must wait for it and, when it acquires the
    instance, leave the instance to it."""
    provider = _pause_saves(RecordingProvider())
    sole = _shared_agent(homes["alpha"], provider, "8613-pd-f-a")
    _queue_paused_save(sole, provider, "first save", hc)
    with scope.release_without_fence_wait():
        assert streaming._close_evicted_agent_at_session_boundary("8613-pd-f-a", sole) is True

    started, proceed = threading.Event(), threading.Event()

    def pause_before_memory():
        started.set()
        assert proceed.wait(30), "the construction was never resumed"

    built: list = []
    builder = threading.Thread(
        target=lambda: built.append(
            _build(homes["alpha"], lambda: provider, "8613-pd-f-b", before_init=pause_before_memory)
        ),
        name="8613-pd-fence-build", daemon=True,
    )
    builder.start()
    assert started.wait(30), "the construction never started"

    provider.save_resume.set()
    assert provider.save_landed.wait(30)
    time.sleep(0.5)
    assert provider.shutdowns == 0, "released while a construction was acquiring the instance"

    proceed.set()
    builder.join(30)
    assert not builder.is_alive() and built, "the construction did not finish"
    time.sleep(0.5)
    assert provider.shutdowns == 0, "released under the agent that just acquired the instance"
    assert surviving_save(built[0], "newcomer save") is provider

    assert streaming._close_evicted_agent_at_session_boundary("8613-pd-f-b", built[0]) is True
    _released_exactly_once(provider)


class _SlowProvider(RecordingProvider):
    """Saves that take a few milliseconds, so evictions race with draining work."""

    def remember(self, marker):
        time.sleep(random.uniform(0.0, 0.03))
        super().remember(marker)


@pytest.mark.parametrize("seed", range(6))
def test_stress_concurrent_evictions_and_draining_saves_release_exactly_once(
    seed, hc, homes, holder_registry
):
    """Four agents share one instance, each with a save in flight, and are evicted
    concurrently (half of them on the no-wait path). Every save must land, no
    thread may hang, and the provider must end up released exactly once."""
    random.seed(seed)
    provider = _SlowProvider()
    agents = [_shared_agent(homes["alpha"], provider, f"8613-pd-s{seed}-{i}") for i in range(4)]
    markers = [f"save-{seed}-{i}" for i in range(4)]
    futures = []
    token = hc.set_hermes_home_override(str(homes["alpha"]))
    try:
        for agent, marker in zip(agents, markers, strict=True):
            agent._memory_manager.sync_all(marker, "reply", session_id=agent.session_id)
            futures.extend(list(agent._memory_manager._background_futures))
    finally:
        hc.reset_hermes_home_override(token)

    barrier = threading.Barrier(len(agents), timeout=10)
    errors: list = []

    def evict(index, agent):
        try:
            barrier.wait()
            time.sleep(random.uniform(0.0, 0.02))
            if index % 2:
                with scope.release_without_fence_wait():
                    streaming._close_evicted_agent_at_session_boundary(agent.session_id, agent)
            else:
                streaming._close_evicted_agent_at_session_boundary(agent.session_id, agent)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))

    threads = [
        threading.Thread(target=evict, args=(i, a), name=f"8613-pd-stress-{i}", daemon=True)
        for i, a in enumerate(agents)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), "an eviction hung"
    assert not errors, errors
    for future in futures:
        future.result(timeout=30)
    assert sorted(provider.writes) == sorted(markers), "a queued save was lost"
    _released_exactly_once(provider)


def test_a_holder_collected_while_the_claim_holds_the_lock_keeps_its_queued_save(
    hc, homes, holder_registry, monkeypatch
):
    """Codex r1 on the salvage (CORE). A is collected on another thread while B's
    claim holds the holder lock: A's GC callback only QUEUES its forget, so A's
    manager's queued save was not a work hold yet, and the proof skipped A as
    "collected" — B's release closed the client under A's save. The save must
    land, and the provider must still be released once it has drained."""
    was_enabled = gc.isenabled()
    gc.disable()
    provider = _pause_saves(RecordingProvider())
    first = _shared_agent(homes["alpha"], provider, "8613-pd-gcq-a")
    second = _shared_agent(homes["alpha"], provider, "8613-pd-gcq-b")
    _queue_paused_save(first, provider, "departed save", hc)
    futures = list(first._memory_manager._background_futures)
    type(first).built.clear()
    first.cycle = first  # only a collection frees it
    del first
    begin_gc, collected = threading.Event(), threading.Event()

    def collect():
        assert begin_gc.wait(10)
        gc.collect()
        collected.set()

    collector = threading.Thread(target=collect, name="8613-pd-gcq-collector", daemon=True)
    collector.start()
    original = scope._agent_holds_instances_locked

    def collect_while_the_claim_holds_the_lock(agent):
        result = original(agent)
        begin_gc.set()
        assert collected.wait(10), "the weak-reference callback blocked on the holder lock"
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(scope, "_agent_holds_instances_locked", collect_while_the_claim_holds_the_lock)
            assert streaming._close_evicted_agent_at_session_boundary("8613-pd-gcq-b", second) is True
        closed_before_save = provider.client.is_closed
        provider.save_resume.set()
        for future in futures:
            future.result(timeout=30)
        assert provider.writes == ["departed save"], {
            "closed_before_save": closed_before_save, "shutdowns": provider.shutdowns,
        }
        assert closed_before_save is False
        _released_exactly_once(provider)
    finally:
        provider.save_resume.set()
        collector.join(10)
        if was_enabled:
            gc.enable()
