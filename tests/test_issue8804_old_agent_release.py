"""#8804 (#8613 item 2, the old-Agent remainder): an Agent from before 2026-10-19 has
no ``MemoryManager.release_soft``/``claim_teardown`` and, before 2026-10-17, no
``_bound_scope``. When a cached agent whose session owner cannot be resolved (its
session file was removed outside the WebUI) is evicted, v0.52.544 stopped its external
provider's writer on every Agent. exp-v0.52.627 stopped it only on current Agents; on
v0.22.2 and older the writer kept running until the WebUI restarted.

On such an Agent the release now runs the manager's ``shutdown_all`` (the teardown
``release_soft`` ends with) in the profile home the WebUI built the agent under, after
the same last-holder proof and drain, and never ``on_session_end``. The oracles are the
provider's own counters, the home each shutdown ran under, and the queued save landing.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 / #8613 modules)
from __future__ import annotations

import logging
import threading

import pytest

from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401 (fixtures)
    _key,
    hc,
    homes,
)
from tests.test_issue8613_provider_instance_holders import (  # noqa: F401
    _pause_saves,
    holder_registry,
)
from tests.test_issue8613_post_drain_release import (  # noqa: F401
    RecordingProvider,
    _released_exactly_once,
)

import api.memory_profile_scope as scope
import api.streaming as streaming

mm_mod = pytest.importorskip("agent.memory_manager")


_LEGACY_HIDDEN = frozenset({"release_soft", "claim_teardown", "_bound_scope", "_home_key", "_teardown_claim"})


class LegacyManager:
    """The MemoryManager surface an Agent from before 2026-10-17 has (v0.22.2,
    v2026.9.24): no soft release, no teardown claim, and no report of the home it
    bound. Every call goes to a REAL MemoryManager underneath (``add_provider``,
    ``initialize_all``, ``sync_all``, ``flush_pending``, ``on_session_end``,
    ``shutdown_all`` and its background save executor), so only the surface the WebUI
    can see is the old one; the real manager keeps its own internal state."""

    def __init__(self):
        object.__setattr__(self, "_inner", mm_mod.MemoryManager())

    def __getattr__(self, name):
        if name in _LEGACY_HIDDEN:
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_inner"), name)


class CanaryManager(LegacyManager):
    """The 2026-10-17/18 canary shape: it reports the home it bound (``_bound_scope``)
    but has no soft release or teardown claim yet."""

    _hidden = _LEGACY_HIDDEN - {"_bound_scope"}

    def __getattr__(self, name):
        if name in self._hidden:
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_inner"), name)


def make_legacy_agent_cls(provider_for_build, *, home, manager_cls=None):
    """Stand-in AIAgent of that era. ``shutdown_memory_provider`` is v0.22.2's
    ``run_agent.AIAgent.shutdown_memory_provider``: no teardown claim, so it runs
    ``on_session_end`` and ``shutdown_all`` whenever its flag is unset."""

    class LegacyAgent:
        def __init__(self, session_id=None, skip_memory=False, **kwargs):
            self.session_id = session_id
            self._memory_manager = None
            self._memory_provider_shutdown = False
            self._session_messages = []
            if not skip_memory:
                manager = (manager_cls or LegacyManager)()
                manager.add_provider(provider_for_build())
                manager.initialize_all(session_id=session_id, hermes_home=str(home))
                self._memory_manager = manager

        def shutdown_memory_provider(self, messages=None):
            if self._memory_provider_shutdown:
                return
            self._memory_provider_shutdown = True
            self.shutdown_bodies_run = getattr(self, "shutdown_bodies_run", 0) + 1
            if self._memory_manager:
                try:
                    self._memory_manager.on_session_end(messages or [])
                except Exception:  # pragma: no cover - the real Agent logs and continues
                    pass
                self._memory_manager.shutdown_all()

    return LegacyAgent


def _legacy_agent(home, provider, session_id, manager_cls=None):
    """One old-Agent build through the production constructor, so the holder table
    and the recorded build home are populated the way a real turn populates them."""
    agent, _cacheable = scope.construct_profile_scoped_agent(
        make_legacy_agent_cls(lambda: provider, home=home, manager_cls=manager_cls),
        {"model": "m"},
        profile_home=str(home),
        session_id=session_id,
    )
    manager = agent._memory_manager
    assert manager is not None
    assert not callable(getattr(manager, "release_soft", None)), "the stand-in must have the pre-2026-10-19 shape"
    assert not callable(getattr(manager, "claim_teardown", None))
    if manager_cls is None:
        assert getattr(manager, "_bound_scope", None) is None
    return agent


def test_an_old_agents_sole_unowned_eviction_stops_its_provider(hc, homes, holder_registry):
    provider = RecordingProvider()
    agent = _legacy_agent(homes["alpha"], provider, "8804-sole")

    assert streaming._close_evicted_agent_at_session_boundary("8804-sole", agent) is True

    assert provider.shutdowns == 1, "the old Agent's provider writer leaked past the eviction"
    assert provider.client.is_closed is True
    assert provider.session_ends == 0, "an unknown owner got a session end"
    assert provider.shutdown_homes == [str(homes["alpha"])], provider.shutdown_homes
    assert agent._memory_provider_shutdown is True


def test_the_old_managers_shutdown_all_runs_exactly_once(hc, homes, holder_registry):
    """The release runs the real manager's ``shutdown_all`` once, and nothing after it
    (a later ``close()`` or a second boundary included) runs it again or runs a
    session end."""
    provider = RecordingProvider()
    agent = _legacy_agent(homes["alpha"], provider, "8804-once")
    inner = object.__getattribute__(agent._memory_manager, "_inner")
    calls = {"shutdown_all": 0, "on_session_end": 0}
    real_shutdown_all, real_on_session_end = inner.shutdown_all, inner.on_session_end

    def counted_shutdown_all():
        calls["shutdown_all"] += 1
        real_shutdown_all()

    def counted_on_session_end(messages):
        calls["on_session_end"] += 1
        real_on_session_end(messages)

    inner.shutdown_all, inner.on_session_end = counted_shutdown_all, counted_on_session_end

    assert streaming._close_evicted_agent_at_session_boundary("8804-once", agent) is True
    agent.shutdown_memory_provider([])
    assert streaming._close_evicted_agent_at_session_boundary("8804-once", agent) is True

    assert calls == {"shutdown_all": 1, "on_session_end": 0}, calls
    assert provider.shutdowns == 1 and provider.session_ends == 0


def test_a_later_close_of_the_released_old_agent_runs_no_session_end(hc, homes, holder_registry):
    """v0.22.2's ``shutdown_memory_provider`` has no teardown claim: if the release did
    not mark the agent, a later ``close()`` would commit under an owner nobody could
    resolve and shut the provider down a second time."""
    provider = RecordingProvider()
    agent = _legacy_agent(homes["alpha"], provider, "8804-close")
    assert streaming._close_evicted_agent_at_session_boundary("8804-close", agent) is True

    agent.shutdown_memory_provider([])

    assert getattr(agent, "shutdown_bodies_run", 0) == 0, (
        "the old Agent's own shutdown ran again after the release: on v0.22.2 it runs "
        "on_session_end under an owner nobody could resolve, then shuts the providers down twice"
    )
    assert provider.session_ends == 0
    assert provider.shutdowns == 1


def test_an_old_agents_provider_shared_with_a_live_agent_keeps_running(hc, homes, holder_registry):
    provider = RecordingProvider()
    first = _legacy_agent(homes["alpha"], provider, "8804-shared-a")
    second = _legacy_agent(homes["alpha"], provider, "8804-shared-b")

    assert streaming._close_evicted_agent_at_session_boundary("8804-shared-a", first) is True
    assert provider.shutdowns == 0, "the provider was shut down under the agent still holding it"
    assert provider.client.is_closed is False
    assert second._memory_provider_shutdown is False

    assert streaming._close_evicted_agent_at_session_boundary("8804-shared-b", second) is True
    assert provider.shutdowns == 1
    assert provider.session_ends == 0


def test_an_old_agents_queued_save_lands_before_its_provider_is_released(hc, homes, holder_registry):
    """The eviction refuses while the agent's own save is queued, keeps the release,
    and runs it on its own once the save has landed: the deferred path drives the old
    manager's ``shutdown_all`` as well."""
    provider = _pause_saves(RecordingProvider())
    agent = _legacy_agent(homes["alpha"], provider, "8804-queued")
    token = hc.set_hermes_home_override(str(homes["alpha"]))
    try:
        agent._memory_manager.sync_all("old agent save", "reply", session_id=agent.session_id)
        assert provider.save_started.wait(30), "the queued save never reached the provider"
    finally:
        hc.reset_hermes_home_override(token)

    with scope.release_without_fence_wait():
        assert streaming._close_evicted_agent_at_session_boundary("8804-queued", agent) is True
    assert provider.shutdowns == 0 and provider.client.is_closed is False

    provider.save_resume.set()
    assert provider.save_landed.wait(30), "the pending save never completed"
    assert provider.writes == ["old agent save"]
    _released_exactly_once(provider)
    assert provider.session_ends == 0
    assert provider.shutdown_homes == [str(homes["alpha"])], provider.shutdown_homes



