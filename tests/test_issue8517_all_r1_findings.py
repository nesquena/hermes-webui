"""#8517 complete fix, Codex review all-r1: the four CORE isolation findings.

1. Fail-closed / memoryless construction must not load or write the BUILT-IN
   MEMORY.md / USER.md store of the ambient (another profile's) home. The real
   Agent keeps the built-in store with ``skip_memory=True`` whenever the
   ``memory`` toolset is requested (agent/agent_init._init_memory, #65429).
2. Agents without ``agent.memory_manager.claim_instance`` (before 2026-10-17): the
   shared-instance guard cannot be installed, so in multi-profile operation a
   singleton provider must not be initialised at all (fail closed).
3. Agents without the context-local home override (< v0.18.0): an env snapshot
   is not a safe admission in multi-profile operation (another worker can switch
   ``HERMES_HOME`` between the check and the Agent's read).
4. ``_handle_chat_sync`` must run the whole synchronous turn (not only
   construction) inside the session's profile home.

Every test below was RED on f532dada6 except the explicit no-behaviour-change
guards (single-profile / per-profile keep-memory cases).
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 P0/P1 module)
from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.profiles as profiles_api
import api.streaming as streaming
from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401  (fixtures)
    SENTINEL,
    HeldWriter,
    SharedInstanceMemoryProvider,
    _cached_agent,
    _key,
    assert_cached_memoryless,
    bank_rows,
    hc,
    homes,
    legacy_hc,
    make_agent_cls,
    run_turn,
    writer,
)


@pytest.fixture
def scope_state(monkeypatch):
    """Fresh warn-once and profile-home bookkeeping in the scope module."""
    import api.memory_profile_scope as scope  # noqa: PLC0415

    monkeypatch.setattr(scope, "_logged_once", set())
    monkeypatch.setattr(scope, "_profile_homes_seen", set(), raising=False)
    return scope


@pytest.fixture
def root_is(monkeypatch, homes):
    """WebUI's own profile machinery answers the test homes."""
    monkeypatch.setattr(
        profiles_api, "get_hermes_home_for_profile",
        lambda name: homes[name if name and name != "default" else "default"],
    )
    monkeypatch.setattr(profiles_api, "get_profile_runtime_env", lambda home: {})
    monkeypatch.setattr(profiles_api, "filter_runtime_env_for_gateway_parity", lambda env: {})
    return homes


def _only_root_profile(homes):
    """Make the test install a real single-profile (root only) install."""
    for name in ("alpha", "beta"):
        homes[name].rmdir()
    (homes["default"] / "profiles").rmdir()


def _warnings(caplog, needle):
    return [r for r in caplog.records if r.levelno >= logging.WARNING and needle in r.getMessage()]


# ── 1. built-in MEMORY.md / USER.md on fail-closed and helper construction ─────

_MEMORY_CFG = {"memory": {"memory_enabled": True, "user_profile_enabled": True}}


def _seed_builtin_memory(homes):
    for name in ("alpha", "beta"):
        d = homes[name] / "memories"
        d.mkdir(parents=True, exist_ok=True)
        (d / "MEMORY.md").write_text(f"{name}-private-memory", encoding="utf-8")


def _real_init_memory(agent, skip_memory):
    agent_init = pytest.importorskip("agent.agent_init")
    agent_init._init_memory(agent, _MEMORY_CFG, skip_memory, "webui")


def _historical_init_memory(semantics):
    """Built-in store gate of each Agent generation (agent/agent_init._init_memory):
    current (>= 2026-09-02): skip_memory off, or 'memory' enabled and not disabled;
    2026-07-22 (#65429): skip_memory off, or 'memory' enabled;
    before 2026-07-22: skip_memory off only;
    'always': a build that loads the store regardless (backstop case)."""
    def _init(agent, skip_memory):
        if semantics == "current":
            return _real_init_memory(agent, skip_memory)
        enabled = "memory" in (agent.enabled_toolsets or [])
        load = {
            "2026-07-22": (not skip_memory) or enabled,
            "pre-2026-07-22": not skip_memory,
            "always": True,
        }[semantics]
        agent._memory_store = None
        agent._memory_manager = None
        if load:
            from tools.memory_tool import MemoryStore  # noqa: PLC0415

            agent._memory_store = MemoryStore(memory_enabled=True, user_profile_enabled=True)
            agent._memory_store.load_from_disk()
    return _init


