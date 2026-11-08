"""#8804, Agent-free (runs in the agent-free CI): an evicted agent of a Hermes Agent from before
2026-10-17 (no ``claim_instance``, no ``MemoryManager.release_soft``/``claim_teardown``) whose session owner
cannot be resolved.

Such an Agent keeps an external provider only while the WebUI serves ONE profile (#8585), and its provider
plugins commonly hand every agent of the profile the SAME instance. The release runs the manager's
``shutdown_all`` (the Agent has nothing softer). The Agent cannot refuse that instance to a new agent while the
teardown runs, so the WebUI must, or a same-profile build that re-initialises the instance mid-teardown loses it
when the teardown closes it (the #8651 crux, now without the Agent's help).

The stand-ins below have exactly the surface the WebUI reads from that era's ``AIAgent`` and ``MemoryManager``;
``tests/test_issue8804_real_agent_managers.py`` repeats the core cases with the real tagged managers.
"""
from __future__ import annotations

import sys
import threading
import time
import types
import weakref

import pytest

import api.memory_profile_scope as scope
import api.profiles as profiles_api
import api.streaming as streaming
from tests.test_issue8517_profile_scoped_memory import hc, homes  # noqa: F401 (fixtures)


class _Client:
    def __init__(self):
        self.closed = False


class SharedProvider:
    """A provider whose plugin hands every agent of the profile the SAME instance: one
    client, closed by ``shutdown()`` and reopened by ``initialize()``. ``events`` is the
    order of initialisations and shutdowns, which is what the race is about."""

    name = "shared-ext"

    def __init__(self):
        self.client = _Client()
        self.inits: list[str] = []
        self.events: list[str] = []
        self.shutdowns = 0
        self.session_ends = 0
        self.writes: list[str] = []

    def initialize(self, session_id, **kwargs):
        self.inits.append(session_id)
        self.events.append(f"init:{session_id}")
        if self.client.closed:
            self.client = _Client()

    def shutdown(self):
        self.shutdowns += 1
        self.events.append("shutdown")
        self.client.closed = True

    def on_session_end(self, messages):
        self.session_ends += 1

    def remember(self, marker):
        if self.client.closed:
            raise RuntimeError("memory provider client is closed: the save failed")
        self.writes.append(marker)


class LegacyManager:
    """``agent.memory_manager.MemoryManager`` of an Agent before 2026-10-17 (v0.22.2) as
    far as the WebUI reads it: no ``release_soft``, ``claim_teardown``, ``_bound_scope``
    or ``_home_key``."""

    def __init__(self):
        self.providers: list = []
        self._background_futures: dict = {}
        self._shutting_down = False
        self.shutdown_all_calls = 0
        self.session_end_calls = 0

    def add_provider(self, provider):
        self.providers.append(provider)

    def initialize_all(self, session_id, **kwargs):
        for provider in self.providers:
            provider.initialize(session_id, **kwargs)

    def flush_pending(self, timeout=None):
        return True

    def on_session_end(self, messages):
        self.session_end_calls += 1
        for provider in self.providers:
            provider.on_session_end(messages)

    def shutdown_all(self):
        self.shutdown_all_calls += 1
        self._shutting_down = True
        for provider in self.providers:
            provider.shutdown()


def make_old_agent_cls(provider):
    class OldAgent:
        def __init__(self, session_id=None, skip_memory=False, **kwargs):
            self.session_id = session_id
            self._memory_manager = None
            self._memory_provider_shutdown = False
            self._session_messages = []
            if not skip_memory:
                manager = LegacyManager()
                manager.add_provider(provider)
                home = str(sys.modules["hermes_constants"].get_hermes_home())
                manager.initialize_all(session_id, hermes_home=home)
                self._memory_manager = manager

        def shutdown_memory_provider(self, messages=None):
            # v0.22.2 ``run_agent.AIAgent.shutdown_memory_provider``: no teardown claim.
            if self._memory_provider_shutdown:
                return
            self._memory_provider_shutdown = True
            if self._memory_manager:
                try:
                    self._memory_manager.on_session_end(messages or [])
                except Exception:  # pragma: no cover - the Agent logs and continues
                    pass
                self._memory_manager.shutdown_all()

    return OldAgent


