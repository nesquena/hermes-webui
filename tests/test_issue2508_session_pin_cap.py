"""Regression checks for issue #2508 session pinning bounds and context menu access."""

import json
import pathlib
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from tests._pytest_port import BASE, TEST_STATE_DIR


ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def make_session(created):
    payload = {
        "title": f"Pin cap {len(created) + 1}",
        "messages": [{"role": "user", "content": "keep this conversation handy"}],
        "model": "test/pin-cap",
    }
    d, status = post("/api/session/import", payload)
    assert status == 200
    sid = d["session"]["session_id"]
    created.append(sid)
    return sid



class _PinSession:
    def __init__(self, sid, profile, pinned=False, persisted=None, parent=None):
        self.session_id, self.profile = sid, profile
        self.pinned, self.archived = pinned, False
        self.messages, self.context_messages, self.pending_user_message = [], [], None
        self.active_stream_id = None
        self.workspace = str(TEST_STATE_DIR / "test-workspace")
        self.model, self.model_provider = "test/pin-cap", "test"
        self.parent_session_id, self._persisted = parent, persisted

    def compact(self):
        return {"session_id": self.session_id, "profile": self.profile, "pinned": self.pinned,
                "archived": self.archived, "parent_session_id": self.parent_session_id,
                "pre_compression_snapshot": False, "default_hidden": False}

    def save(self):
        if self._persisted is not None and self not in self._persisted:
            self._persisted.append(self)


def _configure_pin_route(monkeypatch, sessions, persisted, source, active_profile, root_names=None):
    import threading
    from collections import OrderedDict
    from contextlib import nullcontext
    import api.profiles as profiles
    import api.routes as routes

    by_id = {session.session_id: session for session in sessions}
    names = sorted({"default", *(session.profile for session in sessions)})
    monkeypatch.setattr(routes, "LOCK", threading.Lock())
    monkeypatch.setattr(routes, "SESSIONS", OrderedDict(by_id if source == "memory" else {}))
    monkeypatch.setattr(routes, "all_sessions", lambda: list(persisted) if source == "persisted" else [])
    monkeypatch.setattr(routes, "get_session", lambda sid, **_: by_id[sid])
    monkeypatch.setattr(routes, "get_session_profile_readonly", lambda sid: by_id[sid].profile)
    monkeypatch.setattr(routes, "list_profiles_api", lambda **_: [
        {"name": name, "is_default": name == "default"} for name in names])
    monkeypatch.setattr(profiles, "_root_profile_name_cache", set(root_names or {"default"}))
    monkeypatch.setattr(profiles, "_root_profile_name_cache_loaded", root_names is not None)
    monkeypatch.setattr(routes, "load_settings", lambda: {"pinned_sessions_limit": 3})
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: active_profile)
    monkeypatch.setattr(routes, "_check_csrf", lambda *_: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda *_: False)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda *_: nullcontext())
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_, **__: None)
    responses = []
    monkeypatch.setattr(routes, "j", lambda _, payload, status=200, **__: responses.append((status, payload)))
    monkeypatch.setattr(routes, "bad", lambda _, message, code=400: responses.append((code, {"error": message})))
    return routes, responses


def _configure_placeholder_routes(monkeypatch, routes, target):
    import api.compression_continuation as compression_continuation
    import api.goals as goals
    import api.profiles as profiles
    import api.runtime_adapter as runtime_adapter

    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_: None)
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda sid, **_: target)
    monkeypatch.setattr(compression_continuation, "durable_compression_continuation", lambda _: (False, None))
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _: None)
    monkeypatch.setattr(goals, "goal_command_payload", lambda *_args, **_kwargs: {"ok": True, "action": "status"})
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_runner_enabled", lambda: False)


def _retag_placeholder(routes, target, retag_route):
    body = {"session_id": target.session_id, "profile": "quota-a"}
    if retag_route == "chat":
        # The missing message makes chat start return after the real retag path.
        return routes._handle_chat_start(object(), body)
    return routes._handle_goal_command(object(), body | {"args": "status"})


