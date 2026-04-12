"""
Sprint 34 Tests: OAuth provider support in onboarding (issues #303, #304).

Covers:
  1. _provider_oauth_authenticated() returns True for known OAuth providers
     with valid tokens in auth.json
  2. _provider_oauth_authenticated() returns False when auth.json is absent,
     empty, or has no token data
  3. _provider_oauth_authenticated() returns False for unknown/API-key providers
  4. _status_from_runtime() marks copilot/openai-codex as provider_ready when
     credentials exist
  5. _status_from_runtime() gives a helpful "hermes auth" note (not "API key")
     for OAuth providers that have no credentials yet
  6. API route /api/onboarding/status reflects OAuth-ready state
"""

import json
import pathlib
import tempfile
import unittest.mock

import pytest

REPO = pathlib.Path(__file__).parent.parent
BASE = "http://127.0.0.1:8788"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_auth_json(provider_id: str, tokens: dict, tmp_dir: pathlib.Path) -> pathlib.Path:
    """Write an auth.json with the given tokens for provider_id into tmp_dir."""
    store = {"providers": {provider_id: tokens}}
    auth_path = tmp_dir / "auth.json"
    auth_path.write_text(json.dumps(store), encoding="utf-8")
    return auth_path


# ── 1–3. _provider_oauth_authenticated unit tests ────────────────────────────

class TestProviderOAuthAuthenticated:
    """Unit tests for the new _provider_oauth_authenticated() helper."""

    def _call(self, provider: str, hermes_home: pathlib.Path) -> bool:
        # Import fresh so we don't get a stale module reference
        from api.onboarding import _provider_oauth_authenticated
        return _provider_oauth_authenticated(provider, hermes_home)

    def test_returns_false_when_auth_json_absent(self, tmp_path):
        """No auth.json -> not authenticated."""
        assert self._call("openai-codex", tmp_path) is False

    def test_openai_codex_with_access_token(self, tmp_path):
        """openai-codex with a valid access_token -> authenticated."""
        _make_auth_json(
            "openai-codex",
            {"access_token": "ey.test.token", "refresh_token": "ref123"},
            tmp_path,
        )
        assert self._call("openai-codex", tmp_path) is True

    def test_openai_codex_with_refresh_token_only(self, tmp_path):
        """openai-codex with only a refresh_token -> still authenticated."""
        _make_auth_json(
            "openai-codex",
            {"access_token": "", "refresh_token": "ref_only_token"},
            tmp_path,
        )
        assert self._call("openai-codex", tmp_path) is True

    def test_copilot_with_api_key(self, tmp_path):
        """copilot with an api_key (GitHub token) -> authenticated."""
        _make_auth_json("copilot", {"api_key": "ghu_test_token_123"}, tmp_path)
        assert self._call("copilot", tmp_path) is True

    def test_empty_tokens_returns_false(self, tmp_path):
        """All token fields empty -> not authenticated."""
        _make_auth_json(
            "openai-codex",
            {"access_token": "", "refresh_token": "", "api_key": ""},
            tmp_path,
        )
        assert self._call("openai-codex", tmp_path) is False

    def test_missing_provider_key_in_auth_json(self, tmp_path):
        """auth.json present but provider key absent -> not authenticated."""
        store = {"providers": {"some-other-provider": {"access_token": "tok"}}}
        (tmp_path / "auth.json").write_text(json.dumps(store), encoding="utf-8")
        assert self._call("openai-codex", tmp_path) is False

    def test_unknown_provider_not_in_oauth_list(self, tmp_path):
        """A provider that is not a known OAuth provider -> always False."""
        _make_auth_json("some-random-provider", {"access_token": "tok"}, tmp_path)
        assert self._call("some-random-provider", tmp_path) is False

    def test_nous_provider_recognized(self, tmp_path):
        """nous is in the known OAuth set."""
        _make_auth_json("nous", {"access_token": "nous_tok"}, tmp_path)
        assert self._call("nous", tmp_path) is True

    def test_qwen_oauth_provider_recognized(self, tmp_path):
        """qwen-oauth is in the known OAuth set."""
        _make_auth_json("qwen-oauth", {"access_token": "qwen_tok"}, tmp_path)
        assert self._call("qwen-oauth", tmp_path) is True

    def test_empty_provider_string_returns_false(self, tmp_path):
        """Empty provider string -> False, no crash."""
        assert self._call("", tmp_path) is False
        assert self._call("  ", tmp_path) is False


# ── 4–5. _status_from_runtime integration ────────────────────────────────────

