from __future__ import annotations

import json
import threading
from urllib.parse import urlparse

import pytest


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8"))

    def get_json(self):
        return json.loads(self.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _disable_auth_for_profile_switch(monkeypatch):
    """Keep these watcher/profile-switch tests hermetic w.r.t. leaked auth state.

    The /api/profile/switch handler signs the active-profile cookie when auth is
    enabled (helpers.build_profile_cookie -> auth.sign_profile_cookie_value),
    which raises -> RuntimeError -> HTTP 409 if there's no active session. A
    sibling test that enables password/passkey auth in the shared in-process
    state would otherwise make the switch tests here return 409 instead of 200
    under a full-suite run (they pass in isolation). Forcing auth OFF for this
    module makes them order-independent — they exercise watcher restart, not
    auth. Patch every import site so the value is consistent.
    """
    for _mod in ("api.auth", "api.helpers", "api.routes"):
        try:
            monkeypatch.setattr(f"{_mod}.is_auth_enabled", lambda: False, raising=False)
        except Exception:
            pass
    yield


def test_gateway_watcher_pins_explicit_profile_home(tmp_path, monkeypatch):
    from api import gateway_watcher as gw

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    profile_a.mkdir()
    profile_b.mkdir()
    (profile_a / "state.db").touch()
    seen = []

    def fake_rows(db_path, **kwargs):
        seen.append(db_path)
        return [
            {
                "id": "session-a",
                "title": "Profile A",
                "model": None,
                "message_count": 1,
                "actual_message_count": 1,
                "started_at": 1,
                "last_activity": 2,
                "source": "telegram",
                "raw_source": "telegram",
                "session_source": "gateway",
                "source_label": "Telegram",
            }
        ]

    monkeypatch.setattr(gw, "read_importable_agent_session_rows", fake_rows)
    monkeypatch.setattr(
        gw,
        "_get_state_db_path",
        lambda *args: args[0].resolve() / "state.db" if args and args[0] is not None else profile_b / "state.db",
    )
    watcher = gw.GatewayWatcher(hermes_home=profile_a, profile_name="a")

    sessions = gw._get_agent_sessions_from_db(watcher._state_db_path)

    assert watcher.profile_name == "a"
    assert watcher._state_db_path == profile_a.resolve() / "state.db"
    assert seen == [profile_a.resolve() / "state.db"]
    assert sessions[0]["session_id"] == "session-a"


def test_restart_watcher_for_profile_replaces_singleton_with_profile_home(tmp_path, monkeypatch):
    from api import gateway_watcher as gw
    from api import profiles

    created = []

    class FakeWatcher:
        def __init__(self, *, profile_name="", hermes_home=None, state_db_path=None):
            self.profile_name = profile_name
            self.hermes_home = hermes_home
            self.state_db_path = state_db_path
            self.started = False
            self.stopped = False
            created.append(self)

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

        def stop(self):
            self.stopped = True

    old_home = (tmp_path / "old").resolve()
    old = FakeWatcher(profile_name="old", hermes_home=old_home)
    monkeypatch.setattr(gw, "_watchers", {str(old_home): old})
    monkeypatch.setattr(gw, "GatewayWatcher", FakeWatcher)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: tmp_path / name)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "target")

    watcher = gw.restart_watcher_for_profile("target")

    assert old.stopped is True
    assert watcher is created[-1]
    assert watcher.profile_name == "target"
    assert watcher.hermes_home == tmp_path / "target"
    assert watcher.started is True
    assert gw.get_watcher() is watcher


def test_restart_watcher_for_profile_keeps_subscribed_other_profile(tmp_path, monkeypatch):
    from api import gateway_watcher as gw
    from api import profiles

    default_home = (tmp_path / "default").resolve()
    work_home = (tmp_path / "work").resolve()
    created = []

    class FakeWatcher:
        def __init__(self, *, profile_name="", hermes_home=None, state_db_path=None):
            self.profile_name = profile_name
            self.hermes_home = hermes_home
            self.state_db_path = state_db_path
            self.started = False
            self.stopped = False
            self._subscribers = []
            self._sub_lock = threading.Lock()
            created.append(self)

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

        def stop(self):
            self.stopped = True
            self.started = False

    default_watcher = FakeWatcher(profile_name="default", hermes_home=default_home)
    default_watcher.started = True
    default_watcher._subscribers.append(object())
    monkeypatch.setattr(gw, "_watchers", {str(default_home): default_watcher})
    monkeypatch.setattr(gw, "GatewayWatcher", FakeWatcher)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: work_home)

    watcher = gw.restart_watcher_for_profile("work")

    assert default_watcher.stopped is False
    assert gw._watchers[str(default_home)] is default_watcher
    assert gw._watchers[str(work_home)] is watcher
    assert watcher.profile_name == "work"


