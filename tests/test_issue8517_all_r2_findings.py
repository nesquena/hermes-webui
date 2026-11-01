"""#8517 complete fix, Codex review all-r2: the three CORE isolation findings.

1. Agents < v0.18.0 (no context-local home override) follow the process-wide
   ``HERMES_HOME`` mirror for the whole life of an agent. A "single profile"
   admission at construction cannot be fenced against a second profile that is
   created and starts running later (between the check and the bind, during the
   turn, or after the agent was cached). Memory is off on those Agents.
2. Construction scope is not turn scope. When the streaming turn cannot hold a
   VERIFIED profile home override, every construction site builds without memory
   and a memory-enabled cached agent is not reused for that turn.
3. A session-boundary commit whose owner cannot be resolved from trusted state
   (recorded owner, session record) is refused, logged and left pending; it never
   runs under the agent's own binding or unscoped.

Every test below was RED on ea4bc500c except the explicit no-behaviour-change
guards (healthy scoped turns keep and reuse their memory agent).
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 modules)
from __future__ import annotations

import logging
import re
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.memory_profile_scope as scope
import api.session_lifecycle as session_lifecycle
from tests.test_issue8517_all_r1_findings import (  # noqa: F401  (fixtures)
    _memory_text,
    _only_root_profile,
    _seed_builtin_memory,
    _warnings,
    make_builtin_memory_agent_cls,
    scope_state,
)
from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401  (fixtures)
    SENTINEL,
    HeldWriter,
    SharedInstanceMemoryProvider,
    _cached_agent,
    assert_cached_memoryless,
    bank_rows,
    hc,
    homes,
    legacy_hc,
    make_agent_cls,
    run_turn,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _counting_agent_cls(base, on_turn):
    """``base`` with a construction counter; ``on_turn(agent, n)`` runs inside
    ``run_conversation`` (n = 1-based turn number) and the turn then errors out."""
    seen: list = []

    class Agent(base):
        built = 0

        def __init__(self, session_id=None, skip_memory=False, enabled_toolsets=None,
                     disabled_toolsets=None, **_kw):
            Agent.built += 1
            super().__init__(session_id=session_id, skip_memory=skip_memory,
                             enabled_toolsets=enabled_toolsets, disabled_toolsets=disabled_toolsets)

        def run_conversation(self, **_kw):
            seen.append(self)
            on_turn(self, len(seen))
            raise RuntimeError(SENTINEL)

    Agent.seen = seen
    return Agent


def _write_builtin(agent, text):
    """The model's ``memory`` tool: writes through the agent's store, if it has one."""
    store = getattr(agent, "_memory_store", None)
    if store is not None:
        store.add("memory", text)


# ── 1. Agents < v0.18.0: no memory admission that a new profile could race ─────

def test_legacy_cached_root_agent_never_writes_a_profile_created_later(
    legacy_hc, homes, run_turn, scope_state, monkeypatch
):
    """Codex repro: a root-only install on an Agent < v0.18.0 cached the root agent
    WITH its built-in store; beta was then created and its worker held the env
    mirror while root's next (cached) turn wrote, so root's text landed in beta's
    MEMORY.md."""
    _only_root_profile(homes)
    monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex="r2-cached-legacy"))
    writer = HeldWriter("beta")

    def on_turn(agent, n):
        if n == 2:
            writer.start()
            try:
                _write_builtin(agent, "root-private-after-create-beta")
            finally:
                writer.release()

    Agent = _counting_agent_cls(make_builtin_memory_agent_cls("pre-2026-07-22"), on_turn)
    try:
        sid, _ = run_turn(profile=None, agent_cls=Agent)
        _seed_builtin_memory(homes)  # beta (and alpha) now exist
        sid2, _ = run_turn(profile=None, agent_cls=Agent)
    finally:
        writer.release()

    assert sid2 == sid and len(Agent.seen) == 2
    assert "root-private-after-create-beta" not in _memory_text(homes["beta"])
    assert Agent.seen[1]._memory_store is None
    cached = _cached_agent(sid)
    assert cached is None or getattr(cached, "_memory_store", None) is None


def test_legacy_root_admission_cannot_race_a_profile_created_before_the_bind(
    legacy_hc, homes, run_turn, scope_state, monkeypatch
):
    """Codex repro: root-only at the admission check; beta was created and its
    worker took the env mirror before the Agent bound, so root bound and wrote
    beta's bank and was cached."""
    _only_root_profile(homes)
    monkeypatch.setitem(sys.modules, "agent.memory_manager", types.ModuleType("agent.memory_manager"))
    writer = HeldWriter("beta")

    def new_profile_then_writer():
        homes["beta"].mkdir(parents=True)
        writer.start()

    provider = SharedInstanceMemoryProvider(homes["default"])
    Agent = make_agent_cls(provider, bound_scope=False, hooks={"before_bind": new_profile_then_writer})
    try:
        sid, _ = run_turn(profile=None, agent_cls=Agent)
    finally:
        writer.release()

    assert Agent.turns, "the turn never reached run_conversation"
    assert provider.inits == []
    assert not bank_rows(homes["beta"]), f"root wrote into beta's bank: {bank_rows(homes['beta'])}"
    assert_cached_memoryless(_cached_agent(sid), Agent.turns[0]["agent"])


