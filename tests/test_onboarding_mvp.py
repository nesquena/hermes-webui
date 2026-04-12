import json
import pathlib
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from conftest import TEST_STATE_DIR

BASE = "http://127.0.0.1:8788"


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


@pytest.fixture(autouse=True)
def clean_hermes_config_files():
    for rel in ("config.yaml", ".env"):
        path = TEST_STATE_DIR / rel
        path.unlink(missing_ok=True)
    yield
    for rel in ("config.yaml", ".env"):
        path = TEST_STATE_DIR / rel
        path.unlink(missing_ok=True)


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

    cfg_text = (TEST_STATE_DIR / "config.yaml").read_text(encoding="utf-8")
    env_text = (TEST_STATE_DIR / ".env").read_text(encoding="utf-8")
    assert "provider: openrouter" in cfg_text
    assert "default: anthropic/claude-sonnet-4.6" in cfg_text
    assert "OPENROUTER_API_KEY=sk-or-test" in env_text


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

    cfg_text = (TEST_STATE_DIR / "config.yaml").read_text(encoding="utf-8")
    env_text = (TEST_STATE_DIR / ".env").read_text(encoding="utf-8")
    assert "provider: custom" in cfg_text
    assert "default: google/gemma-3-27b-it" in cfg_text
    assert "base_url: http://localhost:4000/v1" in cfg_text
    assert "OPENAI_API_KEY=sk-custom-test" in env_text


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

    (TEST_STATE_DIR / ".env").unlink(missing_ok=True)
    data, status_code = get("/api/onboarding/status")
    assert status_code == 200
    assert data["system"]["provider_configured"] is True
    assert data["system"]["provider_ready"] is False
    assert data["system"]["chat_ready"] is False
    assert data["system"]["setup_state"] in {"provider_incomplete", "agent_unavailable"}


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
        (TEST_STATE_DIR / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["onboarding_completed"] is True

    data2, status2 = get("/api/onboarding/status")
    assert status2 == 200
    assert data2["completed"] is True


def test_onboarding_complete_preserves_other_settings():
    saved, status = post(
        "/api/settings", {"default_model": "openai/gpt-4o", "bot_name": "Guide"}
    )
    assert status == 200
    assert saved["default_model"] == "openai/gpt-4o"

    done, status2 = post("/api/onboarding/complete", {})
    assert status2 == 200
    assert done["settings"]["default_model"] == "openai/gpt-4o"