def test_a_deferred_release_marks_the_departed_agent_so_its_late_close_spares_a_successor(
    hc, homes, holder_registry
):
    """Codex r3: the release deferred on a queued save runs for a ``_ReleaseIntent``.
    If it did not mark the departed agent, that agent's own v0.22.2
    ``shutdown_memory_provider`` (no teardown claim) could later run a session end under
    the unknown owner and a second ``shutdown_all``, which closes the client under a
    successor that shares the instance."""
    provider = _pause_saves(RecordingProvider())
    agent = _legacy_agent(homes["alpha"], provider, "8804-deferred")
    token = hc.set_hermes_home_override(str(homes["alpha"]))
    try:
        agent._memory_manager.sync_all("the departed agent's save", "reply", session_id=agent.session_id)
        assert provider.save_started.wait(30), "the queued save never reached the provider"
    finally:
        hc.reset_hermes_home_override(token)
    with scope.release_without_fence_wait():
        assert streaming._close_evicted_agent_at_session_boundary("8804-deferred", agent) is True
    provider.save_resume.set()
    assert provider.save_landed.wait(30)
    _released_exactly_once(provider)
    assert agent._memory_provider_shutdown is True, "the deferred release did not mark the departed agent"

    successor = _legacy_agent(homes["alpha"], provider, "8804-deferred-next")
    assert provider.client.is_closed is False
    agent.shutdown_memory_provider([])  # the departed agent's own close, later

    assert getattr(agent, "shutdown_bodies_run", 0) == 0
    assert provider.session_ends == 0 and provider.shutdowns == 1
    assert provider.client.is_closed is False, "the late close shut the provider down under its successor"
    provider.remember("the successor's save")
    assert provider.writes[-1] == "the successor's save"
    assert successor._memory_provider_shutdown is False