def test_legacy_agent_turn_and_commit_scopes_refuse(legacy_hc, homes, scope_state):
    """Every scope helper answers 'no memory' on an Agent < v0.18.0, root-only or not."""
    _only_root_profile(homes)
    root = str(homes["default"])
    assert scope.turn_memory_reason(root, override_installed=False)
    with scope.profile_home_turn_scope(root) as reason:
        assert reason
    agent = SimpleNamespace(_memory_manager=SimpleNamespace(), commit_memory_session=lambda: None)
    assert scope.call_as_session_owner(agent, lambda: pytest.fail("ran"), session_id="s", owner_home=root) is False


# ── 2. the streaming turn must hold a VERIFIED scope, or run without memory ─────

def test_streaming_turn_whose_override_setter_fails_runs_without_memory(
    hc, homes, run_turn, monkeypatch, caplog
):
    """Codex repro: the turn-level setter failed once, the construction setter a
    moment later succeeded. The agent loaded alpha's MEMORY.md, the turn ran on the
    env mirror (beta's worker), wrote alpha's text into beta's MEMORY.md and the
    agent was cached."""
    caplog.set_level(logging.WARNING)
    _seed_builtin_memory(homes)
    real_set = hc.set_hermes_home_override
    calls: list = []

    def transient(home):
        calls.append(str(home))
        if len(calls) == 1:
            raise RuntimeError("turn-level override temporarily failed")
        return real_set(home)

    monkeypatch.setattr(hc, "set_hermes_home_override", transient)
    writer = HeldWriter("beta")
    seen: dict = {}

    def on_turn(agent, n):
        seen["store"] = agent._memory_store
        writer.start()
        try:
            _write_builtin(agent, "alpha-private-transient-write")
        finally:
            writer.release()

    Agent = _counting_agent_cls(make_builtin_memory_agent_cls(), on_turn)
    try:
        sid, _ = run_turn(profile="alpha", agent_cls=Agent)
    finally:
        writer.release()

    assert calls, "the turn never asked for its profile home"
    assert "alpha-private-transient-write" not in _memory_text(homes["beta"])
    assert seen["store"] is None
    assert _cached_agent(sid) is None
    assert _warnings(caplog, "turn-level profile home"), caplog.text


def test_cached_memory_agent_is_not_reused_for_a_turn_without_a_verified_scope(
    hc, homes, run_turn, monkeypatch, caplog
):
    """Codex repro: the cached alpha agent was reused (no constructor runs) on a turn
    whose setter failed, and wrote into beta's MEMORY.md. Now that turn gets a
    memoryless, uncached agent; the cached one stays and is reused once the scope
    holds again."""
    caplog.set_level(logging.WARNING)
    _seed_builtin_memory(homes)
    monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex="r2-cached-current"))
    writer = HeldWriter("beta")

    def on_turn(agent, n):
        if n == 2:
            writer.start()
            try:
                _write_builtin(agent, "alpha-private-cached-write")
            finally:
                writer.release()
        if n == 3:
            _write_builtin(agent, "alpha-private-third-turn")

    Agent = _counting_agent_cls(make_builtin_memory_agent_cls(), on_turn)
    sid, _ = run_turn(profile="alpha", agent_cls=Agent)
    first = Agent.seen[0]
    assert _cached_agent(sid) is first and first._memory_store is not None

    real_set = hc.set_hermes_home_override

    def unavailable(home):
        raise RuntimeError("turn-level override unavailable")

    monkeypatch.setattr(hc, "set_hermes_home_override", unavailable)
    try:
        run_turn(profile="alpha", agent_cls=Agent)
    finally:
        writer.release()
    assert "alpha-private-cached-write" not in _memory_text(homes["beta"])
    second = Agent.seen[1]
    assert second is not first and second._memory_store is None
    assert Agent.built == 2
    assert _cached_agent(sid) is first, "the bypassed agent must stay cached, not be replaced"
    assert _warnings(caplog, "not reused"), caplog.text

    monkeypatch.setattr(hc, "set_hermes_home_override", real_set)
    run_turn(profile="alpha", agent_cls=Agent)
    assert Agent.seen[2] is first and Agent.built == 2
    assert "alpha-private-third-turn" in _memory_text(homes["alpha"])
    assert "alpha-private-third-turn" not in _memory_text(homes["beta"])