@pytest.fixture
def old_agent_root(hc, homes, monkeypatch):  # noqa: F811
    """A single-profile install (the root profile only) on an Agent without
    ``claim_instance``: the one shape in which such an Agent keeps an external
    provider. Fresh WebUI holder and ownership tables."""
    monkeypatch.setitem(sys.modules, "agent.memory_manager", types.ModuleType("agent.memory_manager"))
    for name in ("alpha", "beta"):
        homes[name].rmdir()
    (homes["default"] / "profiles").rmdir()
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda name: homes["default"])
    monkeypatch.setattr(profiles_api, "get_profile_runtime_env", lambda home: {}, raising=False)
    monkeypatch.setattr(profiles_api, "filter_runtime_env_for_gateway_parity", lambda env: {}, raising=False)
    monkeypatch.setattr(scope, "_logged_once", set())
    monkeypatch.setattr(scope, "_profile_homes_seen", set())
    monkeypatch.setattr(scope, "_provider_holders", {})
    monkeypatch.setattr(scope, "_inflight_homes", {})
    if hasattr(scope, "_instance_owners"):
        monkeypatch.setattr(scope, "_instance_owners", {})
    assert scope._claim_seam_available() is False
    return homes["default"]


def _build(root, provider, session_id):
    return scope.construct_profile_scoped_agent(
        make_old_agent_cls(provider), {"session_id": session_id},
        profile_home=str(root), session_id=session_id,
    )


def _evict(agent, session_id):
    return streaming._close_evicted_agent_at_session_boundary(session_id, agent)


def _pause_shutdown(provider):
    in_teardown, finish = threading.Event(), threading.Event()
    real = provider.shutdown

    def paused():
        in_teardown.set()
        assert finish.wait(30), "the test never let the teardown finish"
        real()

    provider.shutdown = paused
    return in_teardown, finish


def test_an_old_agents_sole_release_runs_shutdown_all_once_and_no_session_end(old_agent_root):
    provider = SharedProvider()
    agent, cacheable = _build(old_agent_root, provider, "8804-sole")
    assert cacheable is True and agent._memory_manager is not None
    manager = agent._memory_manager

    assert _evict(agent, "8804-sole") is True

    assert manager.shutdown_all_calls == 1
    assert provider.shutdowns == 1 and provider.client.closed is True, "the writer leaked past the eviction"
    assert manager.session_end_calls == 0 and provider.session_ends == 0, "an unknown owner got a session end"
    assert agent._memory_provider_shutdown is True
    agent.shutdown_memory_provider([])
    assert manager.shutdown_all_calls == 1 and manager.session_end_calls == 0


def test_an_old_agents_shared_instance_keeps_running_for_the_live_agent(old_agent_root):
    provider = SharedProvider()
    first, _ = _build(old_agent_root, provider, "8804-shared-a")
    second, _ = _build(old_agent_root, provider, "8804-shared-b")

    assert _evict(first, "8804-shared-a") is True
    assert provider.shutdowns == 0 and provider.client.closed is False, (
        "v0.52.544 shut the provider down under the agent still holding it; it must keep running"
    )
    assert first._memory_manager.shutdown_all_calls == 0 and provider.session_ends == 0
    provider.remember("live agent's save")

    assert _evict(second, "8804-shared-b") is True
    assert provider.shutdowns == 1 and provider.session_ends == 0



class FenceProbe:
    """Counts the fence's teardown checks made from one thread, so a test knows the
    build is INSIDE the wait rather than merely alive. ``_reserve_acquisition`` checks
    twice (the ``if``, then the ``while``) before its first wait and once after every
    wake-up, all under the holder lock, which the build only lets go of in ``wait()``.
    Shared with ``test_issue8804_real_agent_managers``."""

    def __init__(self, thread):
        self.thread = thread
        self.checks = 0
        self._seen = threading.Condition()
        self._real = scope._teardown_running_locked

    def __enter__(self):
        scope._teardown_running_locked = self._check
        return self

    def __exit__(self, *exc):
        scope._teardown_running_locked = self._real
        return False

    def _check(self, home_key):
        running = self._real(home_key)
        if threading.current_thread() is self.thread:
            with self._seen:
                self.checks += 1
                self._seen.notify_all()
        return running

    def reached(self, checks, timeout=30.0):
        with self._seen:
            return self._seen.wait_for(lambda: self.checks >= checks, timeout)

    def stray_wake_inside_the_wait(self):
        """Send a wake-up meant for something else (another teardown's settlement, a
        construction of another profile ending) while the build is waiting."""
        assert self.reached(2), "the build never reached the fence's wait"
        # The build holds the holder lock from its second check until wait() lets go of
        # it, so taking the Condition here proves the build is inside the wait.
        with scope._provider_holders_cond:
            scope._provider_holders_cond.notify_all()
        assert self.reached(3, 5.0), "a stray wake-up let the build through without re-checking the teardown"

