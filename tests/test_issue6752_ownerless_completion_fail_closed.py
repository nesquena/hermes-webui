"""#6752 — background completions must FAIL CLOSED without an immutable owner.

Security boundary: a ``session_key`` -> WebUI-session mapping (the mutable
``PROCESS_SESSION_INDEX``) is a routing hint, not proof that the completion
belongs to that session. Owner decision (Nathan, 2026-10-21): a completion with
no verifiable immutable owner (``origin_ui_session_id`` on the event, or an
env-immune spawn owner on the recovered ``ProcessSession``) must never be
emitted to, injected into, or woken in a session selected by the session-key
mapping alone. It is parked in a neutral, session-less list and logged instead.
Completions that DO carry an immutable owner keep routing to that owner.

Both queue consumers are covered — the background drain thread
(``api.background_process._process_one``) and the next-turn drain
(``api.streaming._drain_webui_process_notifications``) — because they race on
the same queue and a fix on only one of them is still a bypass.
"""
from __future__ import annotations

import importlib
import logging
import queue
import sys
import threading
import types

import pytest

from tests._wakeup_helpers import FakeProcessRegistry
from tests._wakeup_helpers import install_fake_registry
from tests._wakeup_helpers import install_fake_start_session_turn
from tests._wakeup_helpers import wait_for_wakeup


SESS_A = "webui-tab-A"  # the true commissioning session
SESS_B = "webui-tab-B"  # the session a (foreign/stale) session_key maps to


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _peu():
    return importlib.import_module("api.process_event_utils")


def _parked():
    lister = getattr(_peu(), "list_parked_ownerless_completions", None)
    assert callable(lister), (
        "#6752: api.process_event_utils.list_parked_ownerless_completions() is "
        "missing — ownerless completions have no neutral parking place"
    )
    return lister()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    from api import background_process as bp
    from api import config as cfg

    def _reset():
        with cfg.PROCESS_SESSION_INDEX_LOCK:
            cfg.PROCESS_SESSION_INDEX.clear()
        cfg.PENDING_BG_TASK_COMPLETIONS.clear()
        cfg.BG_TASK_COMPLETE_EVENTS_SEEN.clear()
        with cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            cfg.DEFERRED_PROCESS_WAKEUPS.clear()
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.clear()
        if hasattr(cfg, "ACTIVE_RUNS"):
            with cfg.ACTIVE_RUNS_LOCK:
                cfg.ACTIVE_RUNS.clear()
        for name in ("_LAST_EMIT_TS", "_PENDING_EMIT_PAYLOADS", "_PENDING_EMIT_TIMERS"):
            if hasattr(bp, name):
                getattr(bp, name).clear()
        reset = getattr(_peu(), "_reset_parked_ownerless_completions_for_tests", None)
        if callable(reset):
            reset()

    _reset()
    monkeypatch.setattr(bp, "_EMIT_COALESCE_WINDOW_SECS", 0.0)
    yield
    _reset()


def _capture_emits(monkeypatch):
    from api import background_process as bp

    emits: list[tuple[str, str, dict]] = []

    def _capture(session_id, event, data):
        emits.append((session_id, event, data))
        return 1

    monkeypatch.setattr(bp, "_emit_to_session_streams", _capture)
    return emits


def _completion(process_id="proc_ownerless1", session_key=SESS_B, **extra):
    evt = {
        "type": "completion",
        "session_id": process_id,
        "session_key": session_key,
        "command": "echo secret-output",
        "exit_code": 0,
        "output": "SENSITIVE-OUTPUT-FROM-OTHER-SESSION",
    }
    evt.update(extra)
    return evt


def _registry_with(monkeypatch, *procs):
    fake = FakeProcessRegistry()
    for process_id, attrs in procs:
        fake._procs[process_id] = types.SimpleNamespace(**attrs)
    install_fake_registry(monkeypatch, fake)
    return fake


# ---------------------------------------------------------------------------
# Background consumer: _process_one
# ---------------------------------------------------------------------------

