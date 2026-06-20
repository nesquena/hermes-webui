"""Auth-disable safety tests.

Verify that POST /api/settings requires _current_password when password auth
is already enabled and the user wants to change, clear, or go passwordless.
"""
import io
import json
import os
from urllib.parse import urlparse

import pytest


@pytest.fixture(autouse=True)
def _isolate_auth_settings_state(tmp_path, monkeypatch):
    """Keep these unit tests from mutating the shared pytest server state.

    The full CI shard may already have the test server running. If these tests
    write password_hash into the shared test settings.json, the server process can
    observe/cache it and unrelated later API tests start returning 401. Point the
    in-process config module at a per-test settings file instead.
    """
    import api.config as cfg
    from api.auth import _invalidate_password_hash_cache

    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / "settings.json")
    _invalidate_password_hash_cache()
    os.environ.pop("HERMES_WEBUI_PASSWORD", None)
    yield
    os.environ.pop("HERMES_WEBUI_PASSWORD", None)
    _invalidate_password_hash_cache()


class _FakeHandler:
    def __init__(self, body_bytes: bytes = b"", cookie: str = ""):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {
            "Content-Length": str(len(body_bytes)),
        }
        if cookie:
            self.headers["Cookie"] = cookie
        self.request = None
        # First-password setup is gated to local/private clients when auth is
        # disabled. These in-process settings tests exercise the local bootstrap
        # path, so model a loopback request explicitly.
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _post_settings(body_dict, cookie=""):
    from api.routes import handle_post
    raw = json.dumps(body_dict).encode("utf-8")
    handler = _FakeHandler(body_bytes=raw, cookie=cookie)
    parsed = urlparse("http://example.com/api/settings")
    handle_post(handler, parsed)
    return handler


def _get_settings():
    from api.routes import handle_get
    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/settings")
    handle_get(handler, parsed)
    return handler.json_body(), handler.status


def _set_password_raw(pw):
    from api.config import save_settings
    from api.auth import _invalidate_password_hash_cache
    save_settings({"_set_password": pw})
    _invalidate_password_hash_cache()


def _clear_password_raw():
    from api.config import save_settings
    from api.auth import _invalidate_password_hash_cache
    save_settings({"_clear_password": True})
    _invalidate_password_hash_cache()


class TestChangePasswordRequiresCurrentPassword:
    def test_cannot_change_password_without_current_password(self):
        _set_password_raw("oldpassword")
        handler = _post_settings({"_set_password": "newpassword"})
        assert handler.status == 403
        payload = handler.json_body()
        assert "current password" in payload.get("error", "").lower()

    def test_cannot_change_password_with_wrong_current_password(self):
        _set_password_raw("oldpassword")
        handler = _post_settings({"_set_password": "newpassword", "_current_password": "wrongpw"})
        assert handler.status == 403
        payload = handler.json_body()
        assert "incorrect" in payload.get("error", "").lower()

    def test_can_change_password_with_correct_current_password(self):
        _set_password_raw("oldpassword")
        handler = _post_settings({"_set_password": "newpassword", "_current_password": "oldpassword"})
        assert handler.status == 200
        payload = handler.json_body()
        assert payload.get("auth_enabled") is True
        _clear_password_raw()


class TestClearPasswordRequiresCurrentPassword:
    def test_cannot_clear_password_without_current_password(self):
        _set_password_raw("oldpassword")
        handler = _post_settings({"_clear_password": True})
        assert handler.status == 403
        payload = handler.json_body()
        assert "current password" in payload.get("error", "").lower()

    def test_cannot_clear_password_with_wrong_current_password(self):
        _set_password_raw("oldpassword")
        handler = _post_settings({"_clear_password": True, "_current_password": "wrongpw"})
        assert handler.status == 403

    def test_cannot_clear_password_with_non_string_current_password(self):
        _set_password_raw("oldpassword")
        handler = _post_settings({"_clear_password": True, "_current_password": 123})
        assert handler.status == 403
        payload = handler.json_body()
        assert "current password" in payload.get("error", "").lower()

    def test_can_clear_password_with_correct_current_password(self):
        _set_password_raw("oldpassword")
        handler = _post_settings({"_clear_password": True, "_current_password": "oldpassword"})
        assert handler.status == 200
        payload = handler.json_body()
        assert payload.get("auth_enabled") is False


