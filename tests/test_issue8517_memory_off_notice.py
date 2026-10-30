"""#8517 follow-up: tell the user, in the chat, why memory is off.

#8585 made memory fail closed whenever a profile's agent cannot be held to that
profile's memory, and only logged a server WARNING. These tests pin the per-session
status the streaming path now exposes to the UI (a ``warning`` SSE frame with
``type: "memory_off"`` and a stable reason code) for the three reasons that last
beyond one turn:

(a) Agent < v0.18.0 (no context-local home override): all memory off;
(b) Agent without ``claim_instance`` serving several profiles: external providers off;
(c) a shared provider instance another profile already owns: external provider off
    for this profile until the WebUI restarts.

A turn-level scope failure (memory off for this one turn, retried next turn) reports
the lasting status of the session's cached agent when it has one, and nothing (status
unknown) otherwise. A turn whose agent is the session's lasting agent with memory on
reports an explicit healthy ``memory_status`` frame (``state: "ok"``), the only thing
that clears the notice in the UI. The run journal snapshot carries the latest status
so a reload that resumes after the warning restores it. The payload carries no paths.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 modules)
from __future__ import annotations

import json
import sys
import types
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import api.config as config_mod
import api.models as models
import api.profiles as profiles_api
import api.routes as routes
from api.run_journal import RunJournalWriter
from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401  (fixtures)
    SENTINEL,
    FakeMemoryManager,
    SharedInstanceMemoryProvider,
    _cached_agent,
    _profile_identity,
    hc,
    homes,
    legacy_hc,
    make_agent_cls,
    run_turn,
)

_ALLOWED_KEYS = {"type", "session_id", "stream_id", "reason", "scope", "message"}
_OK_KEYS = {"type", "state", "session_id", "stream_id"}


@pytest.fixture
def scope(monkeypatch):
    """Fresh #8517 bookkeeping: warn-once keys, served homes, shared-instance owners,
    and the Agent's own (unwrapped) ``claim_instance`` when one is loaded."""
    import api.memory_profile_scope as scope_mod  # noqa: PLC0415

    monkeypatch.setattr(scope_mod, "_logged_once", set())
    monkeypatch.setattr(scope_mod, "_profile_homes_seen", set())
    monkeypatch.setattr(scope_mod, "_instance_owners", {})
    mm = sys.modules.get("agent.memory_manager")
    fn = getattr(mm, "claim_instance", None) if mm is not None else None
    while getattr(fn, "__wrapped__", None) is not None:
        fn = fn.__wrapped__
    if callable(fn):
        monkeypatch.setattr(mm, "claim_instance", fn)
    return scope_mod


@pytest.fixture
def root_is(monkeypatch, homes):
    monkeypatch.setattr(
        profiles_api, "get_hermes_home_for_profile",
        lambda name: homes[name if name and name != "default" else "default"],
    )
    return homes


