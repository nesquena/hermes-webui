"""#8817: a memory release refused by a construction in flight keeps its cleanup
intent and runs once that construction has finished.

An agent whose session owner cannot be resolved is evicted while another agent
of the SAME profile is being constructed. The release of the evicted agent's
memory provider is fenced by that construction: a caller that must not block
(``release_without_fence_wait``) refuses at once, and an ordinary caller gives
up after ``_ACQUISITION_FENCE_WAIT_S``. Before #8817 both refusals dropped the
cleanup intent, and the claim had already taken the agent out of the holder
table, so when the new agent got a DIFFERENT provider instance nothing ever
released the old one: its writer thread and client stayed alive until the
WebUI restarted. v0.52.544 ran ``shutdown_all`` on that eviction.

The oracle is the provider instance itself: it must be released exactly once,
in its bound profile home, without a session end, once the construction is
over. When the construction binds the SAME instance, the new agent holds it and
nothing may be released under it.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 P0 module)
from __future__ import annotations

import threading
import time

import pytest

from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401 (fixtures)
    hc,
    homes,
)
from tests.test_issue8613_provider_instance_holders import (  # noqa: F401
    _build,
    _fresh_instance,
    _shared_agent,
    holder_registry,
    surviving_save,
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


class _PausedBuild:
    """A construction of the same profile, held in flight (it holds the
    acquisition reservation but has not acquired anything yet) until resumed."""

    def __init__(self, home, provider_for_build, session_id):
        self.started = threading.Event()
        self.proceed = threading.Event()
        self.result: dict = {}

        def pause_before_memory():
            self.started.set()
            assert self.proceed.wait(30), "the construction was never resumed"

        def build():
            self.result["agent"] = _build(home, provider_for_build, session_id,
                                          before_init=pause_before_memory)

        self.thread = threading.Thread(target=build, name=f"8817-{session_id}", daemon=True)
        self.thread.start()
        assert self.started.wait(30), "the construction never started"

    def finish(self):
        self.proceed.set()
        self.thread.join(30)
        assert not self.thread.is_alive(), "the construction never finished"
        return self.result.get("agent")


def _assert_released_in_its_home(provider, home):
    _released_exactly_once(provider)
    assert provider.session_ends == 0, "an unknown owner got a session end"
    assert provider.shutdown_homes == [str(home)], provider.shutdown_homes


def test_a_release_that_must_not_wait_runs_once_the_construction_finishes(
    hc, homes, holder_registry
):
    """The lock-holding callers (credential self-heal, compression migration)
    refuse at once; the release must still run when the construction is over."""
    provider = RecordingProvider()
    agent = _shared_agent(homes["alpha"], provider, "8817-nowait")
    build = _PausedBuild(homes["alpha"], _fresh_instance, "8817-nowait-builder")

    with scope.release_without_fence_wait():
        assert streaming._close_evicted_agent_at_session_boundary("8817-nowait", agent) is True
    assert provider.shutdowns == 0 and provider.client.is_closed is False, (
        "the release ran while the construction was in flight"
    )

    newcomer = build.finish()
    _assert_released_in_its_home(provider, homes["alpha"])
    # The new agent's own (fresh) instance is untouched.
    assert surviving_save(newcomer, "newcomer's save") is not provider


def test_a_release_that_gave_up_waiting_runs_once_the_construction_finishes(
    hc, homes, holder_registry, monkeypatch
):
    """An ordinary release waits for the construction, bounded; when the bound
    runs out the release must be kept, not dropped."""
    monkeypatch.setattr(scope, "_ACQUISITION_FENCE_WAIT_S", 0.3)
    provider = RecordingProvider()
    agent = _shared_agent(homes["alpha"], provider, "8817-timeout")
    build = _PausedBuild(homes["alpha"], _fresh_instance, "8817-timeout-builder")

    began = time.monotonic()
    assert streaming._close_evicted_agent_at_session_boundary("8817-timeout", agent) is True
    assert time.monotonic() - began < 10
    time.sleep(0.5)  # longer than the fence bound
    assert provider.shutdowns == 0 and provider.client.is_closed is False

    build.finish()
    _assert_released_in_its_home(provider, homes["alpha"])


def test_overlapping_constructions_release_once_the_last_one_finishes(
    hc, homes, holder_registry
):
    """Two constructions of the profile are in flight; the release runs only when
    neither is, and exactly once."""
    provider = RecordingProvider()
    agent = _shared_agent(homes["alpha"], provider, "8817-overlap")
    first = _PausedBuild(homes["alpha"], _fresh_instance, "8817-overlap-b1")
    second = _PausedBuild(homes["alpha"], _fresh_instance, "8817-overlap-b2")

    with scope.release_without_fence_wait():
        assert streaming._close_evicted_agent_at_session_boundary("8817-overlap", agent) is True

    first.finish()
    time.sleep(0.5)
    assert provider.shutdowns == 0, "released while a construction was still in flight"
    assert provider.client.is_closed is False

    second.finish()
    _assert_released_in_its_home(provider, homes["alpha"])


def test_a_construction_that_binds_the_same_instance_keeps_it(hc, homes, holder_registry):
    """Control: when the construction gets the SAME instance, the new agent holds
    it, the kept intent is handed over, and nothing is released under it."""
    provider = RecordingProvider()
    agent = _shared_agent(homes["alpha"], provider, "8817-same")
    build = _PausedBuild(homes["alpha"], lambda: provider, "8817-same-builder")

    with scope.release_without_fence_wait():
        assert streaming._close_evicted_agent_at_session_boundary("8817-same", agent) is True

    newcomer = build.finish()
    time.sleep(0.5)  # a wrongly started retry would release here
    assert provider.shutdowns == 0, "the instance was released under the new agent"
    assert provider.client.is_closed is False
    assert surviving_save(newcomer, "newcomer's save") is provider
    assert not any(
        entry.intent is not None for entry in scope._provider_holders.values()
    ), "a cleanup intent survived the hand-over"


def test_a_deferred_retry_that_meets_a_construction_keeps_waiting(
    hc, homes, holder_registry, monkeypatch
):
    """A release kept for some other reason is retried on its own thread; if that
    retry meets a construction it waits for it, bounded, and must then keep the
    intent rather than drop it."""
    monkeypatch.setattr(scope, "_ACQUISITION_FENCE_WAIT_S", 0.3)
    provider = RecordingProvider()
    agent = _shared_agent(homes["alpha"], provider, "8817-retry")

    intent = scope._ReleaseIntent(
        agent._memory_manager, str(homes["alpha"]),
        scope.profile_home_key(str(homes["alpha"])), "8817-retry",
    )
    build = _PausedBuild(homes["alpha"], _fresh_instance, "8817-retry-builder")
    with scope.release_without_fence_wait():
        ok, why, claimed = scope._claim_last_held_teardown(agent, retain=intent)
    assert ok is False and "acquired by" in why, why

    # The retry runs while the construction is still in flight: it waits out the
    # bound and must keep the intent.
    scope._run_release_intent(intent)
    assert provider.shutdowns == 0
    assert _wait_for(lambda: getattr(intent, "fence_homes", None) is not None, 5), (
        "the retry dropped the intent"
    )

    build.finish()
    _assert_released_in_its_home(provider, homes["alpha"])