def test_ownerless_completion_mapped_by_session_key_is_not_delivered(monkeypatch):
    """THE #6752 bypass: the key maps to session B, no immutable owner exists.
    Nothing may be emitted to B, marked pending for B, deferred for B, or woken
    in B."""
    from api import background_process as bp
    from api import config as cfg

    _registry_with(monkeypatch, ("proc_ownerless1", {"session_key": SESS_B}))
    bp.register_process_session(SESS_B, SESS_B)
    emits = _capture_emits(monkeypatch)
    wakeups = install_fake_start_session_turn(monkeypatch)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)

    bp._process_one(_completion())

    assert not wait_for_wakeup(wakeups, timeout=0.5), (
        f"#6752 bypass: ownerless completion woke {wakeups['calls']!r} via the "
        "session-key mapping alone"
    )
    assert emits == [], f"#6752 bypass: ownerless completion emitted {emits!r}"
    assert SESS_B not in cfg.PENDING_BG_TASK_COMPLETIONS
    with cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
        assert not cfg.DEFERRED_PROCESS_WAKEUPS.get(SESS_B)


def test_ownerless_completion_is_not_deferred_into_a_busy_mapped_session(monkeypatch):
    """Same bypass on the active-turn branch: no deferred wakeup is recorded."""
    from api import background_process as bp
    from api import config as cfg

    _registry_with(monkeypatch, ("proc_ownerless2", {"session_key": SESS_B}))
    bp.register_process_session(SESS_B, SESS_B)
    _capture_emits(monkeypatch)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: True)

    bp._process_one(_completion(process_id="proc_ownerless2"))

    with cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
        assert not cfg.DEFERRED_PROCESS_WAKEUPS.get(SESS_B), (
            "#6752 bypass: ownerless completion was deferred into the mapped session"
        )


def test_ownerless_completion_is_parked_and_logged(monkeypatch, caplog):
    from api import background_process as bp

    _registry_with(monkeypatch, ("proc_ownerless3", {"session_key": SESS_B}))
    bp.register_process_session(SESS_B, SESS_B)
    _capture_emits(monkeypatch)
    install_fake_start_session_turn(monkeypatch)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)

    with caplog.at_level(logging.WARNING):
        bp._process_one(_completion(process_id="proc_ownerless3"))
        # A duplicate enqueue of the same completion parks once, logs once.
        bp._process_one(_completion(process_id="proc_ownerless3"))

    parked = _parked()
    assert len(parked) == 1, parked
    rec = parked[0]
    assert rec["delivery_id"] == "proc_ownerless3"
    assert rec["type"] == "completion"
    assert rec["reason"] == "missing_immutable_owner"
    assert rec["count"] == 2
    # Neutral place: the record never carries the raw output payload.
    assert "SENSITIVE-OUTPUT-FROM-OTHER-SESSION" not in repr(rec)

    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "proc_ownerless3" in r.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "SENSITIVE-OUTPUT-FROM-OTHER-SESSION" not in warnings[0].getMessage()


def test_completion_with_event_origin_still_delivers(monkeypatch):
    """An immutable owner on the event keeps working (and wins over the key)."""
    from api import background_process as bp

    _registry_with(monkeypatch, ("proc_owned1", {"session_key": SESS_B}))
    bp.register_process_session(SESS_B, SESS_B)
    emits = _capture_emits(monkeypatch)
    wakeups = install_fake_start_session_turn(monkeypatch)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)

    bp._process_one(_completion(process_id="proc_owned1", origin_ui_session_id=SESS_A))

    assert wait_for_wakeup(wakeups), "owned completion was not woken"
    assert [c["session_id"] for c in wakeups["calls"]] == [SESS_A]
    assert {sid for sid, ev, _ in emits if ev == "bg_task_complete"} == {SESS_A}
    assert _parked() == []


def test_completion_with_env_immune_spawn_owner_still_delivers(monkeypatch):
    """A spawn-time owner recovered from the ProcessSession is also immutable."""
    from api import background_process as bp

    _registry_with(
        monkeypatch,
        ("proc_owned2", {"session_key": SESS_A, "spawn_session_id": SESS_A}),
    )
    bp.register_process_session(SESS_A, SESS_A)
    _capture_emits(monkeypatch)
    wakeups = install_fake_start_session_turn(monkeypatch)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)

    bp._process_one(_completion(process_id="proc_owned2", session_key=""))

    assert wait_for_wakeup(wakeups), "spawn-owned completion was not woken"
    assert [c["session_id"] for c in wakeups["calls"]] == [SESS_A]
    assert _parked() == []


