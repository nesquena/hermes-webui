from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.auth as auth
import api.routes as routes
import api.profiles as profiles
from tests.js_source_extract import extract_function


PANELS_JS = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


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
    profiles.clear_request_profile()
    yield
    auth._sessions.clear()
    auth._TRUSTED_AUTH_WARNINGS_EMITTED.clear()
    profiles.clear_request_profile()


def _trusted_env(
    monkeypatch,
    *,
    header="Remote-User",
    groups_header=None,
    group_map=None,
    proxy_cidrs=None,
    logout_url=None,
):
    for key in (
        "HERMES_WEBUI_TRUSTED_AUTH_HEADER",
        "HERMES_WEBUI_TRUSTED_GROUPS_HEADER",
        "HERMES_WEBUI_GROUP_PROFILE_MAP",
        "HERMES_WEBUI_TRUSTED_PROXY_CIDRS",
        "HERMES_WEBUI_TRUSTED_AUTH_LOGOUT_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    if header is not None:
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", header)
    if groups_header is not None:
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_GROUPS_HEADER", groups_header)
    if group_map is not None:
        monkeypatch.setenv("HERMES_WEBUI_GROUP_PROFILE_MAP", json.dumps(group_map))
    if proxy_cidrs is not None:
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", proxy_cidrs)
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


def test_malformed_trusted_proxy_cidr_rejects_non_loopback_peer(monkeypatch):
    _trusted_env(monkeypatch, proxy_cidrs="bad-cidr")
    handler = _Handler(headers={"Remote-User": "alice"}, client_address=("10.0.0.5", 12345))

    result = auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query=""))

    assert auth.is_trusted_auth_enabled() is True
    assert auth.is_auth_enabled() is True
    assert result is False
    assert handler.status == 401
    assert getattr(handler, "_pending_set_cookies", []) == []


def test_malformed_trusted_proxy_cidr_rejects_existing_trusted_session(monkeypatch):
    _trusted_env(monkeypatch, proxy_cidrs="bad-cidr")
    cookie = auth.create_session(auth_type="trusted", username="alice")
    handler = _Handler(
        headers={"Cookie": f"hermes_session={cookie}", "Remote-User": "alice"},
        client_address=("10.0.0.5", 12345),
    )

    result = auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query=""))

    assert auth.is_trusted_auth_enabled() is True
    assert auth.is_auth_enabled() is True
    assert result is False
    assert handler.status == 401
    assert handler.body_text() == '{"error":"Authentication required"}'
    assert auth.verify_session(cookie) is False


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


@pytest.mark.parametrize(
    "group_map",
    [
        {"ops": "ops_profile", "": "admin"},
        {"": "admin", "ops": "ops_profile"},
    ],
)
def test_group_map_ignores_invalid_entry_without_discarding_valid_mappings(monkeypatch, group_map):
    _trusted_env(
        monkeypatch,
        groups_header="Remote-Groups",
        group_map=group_map,
    )
    handler = _Handler(headers={"Remote-User": "alice", "Remote-Groups": "ops"})

    assert auth.ensure_trusted_auth_session(handler)["bound_profile"] == "ops_profile"


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
    _trusted_env(monkeypatch, groups_header="Remote-Groups", group_map={"hermes_devops": "devops"})
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}", "Remote-User": "alice", "Remote-Groups": "hermes_devops"})
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "coworkers")

    result = auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query=""))

    assert result is False
    assert handler.status == 403
    assert handler.body_text() == '{"error":"Profile access forbidden"}'


def test_bound_profile_match_allowed(monkeypatch):
    _trusted_env(monkeypatch, groups_header="Remote-Groups", group_map={"hermes_devops": "devops"})
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}", "Remote-User": "alice", "Remote-Groups": "hermes_devops"})
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
    _trusted_env(monkeypatch, groups_header="Remote-Groups", group_map={"hermes_devops": "devops"})
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}", "Remote-User": "alice", "Remote-Groups": "hermes_devops"})
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
    _trusted_env(monkeypatch, groups_header="Remote-Groups", group_map={"hermes_devops": "devops"})
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}", "Remote-User": "alice", "Remote-Groups": "hermes_devops"})
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
    _trusted_env(monkeypatch, groups_header="Remote-Groups", group_map={"hermes_devops": "devops"})
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}", "Remote-User": "alice", "Remote-Groups": "hermes_devops"})
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