def make_builtin_memory_agent_cls(semantics="current"):
    init_memory = _historical_init_memory(semantics)

    class BuiltinMemoryAgent:
        def __init__(self, session_id=None, skip_memory=False, enabled_toolsets=None,
                     disabled_toolsets=None, model=None, **_route_kwargs):
            self.session_id = session_id
            self.skip_memory = skip_memory
            self.enabled_toolsets = enabled_toolsets
            self.disabled_toolsets = list(disabled_toolsets or [])
            self.tools = []
            init_memory(self, skip_memory)

        def _emit_startup_warning(self, *a, **k):
            return None

    return BuiltinMemoryAgent


def _memory_text(home):
    p = Path(home) / "memories" / "MEMORY.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _try_builtin_write(agent, text):
    """What the model's ``memory`` tool does: write through the agent's store."""
    mt = pytest.importorskip("tools.memory_tool")
    return mt.memory_tool(action="add", target="memory", content=text, store=getattr(agent, "_memory_store", None))


@pytest.mark.parametrize("semantics", ["current", "2026-07-22", "pre-2026-07-22", "always"])
def test_fail_closed_construction_never_touches_another_profiles_builtin_memory(
    hc, homes, scope_state, monkeypatch, semantics
):
    """Codex repro (real ``_init_memory``): the override cannot be set for alpha, the
    ambient home is beta; ``skip_memory=True`` alone loaded beta's MEMORY.md and the
    memory tool wrote alpha's text into beta's file."""
    pytest.importorskip("tools.memory_tool")
    _seed_builtin_memory(homes)
    monkeypatch.setenv("HERMES_HOME", str(homes["beta"]))

    def _raise(_path):
        raise RuntimeError("override unavailable (issue8517 all-r1)")

    monkeypatch.setattr(hc, "set_hermes_home_override", _raise)
    agent_cls = make_builtin_memory_agent_cls(semantics)
    kwargs = {"session_id": "alpha-session", "enabled_toolsets": ["memory", "web"]}

    agent, cacheable = scope_state.construct_profile_scoped_agent(
        agent_cls, kwargs, profile_home=str(homes["alpha"]), session_id="alpha-session",
    )
    _try_builtin_write(agent, "alpha-private-write")

    assert cacheable is False
    assert agent._memory_store is None, "the built-in store of the ambient home was loaded"
    assert _memory_text(homes["beta"]) == "beta-private-memory", "alpha wrote into beta's MEMORY.md"
    assert kwargs == {"session_id": "alpha-session", "enabled_toolsets": ["memory", "web"]}
    if semantics == "current":
        assert "memory" in agent.disabled_toolsets
        assert "memory" not in (agent.enabled_toolsets or [])
        assert "web" in agent.enabled_toolsets


@pytest.mark.parametrize("signature", ["names_skip_memory", "kwargs_only"])
def test_mismatch_rebuild_drops_builtin_memory_too(hc, homes, scope_state, monkeypatch, signature):
    """The post-construction mismatch path (rebuild with ``_build_without_memory``,
    or detach in place when the constructor does not name ``skip_memory``) must not
    keep a built-in store either."""
    _seed_builtin_memory(homes)
    base = make_builtin_memory_agent_cls("current")

    def _misbind(agent, kw):
        if not kw.get("skip_memory"):
            agent._memory_manager = SimpleNamespace(_bound_scope={
                "hermes_home": str(homes["beta"]), "home_key": _key(homes["beta"])})

    if signature == "names_skip_memory":
        class MisboundAgent(base):
            def __init__(self, session_id=None, skip_memory=False, enabled_toolsets=None,
                         disabled_toolsets=None):
                kw = dict(session_id=session_id, skip_memory=skip_memory,
                          enabled_toolsets=enabled_toolsets, disabled_toolsets=disabled_toolsets)
                super().__init__(**kw)
                _misbind(self, kw)
    else:
        class MisboundAgent(base):
            def __init__(self, **kw):
                super().__init__(**kw)
                _misbind(self, kw)

    agent, cacheable = scope_state.construct_profile_scoped_agent(
        MisboundAgent, {"session_id": "alpha-session", "enabled_toolsets": ["memory"]},
        profile_home=str(homes["alpha"]), session_id="alpha-session",
    )
    assert cacheable is False and agent._memory_manager is None
    assert agent._memory_store is None


