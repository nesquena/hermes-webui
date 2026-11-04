"""#8804 follow-up: an unowned eviction releases the context engine's background work, never its session end.

When ``call_as_session_owner`` refuses an evicted agent's ``shutdown_memory_provider`` (its session owner cannot
be resolved), the Agent's context-engine session end inside that shutdown is skipped too. That end is a plugin
extension point (``agent/context_engine.py``) that may flush session data, so it stays behind the owner check
(fail closed; Greptile P1 on #8806 at be7878985). What the built-in compressor's end does beyond resetting the
in-memory state of an engine being discarded is ``prepared_compaction.discard`` (Agent v0.22.6+): it cancels a
background summary pass that would otherwise run to completion unused. That close-only call runs, through the
Agent's own ``PreparedCompaction.discard``, in the profile home the agent was built under.

The ``prepared`` fixture uses the installed Agent's real ``PreparedCompaction`` and a real running ``_PendingPass``.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 / #8613 / #8804 modules)
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests.test_issue8517_profile_scoped_memory import hc, homes  # noqa: F401 (fixtures)
from tests.test_issue8613_provider_instance_holders import (  # noqa: F401
    SharedInstanceProvider,
    _shared_agent,
    holder_registry,
)
from tests.test_issue8613_post_drain_release import RecordingProvider
from tests.test_issue8804_old_agent_release import _legacy_agent

import api.memory_profile_scope as scope
import api.streaming as streaming

pytest.importorskip("agent.memory_manager")

OWNER_SKIP_LOG = "stays behind the owner check"


@pytest.fixture
def prepared():
    """The installed Agent's ``agent.prepared_compaction`` module (v0.22.6+)."""
    return pytest.importorskip("agent.prepared_compaction")


@pytest.fixture
def builtin_engine():
    """A built-in ``ContextCompressor`` shell (its class, so its ``on_session_end`` is the
    Agent's own; not initialised, because no test may run that end on an unowned eviction)."""
    compressor = pytest.importorskip("agent.context_compressor")
    return lambda prepared_compaction: _shell(compressor.ContextCompressor, prepared_compaction)


def _shell(cls, prepared_compaction):
    engine = object.__new__(cls)
    engine.prepared_compaction = prepared_compaction
    return engine


class PluginEngine:
    """A plugin context engine whose ``on_session_end`` may write session data: it records
    every call, which must never happen while the session's owner is unknown."""

    def __init__(self, prepared_compaction=None):
        self.ends: list = []
        if prepared_compaction is not None:
            self.prepared_compaction = prepared_compaction

    def on_session_end(self, session_id, messages):
        self.ends.append(session_id)


def _running_pass(prepared, session_id, *, fail=False, cls=None):
    """A real ``PreparedCompaction`` (or ``cls``) with a real summary pass still running.
    The pass records the context-local profile home it is cancelled under."""
    state = (cls or prepared.PreparedCompaction)()
    pending = prepared._PendingPass(prepared._Window(session_id, 0, 0, ()))
    cancelled_in: list = []
    cancel = pending.cancel

    def recording_cancel(reason):
        import hermes_constants  # noqa: PLC0415 - the hc fixture installs it

        cancelled_in.append(str(hermes_constants.get_hermes_home()))
        if fail:
            raise RuntimeError("cancel failed")
        cancel(reason)

    pending.cancel = recording_cancel
    state._pending = pending
    return state, pending, cancelled_in


def _with_engine(agent, engine):
    """Attach ``engine`` and make the agent's own ``shutdown_memory_provider`` end it the
    way every Agent release does (flag, memory end, then the engine's session end), so a
    test that a later close runs no session end is not vacuous."""
    agent.context_compressor = engine
    agent._session_messages = [{"role": "user", "content": "x"}]
    own_shutdown = agent.shutdown_memory_provider

    def agents_shutdown(messages=None):
        if agent._memory_provider_shutdown:
            return
        own_shutdown(messages)
        agent._memory_provider_shutdown = True
        engine.on_session_end(agent.session_id or "", messages or [])

    agent.shutdown_memory_provider = agents_shutdown
    return agent


