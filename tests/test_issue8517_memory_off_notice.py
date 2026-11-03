"""#8517 follow-up: the server reports why memory is off for the agent serving a session.

#8585 made memory fail closed whenever a profile's agent cannot be held to that
profile's memory, and only logged a server WARNING. The UI shows a quiet notice; this
file pins the server side of it. The status is SERVER-AUTHORITATIVE and describes the
agent actually serving the session (the session's cached agent), never a per-turn
transient state:

* ``construct_profile_scoped_agent`` records on the agent it returns a lasting status:
  ``agent_outdated`` (Agent < v0.18.0, all memory off), ``agent_no_instance_claim``
  (Agent without ``claim_instance`` serving several profiles, external providers off),
  ``provider_shared_instance`` (a shared provider instance another profile owns,
  external provider off), or healthy. A one-turn fail-closed build records nothing.
* ``session_memory_status`` reports the cached agent's recorded status; without a
  cached agent, only what a new build would provably get (``agent_outdated``, and
  ``agent_no_instance_claim`` when several profiles exist), else nothing (unknown).
* The session payload (GET /api/session and the ``session`` of stream ``done`` /
  ``apperror``) carries it as ``memory_status``; each turn also sends it as a stream
  event (``warning``/``memory_off`` or ``memory_status``/``ok``), and both credential
  self-heal retries send the replacement agent's status.
* Codes only: no paths, homes or profile names.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 modules)
from __future__ import annotations

import json
import sys
import types
import uuid
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import api.config as config_mod
import api.models as models
import api.profiles as profiles_api
import api.routes as routes
import api.streaming as streaming
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
from tests.test_steer_worker_boundaries import worker_scene  # noqa: F401  (fixture)

_EVENT_KEYS = {"type", "session_id", "stream_id", "reason", "scope", "message"}
_OK_KEYS = {"type", "state", "session_id", "stream_id"}
_STATUS_KEYS = {"state", "reason", "scope"}
OFF_OUTDATED = {"state": "off", "reason": "agent_outdated", "scope": "all"}
OFF_NO_CLAIM = {"state": "off", "reason": "agent_no_instance_claim", "scope": "external"}
OFF_SHARED = {"state": "off", "reason": "provider_shared_instance", "scope": "external"}
OK = {"state": "ok"}


@pytest.fixture
def scope(monkeypatch):
    """Fresh #8517 bookkeeping: warn-once keys, served homes, shared-instance owners,
    an empty agent cache, and the Agent's own (unwrapped) ``claim_instance``."""
    import api.memory_profile_scope as scope_mod  # noqa: PLC0415

    monkeypatch.setattr(scope_mod, "_logged_once", set())
    monkeypatch.setattr(scope_mod, "_profile_homes_seen", set())
    monkeypatch.setattr(scope_mod, "_instance_owners", {})
    monkeypatch.setattr(config_mod, "SESSION_AGENT_CACHE", OrderedDict())
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
    else ``initialize``."""

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


def _status_frames(events):
    """Every memory status frame of a turn, in order: ``("off", reason)`` / ``("ok", None)``."""
    out = []
    for event, data, *_ in events:
        if not isinstance(data, dict):
            continue
        if event == "warning" and data.get("type") == "memory_off":
            out.append(("off", data.get("reason")))
        elif event == "memory_status" and data.get("state") == "ok":
            out.append(("ok", None))
    return out


def _frames_of(events, kind):
    return [
        data for event, data, *_ in events
        if isinstance(data, dict) and (
            (kind == "off" and event == "warning" and data.get("type") == "memory_off")
            or (kind == "ok" and event == "memory_status")
        )
    ]


def _assert_codes_only(payload, homes, allowed):
    blob = json.dumps(payload)
    for home in homes.values():
        assert str(home) not in blob, f"memory status leaks a path: {blob}"
    # The test harness's session ids carry the profile name; nothing else may.
    rest = json.dumps({k: v for k, v in payload.items() if k not in ("session_id", "stream_id")})
    for name in ("alpha", "beta"):
        assert name not in rest, f"memory status leaks a profile name: {rest}"
    assert set(payload) <= allowed, f"unexpected keys: {sorted(set(payload) - allowed)}"


def _cache(session_id, agent):
    with config_mod.SESSION_AGENT_CACHE_LOCK:
        config_mod.SESSION_AGENT_CACHE[session_id] = (agent, "sig")


def _build(scope, cls, homes, profile, sid, **kw):
    return scope.construct_profile_scoped_agent(
        cls, {"session_id": sid}, profile_home=str(homes[profile]), session_id=sid, **kw)


# ── the status each construction records ──────────────────────────────────────

def test_legacy_agent_records_all_memory_off(legacy_hc, homes, scope):
    agent, cacheable = _build(scope, make_agent_cls(SharedInstanceMemoryProvider(homes["default"])), homes, "alpha", "a-s")
    assert cacheable is True and agent._memory_manager is None  # #8585 behaviour, unchanged
    assert scope.memory_status_for_agent(agent) == OFF_OUTDATED


def test_agent_without_claim_seam_serving_several_profiles_records_external_off(hc, homes, root_is, scope, monkeypatch):
    _agent_without_claim_seam(monkeypatch)
    agent, cacheable = _build(scope, make_agent_cls(SharedInstanceMemoryProvider(homes["default"])), homes, "alpha", "a-s")
    assert cacheable is True and agent._memory_manager is None
    assert scope.memory_status_for_agent(agent) == OFF_NO_CLAIM


def test_shared_provider_owned_by_another_profile_records_external_off(hc, homes, root_is, scope):
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = make_claiming_agent_cls(lambda: provider)
    alpha, _ = _build(scope, cls, homes, "alpha", "a-s")
    beta, beta_cacheable = _build(scope, cls, homes, "beta", "b-s")
    assert alpha._memory_manager is not None and beta._memory_manager is None  # #8585 guard
    assert beta_cacheable is True
    assert scope.memory_status_for_agent(alpha) == OK
    assert scope.memory_status_for_agent(beta) == OFF_SHARED
    alpha2, _ = _build(scope, cls, homes, "alpha", "a-s2")
    assert scope.memory_status_for_agent(alpha2) == OK


def test_turn_level_scope_failure_records_nothing(hc, homes, root_is, scope):
    agent, cacheable = _build(
        scope, make_claiming_agent_cls(lambda: SharedInstanceMemoryProvider(homes["default"])), homes,
        "alpha", "a-s", fail_closed_reason="the turn-level profile home override could not be set")
    assert cacheable is False and agent._memory_manager is None
    assert scope.memory_status_for_agent(agent) is None


def test_status_reader_tolerates_foreign_and_forged_agents(scope):
    assert scope.memory_status_for_agent(None) is None
    assert scope.memory_status_for_agent(MagicMock()) is None
    assert scope.memory_status_for_agent(types.SimpleNamespace()) is None
    for forged in ("/etc/passwd", {"reason": "agent_outdated"}, 1, "OK"):
        assert scope.memory_status_for_agent(types.SimpleNamespace(_webui_memory_status=forged)) is None


# ── what the session reports ──────────────────────────────────────────────────

@pytest.mark.parametrize("status", [OFF_OUTDATED, OFF_NO_CLAIM, OFF_SHARED, OK])
def test_session_reports_its_cached_agents_status_codes_only(hc, homes, root_is, scope, status):
    agent = SimpleNamespace(_webui_memory_status=status.get("reason", "ok"))
    _cache("s-1", agent)
    reported = scope.session_memory_status("s-1", str(homes["alpha"]))
    assert reported == status
    _assert_codes_only(reported, homes, _STATUS_KEYS)


def test_session_without_cached_agent_reports_only_a_provable_new_build(hc, homes, root_is, scope, monkeypatch):
    # Current Agent (claim seam present): a new build could still be refused a shared
    # instance or fail for the turn, so nothing is known.
    assert scope.session_memory_status("none", str(homes["alpha"])) is None
    # Agent without claim_instance and several profiles: every new build is external-off.
    _agent_without_claim_seam(monkeypatch)
    assert scope.session_memory_status("none", str(homes["alpha"])) == OFF_NO_CLAIM
    assert scope.session_memory_status("none", str(homes["default"])) == OFF_NO_CLAIM
    assert scope._profile_homes_seen == set(), "a status read must never count as serving a profile"
    assert scope.session_memory_status("none", "") is None


def test_session_without_cached_agent_on_a_legacy_agent_reports_outdated(legacy_hc, homes, root_is, scope):
    assert scope.session_memory_status("none", str(homes["beta"])) == OFF_OUTDATED


def test_no_claim_single_profile_root_is_unknown_without_a_cached_agent(hc, homes, root_is, scope, monkeypatch):
    _agent_without_claim_seam(monkeypatch)
    homes["alpha"].rmdir()
    homes["beta"].rmdir()
    assert scope.session_memory_status("none", str(homes["default"])) is None


def test_cached_root_agent_keeps_its_own_status_after_a_second_profile_appears(hc, homes, root_is, scope, monkeypatch):
    """api/memory_profile_scope.py serves_single_profile: a root agent built while the
    root was the only profile keeps external memory on; it is reported as it is, while
    a session without a cached agent reports what a new build would get."""
    _agent_without_claim_seam(monkeypatch)
    homes["alpha"].rmdir()
    homes["beta"].rmdir()
    provider = SharedInstanceMemoryProvider(homes["default"])
    agent, cacheable = _build(scope, make_agent_cls(provider), homes, "default", "root-s")
    assert cacheable is True and agent._memory_manager is not None
    _cache("root-s", agent)
    assert scope.session_memory_status("root-s", str(homes["default"])) == OK
    homes["alpha"].mkdir()  # a named profile is created later
    assert scope.session_memory_status("root-s", str(homes["default"])) == OK
    assert scope.session_memory_status("root-new", str(homes["default"])) == OFF_NO_CLAIM
    later, _ = _build(scope, make_agent_cls(provider), homes, "default", "root-new")
    assert scope.memory_status_for_agent(later) == OFF_NO_CLAIM


def test_stream_event_payloads_are_codes_only(legacy_hc, homes, scope):
    event, payload = scope.memory_status_event(OFF_OUTDATED, session_id="s-1", stream_id="st-1")
    assert event == "warning" and payload["type"] == "memory_off"
    assert (payload["session_id"], payload["stream_id"], payload["reason"], payload["scope"]) == (
        "s-1", "st-1", "agent_outdated", "all")
    assert payload["message"].startswith("Memory is off")
    _assert_codes_only(payload, homes, _EVENT_KEYS)
    event, payload = scope.memory_status_event(OK, session_id="s-1", stream_id="st-1")
    assert (event, payload) == ("memory_status", {"type": "memory_status", "state": "ok", "session_id": "s-1", "stream_id": "st-1"})
    assert scope.memory_status_event(None, session_id="s-1", stream_id="st-1") is None
    assert scope.memory_status_event({"state": "off", "reason": "/etc"}, session_id="s", stream_id="t") is None


# ── per turn, through the streaming path ──────────────────────────────────────

def test_stream_reports_legacy_agent_memory_off(legacy_hc, homes, run_turn, scope):
    sid, events = run_turn(profile="alpha", agent_cls=make_agent_cls(SharedInstanceMemoryProvider(homes["default"])))
    assert _status_frames(events) == [("off", "agent_outdated")]
    frame = _frames_of(events, "off")[0]
    assert frame["session_id"] == sid
    _assert_codes_only(frame, homes, _EVENT_KEYS)


def test_stream_reports_shared_provider_only_to_the_refused_profile(hc, homes, run_turn, scope):
    provider = SharedInstanceMemoryProvider(homes["default"])
    cls = make_claiming_agent_cls(lambda: provider)
    _, alpha_events = run_turn(profile="alpha", agent_cls=cls)
    beta_sid, beta_events = run_turn(profile="beta", agent_cls=cls)
    assert _status_frames(alpha_events) == [("ok", None)]
    assert _status_frames(beta_events) == [("off", "provider_shared_instance")]
    assert scope.memory_status_for_agent(_cached_agent(beta_sid)) == OFF_SHARED
    ok = _frames_of(alpha_events, "ok")[0]
    assert set(ok) <= _OK_KEYS


def _fixed_session(monkeypatch, tag):
    monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex=tag))


def _break_turn_scope(hc, monkeypatch):
    monkeypatch.setattr(
        hc, "set_hermes_home_override",
        lambda home: (_ for _ in ()).throw(RuntimeError("transient scope failure")),
    )


def _with_builtin(cls):
    class WithBuiltin(cls):
        def __init__(self, session_id=None, skip_memory=False, **kwargs):
            super().__init__(session_id=session_id, skip_memory=skip_memory, **kwargs)
            self._memory_store = object()

    return WithBuiltin


def test_transient_turn_never_changes_the_lasting_status(hc, homes, run_turn, scope, monkeypatch):
    singleton = SharedInstanceMemoryProvider(homes["default"])
    cls = _with_builtin(make_claiming_agent_cls(lambda: singleton))
    run_turn(profile="alpha", agent_cls=cls)
    _fixed_session(monkeypatch, "transient-off")
    sid, first = run_turn(profile="beta", agent_cls=cls)
    cached = _cached_agent(sid)
    assert _status_frames(first) == [("off", "provider_shared_instance")]
    _break_turn_scope(hc, monkeypatch)
    _, transient = run_turn(profile="beta", agent_cls=cls)
    assert _cached_agent(sid) is cached
    assert _status_frames(transient) == [("off", "provider_shared_instance")], transient


def test_transient_turn_of_a_healthy_session_stays_healthy(hc, homes, run_turn, scope, monkeypatch):
    cls = _with_builtin(make_claiming_agent_cls(lambda: SharedInstanceMemoryProvider(homes["default"])))
    _fixed_session(monkeypatch, "transient-ok")
    sid, first = run_turn(profile="alpha", agent_cls=cls)
    assert _status_frames(first) == [("ok", None)]
    _break_turn_scope(hc, monkeypatch)
    _, transient = run_turn(profile="alpha", agent_cls=cls)
    assert _cached_agent(sid) is not None
    assert _status_frames(transient) == [("ok", None)], transient


def test_transient_turn_without_a_cached_agent_reports_nothing(hc, homes, run_turn, scope, monkeypatch):
    _break_turn_scope(hc, monkeypatch)
    _, events = run_turn(profile="alpha", agent_cls=make_claiming_agent_cls(lambda: SharedInstanceMemoryProvider(homes["default"])))
    assert _status_frames(events) == []


# ── credential self-heal: the replacement agent's status (Codex r4 finding 1) ──

def _self_heal_scene(scene, hc, homes, scope, monkeypatch, failure_path, providers, *, claim=True):
    base = streaming._get_ai_agent()
    created = []

    class Agent(base):
        def __init__(self, session_id=None, skip_memory=False, **kwargs):
            super().__init__(session_id=session_id, **kwargs)
            self._memory_manager = None
            provider = providers()
            home = str(hc.get_hermes_home())
            mm = sys.modules["agent.memory_manager"]
            claimed = (not claim) or mm.claim_instance(provider, home)
            if not skip_memory and claimed:
                provider.initialize(session_id, hermes_home=home, agent_identity=_profile_identity(home))
                self._memory_manager = FakeMemoryManager(provider, home, _profile_identity(home))
            created.append(self)

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: Agent)
    monkeypatch.setattr(
        streaming, "_attempt_credential_self_heal",
        lambda *a, **k: {"provider": "openai", "api_key": "synthetic", "base_url": "http://127.0.0.1:1/v1"},
    )
    return created


def _fail_first_run(scene, created, failure_path, before=None):
    def run_hook():
        if len(created) == 1:
            if before is not None:
                before()
            if failure_path == "exception":
                raise RuntimeError("401 unauthorized")
            scene.result = {"error": "401 unauthorized", "messages": []}
        else:
            scene.result = None
    scene.on_run = run_hook


@pytest.mark.parametrize("failure_path", ["exception", "returned"])
@pytest.mark.parametrize("direction", ["on-off", "off-on"])
def test_self_heal_publishes_the_replacement_agents_status(
    worker_scene, hc, homes, root_is, scope, monkeypatch, failure_path, direction
):
    scene = worker_scene
    singleton = SharedInstanceMemoryProvider(homes["default"])
    owner, _ = _build(scope, make_claiming_agent_cls(lambda: singleton), homes, "alpha", "owner")
    assert owner._memory_manager is not None  # alpha owns the shared instance
    private = SharedInstanceMemoryProvider(homes["default"])
    order = iter([private, singleton] if direction == "on-off" else [singleton, private])
    created = _self_heal_scene(scene, hc, homes, scope, monkeypatch, failure_path, lambda: next(order))
    _fail_first_run(scene, created, failure_path)
    scene.run()
    events = list(scene.events.queue)
    assert len(created) == 2 and scene.calls.count("run") == 2
    assert config_mod.SESSION_AGENT_CACHE["original"][0] is created[-1]
    expected = [("ok", None), ("off", "provider_shared_instance")]
    if direction == "off-on":
        expected.reverse()
    assert _status_frames(events) == expected, events
    assert scope.memory_status_for_agent(created[-1]) == (OFF_SHARED if direction == "on-off" else OK)


@pytest.mark.parametrize("failure_path", ["exception", "returned"])
def test_self_heal_after_a_profile_appears_reports_no_instance_claim(
    worker_scene, hc, homes, root_is, scope, monkeypatch, failure_path
):
    scene = worker_scene
    homes["alpha"].rmdir()
    homes["beta"].rmdir()
    _agent_without_claim_seam(monkeypatch)
    created = _self_heal_scene(
        scene, hc, homes, scope, monkeypatch, failure_path,
        lambda: SharedInstanceMemoryProvider(homes["default"]), claim=False)
    _fail_first_run(scene, created, failure_path, before=lambda: homes["alpha"].mkdir())
    scene.run()
    events = list(scene.events.queue)
    assert len(created) == 2 and scene.calls.count("run") == 2
    assert _status_frames(events) == [("ok", None), ("off", "agent_no_instance_claim")], events
    assert config_mod.SESSION_AGENT_CACHE["original"][0] is created[-1]


# ── the session payloads the UI loads ─────────────────────────────────────────

def _saved_session(tmp_path, monkeypatch, sid, profile="alpha"):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    session = models.Session(session_id=sid, workspace=str(tmp_path), model="m", messages=[], context_messages=[])
    session.profile = profile
    session.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS.pop(sid, None)
    return session


def test_stream_terminal_session_payload_carries_the_status(tmp_path, hc, homes, root_is, scope, monkeypatch):
    """``done`` and ``apperror`` embed ``_session_payload_with_full_messages(s)``; after a
    compression ``s`` is the continuation and the cached agent moved to its id."""
    continuation = _saved_session(tmp_path, monkeypatch, "s2-continuation")
    assert "memory_status" not in streaming._session_payload_with_full_messages(continuation)
    _cache("s2-continuation", SimpleNamespace(_webui_memory_status="provider_shared_instance"))
    payload = streaming._session_payload_with_full_messages(continuation)
    assert payload["memory_status"] == OFF_SHARED
    _cache("s2-continuation", SimpleNamespace(_webui_memory_status="ok"))
    assert streaming._session_payload_with_full_messages(continuation)["memory_status"] == OK


def _get_session_payload(monkeypatch, sid):
    import api.session_ops  # noqa: PLC0415

    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_: True)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *_: {})
    monkeypatch.setattr(routes, "find_run_summary", lambda *_: None)
    monkeypatch.setattr(config_mod, "load_settings", lambda: {"api_redact_enabled": False})
    monkeypatch.setattr(api.session_ops, "regeneration_state", lambda _session: ([], []))
    monkeypatch.setattr(api.session_ops, "regeneration_authority", lambda *_a, **_k: None)
    response = {}
    monkeypatch.setattr(
        routes, "j",
        lambda _handler, payload, status=200, **_kwargs: response.update(payload=payload, status=status) or payload,
    )
    routes._handle_session_get(None, SimpleNamespace(path="/api/session", query=f"session_id={sid}&resolve_model=0"))
    assert response["status"] == 200, response
    return response["payload"]["session"]


def test_get_session_carries_the_status_for_an_idle_reload(tmp_path, hc, homes, root_is, scope, monkeypatch):
    _saved_session(tmp_path, monkeypatch, "idle-s")
    assert "memory_status" not in _get_session_payload(monkeypatch, "idle-s")  # unknown: omitted
    _cache("idle-s", SimpleNamespace(_webui_memory_status="agent_no_instance_claim"))
    status = _get_session_payload(monkeypatch, "idle-s")["memory_status"]
    assert status == OFF_NO_CLAIM
    _assert_codes_only(status, homes, _STATUS_KEYS)


def test_get_session_predicts_an_outdated_agent_without_a_cached_agent(tmp_path, legacy_hc, homes, root_is, scope, monkeypatch):
    _saved_session(tmp_path, monkeypatch, "legacy-s")
    assert _get_session_payload(monkeypatch, "legacy-s")["memory_status"] == OFF_OUTDATED


# --- Opus r1 should-fix: never import the Agent runtime from a request thread before it is bound ---

def _spy_import_module(monkeypatch, replacement=None):
    import importlib
    imported = []
    real_import = importlib.import_module

    def spy(name, *args, **kwargs):
        imported.append(name)
        if replacement is not None and name == "agent.memory_manager":
            return replacement
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", spy)
    return imported


def test_claim_seam_probe_never_imports_the_agent_runtime_before_it_is_bound(monkeypatch):
    """The first cache-miss GET /api/session after boot must not import agent.memory_manager
    (measured 72 ms and 84 modules, including the tools registry singleton) on a request thread."""
    from api import memory_profile_scope as mps
    monkeypatch.delitem(sys.modules, "agent.memory_manager", raising=False)
    monkeypatch.delitem(sys.modules, "run_agent", raising=False)
    imported = _spy_import_module(monkeypatch)
    assert mps._claim_seam_available() is None
    assert "agent.memory_manager" not in imported
    assert "agent.memory_manager" not in sys.modules


def test_claim_seam_probe_uses_an_already_imported_memory_manager(monkeypatch):
    from api import memory_profile_scope as mps
    monkeypatch.delitem(sys.modules, "run_agent", raising=False)
    mm = types.ModuleType("agent.memory_manager")
    mm.claim_instance = lambda *args, **kwargs: True
    monkeypatch.setitem(sys.modules, "agent.memory_manager", mm)
    imported = _spy_import_module(monkeypatch)
    assert mps._claim_seam_available() is True
    _agent_without_claim_seam(monkeypatch)
    assert mps._claim_seam_available() is False
    assert imported == []


def test_claim_seam_probe_imports_once_the_agent_runtime_is_bound(monkeypatch):
    from api import memory_profile_scope as mps
    monkeypatch.delitem(sys.modules, "agent.memory_manager", raising=False)
    monkeypatch.setitem(sys.modules, "run_agent", types.ModuleType("run_agent"))
    imported = _spy_import_module(monkeypatch, replacement=types.ModuleType("agent.memory_manager"))
    assert mps._claim_seam_available() is False
    assert imported == ["agent.memory_manager"]