def test_auth_status_rejects_trusted_cookie_when_proxy_cidr_is_malformed(monkeypatch):
    _trusted_env(monkeypatch, proxy_cidrs="bad-cidr")
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(
        headers={"Cookie": f"hermes_session={cookie}", "Remote-User": "alice"},
        client_address=("10.0.0.5", 12345),
    )
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
    assert auth.verify_session(cookie) is False


def test_malformed_trusted_proxy_cidr_keeps_loopback_trusted(monkeypatch):
    _trusted_env(monkeypatch, proxy_cidrs="bad-cidr")
    handler = _Handler(headers={"Remote-User": "alice"})

    assert auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query="")) is True


def test_mapped_ipv6_peer_matches_canonical_trusted_proxy_cidr(monkeypatch):
    _trusted_env(monkeypatch, proxy_cidrs="10.0.0.0/8")
    handler = _Handler(
        headers={"Remote-User": "alice"},
        client_address=("::ffff:10.0.0.5", 12345),
    )

    assert auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query="")) is True
    assert handler._trusted_auth_session_info["username"] == "alice"


def test_existing_trusted_session_rotates_for_current_identity(monkeypatch):
    _trusted_env(
        monkeypatch,
        groups_header="Remote-Groups",
        group_map={"alice-group": "alice", "bob-group": "bob"},
    )
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="alice",
    )
    handler = _Handler(
        headers={
            "Cookie": f"hermes_session={cookie}",
            "Remote-User": "bob",
            "Remote-Groups": "bob-group",
        }
    )

    assert auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query="")) is True
    assert auth.verify_session(cookie) is False
    assert handler._trusted_auth_session_info["username"] == "bob"
    assert handler._trusted_auth_session_info["bound_profile"] == "bob"
    assert handler._trusted_auth_session_cookie_value != cookie
    assert profiles.get_active_profile_name() == "bob"


def test_trusted_reconciliation_cache_resets_between_requests(monkeypatch):
    _trusted_env(monkeypatch)
    handler = _Handler(headers={"Remote-User": "alice"})

    assert auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query="")) is True
    alice_cookie = handler._trusted_auth_session_cookie_value
    handler.headers = {"Cookie": f"hermes_session={alice_cookie}", "Remote-User": "bob"}
    handler._pending_set_cookies = []
    auth.reset_trusted_auth_request_state(handler)

    assert auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query="")) is True
    assert auth.verify_session(alice_cookie) is False
    assert handler._trusted_auth_session_info["username"] == "bob"


def test_server_resets_trusted_auth_request_state_per_request():
    server_source = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")

    assert server_source.count("reset_trusted_auth_request_state(self)") == 2


def test_reset_clears_pending_cookies_across_keepalive_requests(monkeypatch):
    # A queued-but-unflushed Set-Cookie must NOT survive onto the next request on
    # a reused HTTP/1.1 keep-alive handler (regression: a stale trusted-auth
    # cookie leaking across the request boundary could overwrite a later valid
    # login cookie and 401 the user). reset_trusted_auth_request_state() runs at
    # the per-request entry (server.py do_GET/do_POST), so it must drop the queue.
    _trusted_env(monkeypatch)
    handler = _Handler()

    # Request N queues an auth cookie but the response is never flushed.
    auth._queue_pending_cookie(handler, "hermes_session=stale-value; Path=/")
    assert handler._pending_set_cookies == ["hermes_session=stale-value; Path=/"]

    # Request N+1 begins on the same reused handler.
    auth.reset_trusted_auth_request_state(handler)

    # The stale queued cookie must be gone — nothing to flush into N+1's response.
    assert getattr(handler, "_pending_set_cookies", []) == []
    from api.helpers import flush_pending_auth_cookies
    flush_pending_auth_cookies(handler)
    assert handler.header_values("Set-Cookie") == []