@pytest.mark.parametrize("semantics", ["current", "always"])
def test_memoryless_helper_never_loads_the_ambient_builtin_memory(hc, homes, scope_state, monkeypatch, semantics):
    """Helper agents (manual compress passes ``_resolve_cli_toolsets()``, which can
    name ``memory``) are built on the request thread with the ambient home."""
    pytest.importorskip("tools.memory_tool")
    _seed_builtin_memory(homes)
    monkeypatch.setenv("HERMES_HOME", str(homes["beta"]))
    agent = scope_state.construct_memoryless_helper_agent(
        make_builtin_memory_agent_cls(semantics),
        {"session_id": "alpha-helper", "enabled_toolsets": ["memory"]}, purpose="test",
    )
    _try_builtin_write(agent, "alpha-helper-write")
    assert agent._memory_store is None
    assert _memory_text(homes["beta"]) == "beta-private-memory"


def test_scoped_construction_keeps_builtin_memory(hc, homes, scope_state):
    """No behaviour change: a correctly scoped construction keeps MEMORY.md."""
    _seed_builtin_memory(homes)
    agent, cacheable = scope_state.construct_profile_scoped_agent(
        make_builtin_memory_agent_cls("current"),
        {"session_id": "alpha-session", "enabled_toolsets": ["memory"]},
        profile_home=str(homes["alpha"]), session_id="alpha-session",
    )
    assert cacheable is True and agent._memory_store is not None
    assert "alpha-private-memory" in str(agent._memory_store._system_prompt_snapshot)


# ── 2. Agents without claim_instance (before 2026-10-17) ─────────────────────