def test_restart_watcher_for_profile_swaps_atomically(tmp_path, monkeypatch):
    from api import gateway_watcher as gw
    from api import profiles

    target_home = (tmp_path / "target").resolve()
    created = []
    seen = []

    class FakeWatcher:
        def __init__(self, *, profile_name="", hermes_home=None, state_db_path=None):
            self.profile_name = profile_name
            self.hermes_home = hermes_home
            self.state_db_path = state_db_path
            self.started = False
            self.stopped = False
            created.append(self)

        def start(self):
            self.started = True
            if len(seen) == 0:
                seen.append(gw.get_watcher(profile_name="target", hermes_home=target_home))

        def is_alive(self):
            return self.started

        def stop(self):
            self.stopped = True
            self.started = False

    existing = FakeWatcher(profile_name="target", hermes_home=target_home)
    existing.started = True
    monkeypatch.setattr(gw, "_watchers", {str(target_home): existing})
    monkeypatch.setattr(gw, "GatewayWatcher", FakeWatcher)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: target_home)

    watcher = gw.restart_watcher_for_profile("target")

    assert len(created) == 2
    assert seen == [existing]
    assert existing.stopped is True
    assert watcher is created[-1]
    assert gw.get_watcher(profile_name="target", hermes_home=target_home) is watcher


def test_watcher_registry_key_uses_concrete_values(tmp_path, monkeypatch):
    from api import gateway_watcher as gw

    def fail_resolve(**kwargs):
        raise AssertionError("_watcher_registry_key should not resolve profile state")

    monkeypatch.setattr(gw, "_resolve_watcher_target", fail_resolve)

    assert gw._watcher_registry_key("work", tmp_path / "work") == str((tmp_path / "work").resolve())
    assert gw._watcher_registry_key("default", None) == "default"


def test_start_watcher_pins_active_profile_home(tmp_path, monkeypatch):
    from api import gateway_watcher as gw
    from api import profiles

    profile_home = tmp_path / "active-profile"
    created = []

    class FakeWatcher:
        def __init__(self, *, profile_name="", hermes_home=None, state_db_path=None):
            self.profile_name = profile_name
            self.hermes_home = hermes_home
            self.state_db_path = state_db_path
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

    monkeypatch.setattr(gw, "_watchers", {})
    monkeypatch.setattr(gw, "GatewayWatcher", FakeWatcher)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "work")
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: profile_home)

    gw.start_watcher()

    assert len(created) == 1
    assert created[0].profile_name == "work"
    assert created[0].hermes_home == profile_home
    assert created[0].started is True
    assert gw.get_watcher() is created[0]


def test_get_watcher_scopes_lookup_to_active_profile(tmp_path, monkeypatch):
    from api import gateway_watcher as gw
    from api import profiles

    default_home = (tmp_path / "default").resolve()
    work_home = (tmp_path / "work").resolve()

    class FakeWatcher:
        def __init__(self, *, profile_name="", hermes_home=None, state_db_path=None):
            self.profile_name = profile_name
            self.hermes_home = hermes_home
            self.started = True

        def is_alive(self):
            return True

    default_watcher = FakeWatcher(profile_name="default", hermes_home=default_home)
    work_watcher = FakeWatcher(profile_name="work", hermes_home=work_home)
    monkeypatch.setattr(
        gw,
        "_watchers",
        {
            str(default_home): default_watcher,
            str(work_home): work_watcher,
        },
    )
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "work")
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: work_home if name == "work" else default_home,
    )

    assert gw.get_watcher() is work_watcher


def test_profile_switch_restarts_watcher_best_effort(monkeypatch):
    from api import config, gateway_watcher, profiles, routes

    calls = []
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"name": "demo"})
    monkeypatch.setattr(profiles, "_validate_profile_name", lambda name: None)
    monkeypatch.setattr(profiles, "switch_profile", lambda name, process_wide=False: {"ok": True, "name": name})
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: calls.append("cache"))
    monkeypatch.setattr(gateway_watcher, "restart_watcher_for_profile", lambda name: calls.append(("watcher", name)))

    handler = _FakeHandler()
    routes.handle_post(handler, urlparse("/api/profile/switch"))

    assert handler.status == 200
    assert handler.get_json() == {"ok": True, "name": "demo"}
    assert calls == ["cache", ("watcher", "demo")]
    assert any(k == "Set-Cookie" for k, _v in handler.sent_headers)


def test_profile_switch_response_survives_watcher_restart_failure(monkeypatch):
    from api import config, gateway_watcher, profiles, routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"name": "demo"})
    monkeypatch.setattr(profiles, "_validate_profile_name", lambda name: None)
    monkeypatch.setattr(profiles, "switch_profile", lambda name, process_wide=False: {"ok": True, "name": name})
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)
    monkeypatch.setattr(
        gateway_watcher,
        "restart_watcher_for_profile",
        lambda name: (_ for _ in ()).throw(RuntimeError("restart failed")),
    )

    handler = _FakeHandler()
    routes.handle_post(handler, urlparse("/api/profile/switch"))

    assert handler.status == 200
    assert handler.get_json() == {"ok": True, "name": "demo"}


def test_subscribe_after_stop_gets_sentinel_immediately():
    """Stop-race safety (#3629 / Codex gate): a subscriber added AFTER stop() has
    already set _stop_event and drained the then-current subscriber list must still
    receive the None sentinel — otherwise the SSE loop hangs open with keepalives but
    no events (it never learns the watcher it attached to was reaped during a
    concurrent profile switch)."""
    from api import gateway_watcher as gw

    watcher = gw.GatewayWatcher(hermes_home=None, profile_name="race")
    # Simulate the reaped/stopped watcher: stop() ran before this subscribe().
    watcher.stop()
    q = watcher.subscribe()
    # The late subscriber must find the sentinel already queued (no block / hang).
    assert q.get_nowait() is None
