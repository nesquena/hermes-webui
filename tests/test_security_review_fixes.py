import io
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote, urlsplit

import pytest


class _Headers(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _Handler:
    def __init__(self, *, client_ip="8.8.8.8", headers=None, body=b"{}"):
        self.client_address = (client_ip, 12345)
        self.headers = _Headers(headers or {})
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass


def test_onboarding_local_gate_ignores_forwarded_ip_unless_trusted(monkeypatch):
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    handler = _Handler(
        client_ip="8.8.8.8",
        headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "10.0.0.2"},
    )

    assert routes._onboarding_request_is_local(handler) is False


def test_onboarding_local_gate_uses_forwarded_ip_when_explicitly_trusted(monkeypatch):
    from api import routes

    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    handler = _Handler(
        client_ip="8.8.8.8",
        headers={"X-Forwarded-For": "10.0.0.2", "X-Real-IP": "203.0.113.11"},
    )

    assert routes._onboarding_request_is_local(handler) is True


def test_onboarding_trusted_forwarded_for_uses_proxy_appended_rightmost_ip(monkeypatch):
    from api import routes

    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    handler = _Handler(
        client_ip="10.0.0.10",
        headers={"X-Forwarded-For": "127.0.0.1, 8.8.8.8"},
    )

    assert routes._onboarding_request_is_local(handler) is False


def test_docker_env_log_obfuscates_password_and_secret_names():
    src = Path("docker_init.bash").read_text(encoding="utf-8")
    line = next(l for l in src.splitlines() if l.startswith("export ENV_OBFUSCATE_PART="))

    assert "PASSWORD" in line
    assert "SECRET" in line
    assert "TOKEN" in line
    assert "API" in line
    assert "KEY" in line


def test_public_bind_auth_requirement_helper_blocks_passwordless_container(monkeypatch):
    import server

    monkeypatch.setenv("HERMES_WEBUI_REQUIRE_AUTH_FOR_PUBLIC_BIND", "1")
    assert server._public_bind_requires_auth("0.0.0.0", within_container=True, auth_enabled=False) is True
    assert server._public_bind_requires_auth("127.0.0.1", within_container=True, auth_enabled=False) is False
    assert server._public_bind_requires_auth("0.0.0.0", within_container=True, auth_enabled=True) is False


def test_get_update_check_returns_cache_without_fetch(monkeypatch):
    from api import routes, updates

    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": True})
    monkeypatch.setattr(updates, "cached_update_status", lambda include_agent=True: {"checked_at": 123, "webui": None, "agent": None, "include_agent": include_agent})
    monkeypatch.setattr(updates, "check_for_updates", lambda *a, **k: (_ for _ in ()).throw(AssertionError("GET must not fetch")))

    handler = _Handler(client_ip="127.0.0.1")
    routes.handle_get(handler, urlsplit("/api/updates/check?force=1"))
    assert handler.status == 200


def test_cached_update_status_does_not_drop_agent_info_when_reenabled(monkeypatch):
    from api import updates

    cached_agent = {"name": "agent", "behind": 2}
    monkeypatch.setattr(
        updates,
        "_update_cache",
        {
            "webui": {"name": "webui", "behind": 0},
            "agent": cached_agent,
            "checked_at": 123,
            "include_agent": False,
        },
    )

    result = updates.cached_update_status(include_agent=True)

    assert result["agent"] == cached_agent


def test_post_update_check_performs_forced_fetch(monkeypatch):
    from api import routes

    calls = []
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": True})
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

    def fake_check(*, force=False, include_agent=True):
        calls.append((force, include_agent))
        return {"checked_at": 456, "webui": None, "agent": None}

    monkeypatch.setattr("api.updates.check_for_updates", fake_check)
    body = b'{"force": true}'
    handler = _Handler(client_ip="127.0.0.1", body=body, headers={"Content-Length": str(len(body))})
    routes.handle_post(handler, SimpleNamespace(path="/api/updates/check", query=""))
    assert handler.status == 200
    assert calls == [(True, True)]


def test_tmp_media_requires_session_token_or_explicit_extra_root(tmp_path, monkeypatch):
    from api import routes

    target = tmp_path / "notes.txt"
    target.write_text("not a chat artifact", encoding="utf-8")
    monkeypatch.delenv("MEDIA_ALLOWED_ROOTS", raising=False)
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(tmp_path / "other-workspace"))

    handler = _Handler(client_ip="127.0.0.1")
    routes._handle_media(handler, SimpleNamespace(path="/api/media", query=f"path={quote(str(target))}"))

    assert handler.status == 403