def test_auth_status_reports_reconciled_trusted_identity(monkeypatch):
    _trusted_env(
        monkeypatch,
        groups_header="Remote-Groups",
        group_map={"alice-group": "alice", "bob-group": "bob"},
    )
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="alice",
    )
    handler = _Handler(
        headers={
            "Cookie": f"hermes_session={cookie}",
            "Remote-User": "bob",
            "Remote-Groups": "bob-group",
        }
    )
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: False)
    monkeypatch.setattr("api.passkeys.registered_credentials", lambda: [])

    routes.handle_get(handler, SimpleNamespace(path="/api/auth/status", query=""))

    payload = handler.json_body()
    assert payload["logged_in"] is True
    assert payload["user"] == "bob"
    assert payload["bound_profile"] == "bob"
    assert auth.verify_session(cookie) is False


def test_existing_trusted_session_without_header_is_invalidated(monkeypatch):
    _trusted_env(monkeypatch)
    cookie = auth.create_session(auth_type="trusted", username="alice")
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}"})

    assert auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query="")) is False
    assert handler.status == 401
    assert auth.verify_session(cookie) is False


def test_untrusted_existing_trusted_session_is_rejected_by_all_consumers(monkeypatch):
    _trusted_env(monkeypatch)
    headers = {"Remote-User": "alice"}

    protected_cookie = auth.create_session(auth_type="trusted", username="alice")
    protected = _Handler(
        headers={**headers, "Cookie": f"hermes_session={protected_cookie}"},
        client_address=("10.0.0.5", 12345),
    )
    assert auth.check_auth(protected, SimpleNamespace(path="/api/sessions", query="")) is False
    assert protected.status == 401
    assert auth.verify_session(protected_cookie) is False

    status_cookie = auth.create_session(auth_type="trusted", username="alice")
    status = _Handler(
        headers={**headers, "Cookie": f"hermes_session={status_cookie}"},
        client_address=("10.0.0.5", 12345),
    )
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: False)
    monkeypatch.setattr("api.passkeys.registered_credentials", lambda: [])
    routes.handle_get(status, SimpleNamespace(path="/api/auth/status", query=""))
    assert status.json_body()["logged_in"] is False
    assert auth.verify_session(status_cookie) is False

    switch_cookie = auth.create_session(auth_type="trusted", username="alice")
    switch = _Handler(
        headers={**headers, "Cookie": f"hermes_session={switch_cookie}"},
        client_address=("10.0.0.5", 12345),
    )
    switch.command = "POST"
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"name": "default"})
    monkeypatch.setattr(
        "api.profiles.switch_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not switch")),
    )
    routes.handle_post(switch, SimpleNamespace(path="/api/profile/switch", query=""))
    assert switch.status == 401
    assert auth.verify_session(switch_cookie) is False


def test_trusted_session_rehydrates_bound_profile_cookie(monkeypatch):
    _trusted_env(monkeypatch, groups_header="Remote-Groups", group_map={"hermes_devops": "devops"})
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}", "Remote-User": "alice", "Remote-Groups": "hermes_devops"})

    assert auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query="")) is True
    assert profiles.get_active_profile_name() == "devops"
    profile_cookie = next(
        cookie_header for cookie_header in handler._pending_set_cookies if cookie_header.startswith("hermes_profile=")
    )
    profile_value = profile_cookie.split("=", 1)[1].split(";", 1)[0]
    assert auth.verify_profile_cookie_value(profile_value, cookie) == "devops"


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
        groups_header="Remote-Groups",
        group_map={"hermes_devops": "devops"},
        logout_url="https://auth.example.com/logout",
    )
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="devops",
    )
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}", "Remote-User": "alice", "Remote-Groups": "hermes_devops"})
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


