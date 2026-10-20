"""Managed workplace policy: server-owned defaults and temporary administration."""
import json
import http.client
import threading

import pytest

import api.config as config


@pytest.fixture()
def workplace(tmp_path, monkeypatch):
    from api import auth, workplace

    monkeypatch.setenv("HERMES_WEBUI_MANAGED_MODE", "1")
    monkeypatch.setenv("HERMES_WEBUI_ADMIN_PIN", "test-pin-739251")
    monkeypatch.delenv("HERMES_WEBUI_PASSWORD", raising=False)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(auth, "STATE_DIR", tmp_path)
    auth._invalidate_password_hash_cache()
    with workplace._LOCK:
        workplace._sessions.clear()
        workplace._attempts.clear()
    yield workplace
    with workplace._LOCK:
        workplace._sessions.clear()
        workplace._attempts.clear()
    auth._invalidate_password_hash_cache()


@pytest.fixture()
def wire(workplace):
    """Real app HTTP dispatch, including auth, case folding, CSRF and policy."""
    from server import Handler, QuietHTTPServer

    server = QuietHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, cookie=None, origin=None, extra_headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=10)
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        headers.update(extra_headers or {})
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        data = response.read().decode()
        result = response.status, dict(response.getheaders()), data
        connection.close()
        return result

    request.origin = f"http://127.0.0.1:{server.server_port}"
    yield request
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _unlock(wire):
    code, headers, body = wire("POST", "/api/workplace/unlock", {"pin": "test-pin-739251"})
    assert code == 200, body
    assert "HttpOnly" in headers["Set-Cookie"]
    assert "SameSite=Strict" in headers["Set-Cookie"]
    return headers["Set-Cookie"].split(";", 1)[0]


