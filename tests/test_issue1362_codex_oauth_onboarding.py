"""Regression tests for issue #1362 — Codex OAuth from onboarding."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UI_JS = REPO / "static" / "ui.js"
NODE = shutil.which("node")


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


def test_anthropic_env_clear_waits_for_chat_env_read_lock(monkeypatch, tmp_path):
    import api.oauth as oauth
    import api.providers as providers
    from api.streaming import _ENV_LOCK

    monkeypatch.setenv("ANTHROPIC_TOKEN", "old-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "old-key")

    def _fail_before_env_lock(_env_path, _updates):
        raise RuntimeError("env write failed before process-env clear")

    monkeypatch.setattr(providers, "_write_env_file", _fail_before_env_lock)

    started = threading.Event()
    done = threading.Event()
    errors = []

    def _onboarding_clear():
        started.set()
        try:
            oauth._clear_anthropic_env_values(tmp_path)
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)
        finally:
            done.set()

    with _ENV_LOCK:
        worker = threading.Thread(target=_onboarding_clear)
        worker.start()
        assert started.wait(timeout=1)
        assert not done.wait(timeout=0.1)
        assert os.environ["ANTHROPIC_TOKEN"] == "old-token"
        assert os.environ["ANTHROPIC_API_KEY"] == "old-key"

    worker.join(timeout=1)
    assert done.is_set()
    assert errors == []
    assert "ANTHROPIC_TOKEN" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_runtime_provider_reads_use_anthropic_env_lock():
    streaming_src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    routes_src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")

    assert "resolve_runtime_provider_with_anthropic_env_lock" in streaming_src
    assert "resolve_runtime_provider_with_anthropic_env_lock" in routes_src


def test_anthropic_onboarding_setup_allows_linked_oauth_without_api_key(monkeypatch, tmp_path):
    import api.onboarding as onboarding

    # apply_onboarding_setup() short-circuits when HERMES_WEBUI_SKIP_ONBOARDING
    # is set in the environment (hosting providers like Agent37 use it to ship
    # a pre-configured WebUI). Local test runs may also set it for the same
    # reason. The test exercises the file-writing branch, so delete the var
    # for the test's scope. monkeypatch.delenv is a no-op if the var is unset.
    monkeypatch.delenv("HERMES_WEBUI_SKIP_ONBOARDING", raising=False)

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


def test_onboarding_non_custom_provider_mounts_searchable_model_picker():
    js = (REPO / "static" / "onboarding.js").read_text(encoding="utf-8")
    assert "onboardingModelPickerRoot" in js
    assert "_mountSearchableModelSelect" in js
    assert "choices:_getOnboardingProviderModelChoices()" in js
    assert "selectId:'onboardingModelSelect'" in js
    assert "customInputId:'onboardingModelInput'" in js


def test_onboarding_plain_select_fallback_rehydrates_saved_model():
    """When the searchable picker helper is unavailable, the plain <select>
    fallback must still rehydrate ONBOARDING.form.model so a saved/default
    model that isn't the first option isn't silently replaced by option[0]."""
    js = (REPO / "static" / "onboarding.js").read_text(encoding="utf-8")
    # The mount block must have an else branch that restores the plain select value.
    mount_idx = js.find("if(modelPickerRoot && typeof _mountSearchableModelSelect")
    assert mount_idx != -1
    after_mount = js[mount_idx:mount_idx + 900]
    assert "}else{" in after_mount or "} else {" in after_mount
    assert "const modelSel=$('onboardingModelSelect')" in after_mount
    assert "modelSel.value=ONBOARDING.form.model" in after_mount


def test_onboarding_custom_provider_text_input_branch_stays_intact():
    js = (REPO / "static" / "onboarding.js").read_text(encoding="utf-8")
    assert "ONBOARDING.form.provider==='custom'" in js
    assert 'id="onboardingModelInput"' in js
    assert "onboarding_custom_model_placeholder" in js
    assert "onboarding_custom_model_help" in js


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_onboarding_searchable_picker_runtime_and_submit_fallback():
    driver = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  const parenStart = src.indexOf('(', start);
  let parenDepth = 0;
  let inString = null;
  let escaped = false;
  let bodyStart = -1;
  for (let i = parenStart; i < src.length; i++) {
    const ch = src[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === inString) inString = null;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') {
      inString = ch;
      continue;
    }
    if (ch === '(') parenDepth++;
    else if (ch === ')') {
      parenDepth--;
      if (parenDepth === 0) {
        bodyStart = src.indexOf('{', i);
        break;
      }
    }
  }
  if (bodyStart < 0) throw new Error(name + ' body not found');
  let depth = 0;
  inString = null;
  escaped = false;
  for (let i = bodyStart; i < src.length; i++) {
    const ch = src[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === inString) inString = null;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') {
      inString = ch;
      continue;
    }
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + ' brace scan failed');
}

const elementsById = new Map();

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.className = '';
    this.id = '';
    this.hidden = false;
    this.disabled = false;
    this.textContent = '';
    this._value = '';
    this._selectedIndex = -1;
    this._listeners = {};
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    if (child.id) elementsById.set(child.id, child);
    if (this.tagName === 'SELECT' && child.tagName === 'OPTION' && this._selectedIndex < 0 && !child.disabled) {
      this.selectedIndex = this.children.length - 1;
    }
    return child;
  }

  addEventListener(type, handler) {
    this._listeners[type] = handler;
  }

  dispatch(type, extra = {}) {
    const handler = this._listeners[type];
    if (!handler) return;
    handler({
      type,
      target: this,
      key: extra.key,
      preventDefault() {},
    });
  }

  focus() {}

  querySelector(selector) {
    const queue = [...this.children];
    while (queue.length) {
      const node = queue.shift();
      if (selector.startsWith('.')) {
        const cls = selector.slice(1);
        if ((node.className || '').split(/\s+/).includes(cls)) return node;
      } else if (selector.startsWith('#')) {
        if (node.id === selector.slice(1)) return node;
      } else if (node.tagName === selector.toUpperCase()) {
        return node;
      }
      queue.push(...node.children);
    }
    return null;
  }

  set innerHTML(html) {
    this.children = [];
    const selectId = (html.match(/<select id="([^"]+)"/) || [])[1] || '';
    const customInputId = (html.match(/<input id="([^"]+)" class="model-custom-input"/) || [])[1] || '';

    const searchRow = new FakeElement('div');
    searchRow.className = 'model-search-row';
    const searchInput = new FakeElement('input');
    searchInput.className = 'model-search-input';
    const clearBtn = new FakeElement('button');
    clearBtn.className = 'model-search-clear';
    searchRow.appendChild(searchInput);
    searchRow.appendChild(clearBtn);

    const selectEl = new FakeElement('select');
    selectEl.id = selectId;

    const sep = new FakeElement('div');
    sep.className = 'model-group model-custom-sep';

    const customRow = new FakeElement('div');
    customRow.className = 'model-custom-row';
    const customInput = new FakeElement('input');
    customInput.className = 'model-custom-input';
    customInput.id = customInputId;
    const customBtn = new FakeElement('button');
    customBtn.className = 'model-custom-btn';
    customRow.appendChild(customInput);
    customRow.appendChild(customBtn);

    this.appendChild(searchRow);
    this.appendChild(selectEl);
    this.appendChild(sep);
    this.appendChild(customRow);
  }

  get options() {
    return this.children;
  }

  set value(value) {
    this._value = String(value);
    if (this.tagName === 'SELECT') {
      const idx = this.children.findIndex((child) => child.value === this._value);
      this._selectedIndex = idx;
    }
  }

  get value() {
    if (this.tagName === 'SELECT') {
      if (this._selectedIndex >= 0 && this.children[this._selectedIndex]) {
        return this.children[this._selectedIndex].value;
      }
    }
    return this._value || '';
  }

  set selectedIndex(index) {
    this._selectedIndex = index;
    if (this.tagName === 'SELECT') {
      this._value = index >= 0 && this.children[index] ? this.children[index].value : '';
    }
  }

  get selectedIndex() {
    return this._selectedIndex;
  }
}

const document = {
  createElement(tagName) {
    return new FakeElement(tagName);
  },
  getElementById(id) {
    return elementsById.get(id) || null;
  },
};

global.document = document;
global.window = {};
global.esc = (value) => String(value);
global.t = (key) => key === 'model_search_placeholder'
  ? 'Search models…'
  : key === 'model_custom_label'
    ? 'Custom model ID'
    : key === 'model_custom_placeholder'
      ? 'e.g. openai/gpt-5.4'
      : key;
global.li = () => '+';

eval(extractFunc('_mountSearchableModelSelect'));

const ONBOARDING = { form: { model: '' } };
const $ = (id) => document.getElementById(id);
const root = new FakeElement('div');

_mountSearchableModelSelect({
  root,
  selectId: 'onboardingModelSelect',
  customInputId: 'onboardingModelInput',
  choices: [
    { id: 'openrouter/alpha', label: 'OpenRouter Alpha' },
    { id: 'openrouter/beta', label: 'OpenRouter Beta' },
  ],
  selectedValue: ONBOARDING.form.model,
  onModelChange: (value) => { ONBOARDING.form.model = value; },
});

const selectEl = $('onboardingModelSelect');
const customInput = $('onboardingModelInput');
const searchInput = root.querySelector('.model-search-input');

const initial = {
  model: ONBOARDING.form.model,
  selectValue: selectEl.value,
  selectedIndex: selectEl.selectedIndex,
};

searchInput.value = 'beta';
searchInput.dispatch('input');
const hiddenStates = selectEl.options.map((option) => ({
  value: option.value,
  hidden: option.hidden,
}));

customInput.value = 'vendor/custom-model';
customInput.dispatch('input');
const afterCustom = {
  model: ONBOARDING.form.model,
  selectedIndex: selectEl.selectedIndex,
  selectValue: selectEl.value,
};

selectEl.value = 'openrouter/beta';
selectEl.dispatch('change');
const afterSelect = {
  model: ONBOARDING.form.model,
  customValue: customInput.value,
  selectValue: selectEl.value,
};

customInput.value = 'vendor/final-model';
customInput.dispatch('input');
const submitValue = (($('onboardingModelInput') || {}).value || ($('onboardingModelSelect') || {}).value || ONBOARDING.form.model || '').trim();

customInput.value = '   ';
customInput.dispatch('input');
const afterWhitespaceClear = {
  model: ONBOARDING.form.model,
  selectedIndex: selectEl.selectedIndex,
  selectValue: selectEl.value,
};
const whitespaceSubmitValue = (($('onboardingModelInput') || {}).value || ($('onboardingModelSelect') || {}).value || ONBOARDING.form.model || '').trim();

console.log(JSON.stringify({ initial, hiddenStates, afterCustom, afterSelect, submitValue, afterWhitespaceClear, whitespaceSubmitValue, whitespaceCustomValue: customInput.value }));
"""

    with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8", dir=REPO, delete=False) as handle:
        handle.write(driver)
        script = Path(handle.name)

    try:
        result = subprocess.run(
            [NODE, str(script), str(UI_JS)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO),
        )
    finally:
        script.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(result.stderr)

    out = json.loads(result.stdout.strip())
    assert out["initial"] == {
        "model": "openrouter/alpha",
        "selectValue": "openrouter/alpha",
        "selectedIndex": 1,
    }
    assert out["hiddenStates"] == [
        {"value": "", "hidden": True},
        {"value": "openrouter/alpha", "hidden": True},
        {"value": "openrouter/beta", "hidden": False},
    ]
    assert out["afterCustom"] == {
        "model": "vendor/custom-model",
        "selectedIndex": -1,
        "selectValue": "",
    }
    assert out["afterSelect"] == {
        "model": "openrouter/beta",
        "customValue": "",
        "selectValue": "openrouter/beta",
    }
    assert out["submitValue"] == "vendor/final-model"
    assert out["afterWhitespaceClear"] == {
        "model": "openrouter/beta",
        "selectedIndex": 2,
        "selectValue": "openrouter/beta",
    }
    assert out["whitespaceSubmitValue"] == "openrouter/beta"
    assert out["whitespaceCustomValue"] == ""
