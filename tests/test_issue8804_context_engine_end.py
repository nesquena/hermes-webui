"""#8804 follow-up: the context engine's session end on an unowned eviction.

When ``call_as_session_owner`` refuses an evicted agent's ``shutdown_memory_provider`` (its session owner cannot
be resolved), the Agent's ``_notify_context_engine_session_end`` inside that shutdown was skipped too, on every
Agent; v0.52.544 runs it. The #8517 protection is the MEMORY session end; the engine's end is cleanup skipped
along with it. It now runs once, in the profile home the agent was built under, only when the Agent's own shutdown
did not run, decided before the memory release (which marks an old Agent's agent shut down, #8806).
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 / #8613 / #8804 modules)
from __future__ import annotations

import logging

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


class RecordingEngine:
    """A context engine whose ``on_session_end`` records the session, the context-local
    profile home it ran under, and how many transcript messages it was given."""

    def __init__(self, fail=False):
        self.ends: list = []
        self.fail = fail

    def on_session_end(self, session_id, messages):
        import hermes_constants  # noqa: PLC0415 - the hc fixture installs it

        self.ends.append((session_id, str(hermes_constants.get_hermes_home()), len(messages)))
        if self.fail:
            raise RuntimeError("engine flush failed")


def _with_engine(agent, engine, messages=2):
    agent.context_compressor = engine
    agent._session_messages = [{"role": "user", "content": "x"}] * messages
    return agent


def test_an_unowned_eviction_ends_the_context_engine_once_in_the_agents_own_home(hc, homes, holder_registry):
    engine = RecordingEngine()
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-current"), engine)

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-current", agent) is True

    assert engine.ends == [("8804-ce-current", str(homes["alpha"]), 2)], (
        "v0.52.544 ended the context engine on this eviction; it must run once, in the profile "
        "home the agent was built under (the ambient home is the default profile here)"
    )
    assert agent._memory_provider_shutdown is True
    agent.shutdown_memory_provider([])
    assert len(engine.ends) == 1


def test_an_old_agents_engine_ends_although_its_release_marked_it_shut_down(hc, homes, holder_registry):
    """#8806 marks an old Agent's released agent shut down inside the release; whether
    the engine's end is due must be decided before that, or it never runs."""
    engine = RecordingEngine()
    provider = RecordingProvider()
    agent = _with_engine(_legacy_agent(homes["alpha"], provider, "8804-ce-old"), engine, messages=1)

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-old", agent) is True

    assert engine.ends == [("8804-ce-old", str(homes["alpha"]), 1)]
    assert provider.shutdowns == 1 and provider.session_ends == 0
    agent.shutdown_memory_provider([])
    assert getattr(agent, "shutdown_bodies_run", 0) == 0
    assert len(engine.ends) == 1


def test_the_engine_ends_even_when_a_live_agent_keeps_the_shared_provider(hc, homes, holder_registry):
    """The context engine belongs to the evicted agent alone, so it ends even while the
    shared memory provider keeps running for the other agent (kept alive here, and its
    next save checked, so the "shared" case cannot pass because the survivor was
    collected)."""
    provider = SharedInstanceProvider()
    engine = RecordingEngine()
    first = _with_engine(_shared_agent(homes["alpha"], provider, "8804-ce-shared-a"), engine)
    survivor = _shared_agent(homes["alpha"], provider, "8804-ce-shared-b")

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-shared-a", first) is True

    assert [e[0] for e in engine.ends] == ["8804-ce-shared-a"]
    assert provider.shutdowns == 0 and provider.client.is_closed is False
    provider.remember("the survivor's save")
    assert provider.writes[-1] == "the survivor's save"
    assert survivor._memory_provider_shutdown is False


def test_an_engine_whose_end_raises_does_not_break_the_eviction(hc, homes, holder_registry):
    engine = RecordingEngine(fail=True)
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-fail"), engine)

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-fail", agent) is True
    assert len(engine.ends) == 1
    assert agent._memory_provider_shutdown is True


def test_an_owned_eviction_leaves_the_engine_to_the_agents_own_shutdown(hc, homes, holder_registry, monkeypatch):
    """With a resolvable owner the Agent's own ``shutdown_memory_provider`` runs (and ends
    the engine itself on a real Agent); the WebUI must not end it a second time."""
    engine = RecordingEngine()
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-owned"), engine)
    monkeypatch.setattr(scope, "session_owner_profile_home", lambda sid: str(homes["alpha"]))

    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-owned", agent) is True
    assert engine.ends == []


def test_a_refusal_at_hook_time_does_not_end_the_engine_twice(hc, homes, holder_registry, monkeypatch):
    """A refusal can also happen INSIDE the Agent's own shutdown (at a memory hook): the
    shutdown then ran, and ended the engine itself, so the WebUI must not end it again."""
    engine = RecordingEngine()
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-hook"), engine)

    def agents_own_shutdown(messages=None):  # the real Agent's shape: flag, memory, then the engine
        agent._memory_provider_shutdown = True
        engine.on_session_end(agent.session_id or "", messages or [])

    agent.shutdown_memory_provider = agents_own_shutdown

    def refuse_at_hook_time(target, fn, **kwargs):
        fn()
        return False

    monkeypatch.setattr(streaming, "call_as_session_owner", refuse_at_hook_time)
    assert streaming._close_evicted_agent_at_session_boundary("8804-ce-hook", agent) is True
    assert len(engine.ends) == 1


def test_an_agent_without_a_recorded_build_home_keeps_its_engine_unended(hc, homes, holder_registry, caplog):
    engine = RecordingEngine()
    agent = _with_engine(_shared_agent(homes["alpha"], SharedInstanceProvider(), "8804-ce-nohome"), engine)
    delattr(agent, scope._BUILD_HOME_ATTR)
    caplog.set_level(logging.WARNING)

    assert scope.end_unowned_context_engine(agent, session_id="8804-ce-nohome") is False
    assert engine.ends == []
    assert agent._memory_provider_shutdown is True
    assert any("did not get its session end" in r.getMessage() for r in caplog.records)