def test_policy_overrides_saved_preferences_without_rewriting_them(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_MANAGED_MODE", "1")
    path = tmp_path / "settings.json"
    original = {"hidden_tabs": [], "hide_composer_yolo": False, "show_cli_sessions": True}
    path.write_text(json.dumps(original))
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    effective = config.load_settings()
    assert effective["hide_composer_yolo"] is True
    assert effective["show_cli_sessions"] is False
    assert "profiles" in effective["hidden_tabs"]
    assert json.loads(path.read_text()) == original


def test_unmanaged_mode_preserves_saved_preferences(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_MANAGED_MODE", raising=False)
    path = tmp_path / "settings.json"
    path.write_text('{"hidden_tabs":[],"hide_composer_yolo":false}')
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    assert config.load_settings()["hide_composer_yolo"] is False
    assert config.load_settings()["hidden_tabs"] == []


@pytest.mark.parametrize("method,path", [
    ("POST", "/API/Settings"), ("POST", "/api/default-model"),
    ("POST", "/api/profile/switch"), ("POST", "/api/session/yolo"),
    ("POST", "/api/session/toolsets"), ("POST", "/api/session/update"),
    ("POST", "/api/share/create"), ("POST", "/api/commands/exec"),
    ("POST", "/api/terminal/start"), ("POST", "/api/onboarding/setup"),
    ("POST", "/api/auth/passkey/register"), ("GET", "/api/crons/run"),
    ("GET", "/api/approval/inject_test"), ("GET", "/api/providers"),
    ("GET", "/api/logs"), ("PATCH", "/api/mcp/servers/demo"),
    ("DELETE", "/api/mcp/servers/demo"), ("PUT", "/api/mcp/servers/demo"),
    ("POST", "/api/extensions/demo/sidecar/change"),
    ("GET", "/api/extensions/demo/sidecar/status"),
    ("POST", "/api/future-admin-route"),
])
def test_locked_routes_cannot_be_bypassed(wire, method, path):
    code, _, body = wire(method, path, {"enabled": True})
    assert code == 403
    assert json.loads(body)["code"] == "admin_locked"


def test_unlock_save_lock_and_expiry_over_http(wire, workplace):
    cookie = _unlock(wire)
    code, _, body = wire("POST", "/api/settings", {"bot_name": "Company assistant"}, cookie)
    assert code == 200, body
    assert config.load_settings()["bot_name"] == "Company assistant"
    assert wire("GET", "/api/workplace/status", cookie=cookie)[0] == 200
    code, _, _ = wire("POST", "/api/workplace/lock", {}, cookie)
    assert code == 200
    assert wire("POST", "/api/settings", {}, cookie)[0] == 403
    cookie = _unlock(wire)
    token = cookie.split("=", 1)[1]
    with workplace._LOCK:
        _, binding, fingerprint = workplace._sessions[token]
        workplace._sessions[token] = (0, binding, fingerprint)
    assert wire("POST", "/api/settings", {}, cookie)[0] == 403


def test_missing_or_short_pin_fails_closed(wire, monkeypatch):
    for value in ("", "1234"):
        monkeypatch.setenv("HERMES_WEBUI_ADMIN_PIN", value)
        assert wire("POST", "/api/workplace/unlock", {"pin": value})[0] == 503
        assert wire("POST", "/api/settings", {})[0] == 403


def test_pin_guessing_is_limited_and_forwarded_ip_does_not_grant_access(wire):
    for _ in range(5):
        assert wire("POST", "/api/workplace/unlock", {"pin": "incorrect"})[0] == 403
    code, headers, _ = wire("POST", "/api/workplace/unlock", {"pin": "test-pin-739251"})
    assert code == 429
    assert headers["Retry-After"] == "60"


def test_csrf_blocks_unlock_and_lock(wire):
    assert wire("POST", "/api/workplace/unlock", {"pin": "test-pin-739251"},
                origin="https://attacker.invalid")[0] == 403
    cookie = _unlock(wire)
    assert wire("POST", "/api/workplace/lock", {}, cookie,
                origin="https://attacker.invalid")[0] == 403
    _, _, body = wire("GET", "/api/workplace/status", cookie=cookie)
    assert json.loads(body)["admin_unlocked"] is True


def test_pin_rotation_revokes_existing_unlock(wire, monkeypatch):
    cookie = _unlock(wire)
    monkeypatch.setenv("HERMES_WEBUI_ADMIN_PIN", "new-test-pin-862194")
    assert wire("POST", "/api/settings", {}, cookie)[0] == 403


def test_cookie_forgery_is_not_authorization(wire):
    assert wire("POST", "/api/settings", {}, "hermes_session_admin=forged")[0] == 403


def test_employee_settings_and_shell_never_expose_pin_or_saved_secrets(wire):
    config.SETTINGS_FILE.write_text(json.dumps({"unknown_secret": "hidden-credential"}))
    code, _, body = wire("GET", "/api/settings")
    assert code == 200
    assert "hidden-credential" not in body
    assert "password_hash" not in body
    assert "test-pin-739251" not in body
    assert json.loads(body)["onboarding_completed"] is True
    code, _, shell = wire("GET", "/")
    assert code == 200
    assert "__WORKPLACE_JSON__" not in shell
    assert '"admin_unlocked": false' in shell
    assert "test-pin-739251" not in shell
    assert wire("GET", "/share/old-link")[0] == 403


def test_invalid_json_object_does_not_unlock(wire):
    assert wire("POST", "/api/workplace/unlock", ["test-pin-739251"])[0] == 400


def test_invalid_unicode_pin_is_rejected_without_server_error(wire):
    assert wire("POST", "/api/workplace/unlock", {"pin": "\ud800" * 6})[0] == 403


def test_authenticated_unlock_requires_csrf_and_is_bound_to_login(wire, monkeypatch):
    from api import auth

    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-login-password-28639")
    auth._invalidate_password_hash_cache()
    code, headers, _ = wire("POST", "/api/auth/login", {"password": "test-login-password-28639"})
    assert code == 200
    login = headers["Set-Cookie"].split(";", 1)[0]
    csrf = auth.csrf_token_for_session(login.split("=", 1)[1])
    assert wire("POST", "/api/workplace/unlock", {"pin": "test-pin-739251"},
                login, origin=wire.origin)[0] == 403
    code, headers, _ = wire("POST", "/api/workplace/unlock", {"pin": "test-pin-739251"},
                            login, origin=wire.origin, extra_headers={"X-Hermes-CSRF-Token": csrf})
    assert code == 200
    admin = headers["Set-Cookie"].split(";", 1)[0]
    assert json.loads(wire("GET", "/api/workplace/status", cookie=login + "; " + admin)[2])["admin_unlocked"]
    # Another valid login cookie cannot borrow this administrator cookie.
    _, headers, _ = wire("POST", "/api/auth/login", {"password": "test-login-password-28639"})
    other_login = headers["Set-Cookie"].split(";", 1)[0]
    assert not json.loads(wire("GET", "/api/workplace/status", cookie=other_login + "; " + admin)[2])["admin_unlocked"]
    auth.invalidate_session(login.split("=", 1)[1])
    assert wire("POST", "/api/settings", {}, login + "; " + admin)[0] == 401