def test_unmapped_ownerless_completion_is_not_parked(monkeypatch):
    """A non-WebUI key (cron/CLI/gateway) that maps to no WebUI session keeps
    its existing drop behaviour: it is not WebUI-addressed, so it does not
    pollute the WebUI parking list."""
    from api import background_process as bp

    _registry_with(monkeypatch, ("proc_cron", {"session_key": "cron:job-1"}))
    emits = _capture_emits(monkeypatch)

    bp._process_one(_completion(process_id="proc_cron", session_key="cron:job-1"))

    assert emits == []
    assert _parked() == []


@pytest.mark.parametrize(
    "evt",
    [
        {},
        {"type": "completion"},
        {"session_id": "proc_bare"},
        {"type": "completion", "session_id": "proc_bare", "session_key": SESS_B},
        {"type": "heartbeat", "session_id": "proc_hb", "session_key": SESS_B},
        {"type": "watch_match", "session_id": "proc_w", "session_key": SESS_B,
         "pattern": "ERR", "output": "ERR"},
        {"type": "mystery", "session_id": "proc_m", "session_key": SESS_B},
    ],
)
def test_older_agent_event_shapes_never_crash_or_deliver(monkeypatch, evt):
    """Older Agent shapes (missing type/owner/command/output fields, and a
    ProcessSession without the newer attributes) must not raise and must not
    be delivered by key mapping."""
    from api import background_process as bp

    fake = FakeProcessRegistry()
    # A legacy ProcessSession object: no origin/spawn fields at all.
    fake._procs["proc_bare"] = types.SimpleNamespace()
    install_fake_registry(monkeypatch, fake)
    bp.register_process_session(SESS_B, SESS_B)
    emits = _capture_emits(monkeypatch)
    wakeups = install_fake_start_session_turn(monkeypatch)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)

    bp._process_one(dict(evt))

    assert not wait_for_wakeup(wakeups, timeout=0.2)
    assert emits == []


def test_registry_lookup_failure_fails_closed(monkeypatch):
    """A registry whose ``get`` raises cannot provide an owner -> fail closed."""
    from api import background_process as bp

    fake = FakeProcessRegistry()

    def _boom(_pid):
        raise RuntimeError("registry exploded")

    fake.get = _boom
    install_fake_registry(monkeypatch, fake)
    bp.register_process_session(SESS_B, SESS_B)
    emits = _capture_emits(monkeypatch)
    wakeups = install_fake_start_session_turn(monkeypatch)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)

    bp._process_one(_completion(process_id="proc_boom"))

    assert not wait_for_wakeup(wakeups, timeout=0.2)
    assert emits == []


def test_resolvers_no_longer_pass_ownerless_mapping_through():
    """The exact probe from the #6752 report: with no owner fields, the
    resolver must not return the key-mapped foreign session."""
    from api import background_process as bp

    proc = types.SimpleNamespace(session_key="key-produced-for-foreign-session")
    assert bp._resolve_wakeup_target(
        process_id="proc-demo",
        session_key_resolved_sid=SESS_B,
        proc_session=proc,
    ) == ""
    assert bp._resolve_completion_target(
        session_key_resolved_sid=SESS_B,
        origin_ui_session_id="",
    ) == ""


# ---------------------------------------------------------------------------
# Async delegation completions
# ---------------------------------------------------------------------------

def test_ownerless_async_delegation_is_parked_without_claim_or_ack(monkeypatch):
    """An async row without origin_ui_session_id is not WebUI's to ACK: no
    claim, no complete, no wakeup — parked for visibility, durable row left
    pending for whichever consumer can prove ownership."""
    from api import background_process as bp

    _registry_with(monkeypatch)
    bp.register_process_session(SESS_B, SESS_B)
    calls = {"claim": 0, "complete": 0}
    monkeypatch.setattr(
        bp, "claim_async_delegation_delivery",
        lambda *_a, **_k: calls.__setitem__("claim", calls["claim"] + 1),
    )
    monkeypatch.setattr(
        bp, "complete_async_delegation_delivery",
        lambda *_a, **_k: calls.__setitem__("complete", calls["complete"] + 1),
    )
    wakeups = install_fake_start_session_turn(monkeypatch)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)

    bp._process_one({
        "type": "async_delegation",
        "delegation_id": "deleg_ownerless",
        "session_key": SESS_B,
        "summary": "done",
        "result": "SENSITIVE-OUTPUT-FROM-OTHER-SESSION",
    })

    assert calls == {"claim": 0, "complete": 0}
    assert not wait_for_wakeup(wakeups, timeout=0.3)
    parked = _parked()
    assert [p["delivery_id"] for p in parked] == ["deleg_ownerless"]
    assert parked[0]["type"] == "async_delegation"


