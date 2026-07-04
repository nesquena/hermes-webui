from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace

import pytest

import api.auth as auth
import api.routes as routes
import api.profiles as profiles


class _Handler:
    def __init__(self, *, headers=None, client_address=("127.0.0.1", 12345)):
        self.headers = dict(headers or {})
        self.client_address = client_address
        self.command = "GET"
        self.path = "/"
        self.request = SimpleNamespace()
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def body_bytes(self):
        return self.wfile.getvalue()

    def body_text(self):
        return self.body_bytes().decode("utf-8")

    def json_body(self):
        return json.loads(self.body_text())

    def header_values(self, name):
        return [value for key, value in self.sent_headers if key == name]


@pytest.fixture(autouse=True)
def isolated_auth_state(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "STATE_DIR", tmp_path)
    monkeypatch.setattr(auth, "_SESSIONS_FILE", tmp_path / ".sessions.json")
    monkeypatch.setattr(auth, "is_password_auth_enabled", lambda: False)
    monkeypatch.setattr(auth, "are_passkeys_enabled", lambda: False)
    monkeypatch.setattr(auth, "is_oidc_auth_enabled", lambda: False)
    auth._sessions.clear()
    auth._TRUSTED_AUTH_WARNINGS_EMITTED.clear()
    auth._TRUSTED_PROXY_NETWORKS_CACHE = None
    profiles.clear_request_profile()
    yield
    auth._sessions.clear()
    auth._TRUSTED_AUTH_WARNINGS_EMITTED.clear()
    auth._TRUSTED_PROXY_NETWORKS_CACHE = None
    profiles.clear_request_profile()


def _trusted_env(
    monkeypatch,
    *,
    header="Remote-User",
    groups_header=None,
    group_map=None,
    proxy_ips=None,
    logout_url=None,
):
    for key in (
        "HERMES_WEBUI_TRUSTED_AUTH_HEADER",
        "HERMES_WEBUI_TRUSTED_GROUPS_HEADER",
        "HERMES_WEBUI_GROUP_PROFILE_MAP",
        "HERMES_WEBUI_TRUSTED_AUTH_PROXY_IPS",
        "HERMES_WEBUI_TRUSTED_AUTH_LOGOUT_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    if header is not None:
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", header)
    if groups_header is not None:
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_GROUPS_HEADER", groups_header)
    if group_map is not None:
        monkeypatch.setenv("HERMES_WEBUI_GROUP_PROFILE_MAP", json.dumps(group_map))
    if proxy_ips is not None:
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_PROXY_IPS", proxy_ips)
    if logout_url is not None:
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_LOGOUT_URL", logout_url)


def test_trusted_header_only_enables_auth_gate(monkeypatch):
    _trusted_env(monkeypatch)

    assert auth.is_trusted_auth_enabled() is True
    assert auth.is_auth_enabled() is True


def test_untrusted_peer_header_does_not_create_session(monkeypatch):
    _trusted_env(monkeypatch)
    handler = _Handler(
        headers={"Remote-User": "alice"},
        client_address=("10.0.0.5", 12345),
    )

    result = auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query=""))

    assert result is False
    assert handler.status == 401
    assert getattr(handler, "_pending_set_cookies", []) == []


def test_invalid_trusted_proxy_allowlist_fails_closed(monkeypatch):
    _trusted_env(monkeypatch, proxy_ips="bad-cidr")
    handler = _Handler(headers={"Remote-User": "alice"})

    result = auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query=""))

    assert auth.is_trusted_auth_enabled() is True
    assert auth.is_auth_enabled() is True
    assert result is False
    assert handler.status == 401
    assert getattr(handler, "_pending_set_cookies", []) == []


def test_invalid_trusted_proxy_allowlist_rejects_existing_trusted_session(monkeypatch):
    _trusted_env(monkeypatch, proxy_ips="bad-cidr")
    cookie = auth.create_session(auth_type="trusted", username="alice")
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}"})

    result = auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query=""))

    assert auth.is_trusted_auth_enabled() is True
    assert auth.is_auth_enabled() is True
    assert result is False
    assert handler.status == 401
    assert handler.body_text() == '{"error":"Authentication required"}'


