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


def test_onboarding_oauth_rejects_unsupported_providers(monkeypatch):
    import api.oauth as oauth

    for provider in ("nous", "qwen-oauth", "copilot", "bogus"):
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


def test_cancel_during_token_exchange_does_not_persist_credentials(monkeypatch, tmp_path):
    """Cancel arriving while the worker is mid-network-call must win.

    Without the post-exchange status re-check, the worker would proceed to
    persist credentials to auth.json AND override the cancelled status with
    "success" — silently storing tokens the user explicitly aborted.
    """
    import threading
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()

    poll_started = threading.Event()
    poll_continue = threading.Event()

    def _slow_poll(device_auth_id, user_code):
        poll_started.set()
        assert poll_continue.wait(timeout=5)
        return {"authorization_code": "auth-code", "code_verifier": "verifier"}

    def _exchange(authorization_code, code_verifier):
        return {"access_token": "ACCESS", "refresh_token": "REFRESH"}

    monkeypatch.setattr(oauth, "_poll_codex_authorization", _slow_poll)
    monkeypatch.setattr(oauth, "_exchange_codex_authorization", _exchange)

    flow_id = "race-flow"
    oauth._OAUTH_FLOWS[flow_id] = {
        "provider": "openai-codex",
        "status": "pending",
        "device_auth_id": "device-secret",
        "user_code": "ABCD-EFGH",
        "expires_at": time.time() + 600,
        "poll_interval_seconds": 1,
        "hermes_home": str(tmp_path),
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    worker = threading.Thread(target=oauth._run_codex_oauth_worker, args=(flow_id,), daemon=True)
    worker.start()
    assert poll_started.wait(timeout=5)

    oauth.cancel_onboarding_oauth_flow({"flow_id": flow_id})
    assert oauth._OAUTH_FLOWS[flow_id]["status"] == "cancelled"

    poll_continue.set()
    worker.join(timeout=5)
    assert not worker.is_alive()

    assert oauth._OAUTH_FLOWS[flow_id]["status"] == "cancelled"
    assert not (tmp_path / "auth.json").exists()


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


def test_unsupported_note_mentions_codex_and_claude_as_in_app():
    src = (REPO / "api" / "onboarding.py").read_text(encoding="utf-8")
    start = src.find("_UNSUPPORTED_PROVIDER_NOTE")
    body = src[start:start + 500]
    assert "OpenAI Codex, and GitHub" not in body
    assert "OpenAI Codex" in body and "authenticated in this onboarding flow" in body
    assert "Claude" in body or "Anthropic" in body


# ── Claude / Anthropic OAuth slice ─────────────────────────────────────────


def test_claude_provider_aliases_normalize_to_anthropic(monkeypatch, tmp_path):
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()
    monkeypatch.setattr(oauth, "_get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(oauth, "_read_claude_code_credentials", lambda: None)
    monkeypatch.setattr(oauth, "_spawn_anthropic_credential_worker", lambda fid: None)

    for alias in ("anthropic", "claude", "claude-code"):
        payload = oauth.start_onboarding_oauth_flow({"provider": alias})
        assert payload["ok"] is True
        assert payload["provider"] == "anthropic"
        assert payload["status"] == "pending"


def test_anthropic_immediate_success_when_credentials_exist(monkeypatch, tmp_path):
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()
    monkeypatch.setattr(oauth, "_get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(oauth, "_read_claude_code_credentials", lambda: {
        "accessToken": "cc-access-secret",
        "refreshToken": "cc-refresh-secret",
        "expiresAt": 9999999999999,
    })
    linked = []
    monkeypatch.setattr(oauth, "_link_anthropic_credentials", lambda hh: linked.append(str(hh)))

    payload = oauth.start_onboarding_oauth_flow({"provider": "anthropic"})

    assert payload["status"] == "success"
    assert payload["provider"] == "anthropic"
    assert linked == [str(tmp_path)]
    serialized = json.dumps(payload)
    for forbidden in ("cc-access-secret", "cc-refresh-secret", "accessToken", "refreshToken", "access_token", "refresh_token"):
        assert forbidden not in serialized


def test_anthropic_pending_payload_is_action_only_and_secret_free(monkeypatch, tmp_path):
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()
    monkeypatch.setattr(oauth, "_get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(oauth, "_read_claude_code_credentials", lambda: None)
    monkeypatch.setattr(oauth, "_spawn_anthropic_credential_worker", lambda fid: None)

    payload = oauth.start_onboarding_oauth_flow({"provider": "anthropic"})

    assert payload["status"] == "pending"
    assert payload["provider"] == "anthropic"
    assert payload["flow_id"]
    assert "action_required" in payload
    assert "claude" in payload["action_required"].lower()
    serialized = json.dumps(payload)
    for forbidden in (
        "access_token", "refresh_token", "accessToken", "refreshToken",
        ".credentials.json", ".claude", "hermes_home", str(tmp_path),
        "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
    ):
        assert forbidden not in serialized


def test_anthropic_poll_and_cancel_return_high_level_status(tmp_path):
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()
    flow_id = "claude-flow-test"
    oauth._OAUTH_FLOWS[flow_id] = {
        "provider": "anthropic",
        "status": "pending",
        "expires_at": time.time() + 60,
        "poll_interval_seconds": 5,
        "hermes_home": str(tmp_path),
    }

    assert oauth.poll_onboarding_oauth_flow(flow_id) == {
        "ok": True,
        "provider": "anthropic",
        "flow_id": flow_id,
        "status": "pending",
    }
    assert oauth.cancel_onboarding_oauth_flow({"flow_id": flow_id}) == {
        "ok": True,
        "provider": "anthropic",
        "flow_id": flow_id,
        "status": "cancelled",
    }


def test_anthropic_worker_detects_credentials_and_cancel_wins(monkeypatch, tmp_path):
    import threading
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()
    started = threading.Event()
    proceed = threading.Event()
    linked = []

    def _slow_read_creds():
        started.set()
        assert proceed.wait(timeout=5)
        return {"accessToken": "cc-access-secret", "refreshToken": "cc-refresh-secret"}

    monkeypatch.setattr(oauth, "_read_claude_code_credentials", _slow_read_creds)
    monkeypatch.setattr(oauth, "_link_anthropic_credentials", lambda hh: linked.append(str(hh)))

    flow_id = "claude-race-flow"
    oauth._OAUTH_FLOWS[flow_id] = {
        "provider": "anthropic",
        "status": "pending",
        "expires_at": time.time() + 600,
        "poll_interval_seconds": 1,
        "hermes_home": str(tmp_path),
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    worker = threading.Thread(target=oauth._run_anthropic_credential_worker, args=(flow_id,), daemon=True)
    worker.start()
    assert started.wait(timeout=5)
    oauth.cancel_onboarding_oauth_flow({"flow_id": flow_id})
    proceed.set()
    worker.join(timeout=5)

    assert oauth._OAUTH_FLOWS[flow_id]["status"] == "cancelled"
    assert not linked


def test_anthropic_cancel_during_link_keeps_flow_cancelled(monkeypatch, tmp_path):
    import threading
    import api.oauth as oauth
    from api.onboarding import _provider_oauth_authenticated

    oauth._OAUTH_FLOWS.clear()
    link_started = threading.Event()
    link_continue = threading.Event()
    monkeypatch.setattr(oauth.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(oauth, "_read_claude_code_credentials", lambda: {"accessToken": "cc-access-secret", "refreshToken": "cc-refresh-secret"})

    def _slow_clear(_home):
        link_started.set()
        assert link_continue.wait(timeout=5)

    monkeypatch.setattr(oauth, "_clear_anthropic_env_values", _slow_clear)
    flow_id = "claude-link-cancel-race"
    oauth._OAUTH_FLOWS[flow_id] = {
        "provider": "anthropic",
        "status": "pending",
        "expires_at": time.time() + 60,
        "poll_interval_seconds": 1,
        "hermes_home": str(tmp_path),
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    worker = threading.Thread(target=oauth._run_anthropic_credential_worker, args=(flow_id,), daemon=True)
    worker.start()
    assert link_started.wait(timeout=5)
    assert oauth.cancel_onboarding_oauth_flow({"flow_id": flow_id})["status"] == "cancelled"
    link_continue.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert oauth._OAUTH_FLOWS[flow_id]["status"] == "cancelled"
    assert _provider_oauth_authenticated("anthropic", tmp_path) is False


def test_anthropic_cancel_missing_flow_keeps_requested_provider():
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()

    assert oauth.cancel_onboarding_oauth_flow({"flow_id": "missing", "provider": "claude-code"}) == {
        "ok": True,
        "provider": "anthropic",
        "flow_id": "missing",
        "status": "cancelled",
    }


def test_anthropic_worker_expires_flow(tmp_path):
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()
    flow_id = "claude-expired-worker-flow"
    oauth._OAUTH_FLOWS[flow_id] = {
        "provider": "anthropic",
        "status": "pending",
        "expires_at": time.time() - 1,
        "poll_interval_seconds": 1,
        "hermes_home": str(tmp_path),
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    oauth._run_anthropic_credential_worker(flow_id)

    assert oauth._OAUTH_FLOWS[flow_id]["status"] == "expired"


def test_anthropic_worker_reports_link_errors(monkeypatch, tmp_path):
    import api.oauth as oauth

    oauth._OAUTH_FLOWS.clear()
    monkeypatch.setattr(oauth.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(oauth, "_read_claude_code_credentials", lambda: {"accessToken": "cc-access-secret", "refreshToken": "cc-refresh-secret"})

    def _raise_link_error(_home):
        raise RuntimeError("link failed without secrets")

    monkeypatch.setattr(oauth, "_link_anthropic_credentials", _raise_link_error)
    flow_id = "claude-link-error-flow"
    oauth._OAUTH_FLOWS[flow_id] = {
        "provider": "anthropic",
        "status": "pending",
        "expires_at": time.time() + 60,
        "poll_interval_seconds": 1,
        "hermes_home": str(tmp_path),
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    oauth._run_anthropic_credential_worker(flow_id)

    assert oauth._OAUTH_FLOWS[flow_id]["status"] == "error"
    assert "link failed" in oauth._OAUTH_FLOWS[flow_id]["error"]
    payload = oauth.poll_onboarding_oauth_flow(flow_id)
    assert payload == {
        "ok": True,
        "provider": "anthropic",
        "flow_id": flow_id,
        "status": "error",
        "error": "Claude Code credential linking failed. Check server logs.",
    }


def test_anthropic_link_clears_env_and_writes_secret_free_marker(monkeypatch, tmp_path):
    import os
    import api.oauth as oauth
    from api.onboarding import _provider_oauth_authenticated

    env_path = tmp_path / ".env"
    env_path.write_text("ANTHROPIC_TOKEN=old-token\nANTHROPIC_API_KEY=old-key\nOTHER=value\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_TOKEN", "old-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "old-key")

    oauth._link_anthropic_credentials(tmp_path)

    env_text = env_path.read_text(encoding="utf-8")
    assert "ANTHROPIC_TOKEN" not in env_text
    assert "ANTHROPIC_API_KEY" not in env_text
    assert "OTHER=value" in env_text
    assert "ANTHROPIC_TOKEN" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ
    auth = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    marker = auth["credential_pool"]["anthropic"][0]
    assert marker["auth_type"] == "oauth"
    assert marker["source"] == "claude_code_linked"
    assert "access_token" not in marker
    assert "refresh_token" not in marker
    assert _provider_oauth_authenticated("anthropic", tmp_path) is True
    assert _provider_oauth_authenticated("claude-code", tmp_path) is True


def test_anthropic_onboarding_setup_allows_linked_oauth_without_api_key(monkeypatch, tmp_path):
    import api.onboarding as onboarding

    cfg_path = tmp_path / "config.yaml"
    home = tmp_path / "home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({
        "credential_pool": {"anthropic": [{"auth_type": "oauth", "source": "claude_code_linked"}]}
    }), encoding="utf-8")
    monkeypatch.setattr(onboarding, "_get_config_path", lambda: cfg_path)
    monkeypatch.setattr(onboarding, "_get_active_hermes_home", lambda: home)
    monkeypatch.setattr(onboarding, "get_onboarding_status", lambda: {"ok": True})
    monkeypatch.setattr(onboarding, "reload_config", lambda: None)

    result = onboarding.apply_onboarding_setup({"provider": "anthropic", "model": "claude-sonnet-4.6"})

    assert result == {"ok": True}
    saved = cfg_path.read_text(encoding="utf-8")
    assert "provider: anthropic" in saved
    assert "default: claude-sonnet-4.6" in saved


def test_frontend_has_anthropic_oauth_support():
    js = (REPO / "static" / "onboarding.js").read_text(encoding="utf-8")
    assert "startAnthropicOAuth" in js
    assert "cancelAnthropicOAuth" in js
    assert "anthropicOAuthBtn" in js
    assert "Login with Claude Code" in js
    assert "Anthropic API key" in js
    assert "Claude Code subscription" in js
    assert "not the same as an Anthropic API key" in js
    assert "/api/onboarding/oauth/start" in js
    assert "/api/onboarding/oauth/poll" in js
    assert "/api/onboarding/oauth/cancel" in js
    assert "window.open(" not in js[js.find("startAnthropicOAuth"):]
    assert "accessToken" not in js[js.find("startAnthropicOAuth"):]
    assert "refreshToken" not in js[js.find("startAnthropicOAuth"):]
