"""#8517 P2 + P3: helper agents and session-boundary commits must never act on the
wrong profile's memory.

P2: one-shot helper agents in api/routes.py (manual compression, update summary,
git commit message, handoff summary) do not need memory. Built with memory on,
their constructor initialises the memory provider for whatever home the request
thread's ambient state names; a shared provider instance (Mnemosyne 0.7.1 shape)
is then re-pointed at that profile, and the OWNING session's next cached turn
writes into the other profile's bank (probe harness case ``aux_compress``). The
chat-sync agent does use memory and must be built inside the session's profile.

P3: session-boundary commits (LRU eviction from another profile's turn, the
previous-session commit thread, the shutdown drain) must run as the session's
OWNER, and a commit the Agent refuses must stay pending instead of being counted
(probe harness case ``boundary``: refused ``on_session_end``, marked committed).

Fixtures and stand-ins are reused from tests/test_issue8517_profile_scoped_memory.py.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 P0/P1 module)
from __future__ import annotations

import ast
import logging
import threading
from pathlib import Path

import pytest

import api.session_lifecycle as session_lifecycle
import api.streaming as streaming
from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401 (fixtures)
    SharedInstanceMemoryProvider,
    _key,
    _profile_identity,
    bank_rows,
    bound_home,
    hc,
    homes,
    make_agent_cls,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTES = REPO_ROOT / "api" / "routes.py"


# ── P2: /api/session/compress after a UI switch (harness case aux_compress) ──

def test_manual_compress_after_ui_switch_does_not_rebind_owner_memory(
    hc, homes, monkeypatch, cleanup_test_sessions
):
    from api.memory_profile_scope import construct_profile_scoped_agent
    from api.routes import _handle_session_compress
    from tests.test_sprint46 import (
        _FakeCompressor,
        _FakeHandler,
        _install_fake_compression_runtime,
        _make_session,
    )

    provider = SharedInstanceMemoryProvider(homes["default"])
    base_cls = make_agent_cls(provider)

    class CompressingAgent(base_cls):
        # Same parameter surface as run_agent.AIAgent (names ``skip_memory``).
        def __init__(self, session_id=None, skip_memory=False, **kwargs):
            super().__init__(session_id=session_id, skip_memory=skip_memory, **kwargs)
            self.context_compressor = _FakeCompressor()

    sid = _make_session()
    cleanup_test_sessions.append(sid)

    # alpha's session: its cached agent bound alpha's bank through the P1 scope.
    alpha_agent, cacheable = construct_profile_scoped_agent(
        CompressingAgent, {"session_id": sid}, profile_home=str(homes["alpha"]), session_id=sid,
    )
    assert cacheable and bound_home(alpha_agent) == str(homes["alpha"])

    # The UI switches to beta (process-wide switch rewrites the env mirror), then
    # the user compresses alpha's session.
    monkeypatch.setenv("HERMES_HOME", str(homes["beta"]))
    _install_fake_compression_runtime(monkeypatch, CompressingAgent)
    handler = _FakeHandler()
    _handle_session_compress(handler, {"session_id": sid})
    assert handler.status == 200, handler.payload()

    # alpha's next (cached) turn must still write into alpha's own bank.
    with pytest.raises(RuntimeError):
        alpha_agent.run_conversation(user_message="after-compress")
    assert [r["marker"] for r in bank_rows(homes["beta"])] == [], (
        f"alpha's turn wrote into beta's bank; provider inits: {provider.inits}"
    )
    assert f"{sid}:after-compress" in [r["marker"] for r in bank_rows(homes["alpha"])]

    compress_agent = CompressingAgent.built[-1]
    assert compress_agent is not alpha_agent
    assert compress_agent.skip_memory is True, "the compress helper must not build memory"
    assert compress_agent._memory_manager is None
    assert [i["home"] for i in provider.inits] == [str(homes["alpha"])], provider.inits


# ── P2: structural guard over every AIAgent construction in routes.py ─────────

def _routes_tree():
    return ast.parse(ROUTES.read_text(encoding="utf-8"))


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_every_routes_agent_construction_is_memoryless_or_profile_scoped():
    """No bare ``AIAgent(...)`` in routes.py: each one-shot helper goes through
    ``construct_memoryless_helper_agent`` and the memory-using chat-sync agent
    through ``construct_profile_scoped_agent`` with the session's profile home."""
    tree = _routes_tree()
    bare = []
    helpers = {"construct_memoryless_helper_agent": [], "construct_profile_scoped_agent": []}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        class_names = {"AIAgent"}
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and _call_name(node.value) in {"require_ai_agent_class", "get_ai_agent_class", "_get_ai_agent"}
            ):
                class_names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if isinstance(node.func, ast.Name) and name in class_names:
                    bare.append((fn.name, node.lineno))
                if name in helpers:
                    helpers[name].append((fn.name, node))
    assert bare == [], f"bare AIAgent constructions in routes.py: {sorted(set(bare))}"
    memoryless = sorted({f for f, _ in helpers["construct_memoryless_helper_agent"]})
    assert "_handle_session_compress" in memoryless
    assert "_handle_handoff_summary" in memoryless
    assert len(helpers["construct_memoryless_helper_agent"]) >= 4, memoryless
    scoped = [(f, n) for f, n in helpers["construct_profile_scoped_agent"]]
    assert [f for f, _ in scoped] == ["_handle_chat_sync"], scoped
    kw = {k.arg for k in scoped[0][1].keywords}
    assert {"profile_home", "session_id"} <= kw