def test_invalid_trusted_header_name_fails_closed(monkeypatch):
    _trusted_env(monkeypatch, header="Bad Header")
    handler = _Handler(headers={"Bad Header": "alice"})

    result = auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query=""))

    assert auth.is_trusted_auth_enabled() is True
    assert auth.is_auth_enabled() is True
    assert result is False
    assert handler.status == 401


def test_allowlisted_peer_header_creates_trusted_session(monkeypatch):
    _trusted_env(monkeypatch)
    handler = _Handler(headers={"Remote-User": "alice"})

    result = auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query=""))

    assert result is True
    assert handler.status is None
    pending = getattr(handler, "_pending_set_cookies", [])
    assert any(cookie.startswith("hermes_session=") for cookie in pending)
    assert not any(cookie.startswith("hermes_profile=") for cookie in pending)


def test_group_map_binds_profile(monkeypatch):
    _trusted_env(
        monkeypatch,
        groups_header="Remote-Groups",
        group_map={"hermes_devops": "devops"},
    )
    handler = _Handler(
        headers={
            "Remote-User": "alice",
            "Remote-Groups": "hermes_devops,ai_users",
        }
    )

    info = auth.ensure_trusted_auth_session(handler)

    assert info["auth_type"] == "trusted"
    assert info["username"] == "alice"
    assert info["bound_profile"] == "devops"
    cookie_value = handler._trusted_auth_session_cookie_value
    assert auth.session_bound_profile(cookie_value) == "devops"
    assert any(cookie.startswith("hermes_profile=") for cookie in handler._pending_set_cookies)


def test_group_map_prefers_mapping_order_over_header_order(monkeypatch):
    _trusted_env(
        monkeypatch,
        groups_header="Remote-Groups",
        group_map={"admins": "ops", "devs": "sandbox"},
    )
    first = _Handler(headers={"Remote-User": "alice", "Remote-Groups": "devs,admins"})
    second = _Handler(headers={"Remote-User": "bob", "Remote-Groups": "admins,devs"})

    assert auth.ensure_trusted_auth_session(first)["bound_profile"] == "ops"
    assert auth.ensure_trusted_auth_session(second)["bound_profile"] == "ops"


def test_group_map_without_match_binds_default(monkeypatch):
    _trusted_env(
        monkeypatch,
        groups_header="Remote-Groups",
        group_map={"hermes_devops": "devops"},
    )
    handler = _Handler(
        headers={
            "Remote-User": "alice",
            "Remote-Groups": "ai_users,ops",
        }
    )

    info = auth.ensure_trusted_auth_session(handler)

    assert info["bound_profile"] == "default"
    assert auth.session_bound_profile(handler._trusted_auth_session_cookie_value) == "default"


def test_bound_profile_mismatch_rejected(monkeypatch):
    _trusted_env(monkeypatch)
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}"})
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "coworkers")

    result = auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query=""))

    assert result is False
    assert handler.status == 403
    assert handler.body_text() == '{"error":"Profile access forbidden"}'


def test_bound_profile_match_allowed(monkeypatch):
    _trusted_env(monkeypatch)
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}"})
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "devops")

    result = auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query=""))

    assert result is True
    assert handler.status is None


def test_profile_switch_rejects_other_bound_profile(monkeypatch):
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}"})
    handler.command = "POST"
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "devops")
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"name": "coworkers"})
    monkeypatch.setattr("api.profiles.switch_profile", lambda *_, **__: (_ for _ in ()).throw(AssertionError("should not switch")))

    routes.handle_post(handler, SimpleNamespace(path="/api/profile/switch", query=""))

    assert handler.status == 403
    assert handler.json_body()["error"] == "Profile is bound to the current session"