def _agent_without_claim_seam(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.memory_manager", types.ModuleType("agent.memory_manager"))


def test_no_claim_seam_multi_profile_never_repoints_a_shared_instance(
    hc, homes, root_is, scope_state, monkeypatch, caplog
):
    """Codex repro: alpha then beta on an Agent without the claim seam; alpha's
    retained agent then wrote into beta's bank."""
    _agent_without_claim_seam(monkeypatch)
    caplog.set_level(logging.WARNING)
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = make_agent_cls(provider)

    alpha, alpha_cacheable = scope_state.construct_profile_scoped_agent(
        cls, {"session_id": "alpha-session"}, profile_home=str(homes["alpha"]), session_id="alpha-session")
    beta, beta_cacheable = scope_state.construct_profile_scoped_agent(
        cls, {"session_id": "beta-session"}, profile_home=str(homes["beta"]), session_id="beta-session")
    with pytest.raises(RuntimeError, match=SENTINEL):
        alpha.run_conversation(user_message="alpha-private")

    assert not bank_rows(homes["beta"]), "alpha's retained agent wrote into beta's bank"
    assert provider.inits == [], "the shared instance must not be initialised without the claim seam"
    assert alpha._memory_manager is None and beta._memory_manager is None
    assert alpha.skip_memory is True and beta.skip_memory is True
    # External memory stays off for the life of the process here (the Agent cannot gain
    # claim_instance and served homes only accumulate), so, like the pre-v0.18 case, the
    # agent is cacheable: not caching it only rebuilt it on every turn.
    assert alpha_cacheable is True and beta_cacheable is True
    assert len(_warnings(caplog, "Upgrade the Agent to restore them")) == 1, "warn once per process"
    assert _warnings(caplog, "alpha-session") and _warnings(caplog, "beta-session"), "per-session WARNING"


def test_no_claim_seam_root_with_a_named_profile_fails_closed(hc, homes, root_is, scope_state, monkeypatch):
    """Multi-profile is defined conservatively: a root session counts as
    multi-profile as soon as one named profile directory exists."""
    _agent_without_claim_seam(monkeypatch)
    provider = SharedInstanceMemoryProvider(homes["default"])
    agent, cacheable = scope_state.construct_profile_scoped_agent(
        make_agent_cls(provider), {"session_id": "root-session"},
        profile_home=str(homes["default"]), session_id="root-session")
    assert agent._memory_manager is None and cacheable is True and provider.inits == []


def test_no_claim_seam_single_profile_root_keeps_external_memory(hc, homes, root_is, scope_state, monkeypatch, caplog):
    """No behaviour change for a root-only install on an old Agent."""
    _agent_without_claim_seam(monkeypatch)
    _only_root_profile(homes)
    caplog.set_level(logging.WARNING)
    provider = SharedInstanceMemoryProvider(homes["default"])
    agent, cacheable = scope_state.construct_profile_scoped_agent(
        make_agent_cls(provider), {"session_id": "root-session"},
        profile_home=str(homes["default"]), session_id="root-session")
    assert agent._memory_manager is not None and cacheable is True
    assert [i["home"] for i in provider.inits] == [str(homes["default"])]
    assert not _warnings(caplog, "Upgrade the Agent to restore them")
    assert not _warnings(caplog, "memory provider disabled for session")


def test_once_two_homes_were_served_an_old_agent_stays_fail_closed(hc, homes, root_is, scope_state, monkeypatch):
    """A profile whose directory does not exist still counts once it was served."""
    _agent_without_claim_seam(monkeypatch)
    _only_root_profile(homes)
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = make_agent_cls(provider)
    ghost = homes["default"] / "profiles" / "ghost"
    scope_state.construct_profile_scoped_agent(cls, {"session_id": "g"}, profile_home=str(ghost), session_id="g")
    root_agent, cacheable = scope_state.construct_profile_scoped_agent(
        cls, {"session_id": "r"}, profile_home=str(homes["default"]), session_id="r")
    assert root_agent._memory_manager is None and cacheable is True and provider.inits == []


def test_current_agent_keeps_external_memory_for_every_profile(hc, homes, root_is, scope_state):
    """Current Agent (claim seam present), per-instance provider: no change."""
    cls = make_agent_cls_per_instance(homes)
    for name in ("alpha", "beta", "default"):
        agent, cacheable = scope_state.construct_profile_scoped_agent(
            cls, {"session_id": f"{name}-s"}, profile_home=str(homes[name]), session_id=f"{name}-s")
        assert agent._memory_manager is not None and cacheable is True


def make_agent_cls_per_instance(homes):
    class PerInstanceAgent:
        def __init__(self, session_id=None, skip_memory=False, **kw):
            self.skip_memory = skip_memory
            self._memory_manager = None
            if not skip_memory:
                home = str(sys.modules["hermes_constants"].get_hermes_home())
                self._memory_manager = SimpleNamespace(
                    _bound_scope={"hermes_home": home, "home_key": _key(home)})
    return PerInstanceAgent


# ── 3. Agents < v0.18.0 (no context-local override): no env-snapshot admission ─

def test_legacy_agent_multi_profile_never_admits_memory_on_an_env_snapshot(
    legacy_hc, homes, run_turn, writer, scope_state, caplog
):
    """Codex repro through ``_run_agent_streaming``: the env named alpha at the
    check, a beta writer switched it before the Agent bound, and alpha's agent was
    cached after writing into beta's bank."""
    caplog.set_level(logging.WARNING)
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = make_agent_cls(provider, bound_scope=False,
                         hooks={"before_bind": writer.start, "after_bind": writer.release})

    session_id, _events = run_turn(profile="alpha", agent_cls=cls)

    assert cls.turns, "the turn never reached run_conversation"
    assert not bank_rows(homes["beta"]), f"alpha wrote into beta's bank: {bank_rows(homes['beta'])}"
    assert provider.inits == []
    assert cls.turns[0]["agent"]._memory_manager is None
    assert_cached_memoryless(_cached_agent(session_id), cls.turns[0]["agent"])
    assert _warnings(caplog, "older than v0.18.0")


def test_legacy_agent_root_only_install_builds_without_memory(legacy_hc, homes, run_turn, scope_state):
    """all-r2 finding 1 (behaviour change): a root-only install on an Agent < v0.18.0
    no longer keeps memory. A later profile (created mid-turn or after the agent was
    cached) could not be fenced off the env mirror the agent follows."""
    _only_root_profile(homes)
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = make_agent_cls(provider, bound_scope=False)
    session_id, _events = run_turn(profile=None, agent_cls=cls)
    turn = cls.turns[0]
    assert turn["agent"]._memory_manager is None
    assert_cached_memoryless(turn["cached_agent"], turn["agent"])
    assert provider.inits == []
    assert bank_rows(homes["default"]) == []


# ── 4. chat-sync runs the whole turn in the session's profile home ───────────

@pytest.fixture
def chat_sync(hc, homes, root_is, monkeypatch):
    import api.config as config  # noqa: PLC0415
    import api.models as models  # noqa: PLC0415
    import api.routes as routes  # noqa: PLC0415
    import api.state_sync as state_sync  # noqa: PLC0415

    def _run(agent_cls, *, toolsets=()):
        session = models.Session(
            session_id="issue8517-sync", workspace=str(homes["ws"]),
            model="test-model", model_provider="test-provider", profile="alpha",
        )
        monkeypatch.setattr(routes, "get_session", lambda _sid: session)
        monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
        monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda sid: False)
        monkeypatch.setattr(routes, "_read_profile_model_config", lambda *a: ("test-provider", "test-model", {}))
        monkeypatch.setattr(routes, "_resolve_compatible_session_model_state",
                            lambda *a, **kw: ("test-model", "test-provider"))
        monkeypatch.setattr(config, "get_config", lambda: {})
        monkeypatch.setattr(config, "resolve_model_provider", lambda *a, **kw: ("test-model", "test-provider", None))
        monkeypatch.setattr(routes, "get_config", lambda: {})
        monkeypatch.setattr(routes, "_resolve_agent_connection_bundle",
                            lambda *a: {"provider": "test-provider", "api_key": None, "base_url": None})
        monkeypatch.setattr(routes, "_agent_bundle_kwargs", lambda *a: {})
        monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda *a, **kw: homes["ws"])
        monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: list(toolsets))
        monkeypatch.setattr(state_sync, "sync_session_cwd_background", lambda *a: None)
        monkeypatch.setattr(routes, "require_ai_agent_class", lambda: agent_cls)
        return routes._handle_chat_sync(SimpleNamespace(), {"session_id": session.session_id, "message": "hello"})

    return _run