def test_owned_async_delegation_still_claims_and_wakes_origin(monkeypatch):
    from api import background_process as bp

    _registry_with(monkeypatch)
    bp.register_process_session(SESS_B, SESS_B)
    seen = {}

    def _fake_async(evt, *, session_id, delegation_id, process_registry):
        seen["session_id"] = session_id
        seen["delegation_id"] = delegation_id

    monkeypatch.setattr(bp, "_process_async_delegation_event", _fake_async)

    bp._process_one({
        "type": "async_delegation",
        "delegation_id": "deleg_owned",
        "session_key": SESS_B,
        "origin_ui_session_id": SESS_A,
        "summary": "done",
    })

    assert seen == {"session_id": SESS_A, "delegation_id": "deleg_owned"}
    assert _parked() == []


# ---------------------------------------------------------------------------
# Next-turn consumer: _drain_webui_process_notifications
# ---------------------------------------------------------------------------

def _install_drain_registry(monkeypatch, events, procs=None):
    q = queue.Queue()
    for e in events:
        q.put(e)

    class _Reg:
        def __init__(self):
            self.completion_queue = q
            self._lock = threading.RLock()
            self._completion_consumed = set()

        def is_completion_consumed(self, sid):
            return sid in self._completion_consumed

        def get(self, sid):
            attrs = (procs or {}).get(sid)
            return types.SimpleNamespace(**attrs) if attrs is not None else None

    reg = _Reg()
    mod = types.ModuleType("tools.process_registry")
    mod.process_registry = reg
    if "tools" not in sys.modules:
        pkg = types.ModuleType("tools")
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.process_registry", mod)
    return reg


def test_next_turn_drain_does_not_inject_ownerless_completion(monkeypatch):
    streaming = importlib.import_module("api.streaming")
    from api import background_process as bp

    bp.register_process_session(SESS_B, SESS_B)
    reg = _install_drain_registry(
        monkeypatch,
        [_completion(process_id="proc_drain_ownerless")],
        procs={"proc_drain_ownerless": {"session_key": SESS_B}},
    )

    out = streaming._drain_webui_process_notifications(SESS_B)

    assert out == [], f"#6752 bypass: next-turn drain injected {out!r}"
    assert [p["delivery_id"] for p in _parked()] == ["proc_drain_ownerless"]
    # Parked is a terminal WebUI disposition: not requeued for another turn.
    assert reg.completion_queue.qsize() == 0


def test_next_turn_drain_still_injects_owned_completion(monkeypatch):
    streaming = importlib.import_module("api.streaming")
    from api import background_process as bp

    bp.register_process_session(SESS_B, SESS_B)
    _install_drain_registry(
        monkeypatch,
        [_completion(process_id="proc_drain_owned", origin_ui_session_id=SESS_A)],
    )

    assert streaming._drain_webui_process_notifications(SESS_B) == []
    out = streaming._drain_webui_process_notifications(SESS_A)

    assert len(out) == 1 and "proc_drain_owned" in out[0]
    assert _parked() == []


def test_next_turn_drain_respects_env_immune_spawn_owner(monkeypatch):
    streaming = importlib.import_module("api.streaming")
    from api import background_process as bp

    bp.register_process_session(SESS_B, SESS_B)
    _install_drain_registry(
        monkeypatch,
        [_completion(process_id="proc_drain_spawn")],
        procs={"proc_drain_spawn": {"session_key": SESS_B, "owner_session_id": SESS_A}},
    )

    assert streaming._drain_webui_process_notifications(SESS_B) == []
    out = streaming._drain_webui_process_notifications(SESS_A)
    assert len(out) == 1 and "proc_drain_spawn" in out[0]