def test_first_trusted_profile_switch_rejection_keeps_session_cookies(monkeypatch):
    _trusted_env(
        monkeypatch,
        groups_header="Remote-Groups",
        group_map={"hermes_devops": "devops"},
    )
    handler = _Handler(
        headers={
            "Remote-User": "alice",
            "Remote-Groups": "hermes_devops",
        }
    )
    handler.command = "POST"
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "devops")
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"name": "coworkers"})
    monkeypatch.setattr("api.profiles.switch_profile", lambda *_, **__: (_ for _ in ()).throw(AssertionError("should not switch")))

    assert auth.check_auth(handler, SimpleNamespace(path="/api/profile/switch", query="")) is True
    routes.handle_post(handler, SimpleNamespace(path="/api/profile/switch", query=""))

    set_cookies = handler.header_values("Set-Cookie")
    assert handler.status == 403
    assert handler.json_body()["error"] == "Profile is bound to the current session"
    assert any(cookie.startswith("hermes_session=") for cookie in set_cookies)
    assert any(cookie.startswith("hermes_profile=") for cookie in set_cookies)
    assert len([cookie for cookie in set_cookies if cookie.startswith("hermes_session=")]) == 1


def test_profile_switch_accepts_bound_profile(monkeypatch):
    _trusted_env(monkeypatch)
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}"})
    handler.command = "POST"
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "devops")
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"name": "devops"})
    monkeypatch.setattr("api.profiles.switch_profile", lambda name, process_wide=False: {"ok": True, "profile": name})
    monkeypatch.setattr("api.config.invalidate_models_cache", lambda: None)
    monkeypatch.setattr("api.gateway_watcher.restart_watcher_for_profile", lambda _name: None)

    routes.handle_post(handler, SimpleNamespace(path="/api/profile/switch", query=""))

    assert handler.status == 200
    assert handler.json_body()["profile"] == "devops"
    assert any(value.startswith("hermes_profile=") for value in handler.header_values("Set-Cookie"))


def test_auth_status_reports_trusted_session_fields(monkeypatch):
    _trusted_env(monkeypatch)
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}"})
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: False)
    monkeypatch.setattr("api.passkeys.registered_credentials", lambda: [])

    routes.handle_get(handler, SimpleNamespace(path="/api/auth/status", query=""))

    payload = handler.json_body()
    assert payload["auth_enabled"] is True
    assert payload["logged_in"] is True
    assert payload["trusted_auth_enabled"] is True
    assert payload["auth_type"] == "trusted"
    assert payload["user"] == "alice"
    assert payload["bound_profile"] == "devops"


def test_auth_status_rejects_trusted_cookie_when_proxy_allowlist_invalid(monkeypatch):
    _trusted_env(monkeypatch, proxy_ips="bad-cidr")
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}"})
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: False)
    monkeypatch.setattr("api.passkeys.registered_credentials", lambda: [])

    routes.handle_get(handler, SimpleNamespace(path="/api/auth/status", query=""))

    payload = handler.json_body()
    assert payload["auth_enabled"] is True
    assert payload["logged_in"] is False
    assert payload["trusted_auth_enabled"] is True
    assert "auth_type" not in payload
    assert "user" not in payload
    assert "bound_profile" not in payload


def test_first_trusted_shell_response_includes_csrf_token(monkeypatch):
    _trusted_env(monkeypatch)
    handler = _Handler(headers={"Remote-User": "alice"})
    monkeypatch.setattr(routes, "_render_index_shell_base", lambda: "csrfToken:__CSRF_TOKEN_JSON__")
    monkeypatch.setattr("api.extensions.inject_extension_tags", lambda html: html)

    assert auth.check_auth(handler, SimpleNamespace(path="/", query="")) is True
    routes.handle_get(handler, SimpleNamespace(path="/", query=""))

    cookie_value = handler._trusted_auth_session_cookie_value
    assert any(cookie.startswith("hermes_session=") for cookie in handler.header_values("Set-Cookie"))
    assert handler.body_text() == f"csrfToken:{json.dumps(auth.csrf_token_for_session(cookie_value))}"