def test_memoryless_helper_construction_contract(hc, homes, monkeypatch):
    from api.memory_profile_scope import construct_memoryless_helper_agent

    provider = SharedInstanceMemoryProvider(homes["default"])
    agent_cls = make_agent_cls(provider)
    kwargs = {"session_id": "helper-1", "model": "m"}
    agent = construct_memoryless_helper_agent(agent_cls, kwargs, purpose="test")
    assert agent.skip_memory is True and agent._memory_manager is None
    assert provider.inits == []
    assert kwargs == {"session_id": "helper-1", "model": "m"}, "caller kwargs must not be mutated"

    # An Agent whose constructor has no ``skip_memory`` (older than 2026-02-19,
    # before external memory providers existed): the kwarg is not passed, and any
    # manager it built is detached so the helper can never write.
    class OldAgent:
        def __init__(self, session_id=None, model=None):
            self.kwargs = {"session_id": session_id, "model": model}
            self._memory_manager = object()

    old = construct_memoryless_helper_agent(OldAgent, kwargs, purpose="test")
    assert old.kwargs == kwargs and old._memory_manager is None


# ── P3: owner-scoped session-boundary commits (harness case boundary) ─────────

def _caller_home() -> str:
    import sys

    return str(sys.modules["hermes_constants"].get_hermes_home())


def _caller_scope(bound, label):
    """The Agent's refusal rule (agent/memory_caller_scope.caller_scope): the real
    one when installed, else the same comparison + the same refusal log line."""
    try:
        from agent.memory_caller_scope import caller_scope  # noqa: PLC0415

        return caller_scope(bound, label)
    except ImportError:
        import sys

        hc_mod = sys.modules["hermes_constants"]
        caller = (getattr(hc_mod, "get_hermes_home_override", lambda: None)() or str(hc_mod.get_hermes_home()))
        if _key(caller) != bound.get("home_key"):
            logging.getLogger("agent.memory_caller_scope").warning(
                "Memory %s refused (%s): %s; caller profile home %r, provider bound to %r, multiplexed=%s",
                label, "memory_caller_profile_unresolved", "not the bound profile", caller,
                bound.get("hermes_home"), False,
            )
            return None
        return {"hermes_home": bound.get("hermes_home")}