def test_next_turn_drain_leaves_foreign_ownerless_events_queued(monkeypatch):
    """An ownerless event addressed to another session is neither injected
    nor parked by this session's drain (it is not addressed here)."""
    streaming = importlib.import_module("api.streaming")

    reg = _install_drain_registry(
        monkeypatch,
        [_completion(process_id="proc_other", session_key="cron:job-9")],
    )

    assert streaming._drain_webui_process_notifications(SESS_B) == []
    assert _parked() == []
    assert reg.completion_queue.qsize() == 1


# ---------------------------------------------------------------------------
# Neutral, visible parking surface (authenticated API)
# ---------------------------------------------------------------------------

def test_parked_completions_list_is_bounded_and_dismissable():
    peu = _peu()
    park = getattr(peu, "park_ownerless_completion", None)
    dismiss = getattr(peu, "dismiss_parked_ownerless_completions", None)
    assert callable(park) and callable(dismiss)

    cap = peu.PARKED_OWNERLESS_COMPLETIONS_MAX
    for i in range(cap + 5):
        park(_completion(process_id=f"proc_cap_{i}"),
             addressed_session_id=SESS_B, consumer="test")
    parked = _parked()
    assert len(parked) == cap
    assert parked[0]["delivery_id"] == f"proc_cap_{cap + 4}"  # newest first

    assert dismiss([parked[0]["id"]]) == 1
    assert len(_parked()) == cap - 1
    assert dismiss(None) == cap - 1
    assert _parked() == []


class _FakeHandler:
    def __init__(self, body: bytes = b"{}"):
        import io as _io

        self.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
        }
        self.rfile = _io.BytesIO(body)
        self.wfile = _io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass


def test_parked_completions_api_lists_and_dismisses():
    import json as _json
    from urllib.parse import urlparse

    import api.routes as routes

    peu = _peu()
    rec = peu.park_ownerless_completion(
        _completion(process_id="proc_api_1"), addressed_session_id=SESS_B, consumer="test"
    )
    peu.park_ownerless_completion(
        _completion(process_id="proc_api_2"), addressed_session_id=SESS_B, consumer="test"
    )

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("/api/background/parked-completions"))
    assert handler.status == 200
    payload = _json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert [p["delivery_id"] for p in payload["parked"]] == ["proc_api_2", "proc_api_1"]
    assert "SENSITIVE-OUTPUT-FROM-OTHER-SESSION" not in handler.wfile.getvalue().decode("utf-8")

    handler = _FakeHandler()
    routes._handle_parked_completions_dismiss(handler, {"ids": [rec["id"]]})
    assert handler.status == 200
    assert _json.loads(handler.wfile.getvalue().decode("utf-8"))["dismissed"] == 1
    assert [p["delivery_id"] for p in _parked()] == ["proc_api_2"]

    handler = _FakeHandler()
    routes._handle_parked_completions_dismiss(handler, {"ids": "not-a-list"})
    assert handler.status == 400
    assert len(_parked()) == 1

    handler = _FakeHandler()
    routes._handle_parked_completions_dismiss(handler, {"all": True})
    assert handler.status == 200
    assert _parked() == []



# ---------------------------------------------------------------------------
# Parked-completion endpoints: profile isolation (Codex r1 CORE finding)
#
# The parking store is process-global and spans every profile. When auth is
# enabled only a verified, UNBOUND full-access session may read or mutate it;
# a profile-bound (trusted-header group map) or unresolved session gets 403.
# ---------------------------------------------------------------------------

_TRUSTED_ENV_KEYS = (
    "HERMES_WEBUI_TRUSTED_AUTH_HEADER",
    "HERMES_WEBUI_TRUSTED_GROUPS_HEADER",
    "HERMES_WEBUI_GROUP_PROFILE_MAP",
    "HERMES_WEBUI_TRUSTED_PROXY_CIDRS",
    "HERMES_WEBUI_TRUSTED_AUTH_LOGOUT_URL",
)