def _agent_without_claim_seam(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.memory_manager", types.ModuleType("agent.memory_manager"))


def make_claiming_agent_cls(provider_for_build):
    """Stand-in AIAgent that binds memory like ``MemoryManager.initialize_all``:
    ``agent.memory_manager.claim_instance`` (looked up per call) -> drop on refusal,
    else ``initialize``. Works with the real Agent module or the agent-free twin."""

    class ClaimingAgent:
        def __init__(self, session_id=None, skip_memory=False, **kwargs):
            self.session_id = session_id
            self.skip_memory = skip_memory
            self._memory_manager = None
            if not skip_memory:
                provider = provider_for_build()
                home = str(sys.modules["hermes_constants"].get_hermes_home())
                identity = _profile_identity(home)
                if sys.modules["agent.memory_manager"].claim_instance(provider, home):
                    provider.initialize(session_id, hermes_home=home, agent_identity=identity)
                    self._memory_manager = FakeMemoryManager(provider, home, identity)

        def run_conversation(self, *args, **kwargs):
            raise RuntimeError(SENTINEL)

    return ClaimingAgent


def _memory_off_frames(events):
    return [
        data for event, data, *_ in events
        if event == "warning" and isinstance(data, dict) and data.get("type") == "memory_off"
    ]


def _memory_ok_frames(events):
    return [
        data for event, data, *_ in events
        if event == "memory_status" and isinstance(data, dict) and data.get("state") == "ok"
    ]


def _assert_no_paths(payload, homes):
    blob = json.dumps(payload)
    for home in homes.values():
        assert str(home) not in blob, f"memory status leaks a path: {blob}"
    assert set(payload) <= _ALLOWED_KEYS, f"unexpected keys: {sorted(set(payload) - _ALLOWED_KEYS)}"


# ── the per-agent status, case by case ────────────────────────────────────────

def test_legacy_agent_reports_all_memory_off(legacy_hc, homes, scope):
    """(a) Agent < v0.18.0: every memory store is off for every profile."""
    provider = SharedInstanceMemoryProvider(homes["default"])
    agent, cacheable = scope.construct_profile_scoped_agent(
        make_agent_cls(provider), {"session_id": "alpha-s"},
        profile_home=str(homes["alpha"]), session_id="alpha-s",
    )
    assert cacheable is True and agent._memory_manager is None  # #8585 behaviour, unchanged
    assert scope.memory_status_for_agent(agent) == {
        "state": "off", "reason": "agent_outdated", "scope": "all",
    }


def test_agent_without_claim_seam_serving_several_profiles_reports_external_off(
    hc, homes, root_is, scope, monkeypatch
):
    """(b) Agent without ``claim_instance`` and several profiles: external providers
    off, built-in memory still on."""
    _agent_without_claim_seam(monkeypatch)
    provider = SharedInstanceMemoryProvider(homes["default"])
    agent, cacheable = scope.construct_profile_scoped_agent(
        make_agent_cls(provider), {"session_id": "alpha-s"},
        profile_home=str(homes["alpha"]), session_id="alpha-s",
    )
    assert cacheable is True and agent._memory_manager is None
    assert scope.memory_status_for_agent(agent) == {
        "state": "external_off", "reason": "agent_no_instance_claim", "scope": "external",
    }


def test_shared_provider_owned_by_another_profile_reports_external_off(hc, homes, root_is, scope):
    """(c) the first profile owns the shared instance; the second runs without it
    until the WebUI restarts. Only the second is told."""
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = make_claiming_agent_cls(lambda: provider)
    alpha, alpha_cacheable = scope.construct_profile_scoped_agent(
        cls, {"session_id": "alpha-s"}, profile_home=str(homes["alpha"]), session_id="alpha-s")
    beta, beta_cacheable = scope.construct_profile_scoped_agent(
        cls, {"session_id": "beta-s"}, profile_home=str(homes["beta"]), session_id="beta-s")

    assert alpha._memory_manager is not None and beta._memory_manager is None  # #8585 guard
    assert alpha_cacheable is True and beta_cacheable is True
    assert scope.memory_status_for_agent(alpha) is None
    assert scope.memory_status_for_agent(beta) == {
        "state": "external_off", "reason": "provider_shared_instance", "scope": "external",
    }
    # The owner keeps it on later builds; a refusal is never attributed to it.
    alpha2, _ = scope.construct_profile_scoped_agent(
        cls, {"session_id": "alpha-s2"}, profile_home=str(homes["alpha"]), session_id="alpha-s2")
    assert alpha2._memory_manager is not None and scope.memory_status_for_agent(alpha2) is None


def test_current_agent_and_single_owner_report_nothing(hc, homes, root_is, scope):
    provider = SharedInstanceMemoryProvider(homes["default"])
    agent, cacheable = scope.construct_profile_scoped_agent(
        make_claiming_agent_cls(lambda: provider), {"session_id": "alpha-s"},
        profile_home=str(homes["alpha"]), session_id="alpha-s",
    )
    assert cacheable is True and agent._memory_manager is not None
    assert scope.memory_status_for_agent(agent) is None
    assert scope.memory_notice_payload(agent, session_id="alpha-s", stream_id="st") is None


def test_turn_level_scope_failure_is_not_reported(hc, homes, root_is, scope):
    """(d) memory off for this one turn only: not cached, no notice (the next turn
    normally has memory again, and the user has nothing to act on)."""
    provider = SharedInstanceMemoryProvider(homes["default"])
    agent, cacheable = scope.construct_profile_scoped_agent(
        make_claiming_agent_cls(lambda: provider), {"session_id": "alpha-s"},
        profile_home=str(homes["alpha"]), session_id="alpha-s",
        fail_closed_reason="the turn-level profile home override could not be set",
    )
    assert cacheable is False and agent._memory_manager is None
    assert scope.memory_status_for_agent(agent) is None


def test_status_reader_tolerates_foreign_and_mock_agents(scope):
    assert scope.memory_status_for_agent(None) is None
    assert scope.memory_status_for_agent(MagicMock()) is None
    assert scope.memory_status_for_agent(types.SimpleNamespace()) is None
    for forged in ("/etc/passwd", {"reason": "agent_outdated"}, 1):
        assert scope.memory_status_for_agent(types.SimpleNamespace(_webui_memory_status=forged)) is None


def test_notice_payload_shape_has_no_paths(legacy_hc, homes, scope):
    agent, _ = scope.construct_profile_scoped_agent(
        make_agent_cls(SharedInstanceMemoryProvider(homes["default"])), {"session_id": "alpha-s"},
        profile_home=str(homes["alpha"]), session_id="alpha-s",
    )
    payload = scope.memory_notice_payload(agent, session_id="alpha-s", stream_id="st-1")
    assert payload["type"] == "memory_off"
    assert payload["session_id"] == "alpha-s" and payload["stream_id"] == "st-1"
    assert payload["reason"] == "agent_outdated" and payload["scope"] == "all"
    assert payload["message"].startswith("Memory is off")
    _assert_no_paths(payload, homes)


# ── through the streaming path: one ``warning``/``memory_off`` frame per turn ──

def test_stream_reports_legacy_agent_memory_off(legacy_hc, homes, run_turn, scope):
    provider = SharedInstanceMemoryProvider(homes["default"])
    session_id, events = run_turn(profile="alpha", agent_cls=make_agent_cls(provider))
    frames = _memory_off_frames(events)
    assert len(frames) == 1, frames
    assert frames[0]["reason"] == "agent_outdated" and frames[0]["session_id"] == session_id
    _assert_no_paths(frames[0], homes)


def test_stream_reports_no_claim_seam_multi_profile(hc, homes, run_turn, scope, monkeypatch):
    _agent_without_claim_seam(monkeypatch)
    provider = SharedInstanceMemoryProvider(homes["default"])
    session_id, events = run_turn(profile="alpha", agent_cls=make_agent_cls(provider))
    frames = _memory_off_frames(events)
    assert [f["reason"] for f in frames] == ["agent_no_instance_claim"]
    assert frames[0]["scope"] == "external"
    _assert_no_paths(frames[0], homes)


def test_stream_reports_shared_provider_only_to_the_refused_profile(hc, homes, run_turn, scope):
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = make_claiming_agent_cls(lambda: provider)
    _alpha_sid, alpha_events = run_turn(profile="alpha", agent_cls=cls)
    beta_sid, beta_events = run_turn(profile="beta", agent_cls=cls)
    assert _memory_off_frames(alpha_events) == []
    frames = _memory_off_frames(beta_events)
    assert [f["reason"] for f in frames] == ["provider_shared_instance"]
    assert frames[0]["session_id"] == beta_sid
    # The memoryless agent is cached, so the status rides along with it.
    cached = _cached_agent(beta_sid)
    assert cached is not None and cached._memory_manager is None
    assert scope.memory_status_for_agent(cached)["reason"] == "provider_shared_instance"
    _assert_no_paths(frames[0], homes)


def test_stream_reports_explicit_ok_for_a_healthy_session(hc, homes, run_turn, scope):
    """Codex r1 finding 2: clearing is driven by an explicit healthy status, never by
    the absence of a warning."""
    provider = SharedInstanceMemoryProvider(homes["default"])
    sid, events = run_turn(profile="alpha", agent_cls=make_claiming_agent_cls(lambda: provider))
    assert _memory_off_frames(events) == []
    ok = _memory_ok_frames(events)
    assert len(ok) == 1, events
    assert ok[0] == {"type": "memory_status", "state": "ok", "session_id": sid, "stream_id": f"{sid}-stream"}
    assert set(ok[0]) <= _OK_KEYS


def _with_builtin(cls):
    class WithBuiltin(cls):
        def __init__(self, session_id=None, skip_memory=False, **kwargs):
            super().__init__(session_id=session_id, skip_memory=skip_memory, **kwargs)
            self._memory_store = object()

    return WithBuiltin


def _fixed_session(monkeypatch, tag):
    monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex=tag))