@pytest.mark.parametrize("retag_route", ("chat", "goal"))
def test_pin_admission_uses_owner_after_placeholder_retag(monkeypatch, retag_route):
    persisted = []
    pinned_a = [_PinSession(f"quota-a-{i}", "quota-a", True, persisted) for i in range(3)]
    target = _PinSession("quota-b-placeholder", "quota-b", persisted=persisted)
    persisted.extend([*pinned_a, target])
    routes, responses = _configure_pin_route(
        monkeypatch, pinned_a + [target], persisted, "persisted", "quota-b", {"default"}
    )
    _configure_placeholder_routes(monkeypatch, routes, target)

    pin_body = {"session_id": target.session_id, "pinned": True}
    monkeypatch.setattr(routes, "read_body", lambda _: pin_body)
    original_all_sessions = routes.all_sessions

    def snapshot_with_retag():
        monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "quota-a")
        try:
            _retag_placeholder(routes, target, retag_route)
        finally:
            monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "quota-b")
        return original_all_sessions()

    monkeypatch.setattr(routes, "all_sessions", snapshot_with_retag)
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))

    assert target.profile == "quota-a"
    if retag_route == "chat":
        assert (400, {"error": "message is required"}) in responses
    else:
        assert any(status == 200 and payload.get("action") == "status" for status, payload in responses)
    assert responses[-1][0] == 400
    assert "Up to 3 sessions" in responses[-1][1]["error"]
    assert target.pinned is False


@pytest.mark.parametrize("retag_route", ("chat", "goal"))
def test_pin_lock_wins_before_placeholder_retag(monkeypatch, retag_route):
    persisted = []
    pinned_a = [_PinSession(f"quota-a-{i}", "quota-a", True, persisted) for i in range(3)]
    target = _PinSession("quota-b-placeholder", "quota-b", persisted=persisted)
    persisted.extend([*pinned_a, target])
    routes, responses = _configure_pin_route(
        monkeypatch, pinned_a + [target], persisted, "persisted", "quota-b", {"default"}
    )
    _configure_placeholder_routes(monkeypatch, routes, target)
    monkeypatch.setattr(
        routes,
        "_get_active_profile_name",
        lambda: "quota-a" if threading.current_thread().name == "placeholder-retag" else "quota-b",
    )

    pin_body = {"session_id": target.session_id, "pinned": True}
    monkeypatch.setattr(routes, "read_body", lambda _: pin_body)
    pin_has_lock = threading.Event()
    release_pin = threading.Event()
    retag_entered_lock = threading.Event()

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if threading.current_thread().name == "placeholder-retag":
                retag_entered_lock.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_):
            self.lock.release()

    monkeypatch.setattr(routes, "LOCK", ObservedLock())
    original_compact = target.compact
    compact_blocked = False

    def block_pin_inside_admission():
        nonlocal compact_blocked
        if threading.current_thread().name == "pin-admission" and not compact_blocked:
            compact_blocked = True
            pin_has_lock.set()
            if not release_pin.wait(5):
                raise RuntimeError("timed out waiting to release pin admission")
        return original_compact()

    target.compact = block_pin_inside_admission
    retag_attempted = threading.Event()
    thread_errors = []

    def pin():
        try:
            routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
        except BaseException as exc:
            thread_errors.append(exc)

    def retag():
        try:
            _retag_placeholder(routes, target, retag_route)
        except BaseException as exc:
            thread_errors.append(exc)
        finally:
            retag_attempted.set()

    pin_thread = threading.Thread(target=pin, name="pin-admission")
    retag_thread = None
    try:
        pin_thread.start()
        assert pin_has_lock.wait(5), "pin did not reach its locked admission snapshot"
        retag_thread = threading.Thread(target=retag, name="placeholder-retag")
        retag_thread.start()
        # The event is set at entry to the shared lock or after an unlocked
        # retag route finishes.
        assert retag_entered_lock.wait(1) or retag_attempted.wait(1)
    finally:
        release_pin.set()
        pin_thread.join(5)
        if retag_thread is not None:
            retag_thread.join(5)

    assert not pin_thread.is_alive()
    assert retag_thread is not None and not retag_thread.is_alive()
    assert not thread_errors
    assert target.pinned is True
    assert target.profile == "quota-b"
    assert any(
        status == 409
        and payload.get("code") == "session_profile_mismatch"
        and payload.get("profile") == "quota-b"
        for status, payload in responses
    )
    assert retag_entered_lock.is_set()