def test_logout_identity_rotation_preserves_csrf_validation(monkeypatch):
    _trusted_env(
        monkeypatch,
        groups_header="Remote-Groups",
        group_map={"alice-group": "alice", "bob-group": "bob"},
        logout_url="https://auth.example.com/logout",
    )
    cookie = auth.create_session(
        auth_type="trusted",
        username="alice",
        bound_profile="alice",
    )
    handler = _Handler(
        headers={
            "Cookie": f"hermes_session={cookie}",
            "Remote-User": "bob",
            "Remote-Groups": "bob-group",
            auth.CSRF_HEADER_NAME: auth.csrf_token_for_session(cookie),
        }
    )
    handler.command = "POST"
    monkeypatch.setattr(routes, "read_body", lambda _handler: {})

    assert auth.check_auth(handler, SimpleNamespace(path="/api/auth/logout", query="")) is True

    routes.handle_post(handler, SimpleNamespace(path="/api/auth/logout", query=""))

    payload = handler.json_body()
    assert payload["ok"] is True
    assert payload["trusted_logout_url"] == "https://auth.example.com/logout"
    assert handler._trusted_auth_session_info["username"] == "bob"
    assert handler._trusted_auth_session_cookie_value != cookie
    assert auth.verify_session(cookie) is False
    assert auth.verify_session(handler._trusted_auth_session_cookie_value) is False
    set_cookies = handler.header_values("Set-Cookie")
    assert any(cookie_header.startswith("hermes_session=") and "Max-Age=0" in cookie_header for cookie_header in set_cookies)
    assert any(cookie_header.startswith("hermes_profile=") and "Max-Age=0" in cookie_header for cookie_header in set_cookies)


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
    assert calls and calls[0][0] == "ensure"
    assert handler.json_body()["bound_profile"] == "devops"

    calls.clear()
    routes.handle_post(handler, SimpleNamespace(path="/api/profile/switch", query=""))
    assert calls and calls[0][0] == "ensure"
    assert handler.status == 200