@pytest.fixture
def _isolated_auth(monkeypatch, tmp_path):
    import api.auth as auth
    import api.profiles as profiles

    for key in _TRUSTED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(auth, "STATE_DIR", tmp_path)
    monkeypatch.setattr(auth, "_SESSIONS_FILE", tmp_path / ".sessions.json")
    monkeypatch.setattr(auth, "is_password_auth_enabled", lambda: False)
    monkeypatch.setattr(auth, "are_passkeys_enabled", lambda: False)
    monkeypatch.setattr(auth, "is_oidc_auth_enabled", lambda: False)
    auth._sessions.clear()
    auth._TRUSTED_AUTH_WARNINGS_EMITTED.clear()
    profiles.clear_request_profile()
    yield auth
    auth._sessions.clear()
    auth._TRUSTED_AUTH_WARNINGS_EMITTED.clear()
    profiles.clear_request_profile()


class _AuthHandler:
    def __init__(self, path, *, method="GET", headers=None):
        import io as _io

        self.path = path
        self.command = method
        self.headers = {"Host": "localhost", "Content-Length": "0",
                        "Content-Type": "application/json"}
        self.headers.update(headers or {})
        self.rfile = _io.BytesIO(b"")
        self.wfile = _io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.request = types.SimpleNamespace()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def text(self):
        return self.wfile.getvalue().decode("utf-8")


def _park_foreign_record():
    return _peu().park_ownerless_completion(
        _completion(process_id="proc_foreign_blue",
                    command="printf blue-confidential-command"),
        addressed_session_id="blue-private-session",
        consumer="test",
    )


def _drive(handler, monkeypatch, body=None):
    """Run the real auth gate, then the real router (CSRF stubbed)."""
    from urllib.parse import urlparse

    import api.auth as auth
    import api.routes as routes

    parsed = urlparse(handler.path)
    if not auth.check_auth(handler, parsed):
        return False
    if handler.command == "POST":
        monkeypatch.setattr(routes, "_check_csrf", lambda _h: True)
        monkeypatch.setattr(routes, "read_body", lambda _h: dict(body or {}))
        routes.handle_post(handler, parsed)
    else:
        routes.handle_get(handler, parsed)
    return True