class OwnerCheckedManager:
    """agent.memory_manager.MemoryManager lifecycle hooks: refused (silently, WARNING
    on agent.memory_caller_scope) unless the caller is the profile it was bound for."""

    def __init__(self, home, log, *, always_refuse=None):
        self._providers = ["sbxmem"]
        self._bound_scope = {
            "hermes_home": str(home),
            "agent_identity": _profile_identity(str(home)),
            "home_key": _key(home),
        }
        self.log = log
        self.always_refuse = always_refuse or {}

    def on_session_end(self, messages):
        if self.always_refuse.get("on"):
            logging.getLogger("agent.memory_caller_scope").warning(
                "Memory %s refused (%s): %s", "hook on_session_end",
                "memory_caller_profile_unresolved", "test refusal",
            )
            return
        if _caller_scope(self._bound_scope, "hook on_session_end") is None:
            return
        self.log.append(("on_session_end", _caller_home()))

    def shutdown_all(self):
        self.log.append(("shutdown_all", _caller_home()))


class OwnerCheckedAgent:
    def __init__(self, home, log, **manager_kwargs):
        self._memory_manager = OwnerCheckedManager(home, log, **manager_kwargs)
        self._session_messages = []
        self._session_db = None
        self.log = log

    def commit_memory_session(self, messages=None):
        if self._memory_manager:
            self._memory_manager.on_session_end(messages or [])

    def shutdown_memory_provider(self, messages=None):
        if self._memory_manager:
            self._memory_manager.on_session_end(messages or [])
            self._memory_manager.shutdown_all()


@pytest.fixture
def lifecycle(monkeypatch):
    session_lifecycle._reset_for_tests()
    monkeypatch.setattr(session_lifecycle, "_draining", False)
    yield session_lifecycle
    session_lifecycle._reset_for_tests()


def test_eviction_commit_from_another_profiles_turn_runs_as_owner(hc, homes, lifecycle):
    log: list = []
    sid = "issue8517-boundary-alpha"
    alpha_agent = OwnerCheckedAgent(homes["alpha"], log)
    # The streaming path records the owner beside every turn mark (all-r2 finding 3:
    # ownership is never derived from the agent's own binding).
    lifecycle.mark_turn_completed(sid, agent=alpha_agent, profile_home=str(homes["alpha"]))

    # beta's turn (its own override installed) evicts alpha's cached agent.
    token = hc.set_hermes_home_override(str(homes["beta"]))
    try:
        closed = streaming._close_evicted_agent_at_session_boundary(sid, alpha_agent)
        assert _caller_home() == str(homes["beta"]), "beta's turn scope must be restored"
    finally:
        hc.reset_hermes_home_override(token)

    commits = [home for hook, home in log if hook == "on_session_end"]
    assert commits and commits[0] == str(homes["alpha"]), (
        f"alpha's commit was refused or ran as another profile: {log}"
    )
    assert not lifecycle.has_uncommitted_work(sid)
    assert closed is True
    assert ("shutdown_all", str(homes["alpha"])) in log, log


def test_refused_commit_is_not_counted_and_stays_retryable(hc, homes, lifecycle, caplog):
    log: list = []
    refuse = {"on": True}
    sid = "issue8517-refused-alpha"
    alpha_agent = OwnerCheckedAgent(homes["alpha"], log, always_refuse=refuse)
    lifecycle.mark_turn_completed(sid, agent=alpha_agent, profile_home=str(homes["alpha"]))

    with caplog.at_level(logging.WARNING):
        assert lifecycle.commit_session_memory(sid, wait=True) is False
    assert lifecycle.has_uncommitted_work(sid), "a refused commit must stay pending"
    assert any("#8517" in r.getMessage() and sid in r.getMessage()
               for r in caplog.records if r.levelno >= logging.WARNING), caplog.text

    # Eviction keeps the agent (and its pending work) instead of tearing it down.
    assert streaming._close_evicted_agent_at_session_boundary(sid, alpha_agent) is False
    assert not any(hook == "shutdown_all" for hook, _ in log)

    refuse["on"] = False
    assert lifecycle.commit_session_memory(sid, wait=True) is True
    assert not lifecycle.has_uncommitted_work(sid)
    assert log == [("on_session_end", str(homes["alpha"]))]