def test_sign_out_uses_trusted_logout_url_with_login_fallback():
    sign_out = extract_function(PANELS_JS, "signOut", prefix="async function")

    assert "const response=await api('/api/auth/logout',{method:'POST',body:'{}'});" in sign_out
    assert "window.location.href=response.trusted_logout_url||'login';" in sign_out
    assert NODE is not None

    def run_sign_out(logout_url):
        script = f"""
const signOut = (0, eval)("(" + {json.dumps(sign_out)} + ")");
globalThis.api = async () => ({{trusted_logout_url: {json.dumps(logout_url)}}});
globalThis.window = {{location: {{href: null}}}};
globalThis.showToast = () => {{}};
globalThis.t = (key) => key;
signOut().then(() => process.stdout.write(JSON.stringify(window.location.href)));
"""
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            [NODE, "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=creationflags,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    assert run_sign_out("https://auth.example.com/logout") == "https://auth.example.com/logout"
    assert run_sign_out(None) == "login"


# ── PR #6798: stale non-trusted sessions vs. trusted-header reconciliation ──


@pytest.mark.parametrize(
    "group_map",
    [
        {"devs": "dev", "admins": "*"},
        {"admins": "*", "devs": "dev"},
    ],
)
def test_wildcard_group_dominates_concrete_mappings_regardless_of_order(monkeypatch, group_map):
    # An identity in both a bound group and a wildcard (unbound/admin) group
    # must be unbound no matter how the operator ordered the mapping.
    _trusted_env(monkeypatch, groups_header="Remote-Groups", group_map=group_map)
    handler = _Handler(headers={"Remote-User": "alice", "Remote-Groups": "devs,admins"})

    info = auth.ensure_trusted_auth_session(handler)

    assert info["auth_type"] == "trusted"
    assert info["bound_profile"] is None
    assert auth.trusted_session_allows_active_profile(info) is True


def test_wildcard_only_match_creates_unbound_session(monkeypatch):
    _trusted_env(monkeypatch, groups_header="Remote-Groups", group_map={"admins": "*"})
    handler = _Handler(headers={"Remote-User": "alice", "Remote-Groups": "admins"})

    info = auth.ensure_trusted_auth_session(handler)

    assert info["auth_type"] == "trusted"
    assert info["bound_profile"] is None
    # No profile cookie is forced for an unbound session.
    pending = getattr(handler, "_pending_set_cookies", [])
    assert not any(cookie.startswith("hermes_profile=") for cookie in pending)


def _stale_session_cases():
    # (case id, create_session kwargs, group map, groups header value)
    return [
        pytest.param({}, None, None, id="legacy-password-no-metadata"),
        pytest.param(
            {"auth_type": "password", "username": "mallory", "bound_profile": "other"},
            None,
            None,
            id="non-trusted-different-identity",
        ),
        pytest.param(
            {"auth_type": "password", "username": "alice", "bound_profile": None},
            {"admins": "*"},
            "admins",
            id="non-trusted-exact-match-wildcard-unbound",
        ),
        pytest.param(
            {"auth_type": "password", "username": "alice", "bound_profile": "devops"},
            {"hermes_devops": "devops"},
            "hermes_devops",
            id="non-trusted-exact-match-bound-profile",
        ),
    ]


@pytest.mark.parametrize("session_kwargs,group_map,groups_value", _stale_session_cases())
def test_stale_non_trusted_cookie_replaced_on_trusted_get(
    monkeypatch, session_kwargs, group_map, groups_value
):
    # Any non-trusted session presented on a valid trusted request must be
    # invalidated and replaced by a trusted session — including a non-trusted
    # record whose username/bound_profile exactly match the proxy assertion
    # (create_session permits arbitrary metadata for other auth flows).
    _trusted_env(
        monkeypatch,
        groups_header="Remote-Groups" if group_map else None,
        group_map=group_map,
    )
    stale_cookie = auth.create_session(**session_kwargs)
    headers = {"Cookie": f"hermes_session={stale_cookie}", "Remote-User": "alice"}
    if groups_value:
        headers["Remote-Groups"] = groups_value
    handler = _Handler(headers=headers)

    assert auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query="")) is True

    # The old cookie is dead, the replacement is trusted, exactly one new
    # auth cookie is queued.
    assert auth.verify_session(stale_cookie) is False
    info = handler._trusted_auth_session_info
    assert info["auth_type"] == "trusted"
    assert info["username"] == "alice"
    replacement = handler._trusted_auth_session_cookie_value
    assert replacement != stale_cookie
    assert auth.verify_session(replacement) is True
    auth_cookies = [
        cookie for cookie in handler._pending_set_cookies if cookie.startswith("hermes_session=")
    ]
    assert len(auth_cookies) == 1
    assert getattr(handler, "_trusted_auth_session_rotated", False) is True


def test_untrusted_peer_with_stale_non_trusted_cookie_does_not_rotate(monkeypatch):
    # The same header from an untrusted network peer must not gain
    # trusted-session rotation: no invalidation, no trusted cookie.
    _trusted_env(monkeypatch)
    stale_cookie = auth.create_session(auth_type="password", username="alice")
    handler = _Handler(
        headers={"Cookie": f"hermes_session={stale_cookie}", "Remote-User": "alice"},
        client_address=("10.0.0.5", 12345),
    )

    info = auth.ensure_trusted_auth_session(handler)

    assert info["auth_type"] == "password"
    assert auth.verify_session(stale_cookie) is True
    assert not hasattr(handler, "_trusted_auth_session_info")
    assert getattr(handler, "_pending_set_cookies", []) == []
    assert getattr(handler, "_trusted_auth_session_rotated", False) is False