def _trusted_bound(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_GROUPS_HEADER", "Remote-Groups")
    monkeypatch.setenv("HERMES_WEBUI_GROUP_PROFILE_MAP", '{"red-group": "red"}')
    return {"Remote-User": "bob", "Remote-Groups": "red-group"}


def _assert_foreign_record_hidden(handler):
    text = handler.text()
    assert handler.status == 403, (handler.status, text)
    assert "blue-confidential-command" not in text
    assert "blue-private-session" not in text
    assert "proc_foreign_blue" not in text


def test_parked_completions_get_rejects_profile_bound_session(monkeypatch, _isolated_auth):
    auth = _isolated_auth
    headers = _trusted_bound(monkeypatch)
    _park_foreign_record()

    handler = _AuthHandler("/api/background/parked-completions", headers=headers)
    assert _drive(handler, monkeypatch) is True, "auth gate itself must admit bob"
    info = auth.get_session_info(handler._trusted_auth_session_cookie_value)
    assert info["bound_profile"] == "red"
    _assert_foreign_record_hidden(handler)
    assert len(_parked()) == 1


@pytest.mark.parametrize("body", [{"all": True}, {"ids": ["completion:proc_foreign_blue"]}])
def test_parked_completions_dismiss_rejects_profile_bound_session(monkeypatch, _isolated_auth, body):
    headers = _trusted_bound(monkeypatch)
    rec = _park_foreign_record()
    assert rec["id"] == "completion:proc_foreign_blue"

    handler = _AuthHandler("/api/background/parked-completions/dismiss",
                           method="POST", headers=headers)
    assert _drive(handler, monkeypatch, body) is True
    _assert_foreign_record_hidden(handler)
    assert [p["id"] for p in _parked()] == [rec["id"]], "record must survive"


def test_parked_completions_reject_trusted_session_bound_to_default(monkeypatch, _isolated_auth):
    """A group map with no matching group binds the session to 'default'."""
    headers = _trusted_bound(monkeypatch)
    headers["Remote-Groups"] = "unmapped-group"
    rec = _park_foreign_record()

    get = _AuthHandler("/api/background/parked-completions", headers=headers)
    assert _drive(get, monkeypatch) is True
    _assert_foreign_record_hidden(get)

    post = _AuthHandler("/api/background/parked-completions/dismiss",
                        method="POST", headers=headers)
    assert _drive(post, monkeypatch, {"all": True}) is True
    _assert_foreign_record_hidden(post)
    assert [p["id"] for p in _parked()] == [rec["id"]]


def test_parked_completions_reject_unresolved_session_when_auth_enabled(monkeypatch, _isolated_auth):
    """Defense in depth: a handler reached without a verified session is refused."""
    import api.routes as routes
    from urllib.parse import urlparse

    monkeypatch.setattr(_isolated_auth, "is_password_auth_enabled", lambda: True)
    rec = _park_foreign_record()

    for cookie in (None, "deadbeef.forged-signature"):
        headers = {"Cookie": f"hermes_session={cookie}"} if cookie else {}
        get = _AuthHandler("/api/background/parked-completions", headers=headers)
        routes.handle_get(get, urlparse(get.path))
        _assert_foreign_record_hidden(get)

        for body in ({"all": True}, {"ids": [rec["id"]]}):
            post = _AuthHandler("/api/background/parked-completions/dismiss",
                                method="POST", headers=headers)
            routes._handle_parked_completions_dismiss(post, body)
            _assert_foreign_record_hidden(post)
    assert [p["id"] for p in _parked()] == [rec["id"]]


def test_parked_completions_allow_unbound_password_session(monkeypatch, _isolated_auth):
    import json as _json

    auth = _isolated_auth
    monkeypatch.setattr(auth, "is_password_auth_enabled", lambda: True)
    cookie = auth.create_session()
    headers = {"Cookie": f"hermes_session={cookie}"}
    rec = _park_foreign_record()
    _peu().park_ownerless_completion(
        _completion(process_id="proc_other"), addressed_session_id=SESS_B, consumer="test"
    )

    get = _AuthHandler("/api/background/parked-completions", headers=headers)
    assert _drive(get, monkeypatch) is True
    assert get.status == 200, get.text()
    ids = [p["id"] for p in _json.loads(get.text())["parked"]]
    assert ids == ["completion:proc_other", rec["id"]]

    post = _AuthHandler("/api/background/parked-completions/dismiss",
                        method="POST", headers=headers)
    assert _drive(post, monkeypatch, {"ids": [rec["id"]]}) is True
    assert post.status == 200, post.text()
    assert _json.loads(post.text())["dismissed"] == 1

    post = _AuthHandler("/api/background/parked-completions/dismiss",
                        method="POST", headers=headers)
    assert _drive(post, monkeypatch, {"all": True}) is True
    assert post.status == 200, post.text()
    assert _parked() == []


def test_parked_completions_allow_unbound_trusted_session(monkeypatch, _isolated_auth):
    """Trusted-header auth WITHOUT a group map yields an unbound session."""
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    headers = {"Remote-User": "admin"}
    _park_foreign_record()

    get = _AuthHandler("/api/background/parked-completions", headers=headers)
    assert _drive(get, monkeypatch) is True
    assert get.status == 200, get.text()

    post = _AuthHandler("/api/background/parked-completions/dismiss",
                        method="POST", headers=headers)
    assert _drive(post, monkeypatch, {"all": True}) is True
    assert post.status == 200, post.text()
    assert _parked() == []


def test_parked_completions_allow_auth_disabled_local_mode(monkeypatch, _isolated_auth):
    import api.routes as routes
    from urllib.parse import urlparse

    assert _isolated_auth.is_auth_enabled() is False
    rec = _park_foreign_record()
    get = _AuthHandler("/api/background/parked-completions")
    routes.handle_get(get, urlparse(get.path))
    assert get.status == 200, get.text()

    post = _AuthHandler("/api/background/parked-completions/dismiss", method="POST")
    routes._handle_parked_completions_dismiss(post, {"ids": [rec["id"]]})
    assert post.status == 200, post.text()
    assert _parked() == []


def test_parked_record_omits_command_text():
    """Smallest safe payload: no command text (it can embed secrets)."""
    rec = _park_foreign_record()
    for snapshot in (rec, *_parked()):
        assert "command_preview" not in snapshot
        assert "blue-confidential-command" not in repr(snapshot)