@pytest.mark.parametrize("retag_route", ("chat", "goal"))
def test_unpinned_empty_placeholder_can_still_retag(monkeypatch, retag_route):
    target = _PinSession("quota-b-placeholder", "quota-b")
    routes, responses = _configure_pin_route(
        monkeypatch, [target], [], "memory", "quota-a", {"default"}
    )
    _configure_placeholder_routes(monkeypatch, routes, target)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "quota-a")

    _retag_placeholder(routes, target, retag_route)

    assert target.profile == "quota-a"
    if retag_route == "chat":
        assert (400, {"error": "message is required"}) in responses
    else:
        assert any(status == 200 and payload.get("action") == "status" for status, payload in responses)


@pytest.mark.parametrize("source", ("persisted", "memory"))
def test_pin_quota_uses_target_owner_and_keeps_post_profile_guard(monkeypatch, source):
    persisted = []
    store = persisted if source == "persisted" else None
    pinned_a = [_PinSession(f"a-{i}", "A", True, store) for i in range(3)]
    parents = [_PinSession(f"b-parent-{i}", "B", parent="shared-parent") for i in range(3)]
    targets = [_PinSession(f"b-{i}", "B", persisted=store, parent=f"b-parent-{i}" if i < 3 else None)
               for i in range(4)]
    if store is not None:
        persisted.extend(pinned_a + parents)
    routes, responses = _configure_pin_route(
        monkeypatch, pinned_a + parents + targets, persisted, source, "A", {"default"})
    body = {}
    monkeypatch.setattr(routes, "read_body", lambda _: body)

    # Direct calls exercise ownership; handler requests exercise the profile guard.
    for target in targets:
        body.update(session_id=target.session_id, pinned=True)
        routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))

    assert [status for status, _ in responses] == [200, 200, 200, 400]
    assert all(targets[i].pinned for i in range(3)) and not targets[3].pinned
    routes.handle_post(object(), SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 409
    def unavailable(**_):
        raise RuntimeError("profile listing unavailable")
    monkeypatch.setattr(routes, "list_profiles_api", unavailable)
    monkeypatch.setattr(routes, "_root_profile_names_snapshot", lambda: None)
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 400 and not targets[3].pinned
    for pin in pinned_a:
        pin.pinned = False
    for parent in parents:
        parent.profile, parent.pinned = "default", True
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 200 and targets[3].pinned


@pytest.mark.parametrize("source", ("persisted", "memory"))
def test_pin_quota_includes_known_root_aliases_and_fails_closed(monkeypatch, source):
    pins = []
    store = pins if source == "persisted" else None
    pins.extend(_PinSession(f"root-{i}", "root-alias", True, store) for i in range(3))
    target = _PinSession("root-target", "default", persisted=store, parent="b-target")
    b_target = _PinSession("b-target", "B", persisted=store, parent="shared-root")
    routes, responses = _configure_pin_route(
        monkeypatch, pins + [target, b_target], pins, source, "default", {"default", "root-alias"})
    monkeypatch.setattr(routes, "list_profiles_api", lambda **_: [{"name": "default", "is_default": True}])
    monkeypatch.setattr(routes, "read_body", lambda _: {"session_id": target.session_id, "pinned": True})
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 400 and not target.pinned

    monkeypatch.setattr(routes, "list_profiles_api", lambda **_: [{"name": "root-alias", "is_default": False}])
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 503 and not target.pinned
    monkeypatch.setattr(routes, "list_profiles_api", lambda **_: [{"name": "default", "is_default": True}])
    monkeypatch.setattr(routes, "_root_profile_names_snapshot", lambda: None)
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 503 and not target.pinned
    pins[0].pinned = False
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 200 and target.pinned
    target.pinned = False
    pins[0].pinned = True
    monkeypatch.setattr(routes, "_root_profile_names_snapshot", lambda: {"default"})

    def unavailable(**_):
        raise RuntimeError("profile listing unavailable")
    monkeypatch.setattr(routes, "list_profiles_api", unavailable)
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 503 and not target.pinned
    monkeypatch.setattr(routes, "_root_profile_names_snapshot", lambda: {"default", "root-alias"})
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 400 and not target.pinned

    monkeypatch.setattr(routes, "read_body", lambda _: {"session_id": b_target.session_id, "pinned": True})
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 503 and not b_target.pinned
    monkeypatch.setattr(routes, "list_profiles_api", lambda **_: [
        {"name": "default", "is_default": True}, {"name": "B", "is_default": False}])
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 200 and b_target.pinned

    monkeypatch.setattr(routes, "read_body", lambda _: {"session_id": target.session_id, "pinned": True})
    monkeypatch.setattr(routes, "list_profiles_api", unavailable)
    b_target.pinned = False
    for pin in pins:
        pin.pinned = False
    monkeypatch.setattr(routes, "_root_profile_names_snapshot", lambda: None)
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 200 and target.pinned
    target.pinned = False
    monkeypatch.setattr(routes, "_root_profile_names_snapshot", lambda: {"default"})
    monkeypatch.setattr(routes, "list_profiles_api", lambda **_: [{"name": "default", "is_default": False}])
    routes.handle_post(None, SimpleNamespace(path="/api/session/pin", query=""))
    assert responses[-1][0] == 200 and target.pinned


def inject_hidden_pinned_snapshot(sid="hidden-pinned-snapshot"):
    """Add a persisted legacy hidden snapshot without touching server memory."""
    sessions_dir = TEST_STATE_DIR / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    row = {
        "session_id": sid,
        "title": "Hidden pinned snapshot",
        "workspace": str(TEST_STATE_DIR / "test-workspace"),
        "model": "test/pin-cap",
        "created_at": now,
        "updated_at": now,
        "last_message_at": now,
        "message_count": 1,
        "messages": [{"role": "user", "content": "legacy hidden snapshot"}],
        "tool_calls": [],
        "pinned": True,
        "archived": False,
        "pre_compression_snapshot": True,
        "_show_pre_compression_snapshot": False,
    }
    (sessions_dir / f"{sid}.json").write_text(json.dumps(row), encoding="utf-8")
    index_path = sessions_dir / "_index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        index = []
    compact = {k: v for k, v in row.items() if k not in {"messages", "tool_calls"}}
    index = [item for item in index if item.get("session_id") != sid]
    index.append(compact)
    index_path.write_text(json.dumps(index), encoding="utf-8")
    return sid


def test_session_pin_endpoint_caps_pinned_sessions_at_three():
    created = []
    try:
        pinned = [make_session(created) for _ in range(3)]
        for sid in pinned:
            d, status = post("/api/session/pin", {"session_id": sid, "pinned": True})
            assert status == 200
            assert d["session"]["pinned"] is True

        fourth = make_session(created)
        d, status = post("/api/session/pin", {"session_id": fourth, "pinned": True})
        assert status == 400
        assert "3 sessions" in d.get("error", "")

        d, status = post("/api/session/pin", {"session_id": pinned[0], "pinned": False})
        assert status == 200
        assert d["session"]["pinned"] is False

        d, status = post("/api/session/pin", {"session_id": fourth, "pinned": True})
        assert status == 200
        assert d["session"]["pinned"] is True
    finally:
        for sid in created:
            post("/api/session/delete", {"session_id": sid})


def test_session_pin_endpoint_ignores_hidden_snapshot_when_enforcing_cap():
    created = []
    hidden_sid = "hidden-pinned-snapshot-quota-route"
    try:
        hidden = inject_hidden_pinned_snapshot(hidden_sid)
        pinned = [make_session(created) for _ in range(2)]
        for sid in pinned:
            d, status = post("/api/session/pin", {"session_id": sid, "pinned": True})
            assert status == 200
            assert d["session"]["pinned"] is True

        third_visible = make_session(created)
        d, status = post("/api/session/pin", {"session_id": third_visible, "pinned": True})
        assert status == 200, d
        assert d["session"]["pinned"] is True
        assert hidden not in {third_visible, *pinned}
    finally:
        for sid in created:
            post("/api/session/delete", {"session_id": sid})
        (TEST_STATE_DIR / "sessions" / f"{hidden_sid}.json").unlink(missing_ok=True)
        index_path = TEST_STATE_DIR / "sessions" / "_index.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index = [item for item in index if item.get("session_id") != hidden_sid]
            index_path.write_text(json.dumps(index), encoding="utf-8")
        except (FileNotFoundError, json.JSONDecodeError):
            pass


def test_hidden_pre_compression_snapshot_does_not_count_toward_pin_quota():
    from api.routes import _session_counts_toward_pin_quota

    assert _session_counts_toward_pin_quota({
        "session_id": "hidden-snapshot",
        "pinned": True,
        "archived": False,
        "pre_compression_snapshot": True,
    }) is False
    assert _session_counts_toward_pin_quota({
        "session_id": "visible-session",
        "pinned": True,
        "archived": False,
        "pre_compression_snapshot": False,
    }) is True


def test_hidden_in_memory_snapshot_does_not_count_toward_pin_quota():
    from api.routes import _session_counts_toward_pin_quota

    snapshot = SimpleNamespace(
        session_id="hidden-memory-snapshot",
        pinned=True,
        archived=False,
        pre_compression_snapshot=True,
    )
    assert _session_counts_toward_pin_quota(snapshot) is False


def test_session_pin_cap_has_backend_and_frontend_guards():
    # #3288 renamed the in-LOCK pin counter to count visible lineages
    # (pinned_lineage_ids) instead of raw session ids (pinned_ids), so a
    # continuation lineage no longer consumes multiple pin slots. The guard
    # behaviour (snapshot, merge under LOCK, compare against the limit, 400) is
    # unchanged.
    assert 'persisted_rows = [' in ROUTES_PY
    assert 'candidate_rows.extend(' in ROUTES_PY
    assert 'pinned_lineage_ids = _visible_pinned_lineage_ids(candidate_rows, profiles=quota_profile_names)' in ROUTES_PY
    assert 'pinned_sessions_limit = int(load_settings().get("pinned_sessions_limit", 3) or 3)' in ROUTES_PY
    assert 'if (own_count if uncertain_owner else pinned_count) >= pinned_sessions_limit:' in ROUTES_PY
    assert 'Up to {pinned_sessions_limit} sessions can be pinned' in ROUTES_PY

    assert 'function _pinnedSessionCount()' in SESSIONS_JS
    assert 'function _getPinnedSessionsLimit()' in SESSIONS_JS
    assert 'function _pinnedSessionsLimit()' not in SESSIONS_JS
    assert 'const pinLimitReached=!session.pinned&&_pinnedSessionCount()>=_getPinnedSessionsLimit();' not in SESSIONS_JS
    assert 'if(pinLimitReached)' not in SESSIONS_JS
    assert "await api('/api/session/pin'" in SESSIONS_JS
    assert 'Only ${limit} conversations can be pinned' in SESSIONS_JS
    assert ".session-action-opt.is-disabled{opacity:.55;cursor:not-allowed;}" in STYLE_CSS


def test_session_rows_open_action_menu_from_right_click():
    assert 'el.oncontextmenu=(e)=>{' in SESSIONS_JS
    context_idx = SESSIONS_JS.find('el.oncontextmenu=(e)=>{')
    assert context_idx != -1
    block = SESSIONS_JS[context_idx:SESSIONS_JS.find('};', context_idx) + 2]
    assert 'e.preventDefault();' in block
    assert 'e.stopPropagation();' in block
    assert '_openSessionActionMenu(s, actions||el);' in block