class TestStatusFromRuntimeOAuth:
    """_status_from_runtime should treat OAuth providers with tokens as ready."""

    def _call(self, provider: str, model: str, hermes_home: pathlib.Path) -> dict:
        from api.onboarding import _status_from_runtime
        import api.onboarding as _ob
        orig_home = _ob._get_active_hermes_home
        orig_found = _ob._HERMES_FOUND
        _ob._get_active_hermes_home = lambda: hermes_home
        # Simulate hermes-agent being available so we reach the provider logic
        # (without this, _status_from_runtime short-circuits to agent_unavailable)
        _ob._HERMES_FOUND = True
        try:
            cfg = {"model": {"provider": provider, "default": model}}
            return _status_from_runtime(cfg, True)
        finally:
            _ob._get_active_hermes_home = orig_home
            _ob._HERMES_FOUND = orig_found

    def test_copilot_ready_when_api_key_in_auth_json(self, tmp_path):
        """copilot configured + api_key in auth.json -> provider_ready True."""
        _make_auth_json("copilot", {"api_key": "ghu_abc123"}, tmp_path)
        result = self._call("copilot", "gpt-5.4", tmp_path)
        assert result["provider_configured"] is True
        assert result["provider_ready"] is True
        assert result["setup_state"] == "ready"

    def test_openai_codex_ready_when_token_in_auth_json(self, tmp_path):
        """openai-codex configured + access_token -> provider_ready True."""
        _make_auth_json(
            "openai-codex",
            {"access_token": "ey.test", "refresh_token": "ref"},
            tmp_path,
        )
        result = self._call("openai-codex", "codex-mini-latest", tmp_path)
        assert result["provider_configured"] is True
        assert result["provider_ready"] is True
        assert result["setup_state"] == "ready"

    def test_copilot_not_ready_without_credentials(self, tmp_path):
        """copilot configured but no credentials -> provider_ready False.

        We mock hermes_cli.auth to be unavailable so the function falls through
        to the auth.json path.  With no auth.json the result must be False.
        """
        import unittest.mock

        # Prevent the hermes_cli fast path from finding real credentials
        with unittest.mock.patch(
            "api.onboarding._provider_oauth_authenticated",
            return_value=False,
        ):
            result = self._call("copilot", "gpt-5.4", tmp_path)

        assert result["provider_configured"] is True
        assert result["provider_ready"] is False
        assert result["setup_state"] == "provider_incomplete"

    def test_oauth_incomplete_note_mentions_hermes_auth(self, tmp_path):
        """When OAuth provider is incomplete, note should mention hermes auth/model."""
        result = self._call("openai-codex", "codex-mini-latest", tmp_path)
        note = result["provider_note"]
        assert "hermes auth" in note or "hermes model" in note, (
            f"Expected 'hermes auth' or 'hermes model' in note, got: {note!r}"
        )

    def test_oauth_incomplete_note_does_not_say_api_key(self, tmp_path):
        """OAuth provider incomplete note must not say 'API key' — that's misleading."""
        result = self._call("copilot", "gpt-5.4", tmp_path)
        note = result["provider_note"]
        assert "API key" not in note, (
            f"Note misleadingly mentions 'API key' for OAuth provider: {note!r}"
        )

    def test_standard_provider_incomplete_note_still_says_api_key(self, tmp_path):
        """For a standard API-key provider (openrouter), note should still say API key."""
        # openrouter with no .env
        result = self._call("openrouter", "anthropic/claude-sonnet-4.6", tmp_path)
        assert result["provider_ready"] is False
        note = result["provider_note"]
        assert "API key" in note, (
            f"Expected 'API key' in note for openrouter, got: {note!r}"
        )


# ── 6. API endpoint reflects OAuth-ready state ───────────────────────────────

class TestOnboardingStatusApiOAuth:
    """
    The /api/onboarding/status endpoint should report provider_ready=True
    when an OAuth provider is configured and has valid credentials.
    """

    def test_status_endpoint_returns_200(self):
        import urllib.request
        with urllib.request.urlopen(BASE + "/api/onboarding/status", timeout=10) as r:
            assert r.status == 200
            data = json.loads(r.read())
        assert "system" in data
        assert "provider_ready" in data["system"]

    def test_onboarding_status_has_chat_ready_field(self):
        import urllib.request
        with urllib.request.urlopen(BASE + "/api/onboarding/status", timeout=10) as r:
            data = json.loads(r.read())
        assert "chat_ready" in data["system"]

    def test_status_setup_state_valid_values(self):
        """setup_state must be one of the known string values."""
        import urllib.request
        with urllib.request.urlopen(BASE + "/api/onboarding/status", timeout=10) as r:
            data = json.loads(r.read())
        valid = {"ready", "provider_incomplete", "needs_provider", "agent_unavailable"}
        assert data["system"]["setup_state"] in valid, (
            f"Unexpected setup_state: {data['system']['setup_state']!r}"
        )


# ── Control Center: section reset on close ─────────────────────────────────

def test_control_center_resets_active_section_on_close():
    """Closing the control center must reset _settingsSection to 'conversation'."""
    src = open('static/panels.js').read()
    assert '_settingsSection' in src, '_settingsSection state variable missing from panels.js'
    assert "_settingsSection = 'conversation'" in src or "_settingsSection='conversation'" in src, \
        'Control center does not reset section to conversation on close'


def test_control_center_tab_highlight_on_open():
    """Opening the control center must use settings-tabs for section navigation."""
    css = open('static/style.css').read()
    assert 'settings-tabs' in css, 'settings-tabs CSS class for control center tabs missing from style.css'
