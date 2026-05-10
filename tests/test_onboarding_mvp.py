"""Onboarding MVP tests — first-run wizard and provider config persistence.

Tests that call /api/onboarding/setup require PyYAML in the test server's
Python environment (the agent venv). They are skipped when hermes-agent is
not installed, since the server falls back to system Python which typically
lacks pyyaml.
"""

import json
import pathlib
import urllib.error
import urllib.request

import pytest

from tests._pytest_port import BASE

# Check if pyyaml is available — onboarding setup tests need it on the server
try:
    import yaml as _yaml

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False
_needs_yaml = pytest.mark.skipif(
    not _HAS_YAML, reason="PyYAML not installed — onboarding setup tests require it"
)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _server_hermes_home() -> pathlib.Path:
    """Get the hermes home path the test server is actually using.

    Using the server's own /api/onboarding/status response is more robust than
    reading TEST_STATE_DIR from conftest, which can get the wrong path when
    conftest is imported multiple times under different HERMES_HOME environments
    (api.config resets HERMES_HOME at module import time via init_profile_state).
    """
    data, _ = get("/api/onboarding/status")
    env_path = data.get("system", {}).get("env_path", "")
    if env_path:
        return pathlib.Path(env_path).parent
    # Fallback
    hermes_home = pathlib.Path.home() / ".hermes"
    return hermes_home / "webui-mvp-test"


@pytest.fixture(autouse=True)
def clean_hermes_config_files():
    hermes_home = _server_hermes_home()
    for rel in ("config.yaml", ".env"):
        (hermes_home / rel).unlink(missing_ok=True)
    yield
    for rel in ("config.yaml", ".env"):
        (hermes_home / rel).unlink(missing_ok=True)


def test_onboarding_status_defaults_incomplete():
    data, status = get("/api/onboarding/status")
    assert status == 200
    assert data["completed"] is False
    assert data["settings"]["password_enabled"] is False
    assert data["system"]["provider_configured"] is False
    assert data["system"]["chat_ready"] is False
    assert data["system"]["setup_state"] in {"needs_provider", "agent_unavailable"}
    assert "provider_note" in data["system"]
    assert isinstance(data["workspaces"]["items"], list)
    assert data["setup"]["providers"]


@_needs_yaml
def test_onboarding_setup_openrouter_writes_real_config_and_env():
    data, status = post(
        "/api/onboarding/setup",
        {
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "api_key": "sk-or-test",
        },
    )
    assert status == 200
    assert data["system"]["provider_configured"] is True
    assert data["system"]["provider_ready"] is True
    if data["system"]["imports_ok"] and data["system"]["hermes_found"]:
        assert data["system"]["chat_ready"] is True
        assert data["system"]["setup_state"] == "ready"
    else:
        assert data["system"]["chat_ready"] is False
        assert data["system"]["setup_state"] == "agent_unavailable"

    cfg_text = (_server_hermes_home() / "config.yaml").read_text(encoding="utf-8")
    env_text = (_server_hermes_home() / ".env").read_text(encoding="utf-8")
    assert "provider: openrouter" in cfg_text
    assert "default: anthropic/claude-sonnet-4.6" in cfg_text
    assert "OPENROUTER_API_KEY=sk-or-test" in env_text


@_needs_yaml
def test_onboarding_setup_custom_endpoint_writes_runtime_files():
    data, status = post(
        "/api/onboarding/setup",
        {
            "provider": "custom",
            "model": "google/gemma-3-27b-it",
            "base_url": "http://localhost:4000/v1",
            "api_key": "sk-custom-test",
        },
    )
    assert status == 200
    assert data["system"]["provider_configured"] is True
    assert data["system"]["provider_ready"] is True
    if data["system"]["imports_ok"] and data["system"]["hermes_found"]:
        assert data["system"]["chat_ready"] is True
        assert data["system"]["setup_state"] == "ready"
    else:
        assert data["system"]["chat_ready"] is False
        assert data["system"]["setup_state"] == "agent_unavailable"
    assert data["system"]["current_provider"] == "custom"
    assert data["system"]["current_base_url"] == "http://localhost:4000/v1"

    cfg_text = (_server_hermes_home() / "config.yaml").read_text(encoding="utf-8")
    env_text = (_server_hermes_home() / ".env").read_text(encoding="utf-8")
    assert "provider: custom" in cfg_text
    assert "default: google/gemma-3-27b-it" in cfg_text
    assert "base_url: http://localhost:4000/v1" in cfg_text
    assert "OPENAI_API_KEY=sk-custom-test" in env_text


@_needs_yaml
def test_onboarding_setup_detects_incomplete_saved_provider():
    status, code = post(
        "/api/onboarding/setup",
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4.6",
            "api_key": "sk-ant-test",
        },
    )
    assert code == 200

    (_server_hermes_home() / ".env").unlink(missing_ok=True)
    data, status_code = get("/api/onboarding/status")
    assert status_code == 200
    assert data["system"]["provider_configured"] is True
    assert data["system"]["provider_ready"] is False
    assert data["system"]["chat_ready"] is False
    assert data["system"]["setup_state"] in {"provider_incomplete", "agent_unavailable"}


@_needs_yaml
def test_onboarding_setup_rejects_missing_custom_base_url():
    data, status = post(
        "/api/onboarding/setup",
        {
            "provider": "custom",
            "model": "qwen2.5-coder",
            "api_key": "sk-test",
        },
    )
    assert status == 400
    assert "base_url is required" in data["error"]


def test_onboarding_complete_persists_flag():
    data, status = post("/api/onboarding/complete", {})
    assert status == 200
    assert data["completed"] is True

    settings = json.loads(
        (_server_hermes_home() / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["onboarding_completed"] is True

    data2, status2 = get("/api/onboarding/status")
    assert status2 == 200
    assert data2["completed"] is True


def test_onboarding_complete_preserves_other_settings():
    """Completing onboarding must not overwrite other user settings."""
    # Use send_key (a safe enum setting) to verify settings preservation
    # without contaminating bot_name or theme checks in other test files.
    # Use GET /api/settings (not onboarding status) to check preservation
    # since the onboarding status only returns a subset of settings fields.
    try:
        saved, s1 = post("/api/settings", {"send_key": "ctrl+enter"})
        assert s1 == 200
        assert saved["send_key"] == "ctrl+enter"

        _, s2 = post("/api/onboarding/complete", {})
        assert s2 == 200

        # Verify the non-onboarding setting survived the completion call
        current_settings, s3 = get("/api/settings")
        assert s3 == 200
        assert current_settings["send_key"] == "ctrl+enter"
    finally:
        # Always restore default send_key to avoid contaminating other tests
        post("/api/settings", {"send_key": "enter"})


def test_onboarding_already_completed_status():
    """After marking onboarding complete, status must reflect completed=True
    so the wizard does not re-appear for returning users."""
    done, status = post("/api/onboarding/complete", {})
    assert status == 200
    assert done["completed"] is True

    data, status2 = get("/api/onboarding/status")
    assert status2 == 200
    assert data["completed"] is True

    # Reset so test doesn't contaminate others
    post("/api/settings", {"onboarding_completed": False})


@_needs_yaml
def test_onboarding_setup_rejects_api_key_with_newline():
    """API keys containing embedded newlines must be rejected to prevent .env injection."""
    injected_key = "sk-bad" + chr(10) + "OTHER_KEY=injected"
    data, status = post(
        "/api/onboarding/setup",
        {
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "api_key": injected_key,
        },
    )
    assert status == 400
    assert "newline" in data["error"].lower()
