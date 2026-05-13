import os
from pathlib import Path

import pytest
import yaml


def _write_config(path: Path, model: dict) -> None:
    path.write_text(yaml.safe_dump({"model": model}, sort_keys=False), encoding="utf-8")


def _read_model(path: Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("model") or {}


def test_onboarding_confirm_overwrite_cannot_change_locked_default_model(tmp_path, monkeypatch):
    from api import onboarding

    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        {
            "provider": "openai-codex",
            "default": "gpt-5.5",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "locked": True,
        },
    )
    monkeypatch.setattr(onboarding, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(onboarding, "_get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(onboarding, "save_settings", lambda _settings: None)

    with pytest.raises(PermissionError, match="default model is locked"):
        onboarding.apply_onboarding_setup(
            {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "api_key": "test-key",
                "confirm_overwrite": True,
            }
        )

    assert _read_model(config_path) == {
        "provider": "openai-codex",
        "default": "gpt-5.5",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "locked": True,
    }


def test_guarded_yaml_save_cannot_remove_locked_model_block(tmp_path):
    from api.config import _save_yaml_config_file

    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        {
            "provider": "openai-codex",
            "default": "gpt-5.5",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "locked": True,
        },
    )

    with pytest.raises(PermissionError, match="refusing to remove model config"):
        _save_yaml_config_file(config_path, {"display": {"show_reasoning": False}})

    assert _read_model(config_path) == {
        "provider": "openai-codex",
        "default": "gpt-5.5",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "locked": True,
    }


def test_locked_default_model_same_model_save_preserves_provider_and_base_url(tmp_path, monkeypatch):
    from api import config

    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        {
            "provider": "openai-codex",
            "default": "gpt-5.5",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "locked": True,
        },
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(config_path))
    config.reload_config()

    result = config.set_hermes_default_model("gpt-5.5")

    assert result["ok"] is True
    assert _read_model(config_path) == {
        "provider": "openai-codex",
        "default": "gpt-5.5",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "locked": True,
    }
    config.reload_config()
