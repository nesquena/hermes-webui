"""#8613 (RM fold on the #8793 release stage): a retained release intent arms
each queued save's future at most once.

When several agents share one provider instance and each departed agent leaves
a queued save holding it, the last holder's unowned eviction stores a release
intent and arms a done-callback on every manager's last queued future. A retry
that runs when one save drains still finds the others outstanding and re-arms.
Before this fold the re-arm added the callback AGAIN to every future that
already carried it, so the callbacks on each remaining future doubled at every
completion: 2**K - 1 retry threads for K managers draining in turn (1,023 for
ten, measured with the installed Agent). Each future is now armed once per
intent, so the retries stay at one per manager and the provider is still
released exactly once.
"""
from __future__ import annotations

import threading

import pytest

from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401 (fixtures)
    hc,
    homes,
)
from tests.test_issue8613_provider_instance_holders import (  # noqa: F401
    _agent_home,
    _shared_agent,
    holder_registry,
)
from tests.test_issue8613_post_drain_release import (
    RecordingProvider,
    _released_exactly_once,
    _wait_for,
)

import api.memory_profile_scope as scope
import api.streaming as streaming

mm_mod = pytest.importorskip("agent.memory_manager")
if not callable(getattr(getattr(mm_mod, "MemoryManager", None), "release_soft", None)):
    pytest.skip("installed Agent predates MemoryManager.release_soft", allow_module_level=True)

AGENTS = 6


def test_a_retained_intent_arms_each_queued_save_once(
    hc, homes, holder_registry, monkeypatch  # noqa: F811 (pytest fixtures imported above)
):
    provider = RecordingProvider()
    markers = [f"agent {i}'s turn" for i in range(AGENTS)]
    started = {marker: threading.Event() for marker in markers}
    resume = {marker: threading.Event() for marker in markers}
    real_remember = provider.remember

    def gated_remember(marker):
        started[marker].set()
        assert resume[marker].wait(30), "a queued save was never released"
        real_remember(marker)

    provider.remember = gated_remember

    agents = [_shared_agent(homes["alpha"], provider, f"8613-arm-{i}") for i in range(AGENTS)]
    for agent, marker in zip(agents, markers, strict=True):
        token = hc.set_hermes_home_override(_agent_home(agent))
        try:
            agent._memory_manager.sync_all(marker, "reply", session_id=agent.session_id)
        finally:
            hc.reset_hermes_home_override(token)
        assert started[marker].wait(30), "the queued save never reached the provider"

    retries = []
    real_start = scope._start_intent_retry

    def counting_start(intent, **kwargs):
        retries.append(intent)
        return real_start(intent, **kwargs)

    monkeypatch.setattr(scope, "_start_intent_retry", counting_start)

    # Every agent but the last departs with its save still queued; the last one,
    # evicted with no resolvable owner, is refused and keeps the release intent.
    last = agents.pop()
    for agent in agents:
        scope.unregister_provider_holders(agent)
    agents.clear()
    with scope.release_without_fence_wait():
        assert streaming._close_evicted_agent_at_session_boundary("8613-arm-last", last) is True
    del last
    assert provider.shutdowns == 0, "the provider was torn down under the queued saves"

    for count, marker in enumerate(markers, start=1):
        resume[marker].set()
        assert _wait_for(lambda count=count: len(provider.writes) >= count, 30), provider.writes
    assert sorted(provider.writes) == sorted(markers)

    _released_exactly_once(provider)
    assert len(retries) <= AGENTS, (
        f"{len(retries)} retry threads for {AGENTS} queued saves: a future was armed "
        f"more than once (the doubling gives {2 ** AGENTS - 1})"
    )


class _QueuedFuture:
    """A queued future as the gate reads it (``_state`` without the lock)."""

    _state = "PENDING"

    def __init__(self, fail=False):
        self.fail = fail
        self.callbacks = []

    def add_done_callback(self, callback):
        if self.fail:
            raise RuntimeError("cannot attach")
        self.callbacks.append(callback)


def test_a_failed_attach_leaves_the_unattached_futures_armable(monkeypatch):
    """When attaching to one future fails, the futures after it are not left
    reserved without a callback: the next arm hooks them (senior review of the
    fold). Unreachable with the installed Agent, whose futures always accept a
    callback, so this drives the arm directly."""
    import types

    failing, good = _QueuedFuture(fail=True), _QueuedFuture()
    departed = types.SimpleNamespace(_background_futures={failing: None})
    other = types.SimpleNamespace(_background_futures={good: None})
    intent = scope._ReleaseIntent(departed, "/tmp/home", None, "8613-attach")
    entry = types.SimpleNamespace(intent=intent, work={1: lambda: other})
    monkeypatch.setattr(scope, "_provider_holders", {"instance": entry})

    assert scope._arm_release_intent(intent) == "unhookable"
    assert good.callbacks == []
    failing.fail = False
    assert scope._arm_release_intent(intent) == "hooked"
    assert len(good.callbacks) == 1, "the unattached future was never armed"
    assert len(failing.callbacks) == 1
    assert scope._arm_release_intent(intent) == "hooked"
    assert len(good.callbacks) == 1 and len(failing.callbacks) == 1, "a future was armed twice"