def test_first_unsafe_request_during_rotation_is_deliberately_retryable(monkeypatch):
    # First browser POST carrying a stale non-trusted cookie plus its CSRF
    # token: reconciliation rotates the session mid-request, so the CSRF gate
    # rejects the write with the deliberate retryable reason while the
    # replacement trusted cookie is emitted on the 403 response.
    _trusted_env(monkeypatch)
    stale_cookie = auth.create_session(auth_type="password", username="alice")
    handler = _Handler(
        headers={
            "Cookie": f"hermes_session={stale_cookie}",
            "Remote-User": "alice",
            "Host": "127.0.0.1:8787",
            "Origin": "http://127.0.0.1:8787",
            auth.CSRF_HEADER_NAME: auth.csrf_token_for_session(stale_cookie),
        }
    )
    handler.command = "POST"

    # Authentication reconciles first (server.py order) and rotates.
    assert auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query="")) is True
    assert auth.verify_session(stale_cookie) is False

    # The CSRF gate then sees the old request cookie: deliberate rejection.
    assert routes._check_csrf(handler) is False
    assert routes._csrf_rejection_error(handler) == "Session was re-authenticated - retry the request"

    # The production 403 path flushes the queued replacement cookie and hands
    # back the replacement CSRF token so an open tab can actually retry.
    from api.helpers import j

    j(handler, routes._csrf_rejection_payload(handler), status=403)
    assert handler.status == 403
    set_cookies = handler.header_values("Set-Cookie")
    assert any(cookie.startswith("hermes_session=") for cookie in set_cookies)
    replacement = handler._trusted_auth_session_cookie_value
    assert auth.verify_session(replacement) is True
    body = handler.json_body()
    assert body["reason"] == "session_rotated"
    assert body["csrf_token"] == auth.csrf_token_for_session(replacement)


def test_second_request_after_rotation_succeeds_with_replacement_cookie(monkeypatch):
    # Real client path: the retry may only use material the first RESPONSE
    # handed back — the replacement cookie from Set-Cookie and the CSRF token
    # from the 403 body. An open tab cannot mint tokens server-side.
    _trusted_env(monkeypatch)
    stale_cookie = auth.create_session(auth_type="password", username="alice")
    first = _Handler(
        headers={
            "Cookie": f"hermes_session={stale_cookie}",
            "Remote-User": "alice",
            "Host": "127.0.0.1:8787",
            "Origin": "http://127.0.0.1:8787",
            auth.CSRF_HEADER_NAME: auth.csrf_token_for_session(stale_cookie),
        }
    )
    first.command = "POST"
    assert auth.check_auth(first, SimpleNamespace(path="/api/sessions", query="")) is True
    assert routes._check_csrf(first) is False
    from api.helpers import j

    j(first, routes._csrf_rejection_payload(first), status=403)
    replacement_cookie = next(
        cookie for cookie in first.header_values("Set-Cookie") if cookie.startswith("hermes_session=")
    ).split("=", 1)[1].split(";", 1)[0]
    retry_token = first.json_body()["csrf_token"]

    second = _Handler(
        headers={
            "Cookie": f"hermes_session={replacement_cookie}",
            "Remote-User": "alice",
            "Host": "127.0.0.1:8787",
            "Origin": "http://127.0.0.1:8787",
            auth.CSRF_HEADER_NAME: retry_token,
        }
    )
    second.command = "POST"

    assert auth.check_auth(second, SimpleNamespace(path="/api/sessions", query="")) is True
    assert routes._check_csrf(second) is True
    # No further rotation: the trusted session is reused as-is.
    assert second._trusted_auth_session_cookie_value == replacement_cookie
    assert getattr(second, "_trusted_auth_session_rotated", False) is False


def test_stale_cookie_shell_renders_replacement_csrf_token(monkeypatch):
    # Regression (#6798 review): a PRESENT-but-invalidated request cookie must
    # not leave the shell with an empty CSRF token — that disabled client-side
    # token injection and bricked every unsafe request for exactly the rotated
    # user until a manual reload.
    _trusted_env(monkeypatch)
    stale_cookie = auth.create_session(auth_type="password", username="alice")
    handler = _Handler(headers={"Cookie": f"hermes_session={stale_cookie}", "Remote-User": "alice"})
    monkeypatch.setattr(routes, "_render_index_shell_base", lambda: "csrfToken:__CSRF_TOKEN_JSON__")
    monkeypatch.setattr("api.extensions.inject_extension_tags", lambda html: html)

    assert auth.check_auth(handler, SimpleNamespace(path="/", query="")) is True
    routes.handle_get(handler, SimpleNamespace(path="/", query=""))

    replacement = handler._trusted_auth_session_cookie_value
    assert replacement != stale_cookie
    expected = auth.csrf_token_for_session(replacement)
    assert expected
    assert handler.body_text() == f"csrfToken:{json.dumps(expected)}"