@pytest.fixture(autouse=True)
def _fresh_once_only_logs(monkeypatch):
    monkeypatch.setattr(scope, "_logged_once", set())


PLUGIN_RAN = "plugin-ran.txt"


def _plugin_code_ran(calls, label):
    """What a plugin hook does when it runs: it records the profile home it ran under and
    writes a file there, the write the owner check exists to stop."""
    import hermes_constants  # noqa: PLC0415 - the hc fixture installs it

    home = hermes_constants.get_hermes_home()
    calls.append((label, str(home)))
    (Path(home) / PLUGIN_RAN).write_text(label)


def _no_plugin_file_under_any_profile(homes):
    written = [str(f) for home in homes.values() for f in Path(home).rglob(PLUGIN_RAN)]
    assert written == [], f"plugin code wrote under a profile: {written}"


def test_an_unowned_eviction_cancels_the_running_summary_pass_in_the_agents_own_home(
    hc, homes, holder_registry, prepared, builtin_engine, caplog
):
    state, pending, cancelled_in = _running_pass(prepared, "8804-ce-current")
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-current"),
                         builtin_engine(state))
    caplog.set_level(logging.WARNING)

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-current", agent) is True

    assert pending.cancelled.is_set() and pending.cancel_reason == "agent_released"  # the Agent's own release code
    assert state._pending is None
    assert cancelled_in == [str(homes["alpha"])], (
        "the pass must be cancelled in the profile home the agent was built under "
        "(the ambient home is the default profile here)"
    )
    assert agent._memory_provider_shutdown is True
    assert not any(OWNER_SKIP_LOG in r.getMessage() for r in caplog.records), "the built-in engine is not a plugin"
    agent.shutdown_memory_provider([])  # a later close: no session end, nothing cancelled again
    assert cancelled_in == [str(homes["alpha"])]


def test_a_plugin_engines_session_end_is_never_run_on_an_unowned_eviction(hc, homes, holder_registry, caplog):
    """Greptile P1: a plugin's ``on_session_end`` may flush session data, and the owner
    check refused, so it is not run, now or by a later close."""
    engine = PluginEngine()
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-plugin"), engine)
    caplog.set_level(logging.WARNING)

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-plugin", agent) is True

    assert engine.ends == []
    assert agent._memory_provider_shutdown is True
    agent.shutdown_memory_provider([])
    assert engine.ends == [], "a later close ran the plugin's session end under the unresolved owner"
    assert any(OWNER_SKIP_LOG in r.getMessage() for r in caplog.records)


def test_a_plugin_engine_carrying_the_agents_prepared_compaction_is_left_alone(
    hc, homes, holder_registry, prepared
):
    """Everything a plugin engine carries is the plugin's to replace, so nothing it holds
    is called on this path, not even an object of the Agent's own class."""
    state, pending, cancelled_in = _running_pass(prepared, "8804-ce-plugin-pc")
    engine = PluginEngine(state)
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-plugin-pc"), engine)

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-plugin-pc", agent) is True

    assert cancelled_in == [] and not pending.cancelled.is_set()
    assert engine.ends == []
    assert agent._memory_provider_shutdown is True


def test_a_subclass_of_the_built_in_compressor_is_left_alone(hc, homes, holder_registry, prepared):
    """A plugin engine subclassing the built-in compressor is still a plugin's engine."""
    compressor = pytest.importorskip("agent.context_compressor")

    class PluginsCompressor(compressor.ContextCompressor):
        pass

    state, pending, cancelled_in = _running_pass(prepared, "8804-ce-subclass-engine")
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-subclass-engine"),
                         _shell(PluginsCompressor, state))

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-subclass-engine", agent) is True
    assert cancelled_in == [] and not pending.cancelled.is_set()
    assert agent._memory_provider_shutdown is True


def test_a_plugins_own_discard_is_not_called(hc, homes, holder_registry, prepared, builtin_engine):
    """Only the Agent's own close-only ``discard`` runs; a plugin's override of it is a
    plugin hook like any other."""
    calls: list = []

    class PluginsPreparedCompaction(prepared.PreparedCompaction):
        def discard(self, reason, code="session_boundary"):
            calls.append(reason)

    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-override"),
                         builtin_engine(PluginsPreparedCompaction()))

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-override", agent) is True
    assert calls == []
    assert agent._memory_provider_shutdown is True


