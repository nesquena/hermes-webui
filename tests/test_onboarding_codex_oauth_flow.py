"""Codex OAuth onboarding flow regressions for issue #1362.

The browser must never receive the provider's device_auth_id/device_code or token
payload.  The WebUI owns a short-lived server-side flow keyed by an opaque
polling token, then relies on the existing Hermes auth.json detection path once
Codex credentials are persisted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


def _reset_flows(oauth):
    with oauth._OAUTH_FLOW_LOCK:
        oauth._OAUTH_FLOWS.clear()


@pytest.fixture()
def oauth_module(monkeypatch, tmp_path):
    import api.oauth as oauth

    _reset_flows(oauth)
    monkeypatch.setattr(oauth, "_now", lambda: 1_000.0)
    yield oauth
    _reset_flows(oauth)


def test_start_codex_oauth_uses_opaque_polling_token_and_hides_device_code(oauth_module, monkeypatch):
    oauth = oauth_module
    monkeypatch.setattr(
        oauth,
        "_codex_request_device_authorization",
        lambda: {
            "device_auth_id": "provider-device-secret",
            "user_code": "ABCD-EFGH",
            "verification_url": "https://auth.openai.com/codex/device",
            "interval": 1,
            "expires_in": 900,
        },
    )

    started = oauth.start_oauth_flow("openai-codex")

    assert started["provider"] == "openai-codex"
    assert started["status"] == "pending"
    assert started["user_code"] == "ABCD-EFGH"
    assert started["verification_url"] == "https://auth.openai.com/codex/device"
    assert started["polling_token"]
    serialized = json.dumps(started)
    assert "provider-device-secret" not in serialized
    assert "device_auth_id" not in started
    assert "device_code" not in started


def test_start_rejects_non_codex_providers(oauth_module):
    with pytest.raises(ValueError, match="Unsupported OAuth provider"):
        oauth_module.start_oauth_flow("anthropic")


def test_poll_pending_returns_only_high_level_status(oauth_module, monkeypatch):
    oauth = oauth_module
    monkeypatch.setattr(
        oauth,
        "_codex_request_device_authorization",
        lambda: {
            "device_auth_id": "provider-device-secret",
            "user_code": "ABCD-EFGH",
            "verification_url": "https://auth.openai.com/codex/device",
            "interval": 1,
            "expires_in": 900,
        },
    )
    monkeypatch.setattr(oauth, "_codex_poll_device_authorization", lambda flow: None)

    token = oauth.start_oauth_flow("openai-codex")["polling_token"]
    polled = oauth.poll_oauth_flow(token)

    assert polled == {"ok": True, "provider": "openai-codex", "status": "pending"}
    assert "provider-device-secret" not in json.dumps(polled)
    assert "device_auth_id" not in polled
    assert "device_code" not in polled


def test_cancel_marks_flow_cancelled_then_stops_polling(oauth_module, monkeypatch):
    oauth = oauth_module
    monkeypatch.setattr(
        oauth,
        "_codex_request_device_authorization",
        lambda: {
            "device_auth_id": "provider-device-secret",
            "user_code": "ABCD-EFGH",
            "verification_url": "https://auth.openai.com/codex/device",
            "interval": 1,
            "expires_in": 900,
        },
    )

    token = oauth.start_oauth_flow("openai-codex")["polling_token"]
    cancelled = oauth.cancel_oauth_flow(token)
    polled = oauth.poll_oauth_flow(token)

    assert cancelled == {"ok": True, "provider": "openai-codex", "status": "cancelled"}
    assert polled == {"ok": True, "provider": "openai-codex", "status": "cancelled"}


def test_expired_flow_is_cleaned_up(oauth_module, monkeypatch):
    oauth = oauth_module
    now = {"value": 1_000.0}
    monkeypatch.setattr(oauth, "_now", lambda: now["value"])
    monkeypatch.setattr(
        oauth,
        "_codex_request_device_authorization",
        lambda: {
            "device_auth_id": "provider-device-secret",
            "user_code": "ABCD-EFGH",
            "verification_url": "https://auth.openai.com/codex/device",
            "interval": 1,
            "expires_in": 10,
        },
    )

    token = oauth.start_oauth_flow("openai-codex")["polling_token"]
    now["value"] = 1_011.0

    assert oauth.poll_oauth_flow(token) == {"ok": True, "provider": "openai-codex", "status": "expired"}
    with oauth._OAUTH_FLOW_LOCK:
        assert token not in oauth._OAUTH_FLOWS


def test_success_persists_existing_auth_json_shape_and_does_not_return_tokens(
    oauth_module, monkeypatch, tmp_path
):
    oauth = oauth_module
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        oauth,
        "_codex_request_device_authorization",
        lambda: {
            "device_auth_id": "provider-device-secret",
            "user_code": "ABCD-EFGH",
            "verification_url": "https://auth.openai.com/codex/device",
            "interval": 1,
            "expires_in": 900,
        },
    )
    monkeypatch.setattr(
        oauth,
        "_codex_poll_device_authorization",
        lambda flow: {"authorization_code": "auth-code", "code_verifier": "verifier"},
    )
    monkeypatch.setattr(
        oauth,
        "_codex_exchange_authorization_code",
        lambda authorization_code, code_verifier: {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
        },
    )

    token = oauth.start_oauth_flow("openai-codex")["polling_token"]
    polled = oauth.poll_oauth_flow(token)

    assert polled == {"ok": True, "provider": "openai-codex", "status": "success"}
    serialized = json.dumps(polled)
    assert "access-secret" not in serialized
    assert "refresh-secret" not in serialized
    auth_path = tmp_path / "auth.json"
    store = json.loads(auth_path.read_text(encoding="utf-8"))
    assert store["providers"]["openai-codex"]["tokens"]["access_token"] == "access-secret"
    from api.onboarding import _provider_oauth_authenticated

    assert _provider_oauth_authenticated("openai-codex", tmp_path) is True


def test_static_frontend_uses_onboarding_oauth_endpoints_without_auto_opening():
    src = Path("static/onboarding.js").read_text(encoding="utf-8")

    assert "api('/api/onboarding/oauth/start'" in src
    assert "api('/api/onboarding/oauth/poll'" in src
    assert "api('/api/onboarding/oauth/cancel'" in src
    assert "polling_token" in src
    assert "copyCodexOAuthCode" in src
    assert "cancelCodexOAuth" in src
    assert "window.open(" not in src
    assert "new EventSource" not in src


def test_static_frontend_never_sends_device_code_to_poll_endpoint():
    src = Path("static/onboarding.js").read_text(encoding="utf-8")
    poll_idx = src.index("/api/onboarding/oauth/poll")
    poll_block = src[poll_idx : poll_idx + 450]

    assert "polling_token" in poll_block
    assert "device_code" not in poll_block
    assert "device_auth_id" not in poll_block


def test_routes_expose_onboarding_oauth_lifecycle_endpoints():
    src = Path("api/routes.py").read_text(encoding="utf-8")

    assert 'parsed.path == "/api/onboarding/oauth/start"' in src
    assert 'parsed.path == "/api/onboarding/oauth/poll"' in src
    assert 'parsed.path == "/api/onboarding/oauth/cancel"' in src
    assert '"device_code required"' not in src