def test_every_streaming_construction_site_passes_the_turn_memory_reason():
    src = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    sites = re.findall(r"construct_profile_scoped_agent\((.*?)\n\s*\)", src, re.S)
    assert len(sites) == 4, sites
    for body in sites:
        assert "fail_closed_reason=_turn_memory_reason" in body, body


def test_turn_memory_reason_requires_an_installed_and_effective_override(hc, homes, monkeypatch):
    alpha = str(homes["alpha"])
    assert scope.turn_memory_reason(alpha, override_installed=False)
    assert scope.turn_memory_reason("", override_installed=True)
    token = hc.set_hermes_home_override(str(homes["beta"]))
    try:
        assert "did not take effect" in scope.turn_memory_reason(alpha, override_installed=True)
    finally:
        hc.reset_hermes_home_override(token)
    token = hc.set_hermes_home_override(alpha)
    try:
        assert scope.turn_memory_reason(alpha, override_installed=True) == ""
    finally:
        hc.reset_hermes_home_override(token)


def test_healthy_turns_keep_and_reuse_their_memory_agent(hc, homes, run_turn, monkeypatch):
    """No behaviour change: a verified scope keeps the built-in store, the agent is
    cached and reused, and its writes stay in the session's own profile."""
    _seed_builtin_memory(homes)
    monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex="r2-healthy"))

    def on_turn(agent, n):
        _write_builtin(agent, f"alpha-healthy-{n}")

    Agent = _counting_agent_cls(make_builtin_memory_agent_cls(), on_turn)
    sid, _ = run_turn(profile="alpha", agent_cls=Agent)
    run_turn(profile="alpha", agent_cls=Agent)
    assert Agent.built == 1 and Agent.seen[0] is Agent.seen[1]
    assert _cached_agent(sid) is Agent.seen[0]
    text = _memory_text(homes["alpha"])
    assert "alpha-healthy-1" in text and "alpha-healthy-2" in text
    assert "alpha-healthy" not in _memory_text(homes["beta"])


# ── 3. unknown boundary owner: refused, pending, never the agent's own binding ──

@pytest.fixture
def lifecycle(monkeypatch):
    session_lifecycle._reset_for_tests()
    monkeypatch.setattr(session_lifecycle, "_draining", False)
    yield session_lifecycle
    session_lifecycle._reset_for_tests()


@pytest.mark.parametrize("binding", ["bound_to_beta", "no_bound_scope"])
def test_boundary_commit_with_unknown_owner_is_refused_and_stays_pending(
    hc, homes, lifecycle, binding, caplog
):
    """Codex repro: no recorded owner and no session record; the beta-bound agent
    was accepted as beta (``or bound_home``), its commit ran and was counted. An
    Agent without ``_bound_scope`` ran the commit unscoped."""
    caplog.set_level(logging.WARNING)
    sid = f"r2-unknown-owner-{binding}"
    written: list = []

    class Agent:
        def __init__(self):
            if binding == "bound_to_beta":
                self._memory_manager = SimpleNamespace(
                    _bound_scope={"hermes_home": str(homes["beta"]), "home_key": scope.profile_home_key(homes["beta"])})
            else:
                self._memory_manager = SimpleNamespace()

        def commit_memory_session(self):
            written.append(str(hc.get_hermes_home()))

    lifecycle.mark_turn_completed(sid, agent=Agent())
    assert scope.session_owner_profile_home(sid) == ""
    token = hc.set_hermes_home_override(str(homes["alpha"]))
    try:
        assert lifecycle.commit_session_memory(sid, wait=True) is False
    finally:
        hc.reset_hermes_home_override(token)

    assert written == []
    assert lifecycle.has_uncommitted_work(sid), "the refused generation must stay pending"
    assert _warnings(caplog, "owner"), caplog.text


def test_boundary_commit_with_a_recorded_owner_still_runs_as_that_owner(hc, homes, lifecycle):
    """No behaviour change when the owner is known (the streaming path records it)."""
    sid = "r2-known-owner"
    written: list = []

    class Agent:
        _memory_manager = SimpleNamespace(
            _bound_scope={"hermes_home": str(homes["alpha"]), "home_key": scope.profile_home_key(homes["alpha"])})

        def commit_memory_session(self):
            written.append(str(hc.get_hermes_home()))

    lifecycle.record_owner_profile_home(sid, str(homes["alpha"]))
    lifecycle.mark_turn_completed(sid, agent=Agent())
    assert lifecycle.commit_session_memory(sid, wait=True) is True
    assert written == [str(homes["alpha"])]
    assert not lifecycle.has_uncommitted_work(sid)