def test_chat_sync_turn_runs_in_the_sessions_profile_home(hc, homes, chat_sync, monkeypatch):
    """Codex repro: construction bound alpha, then ``run_conversation`` ran in beta's
    home (the UI's env mirror) and the Agent refused alpha's own memory hook."""
    mcs = pytest.importorskip("agent.memory_caller_scope")
    monkeypatch.setenv("HERMES_HOME", str(homes["beta"]))
    seen = {}

    class MemoryAgent:
        def __init__(self, skip_memory=False, **kw):
            seen["bound_home"] = str(hc.get_hermes_home())
            self._memory_manager = SimpleNamespace(_bound_scope=mcs.bound_scope(seen["bound_home"], "alpha"))

        def run_conversation(self, **kw):
            seen["turn_home"] = str(hc.get_hermes_home())
            seen["allowed"] = mcs.caller_scope(self._memory_manager._bound_scope, "hook on_turn_start")
            raise RuntimeError("sync-stop-after-memory-hook")

    with pytest.raises(RuntimeError, match="sync-stop-after-memory-hook"):
        chat_sync(MemoryAgent)

    assert _key(seen["bound_home"]) == _key(homes["alpha"])
    assert _key(seen["turn_home"]) == _key(homes["alpha"]), "the sync turn ran in another profile's home"
    assert seen["allowed"], "the Agent refused alpha's own memory hook"
    assert _key(hc.get_hermes_home()) == _key(homes["beta"]), "the turn scope leaked out of the route"