def _break_turn_scope(hc, monkeypatch):
    monkeypatch.setattr(
        hc, "set_hermes_home_override",
        lambda home: (_ for _ in ()).throw(RuntimeError("transient scope failure")),
    )


def test_transient_turn_reports_the_cached_agents_lasting_reason(hc, homes, run_turn, scope, monkeypatch):
    """Codex r1 finding 2 (backend): off -> transient -> off. The transient turn runs a
    one-turn memoryless agent while the session's cached agent keeps its lasting
    reason; it reports that reason (never a healthy status), so the notice stays."""
    singleton = SharedInstanceMemoryProvider(homes["default"])
    cls = _with_builtin(make_claiming_agent_cls(lambda: singleton))
    run_turn(profile="alpha", agent_cls=cls)
    _fixed_session(monkeypatch, "transient-notice")
    sid, first = run_turn(profile="beta", agent_cls=cls)
    cached = _cached_agent(sid)
    assert [f["reason"] for f in _memory_off_frames(first)] == ["provider_shared_instance"]
    setter = hc.set_hermes_home_override
    _break_turn_scope(hc, monkeypatch)
    _, transient = run_turn(profile="beta", agent_cls=cls)
    assert _cached_agent(sid) is cached, "the transient turn must not replace the cached agent"
    frames = _memory_off_frames(transient)
    assert [f["reason"] for f in frames] == ["provider_shared_instance"], transient
    assert frames[0]["session_id"] == sid
    _assert_no_paths(frames[0], homes)
    assert _memory_ok_frames(transient) == [], "a transient turn is never reported healthy"
    monkeypatch.setattr(hc, "set_hermes_home_override", setter)
    _, third = run_turn(profile="beta", agent_cls=cls)
    assert [f["reason"] for f in _memory_off_frames(third)] == ["provider_shared_instance"]
    assert _memory_ok_frames(third) == []