def test_an_old_agent_without_a_recorded_build_home_is_left_alone(hc, homes, holder_registry, caplog):
    provider = RecordingProvider()
    agent = _legacy_agent(homes["alpha"], provider, "8804-nohome")
    delattr(agent, scope._BUILD_HOME_ATTR)
    caplog.set_level(logging.WARNING)

    assert scope.release_unowned_memory_provider(agent, session_id="8804-nohome") is False
    assert provider.shutdowns == 0
    assert any("does not report the profile home" in r.getMessage() for r in caplog.records)


def test_an_old_manager_already_shutting_down_is_not_reported_released(hc, homes, holder_registry):
    provider = RecordingProvider()
    agent = _legacy_agent(homes["alpha"], provider, "8804-shutting")
    # On the REAL manager underneath, as v0.22.2's own shutdown_all leaves it.
    object.__getattribute__(agent._memory_manager, "_inner")._shutting_down = True

    assert scope.release_unowned_memory_provider(agent, session_id="8804-shutting") is False
    assert provider.shutdowns == 0


def test_an_owned_old_agent_session_keeps_its_session_end(hc, homes, holder_registry, monkeypatch):
    """A resolvable owner takes the ordinary path on an old Agent too: the release is
    not even attempted, and the boundary runs the Agent's own session end."""
    provider = RecordingProvider()
    agent = _legacy_agent(homes["alpha"], provider, "8804-owned")
    monkeypatch.setattr(scope, "session_owner_profile_home", lambda sid: str(homes["alpha"]))

    assert scope.release_unowned_memory_provider(agent, session_id="8804-owned") is False
    assert provider.shutdowns == 0
    assert streaming._close_evicted_agent_at_session_boundary("8804-owned", agent) is True
    assert provider.session_ends == 1 and provider.shutdowns == 1


def test_the_old_agent_release_runs_its_teardown_outside_the_holder_lock(hc, homes, holder_registry):
    """The release takes the holder lock for the proof and claim only; the teardown
    itself runs outside it (the module never holds it across provider I/O). A build
    that would attach to the instance meanwhile is held off by the reservation
    instead (``tests/test_issue8804_unfenced_teardown.py``).

    ``_provider_holders_lock`` is an RLock, so the check must come from ANOTHER thread
    than the one running the teardown: the teardown is paused at the provider's
    ``shutdown()`` and the test thread tries the lock while it waits."""
    provider = RecordingProvider()
    agent = _legacy_agent(homes["alpha"], provider, "8804-lock")
    in_teardown, finish = threading.Event(), threading.Event()
    real_shutdown = provider.shutdown

    def paused_shutdown():
        in_teardown.set()
        assert finish.wait(30), "the test never let the teardown finish"
        real_shutdown()

    provider.shutdown = paused_shutdown
    t = threading.Thread(target=streaming._close_evicted_agent_at_session_boundary, args=("8804-lock", agent))
    t.start()
    try:
        assert in_teardown.wait(30), "the teardown never reached the provider"
        got = scope._provider_holders_lock.acquire(timeout=2)
        if got:
            scope._provider_holders_lock.release()
    finally:
        finish.set()
        t.join(30)
    assert not t.is_alive()
    assert got is True, "the provider teardown ran with the holder lock held"
    assert provider.shutdowns == 1


def test_a_canary_agent_with_a_bound_scope_but_no_soft_release_is_released_in_its_bound_home(
    hc, homes, holder_registry
):
    """Agents from 2026-10-17/18 report ``_bound_scope`` but have no ``release_soft``: the
    legacy teardown runs in the home the manager reports, the build home unused."""
    provider = RecordingProvider()
    agent = _legacy_agent(homes["alpha"], provider, "8804-canary", manager_cls=CanaryManager)
    assert (agent._memory_manager._bound_scope or {}).get("hermes_home") == str(homes["alpha"])
    setattr(agent, scope._BUILD_HOME_ATTR, str(homes["beta"]))  # must NOT be used when the manager reports one

    assert streaming._close_evicted_agent_at_session_boundary("8804-canary", agent) is True
    assert provider.shutdowns == 1 and provider.session_ends == 0
    assert provider.shutdown_homes == [str(homes["alpha"])], provider.shutdown_homes