def test_unbound_rotation_preserves_authenticated_profile_selection(monkeypatch):
    # Rotating into an UNBOUND session (wildcard map) must re-sign the
    # authenticated profile cookie for the replacement session; otherwise the
    # next request rejects the stale signature and silently falls back to the
    # process default profile.
    _trusted_env(monkeypatch, groups_header="Remote-Groups", group_map={"admins": "*"})
    stale_cookie = auth.create_session(auth_type="password", username="alice")
    profile_cookie = auth.sign_profile_cookie_value("devops", stale_cookie)
    handler = _Handler(
        headers={
            "Cookie": f"hermes_session={stale_cookie}; hermes_profile={profile_cookie}",
            "Remote-User": "alice",
            "Remote-Groups": "admins",
        }
    )

    assert auth.check_auth(handler, SimpleNamespace(path="/api/sessions", query="")) is True

    replacement = handler._trusted_auth_session_cookie_value
    assert handler._trusted_auth_session_info["bound_profile"] is None
    assert profiles.get_active_profile_name() == "devops"
    reissued = next(
        cookie for cookie in handler._pending_set_cookies if cookie.startswith("hermes_profile=")
    ).split("=", 1)[1].split(";", 1)[0]
    assert auth.verify_profile_cookie_value(reissued, replacement) == "devops"

    # The follow-up request presenting only response material keeps the profile.
    second = _Handler(
        headers={
            "Cookie": f"hermes_session={replacement}; hermes_profile={reissued}",
            "Remote-User": "alice",
            "Remote-Groups": "admins",
        }
    )
    assert auth.check_auth(second, SimpleNamespace(path="/api/sessions", query="")) is True
    from api.helpers import get_profile_cookie

    assert get_profile_cookie(second) == "devops"


@pytest.mark.skipif(NODE is None, reason="node is required to execute the fetch wrapper")
def test_fetch_wrapper_adopts_rotated_csrf_token_for_retry():
    # Execute the REAL inline wrapper from index.html: the first unsafe fetch
    # sends the shell token and receives the rotation 403; the retry must send
    # the replacement token from that 403 body — no page reload involved.
    src = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    script = next(
        (
            match.group(1)
            for match in re.finditer(r"<script>((?:(?!</script>).)*)</script>", src, re.S)
            if "X-Hermes-CSRF-Token" in match.group(1)
        ),
        None,
    )
    assert script, "CSRF fetch wrapper script not found in index.html"
    harness = """
'use strict';
global.window = globalThis;
global.location = new URL('http://127.0.0.1:8787/');
global.document = { baseURI: 'http://127.0.0.1:8787/' };
window.__HERMES_CONFIG__ = { csrfToken: 'OLD_TOKEN' };
const sentTokens = [];
window.fetch = function (input, init) {
  const headers = init && init.headers ? Object.fromEntries(init.headers) : {};
  sentTokens.push(headers['x-hermes-csrf-token'] || null);
  if (sentTokens.length === 1) {
    return Promise.resolve({
      status: 403,
      clone() {
        return {
          json: () =>
            Promise.resolve({
              error: 'Session was re-authenticated - retry the request',
              reason: 'session_rotated',
              csrf_token: 'NEW_TOKEN',
            }),
        };
      },
    });
  }
  return Promise.resolve({ status: 200, clone() { return { json: () => Promise.resolve({}) }; } });
};
%SCRIPT%
;(async () => {
  await fetch('/api/sessions', { method: 'POST' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  await fetch('/api/sessions', { method: 'POST' });
  console.log(JSON.stringify(sentTokens));
})();
"""
    harness = harness.replace("%SCRIPT%", script)
    result = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    first, second = json.loads(result.stdout.strip())
    assert first == "OLD_TOKEN"
    assert second == "NEW_TOKEN"