def test_a_subclass_overriding_what_discard_calls_is_not_run(hc, homes, holder_registry, prepared, builtin_engine):
    """Codex r2: a subclass can inherit ``discard`` unchanged and override a method it
    calls (``_count_unspliced``); that override must not run on this path."""
    calls: list = []

    class Sneaky(prepared.PreparedCompaction):
        def _count_unspliced(self):
            calls.append("_count_unspliced")

    state, pending, cancelled_in = _running_pass(prepared, "8804-ce-sneaky", cls=Sneaky)
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-sneaky"),
                         builtin_engine(state))

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-sneaky", agent) is True
    assert calls == [] and cancelled_in == [] and not pending.cancelled.is_set()
    assert agent._memory_provider_shutdown is True


@pytest.mark.parametrize("shadowed", ["discard", "_count_unspliced"])
def test_an_instance_shadowing_discard_or_what_it_calls_is_not_run(
    hc, homes, holder_registry, prepared, builtin_engine, shadowed
):
    """Codex r2: an exact ``PreparedCompaction`` whose ``discard`` (or a method it calls)
    is replaced on the instance runs someone else's code; it must be left alone."""
    calls: list = []
    state, pending, cancelled_in = _running_pass(prepared, f"8804-ce-shadow-{shadowed}")
    setattr(state, shadowed, lambda *args, **kwargs: calls.append(shadowed))
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), f"8804-ce-shadow-{shadowed}"),
                         builtin_engine(state))

    assert streaming._close_evicted_agent_at_session_boundary(f"8804-ce-shadow-{shadowed}", agent) is True
    assert calls == [] and cancelled_in == [] and not pending.cancelled.is_set()
    assert agent._memory_provider_shutdown is True


def test_an_old_agents_pass_is_cancelled_although_its_release_marked_it_shut_down(
    hc, homes, holder_registry, prepared, builtin_engine
):
    """#8806 marks an old Agent's released agent shut down inside the release; whether
    the engine's release is due must be decided before that, or it never runs."""
    state, pending, cancelled_in = _running_pass(prepared, "8804-ce-old")
    provider = RecordingProvider()
    agent = _with_engine(_legacy_agent(homes["alpha"], provider, "8804-ce-old"), builtin_engine(state))

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-old", agent) is True

    assert pending.cancelled.is_set() and cancelled_in == [str(homes["alpha"])]
    assert provider.shutdowns == 1 and provider.session_ends == 0
    agent.shutdown_memory_provider([])
    assert getattr(agent, "shutdown_bodies_run", 0) == 0


def test_the_pass_is_cancelled_while_a_live_agent_keeps_the_shared_provider(
    hc, homes, holder_registry, prepared, builtin_engine
):
    """The engine belongs to the evicted agent alone, so its pass is cancelled even while
    the shared memory provider keeps running for the other agent (kept alive here, and its
    next save checked, so the case cannot pass because the survivor was collected)."""
    state, pending, _ = _running_pass(prepared, "8804-ce-shared-a")
    provider = SharedInstanceProvider()
    first = _with_engine(_shared_agent(homes["alpha"], provider, "8804-ce-shared-a"), builtin_engine(state))
    survivor = _shared_agent(homes["alpha"], provider, "8804-ce-shared-b")

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-shared-a", first) is True

    assert pending.cancelled.is_set()
    assert provider.shutdowns == 0 and provider.client.is_closed is False
    provider.remember("the survivor's save")
    assert provider.writes[-1] == "the survivor's save"
    assert survivor._memory_provider_shutdown is False


def test_a_cancel_that_raises_does_not_break_the_eviction(hc, homes, holder_registry, prepared, builtin_engine):
    state, _, cancelled_in = _running_pass(prepared, "8804-ce-fail", fail=True)
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-fail"), builtin_engine(state))

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-fail", agent) is True
    assert cancelled_in == [str(homes["alpha"])]
    assert agent._memory_provider_shutdown is True