def test_logout_clears_auth_and_profile_cookies(monkeypatch):
    _trusted_env(
        monkeypatch,
        logout_url="https://auth.example.com/logout",
    )
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}"})
    handler.command = "POST"
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "devops")
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {})

    routes.handle_post(handler, SimpleNamespace(path="/api/auth/logout", query=""))

    payload = handler.json_body()
    assert payload["ok"] is True
    assert payload["trusted_logout_url"] == "https://auth.example.com/logout"
    set_cookies = handler.header_values("Set-Cookie")
    assert any(cookie.startswith("hermes_session=") and "Max-Age=0" in cookie for cookie in set_cookies)
    assert any(cookie.startswith("hermes_profile=") and "Max-Age=0" in cookie and "SameSite=Lax" in cookie for cookie in set_cookies)
    assert auth.verify_session(cookie) is False


def test_unconfigured_remote_user_header_is_ordinary_header(monkeypatch):
    _trusted_env(monkeypatch, header=None)
    handler = _Handler(headers={"Remote-User": "alice"})

    assert auth.is_trusted_auth_enabled() is False
    assert auth.ensure_trusted_auth_session(handler) is None
    assert auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query="")) is True


def test_trusted_auth_owner_contract(monkeypatch):
    _trusted_env(
        monkeypatch,
        groups_header="Remote-Groups",
        group_map={"hermes_devops": "devops"},
    )
    handler = _Handler(
        headers={
            "Remote-User": "alice",
            "Remote-Groups": "hermes_devops,ai_users",
        }
    )

    info = auth.ensure_trusted_auth_session(handler)
    assert info["auth_type"] == "trusted"
    assert info["bound_profile"] == "devops"
    assert auth.is_trusted_auth_enabled() is True

    token = "deadbeef" * 8
    auth._sessions[token] = time.time() + 3600
    legacy_sig = auth.hmac.new(
        auth._signing_key(),
        token.encode(),
        auth.hashlib.sha256,
    ).hexdigest()
    legacy_cookie = f"{token}.{legacy_sig}"
    legacy_info = auth.get_session_info(legacy_cookie)

    assert legacy_info["auth_type"] is None
    assert legacy_info["bound_profile"] is None
    assert legacy_info["expiry"] > time.time()


def test_consumers_route_through_auth_owner(monkeypatch):
    _trusted_env(
        monkeypatch,
        groups_header="Remote-Groups",
        group_map={"hermes_devops": "devops"},
        logout_url="https://auth.example.com/logout",
    )
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}"})
    handler.command = "POST"
    calls = []

    def _get_session_info(cookie_value):
        calls.append(("get_session_info", cookie_value))
        return {
            "token": cookie_value.split(".", 1)[0],
            "expiry": time.time() + 3600,
            "auth_type": "trusted",
            "username": "alice",
            "bound_profile": "devops",
        }

    monkeypatch.setattr(auth, "get_session_info", _get_session_info)
    monkeypatch.setattr(auth, "ensure_trusted_auth_session", lambda _handler: calls.append(("ensure", None)) or {
        "token": "token",
        "expiry": time.time() + 3600,
        "auth_type": "trusted",
        "username": "alice",
        "bound_profile": "devops",
    })
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: cookie)
    monkeypatch.setattr(auth, "verify_session", lambda _cookie: True)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: False)
    monkeypatch.setattr("api.passkeys.registered_credentials", lambda: [])
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "devops")
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"name": "devops"})
    monkeypatch.setattr("api.profiles.switch_profile", lambda name, process_wide=False: {"ok": True, "profile": name})
    monkeypatch.setattr("api.config.invalidate_models_cache", lambda: None)
    monkeypatch.setattr("api.gateway_watcher.restart_watcher_for_profile", lambda _name: None)

    routes.handle_get(handler, SimpleNamespace(path="/api/auth/status", query=""))
    assert calls and calls[0][0] == "get_session_info"
    assert handler.json_body()["bound_profile"] == "devops"

    calls.clear()
    routes.handle_post(handler, SimpleNamespace(path="/api/profile/switch", query=""))
    assert calls and calls[0][0] == "ensure"
    assert handler.status == 200
