"""Regression tests for issue #1362 — Codex OAuth from onboarding."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_onboarding_codex_oauth_routes_use_post_start_cancel_and_get_poll():
    routes = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    get_idx = routes.find("def handle_get(")
    post_idx = routes.find("def handle_post(")
    assert get_idx != -1 and post_idx != -1
    get_body = routes[get_idx:post_idx]
    post_body = routes[post_idx:]

    assert '"/api/onboarding/oauth/poll"' in get_body
    assert '"/api/onboarding/oauth/start"' not in get_body
    assert '"/api/oauth/codex/start"' not in routes
    assert '"/api/oauth/codex/poll"' not in routes
    assert '"/api/onboarding/oauth/start"' in post_body
    assert '"/api/onboarding/oauth/cancel"' in post_body


def test_onboarding_oauth_rejects_non_codex_providers(monkeypatch):
    import api.oauth as oauth

    for provider in ("anthropic", "claude", "claude-code", "nous", "qwen-oauth", "copilot", "bogus"):
        with pytest.raises(ValueError):
            oauth.start_onboarding_oauth_flow({"provider": provider})


def test_start_payload_does_not_leak_provider_device_secrets(monkeypatch, tmp_path):
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()
    monkeypatch.setattr(oauth, "_get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(oauth, "_request_codex_user_code", lambda: {
        "device_auth_id": "device-secret",
        "user_code": "ABCD-EFGH",
        "interval": 3,
    })
    monkeypatch.setattr(oauth, "_spawn_codex_oauth_worker", lambda flow_id: None)

    payload = oauth.start_onboarding_oauth_flow({"provider": "openai-codex"})

    assert payload["ok"] is True
    assert payload["provider"] == "openai-codex"
    assert payload["status"] == "pending"
    assert payload["verification_uri"] == "https://auth.openai.com/codex/device"
    assert payload["user_code"] == "ABCD-EFGH"
    serialized = json.dumps(payload)
    for forbidden in (
        "device_auth_id",
        "device-secret",
        "authorization_code",
        "code_verifier",
        "access_token",
        "refresh_token",
    ):
        assert forbidden not in serialized


def test_poll_returns_high_level_status_only(monkeypatch, tmp_path):
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()
    flow_id = "flow-test"
    oauth._OAUTH_FLOWS[flow_id] = {
        "provider": "openai-codex",
        "status": "pending",
        "device_auth_id": "device-secret",
        "user_code": "ABCD-EFGH",
        "code_verifier": "verifier-secret",
        "authorization_code": "auth-secret",
        "expires_at": time.time() + 60,
        "poll_interval_seconds": 3,
        "hermes_home": tmp_path,
    }

    payload = oauth.poll_onboarding_oauth_flow(flow_id)

    assert payload == {"ok": True, "provider": "openai-codex", "flow_id": flow_id, "status": "pending"}
    serialized = json.dumps(payload)
    for forbidden in ("device_auth_id", "device-secret", "code_verifier", "authorization_code"):
        assert forbidden not in serialized


def test_cancel_marks_flow_cancelled_and_poll_stops(tmp_path):
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()
    flow_id = "flow-cancel"
    oauth._OAUTH_FLOWS[flow_id] = {
        "provider": "openai-codex",
        "status": "pending",
        "expires_at": time.time() + 60,
        "hermes_home": tmp_path,
    }

    cancelled = oauth.cancel_onboarding_oauth_flow({"flow_id": flow_id})
    polled = oauth.poll_onboarding_oauth_flow(flow_id)

    assert cancelled["status"] == "cancelled"
    assert polled["status"] == "cancelled"


def test_expired_flow_reports_expired_and_drops_sensitive_lifecycle(tmp_path):
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()
    flow_id = "flow-expired"
    oauth._OAUTH_FLOWS[flow_id] = {
        "provider": "openai-codex",
        "status": "pending",
        "device_auth_id": "device-secret",
        "expires_at": time.time() - 1,
        "hermes_home": tmp_path,
    }

    payload = oauth.poll_onboarding_oauth_flow(flow_id)

    assert payload["status"] == "expired"
    assert oauth._OAUTH_FLOWS[flow_id]["status"] == "expired"
    assert "device_auth_id" not in oauth._OAUTH_FLOWS[flow_id]


def test_codex_credentials_written_to_active_profile_auth_json(monkeypatch, tmp_path):
    import api.oauth as oauth
    from api.onboarding import _provider_oauth_authenticated

    active_home = tmp_path / "active-profile"
    realish_home = tmp_path / "process-home"
    active_home.mkdir()
    realish_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: realish_home)

    auth_path = oauth._persist_codex_credentials(
        active_home,
        {"access_token": "access-secret", "refresh_token": "refresh-secret"},
    )

    assert auth_path == active_home / "auth.json"
    assert auth_path.exists()
    assert not (realish_home / ".hermes" / "auth.json").exists()
    mode = stat.S_IMODE(auth_path.stat().st_mode)
    assert mode == 0o600
    store = json.loads(auth_path.read_text(encoding="utf-8"))
    entry = store["credential_pool"]["openai-codex"][0]
    assert entry["auth_type"] == "oauth"
    assert entry["source"] == "manual:device_code"
    assert entry["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert _provider_oauth_authenticated("openai-codex", active_home) is True


def test_frontend_uses_onboarding_oauth_endpoints_and_no_secret_poll_url():
    js = (REPO / "static" / "onboarding.js").read_text(encoding="utf-8")
    assert "/api/onboarding/oauth/start" in js
    assert "/api/onboarding/oauth/poll" in js
    assert "/api/onboarding/oauth/cancel" in js
    assert "window.open(verification_uri" not in js
    assert "device_code=" not in js
    assert "device_code" not in js
    assert "flow_id" in js
    assert "copyCodexOAuthCode" in js
    assert "cancelCodexOAuth" in js


def test_unsupported_note_no_longer_calls_openai_codex_terminal_first():
    src = (REPO / "api" / "onboarding.py").read_text(encoding="utf-8")
    start = src.find("_UNSUPPORTED_PROVIDER_NOTE")
    body = src[start:start + 400]
    assert "OpenAI Codex, and GitHub" not in body
    assert "OpenAI Codex" in body and "authenticated in this onboarding flow" in body
    assert "Anthropic" not in body
    assert "Claude" not in body