def test_previous_session_thread_and_shutdown_drain_commit_as_owner(hc, homes, lifecycle, monkeypatch):
    """The /api/session/new previous-session thread and the shutdown drain run on
    fresh threads with no override; after a UI switch the env mirror names beta."""
    log: list = []
    sid_a, sid_b = "issue8517-prev-alpha", "issue8517-drain-alpha"
    agent_a = OwnerCheckedAgent(homes["alpha"], log)
    agent_b = OwnerCheckedAgent(homes["alpha"], log)
    lifecycle.mark_turn_completed(sid_a, agent=agent_a, profile_home=str(homes["alpha"]))
    lifecycle.mark_turn_completed(sid_b, agent=agent_b, profile_home=str(homes["alpha"]))
    monkeypatch.setenv("HERMES_HOME", str(homes["beta"]))

    result = {}
    t = threading.Thread(
        target=lambda: result.setdefault("ok", lifecycle.commit_session_memory(sid_a, agent=agent_a)),
        name="issue8517-prev-session-commit",
    )
    t.start()
    t.join(10)
    assert result.get("ok") is True
    lifecycle.drain_all_on_shutdown(deadline=10.0)

    assert log == [("on_session_end", str(homes["alpha"]))] * 2, log
    assert not lifecycle.has_uncommitted_work(sid_a)
    assert not lifecycle.has_uncommitted_work(sid_b)


def test_commit_for_provider_bound_to_another_profile_is_not_run(hc, homes, lifecycle, caplog):
    """The owner recorded at turn time is alpha but the agent's provider is bound to
    beta (a mis-bound agent): committing would flush alpha's turns into beta's
    bank, so the commit is not run and the work stays pending."""
    log: list = []
    sid = "issue8517-misbound-alpha"
    misbound = OwnerCheckedAgent(homes["beta"], log)
    # What the streaming turn does after a completed turn.
    lifecycle.record_owner_profile_home(sid, str(homes["alpha"]))
    lifecycle.mark_turn_completed(sid, agent=misbound)

    with caplog.at_level(logging.WARNING):
        assert lifecycle.commit_session_memory(sid, wait=True) is False
    assert log == []
    assert lifecycle.has_uncommitted_work(sid)
    assert any("#8517" in r.getMessage() for r in caplog.records), caplog.text


def test_commit_without_external_memory_is_unchanged(lifecycle):
    class PlainAgent:
        calls = 0

        def commit_memory_session(self, messages=None):
            PlainAgent.calls += 1

    sid = "issue8517-plain"
    lifecycle.mark_turn_completed(sid, agent=PlainAgent())
    assert lifecycle.commit_session_memory(sid, wait=True) is True
    assert PlainAgent.calls == 1 and not lifecycle.has_uncommitted_work(sid)


def test_streaming_records_the_owner_before_marking_a_completed_turn():
    src = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    mark = src.index("mark_turn_completed(s.session_id, agent=agent)")
    record = src.rindex("record_owner_profile_home(s.session_id, _owner_profile_home)", 0, mark)
    assert mark - record < 200, "the owner must be recorded right before the turn is marked"


def test_mark_turn_completed_accepts_the_owner_profile_home(lifecycle, homes):
    sid = "issue8517-mark-owner"
    lifecycle.mark_turn_completed(sid, agent=object(), profile_home=str(homes["alpha"]))
    assert lifecycle.recorded_owner_profile_home(sid) == str(homes["alpha"])
    lifecycle.record_owner_profile_home(sid, "")
    assert lifecycle.recorded_owner_profile_home(sid) == str(homes["alpha"]), "empty never clears"