def test_transient_turn_of_a_healthy_session_reports_nothing(hc, homes, run_turn, scope, monkeypatch):
    """Status unknown for the turn: neither memory_off nor healthy (the UI keeps
    whatever it shows)."""
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = _with_builtin(make_claiming_agent_cls(lambda: provider))
    _fixed_session(monkeypatch, "transient-healthy")
    sid, first = run_turn(profile="alpha", agent_cls=cls)
    assert len(_memory_ok_frames(first)) == 1
    _break_turn_scope(hc, monkeypatch)
    _, transient = run_turn(profile="alpha", agent_cls=cls)
    assert _cached_agent(sid) is not None
    assert _memory_off_frames(transient) == [] and _memory_ok_frames(transient) == [], transient


def test_recovered_agent_reports_healthy(hc, homes, run_turn, scope, monkeypatch):
    """A session whose memory was off gets an explicit healthy status once its agent
    is rebuilt with memory (here: the WebUI restarted, so no profile owns the shared
    instance any more and the session's agent cache is empty)."""
    singleton = SharedInstanceMemoryProvider(homes["default"])
    cls = _with_builtin(make_claiming_agent_cls(lambda: singleton))
    run_turn(profile="alpha", agent_cls=cls)
    _fixed_session(monkeypatch, "recovered-notice")
    sid, before = run_turn(profile="beta", agent_cls=cls)
    assert [f["reason"] for f in _memory_off_frames(before)] == ["provider_shared_instance"]
    assert _memory_ok_frames(before) == []
    # "Restart": nothing owns the shared instance, nothing is cached.
    monkeypatch.setattr(scope, "_instance_owners", {})
    with config_mod.SESSION_AGENT_CACHE_LOCK:
        config_mod.SESSION_AGENT_CACHE.pop(sid, None)
    _, after = run_turn(profile="beta", agent_cls=cls)
    rebuilt = _cached_agent(sid)
    assert rebuilt is not None and rebuilt._memory_manager is not None
    assert _memory_off_frames(after) == []
    assert [f["session_id"] for f in _memory_ok_frames(after)] == [sid]


# ── reload / reattach: the run journal snapshot carries the status ────────────

def test_journal_snapshot_carries_the_latest_memory_status(tmp_path, monkeypatch, scope):
    """Codex r1 finding 1: a reload that resumes the live stream after the warning's
    seq still learns the status, from the recovery snapshot it already restores."""
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    sid, run_id = "notice-journal-session", "notice-journal-run"
    writer = RunJournalWriter(sid, run_id)
    writer.append_sse_event("token", {"text": "Working"})
    off = {
        "type": "memory_off", "session_id": sid, "stream_id": run_id,
        "reason": "provider_shared_instance", "scope": "external", "message": "x",
    }
    writer.append_sse_event("warning", off)
    writer.append_sse_event("warning", {"type": "fallback", "message": "unrelated"})
    for _ in range(20):
        writer.append_sse_event("token", {"text": "."})
    snapshot = routes._run_journal_live_snapshot(run_id)
    expected = {"state": "external_off", "reason": "provider_shared_instance", "scope": "external"}
    assert snapshot["memory_status"] == expected
    assert routes._runtime_journal_snapshot_for_session_payload(snapshot)["memory_status"] == expected
    assert scope.memory_status_from_journal_events(
        [{"event": "memory_status", "payload": {"type": "memory_status", "state": "ok"}}]
    ) == {"state": "ok"}


def test_journal_snapshot_memory_status_absent_or_forged(scope):
    read = scope.memory_status_from_journal_events
    assert read([]) is None
    assert read([{"event": "token", "payload": {"text": "x"}}]) is None
    forged = {"event": "warning", "payload": {"type": "memory_off", "reason": "/etc/passwd"}}
    assert read([forged]) is None
    off = {"event": "warning", "payload": {"type": "memory_off", "reason": "agent_outdated"}}
    ok = {"event": "memory_status", "payload": {"type": "memory_status", "state": "ok"}}
    assert read([off, ok]) == {"state": "ok"}
    assert read([ok, off]) == {"state": "off", "reason": "agent_outdated", "scope": "all"}