def test_an_owned_eviction_leaves_the_engine_to_the_agents_own_shutdown(hc, homes, holder_registry, monkeypatch):
    """With a resolvable owner the Agent's own ``shutdown_memory_provider`` runs and ends
    the engine itself, once; the WebUI adds nothing."""
    engine = PluginEngine()
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-owned"), engine)
    monkeypatch.setattr(scope, "session_owner_profile_home", lambda sid: str(homes["alpha"]))

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-owned", agent) is True
    assert len(engine.ends) == 1, engine.ends  # the Agent's own end, once


def test_a_refusal_at_hook_time_releases_nothing_more(hc, homes, holder_registry, monkeypatch, prepared,
                                                     builtin_engine):
    """A refusal can also happen INSIDE the Agent's own shutdown (at a memory hook): the
    shutdown then ran and handled the engine itself, so the WebUI must not touch it. The
    built-in engine is used, because the WebUI would release it if the release were due."""
    state, pending, _ = _running_pass(prepared, "8804-ce-hook")
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-hook"), builtin_engine(state))
    own_ends: list = []

    def agents_own_shutdown(messages=None):  # the real Agent's shape: flag, memory, then the engine
        agent._memory_provider_shutdown = True
        own_ends.append("engine end")

    agent.shutdown_memory_provider = agents_own_shutdown

    def refuse_at_hook_time(target, fn, **kwargs):
        fn()
        return False

    monkeypatch.setattr(streaming, "call_as_session_owner", refuse_at_hook_time)
    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-hook", agent) is True
    assert own_ends == ["engine end"]  # the Agent's own end, once
    assert not pending.cancelled.is_set(), "the WebUI released an engine the Agent's own shutdown already handled"


def test_an_agent_without_a_recorded_build_home_cancels_nothing(hc, homes, holder_registry, prepared, builtin_engine,
                                                               caplog):
    state, pending, cancelled_in = _running_pass(prepared, "8804-ce-nohome")
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-nohome"),
                         builtin_engine(state))
    delattr(agent, scope._BUILD_HOME_ATTR)
    caplog.set_level(logging.WARNING)

    assert scope.release_unowned_context_engine(agent, session_id="8804-ce-nohome") is False
    assert cancelled_in == [] and not pending.cancelled.is_set()
    assert agent._memory_provider_shutdown is True
    assert any("was not cancelled" in r.getMessage() for r in caplog.records)


def test_an_older_agents_engine_without_prepared_compaction_is_left_alone(hc, homes, holder_registry, builtin_engine,
                                                                         caplog):
    """Before v0.22.6 the built-in engine has no prepared compaction: nothing to cancel,
    and nothing logged (it is not a plugin)."""
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-older"), builtin_engine(None))
    caplog.set_level(logging.WARNING)

    assert scope.release_unowned_context_engine(agent, session_id="8804-ce-older") is False
    assert agent._memory_provider_shutdown is True
    assert not any(OWNER_SKIP_LOG in r.getMessage() for r in caplog.records)


def test_a_descriptor_on_a_plugin_engines_class_never_runs(hc, homes, holder_registry, prepared, caplog):
    """Codex r3: reading ``on_session_end`` from a plugin's engine class runs its
    descriptor. Nothing on the engine's class may be read before the identity check."""
    compressor = pytest.importorskip("agent.context_compressor")
    calls: list = []

    class SessionEndDescriptor:
        def __get__(self, obj, cls):
            _plugin_code_ran(calls, "descriptor")
            return lambda *args: None

    class PluginCompressor(compressor.ContextCompressor):
        on_session_end = SessionEndDescriptor()

    state, pending, cancelled_in = _running_pass(prepared, "8804-ce-descriptor")
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-descriptor"),
                         _shell(PluginCompressor, state))
    caplog.set_level(logging.WARNING)

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-descriptor", agent) is True
    assert calls == [], f"plugin code ran under the unresolved owner: {calls}"
    _no_plugin_file_under_any_profile(homes)
    assert cancelled_in == [] and not pending.cancelled.is_set()
    assert agent._memory_provider_shutdown is True
    skips = [r.getMessage() for r in caplog.records if OWNER_SKIP_LOG in r.getMessage()]
    assert len(skips) == 1 and "PluginCompressor" not in skips[0], skips