class TestFirstTimePasswordNoCurrentRequired:
    def test_first_time_password_works_without_current_password(self):
        _clear_password_raw()
        from api.auth import _invalidate_password_hash_cache
        _invalidate_password_hash_cache()
        handler = _post_settings({"_set_password": "firstpw"})
        assert handler.status == 200
        payload = handler.json_body()
        assert payload.get("auth_enabled") is True
        _clear_password_raw()
        _invalidate_password_hash_cache()

    def test_first_time_password_uses_auth_enabled_state_not_stale_hash(self, monkeypatch):
        """Disabled-auth bootstrap must not be blocked by stale password-hash state."""
        monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
        monkeypatch.setattr("api.auth.get_password_hash", lambda: "stale-hash")
        handler = _post_settings({"_set_password": "firstpw"})
        assert handler.status == 200
        payload = handler.json_body()
        assert payload.get("auth_enabled") is False


class TestEnvVarPasswordLockStillRejects:
    def test_env_var_lock_rejects_password_change(self):
        os.environ["HERMES_WEBUI_PASSWORD"] = "envpw"
        handler = _post_settings({"_set_password": "newpw", "_current_password": "envpw"})
        assert handler.status == 409

    def test_env_var_lock_rejects_password_clear(self):
        os.environ["HERMES_WEBUI_PASSWORD"] = "envpw"
        handler = _post_settings({"_clear_password": True, "_current_password": "envpw"})
        assert handler.status == 409


class TestCurrentPasswordNotPersisted:
    def test_current_password_not_in_settings_response(self):
        _set_password_raw("oldpassword")
        handler = _post_settings({"_set_password": "newpassword", "_current_password": "oldpassword"})
        assert handler.status == 200
        payload = handler.json_body()
        assert "_current_password" not in payload
        assert "current_password" not in payload
        _clear_password_raw()

    def test_current_password_not_in_get_settings(self):
        payload, status = _get_settings()
        assert status == 200
        assert "_current_password" not in payload
        assert "current_password" not in payload


class TestSettingsExposesAuthStateFields:
    def test_get_settings_has_auth_enabled(self):
        payload, status = _get_settings()
        assert status == 200
        assert "auth_enabled" in payload
        assert "password_auth_enabled" in payload

    def test_get_settings_never_exposes_password_hash(self):
        payload, status = _get_settings()
        assert status == 200
        assert "password_hash" not in payload

    def test_post_settings_includes_auth_state_fields(self):
        _clear_password_raw()
        from api.auth import _invalidate_password_hash_cache
        _invalidate_password_hash_cache()
        handler = _post_settings({"send_key": "enter"})
        assert handler.status == 200
        payload = handler.json_body()
        assert "auth_enabled" in payload
        assert "password_auth_enabled" in payload


class TestAuthDisabledAcknowledged:
    def test_acknowledged_can_be_set_when_auth_disabled(self):
        _clear_password_raw()
        from api.auth import _invalidate_password_hash_cache
        _invalidate_password_hash_cache()
        handler = _post_settings({"_auth_disabled_acknowledged": True})
        assert handler.status == 200
        payload = handler.json_body()
        assert payload.get("auth_disabled_acknowledged") is True

    def test_acknowledged_resets_when_auth_enabled(self):
        _set_password_raw("testpw")
        handler = _post_settings({"_auth_disabled_acknowledged": True, "_current_password": "testpw"})
        assert handler.status == 200
        payload = handler.json_body()
        assert payload.get("auth_disabled_acknowledged") is False
        _clear_password_raw()
        from api.auth import _invalidate_password_hash_cache
        _invalidate_password_hash_cache()

    def test_acknowledged_resets_when_setting_first_password(self):
        _clear_password_raw()
        handler = _post_settings({"_auth_disabled_acknowledged": True})
        assert handler.status == 200
        assert handler.json_body().get("auth_disabled_acknowledged") is True

        handler = _post_settings({"_set_password": "firstpw"})
        assert handler.status == 200
        payload = handler.json_body()
        assert payload.get("auth_enabled") is True
        assert payload.get("auth_disabled_acknowledged") is False
        _clear_password_raw()
        from api.auth import _invalidate_password_hash_cache
        _invalidate_password_hash_cache()

    def test_auth_status_exposes_acknowledgement_only_while_disabled(self):
        _clear_password_raw()
        handler = _post_settings({"_auth_disabled_acknowledged": True})
        assert handler.status == 200

        from api.routes import handle_get
        status_handler = _FakeHandler()
        handle_get(status_handler, urlparse("http://example.com/api/auth/status"))
        status_payload = status_handler.json_body()
        assert status_payload.get("auth_enabled") is False
        assert status_payload.get("auth_disabled_acknowledged") is True

        _set_password_raw("testpw")
        status_handler = _FakeHandler()
        handle_get(status_handler, urlparse("http://example.com/api/auth/status"))
        status_payload = status_handler.json_body()
        assert status_payload.get("auth_enabled") is True
        assert status_payload.get("auth_disabled_acknowledged") is False
        _clear_password_raw()
        from api.auth import _invalidate_password_hash_cache
        _invalidate_password_hash_cache()