def test_a_build_during_an_old_agents_teardown_waits_and_gets_a_live_provider(old_agent_root):
    """The #8651 crux without ``claim_teardown``: a same-profile agent attaching while
    the evicted agent's ``shutdown_all`` runs must not have the instance closed under it."""
    provider = SharedProvider()
    old, _ = _build(old_agent_root, provider, "8804-race-old")
    in_teardown, finish = _pause_shutdown(provider)
    evicting = threading.Thread(target=_evict, args=(old, "8804-race-old"))
    evicting.start()
    built: dict = {}

    def build_new():
        built["agent"], built["cacheable"] = _build(old_agent_root, provider, "8804-race-new")

    attaching = threading.Thread(target=build_new)
    try:
        assert in_teardown.wait(30), "the teardown never reached the provider"
        with FenceProbe(attaching) as fence:
            attaching.start()
            attaching.join(0.5)
            assert attaching.is_alive(), "the build did not wait for the teardown of the instance it shares"
            fence.stray_wake_inside_the_wait()
            assert attaching.is_alive(), "a stray wake-up let the build through while the teardown still runs"
        assert provider.inits == ["8804-race-old"], "the build re-initialised the instance mid-teardown"
    finally:
        finish.set()
        evicting.join(30)
    assert not evicting.is_alive()
    # The teardown's settlement wakes the waiting build: it must not sit out its whole
    # bounded wait (30 s) before it notices the instance is free.
    attaching.join(5)
    assert not attaching.is_alive(), "the build was not woken when the teardown settled"

    assert provider.events == ["init:8804-race-old", "shutdown", "init:8804-race-new"]
    assert built["cacheable"] is True and built["agent"]._memory_manager is not None
    assert provider.client.closed is False
    provider.remember("new agent's save")
    ok, why = scope.agent_is_last_provider_holder(built["agent"])
    assert ok, why


def test_a_build_that_outwaits_the_teardown_runs_without_memory_and_is_not_cached(old_agent_root, monkeypatch):
    monkeypatch.setattr(scope, "_UNFENCED_TEARDOWN_WAIT_S", 0.3)
    provider = SharedProvider()
    old, _ = _build(old_agent_root, provider, "8804-slow-old")
    in_teardown, finish = _pause_shutdown(provider)
    evicting = threading.Thread(target=_evict, args=(old, "8804-slow-old"))
    evicting.start()
    try:
        assert in_teardown.wait(30)
        started = time.monotonic()
        agent, cacheable = _build(old_agent_root, provider, "8804-slow-new")
        waited = time.monotonic() - started
    finally:
        finish.set()
        evicting.join(30)

    assert 0.25 <= waited < 10, waited
    assert cacheable is False and agent._memory_manager is None
    assert provider.inits == ["8804-slow-old"], "the timed-out build still initialised the closing instance"
    assert provider.shutdowns == 1 and provider.client.closed is True
    assert scope._inflight_homes == {}, "the timed-out build left a reservation behind"


class _Instance:
    name = "x"


def _flag_teardown(home_key: str):
    """A provider instance of ``home_key`` whose teardown is running."""
    instance = _Instance()
    entry = scope._InstanceHolders(weakref.ref(instance), home_key=home_key)
    entry.teardown = True
    scope._provider_holders[id(instance)] = entry
    return instance, entry


def test_an_agent_with_the_claim_seam_never_waits_to_reserve(old_agent_root, monkeypatch):
    """Current Agents (``claim_instance``) refuse the instance themselves, so their
    reservation is exactly what it was: immediate, even while a teardown runs."""
    monkeypatch.setattr(scope, "_claim_seam_available", lambda: True)
    key = scope.profile_home_key(str(old_agent_root)) or ""
    assert key
    _instance, _entry = _flag_teardown(key)
    started = time.monotonic()
    assert scope._reserve_acquisition(key) is True
    assert time.monotonic() - started < 0.5
    assert scope._inflight_homes == {key: 1}
    scope._end_acquisition(key)


def test_without_the_seam_only_a_teardown_this_profile_may_share_holds_a_build(old_agent_root, monkeypatch):
    monkeypatch.setattr(scope, "_UNFENCED_TEARDOWN_WAIT_S", 0.2)
    key = scope.profile_home_key(str(old_agent_root)) or ""
    assert key
    _instance, entry = _flag_teardown("another-profile-home")
    assert scope._reserve_acquisition(key) is True
    scope._end_acquisition(key)

    entry.home_key = ""  # an instance of unknown profile could be this one's
    assert scope._reserve_acquisition(key) is False
    assert scope._inflight_homes == {}
    entry.home_key = key
    assert scope._reserve_acquisition(key) is False
    entry.teardown = False
    assert scope._reserve_acquisition(key) is True
    scope._end_acquisition(key)