def test_a_plugin_engines_metaclass_never_runs(hc, homes, holder_registry, prepared, caplog):
    """Codex r3: ``type(engine).__name__`` (or any read on the class) runs a plugin's
    metaclass ``__getattribute__``."""
    compressor = pytest.importorskip("agent.context_compressor")
    calls: list = []
    armed: list = []

    class EngineMeta(type(compressor.ContextCompressor)):
        def __getattribute__(cls, name):
            if armed:
                _plugin_code_ran(calls, f"metaclass {name}")
            return super().__getattribute__(name)

    class PluginCompressor(compressor.ContextCompressor, metaclass=EngineMeta):
        def on_session_end(self, session_id, messages):
            _plugin_code_ran(calls, "session end")

    state, pending, cancelled_in = _running_pass(prepared, "8804-ce-metaclass")
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-metaclass"),
                         _shell(PluginCompressor, state))
    caplog.set_level(logging.WARNING)
    armed.append(True)
    try:
        assert streaming._close_evicted_agent_at_session_boundary("8804-ce-metaclass", agent) is True
    finally:
        armed.clear()
    assert calls == [], f"plugin code ran under the unresolved owner: {calls}"
    _no_plugin_file_under_any_profile(homes)
    assert cancelled_in == [] and not pending.cancelled.is_set()
    assert agent._memory_provider_shutdown is True
    assert any(OWNER_SKIP_LOG in r.getMessage() for r in caplog.records)


def test_a_plugin_engine_claiming_the_compressor_class_through_a_class_property_never_runs(
    hc, homes, holder_registry, prepared, caplog
):
    """``isinstance`` falls back to ``obj.__class__`` when the type isn't a subclass, and a
    plugin can define ``__class__`` as a property, so the check must be an identity compare
    of ``type(engine)``."""
    compressor = pytest.importorskip("agent.context_compressor")
    calls: list = []
    state, pending, cancelled_in = _running_pass(prepared, "8804-ce-class-property")

    class ClaimsToBeTheCompressor:
        prepared_compaction = state

        @property
        def __class__(self):
            _plugin_code_ran(calls, "__class__")
            return compressor.ContextCompressor

        def on_session_end(self, session_id, messages):
            _plugin_code_ran(calls, "session end")

    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-class-property"),
                         ClaimsToBeTheCompressor())
    caplog.set_level(logging.WARNING)

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-class-property", agent) is True
    assert calls == [], f"plugin code ran under the unresolved owner: {calls}"
    _no_plugin_file_under_any_profile(homes)
    assert cancelled_in == [] and not pending.cancelled.is_set()
    assert agent._memory_provider_shutdown is True
    assert any(OWNER_SKIP_LOG in r.getMessage() for r in caplog.records)


def test_prepared_state_claiming_the_agents_class_through_a_class_property_is_left_alone(
    hc, homes, holder_registry, prepared, builtin_engine, caplog
):
    """The same for the object the built-in engine holds: an identity compare of
    ``type(prepared)``, and a debug line that names no class."""
    calls: list = []

    class ClaimsToBePreparedCompaction:
        @property
        def __class__(self):
            _plugin_code_ran(calls, "__class__")
            return prepared.PreparedCompaction

        def discard(self, *args, **kwargs):
            _plugin_code_ran(calls, "discard")

        def _count_unspliced(self):
            _plugin_code_ran(calls, "_count_unspliced")

    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-prepared-property"),
                         builtin_engine(ClaimsToBePreparedCompaction()))
    caplog.set_level(logging.DEBUG, logger=scope.logger.name)

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-prepared-property", agent) is True
    assert calls == [], f"plugin code ran under the unresolved owner: {calls}"
    _no_plugin_file_under_any_profile(homes)
    assert agent._memory_provider_shutdown is True
    skipped = [r.getMessage() for r in caplog.records if "is left alone" in r.getMessage()
               and "prepared-summary state" in r.getMessage()]
    assert len(skipped) == 1 and "ClaimsToBePreparedCompaction" not in skipped[0], skipped