def test_chat_sync_ui_switch_during_the_turn_cannot_redirect_builtin_memory(hc, homes, chat_sync, monkeypatch):
    """Real ``_init_memory`` + real ``MemoryStore``: the UI is on alpha, a beta
    writer (the real production env writer) takes the env mirror mid-turn, and the
    turn's memory-tool write must still land in alpha's MEMORY.md."""
    pytest.importorskip("tools.memory_tool")
    _seed_builtin_memory(homes)
    monkeypatch.setenv("HERMES_HOME", str(homes["alpha"]))
    switch = HeldWriter("beta")
    result = {}

    base = make_builtin_memory_agent_cls("current")

    class SyncAgent(base):
        def run_conversation(self, **kw):
            switch.start()
            try:
                result["write"] = _try_builtin_write(self, "alpha-sync-write")
            finally:
                switch.release()
            raise RuntimeError("sync-stop-after-write")

    try:
        with pytest.raises(RuntimeError, match="sync-stop-after-write"):
            chat_sync(SyncAgent, toolsets=["memory"])
    finally:
        switch.release()

    assert switch.env_seen == str(homes["beta"]), "the switch was not in effect"
    assert "alpha-sync-write" not in _memory_text(homes["beta"]), "alpha's write landed in beta's MEMORY.md"
    assert "alpha-sync-write" in _memory_text(homes["alpha"])


def test_chat_sync_without_a_turn_scope_builds_without_memory(hc, homes, chat_sync, monkeypatch, scope_state):
    """If the turn-level home cannot be held, the sync agent is memoryless."""
    monkeypatch.setenv("HERMES_HOME", str(homes["beta"]))
    real_set = hc.set_hermes_home_override
    calls = {"n": 0}

    def _first_call_fails(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient (issue8517 all-r1)")
        return real_set(path)

    monkeypatch.setattr(hc, "set_hermes_home_override", _first_call_fails)
    seen = {}

    class Agent:
        def __init__(self, skip_memory=False, **kw):
            seen["skip_memory"] = skip_memory

        def run_conversation(self, **kw):
            raise RuntimeError("sync-stop")

    with pytest.raises(RuntimeError, match="sync-stop"):
        chat_sync(Agent)
    assert seen["skip_memory"] is True


# ── streaming: the turn-level override spans run_conversation ────────────────

def test_streaming_turn_with_a_stale_override_flag_still_runs_in_its_profile_home(
    hc, homes, run_turn, scope_state
):
    """A cached 'override unavailable' flag let construction keep memory (scoped by
    the helper) while ``run_conversation`` ran without the turn-level override: a
    built-in store write then followed the env mirror to beta."""
    streaming._streaming_hermes_home_override_available = False
    provider = SharedInstanceMemoryProvider(homes["default"])
    switch = HeldWriter("beta")
    seen = {}
    base = make_agent_cls(provider)

    class TurnHomeAgent(base):
        def run_conversation(self, *a, **kw):
            switch.start()
            try:
                seen["turn_home"] = str(sys.modules["hermes_constants"].get_hermes_home())
            finally:
                switch.release()
            return super().run_conversation(*a, **kw)

    try:
        session_id, _events = run_turn(profile="alpha", agent_cls=TurnHomeAgent)
    finally:
        switch.release()
    assert switch.env_seen == str(homes["beta"])
    assert _key(seen["turn_home"]) == _key(homes["alpha"]), "the streaming turn ran outside its profile home"
