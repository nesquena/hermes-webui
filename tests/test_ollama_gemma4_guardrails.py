"""Guardrails for local Ollama/Gemma4 WebUI routing.

These tests lock the failure mode fixed for local Gemma4:
  - Ollama aliases must stay visible in the WebUI catalog.
  - Profile-specific model caches must not bleed across profiles.
  - Streaming must read reasoning_effort from the config dict and adopt the
    Hermes runtime "custom" transport for named local providers.
  - /api/models/live has a cold-start local Ollama fallback.
"""

from __future__ import annotations

import sys
import types


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_streaming_reasoning_config_uses_config_dict_not_cfg_attribute():
    """Regression guard: _get_config() returns a dict, not an object with .cfg."""
    src = _read("api/streaming.py")
    assert "_cfg.cfg" not in src, (
        "streaming.py must not read _cfg.cfg; that silently drops "
        "agent.reasoning_effort and prevents Gemma/Ollama think:false."
    )
    assert "_cfg.get('agent', {})" in src or '_cfg.get("agent", {})' in src


def test_streaming_adopts_custom_runtime_provider_for_named_local_providers():
    """Named local providers must use Hermes' custom transport at runtime."""
    src = _read("api/streaming.py")
    assert '_runtime_provider == "custom"' in src or "_runtime_provider == 'custom'" in src
    assert "resolved_provider = _runtime_provider" in src, (
        "ollama-local resolves to runtime provider 'custom'; without adopting it, "
        "the chat-completions transport will not send Ollama extras such as think:false."
    )


def test_routes_have_cold_start_ollama_live_models_fallback():
    """The first /api/models/live request after restart must still see Ollama."""
    src = _read("api/routes.py")
    assert 'provider in ("ollama", "ollama-local", "local-ollama")' in src
    assert "http://127.0.0.1:11434/v1" in src
    assert "_fetch_openai_compatible_models" in src


def test_disk_models_cache_is_keyed_and_rejects_wrong_profile(tmp_path, monkeypatch):
    """Disk cache entries must not be reused for a different profile/config."""
    import api.config as cfg

    monkeypatch.setattr(cfg, "_models_cache_path", tmp_path / "models_cache.json")
    payload = {
        "active_provider": "openrouter",
        "default_model": "qwen/qwen3.6-plus",
        "groups": [{"provider": "OpenRouter", "provider_id": "openrouter", "models": []}],
    }

    cfg._save_models_cache_to_disk(payload, "profile-a:mtime")

    assert cfg._load_models_cache_from_disk("profile-b:mtime") is None
    assert cfg._load_models_cache_from_disk("profile-a:mtime") == payload


def test_available_models_cache_does_not_bleed_between_profile_paths(tmp_path, monkeypatch):
    """Switching config paths must rebuild catalog instead of serving stale cache."""
    import yaml
    import api.config as cfg

    tom_cfg = tmp_path / "tom-config.yaml"
    ollama_cfg = tmp_path / "ollama-config.yaml"
    tom_cfg.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "openrouter",
                    "default": "qwen/qwen3.6-plus",
                    "base_url": "https://openrouter.ai/api/v1",
                },
                "providers": {
                    "ollama-local": {
                        "base_url": "http://127.0.0.1:11434/v1",
                        "api_key": "ollama",
                        "models": ["gemma4:26b"],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    ollama_cfg.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "ollama-local",
                    "default": "gemma4:26b",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "api_key": "ollama",
                },
                "providers": {
                    "ollama-local": {
                        "base_url": "http://127.0.0.1:11434/v1",
                        "api_key": "ollama",
                        "models": ["gemma4:26b"],
                    }
                },
                "agent": {"reasoning_effort": "none"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    current = {"path": tom_cfg}
    monkeypatch.setattr(cfg, "_get_config_path", lambda: current["path"])
    monkeypatch.setattr(cfg, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(
        cfg,
        "_fetch_openai_compatible_models",
        lambda *a, **k: [{"id": "gemma4:26b", "label": "Gemma4 (26B)"}],
    )

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda pid: {"key_source": "env"}
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

    old_cfg = dict(cfg.cfg)
    old_mtime = cfg._cfg_mtime
    old_path = cfg._cfg_path
    try:
        cfg.invalidate_models_cache()
        cfg._cfg_cache.clear()
        cfg._cfg_mtime = 0.0
        cfg._cfg_path = None

        first = cfg.get_available_models()
        assert first["active_provider"] == "openrouter"
        assert first["default_model"] == "qwen/qwen3.6-plus"
        assert any(
            m["id"] == "@ollama-local:gemma4:26b"
            for g in first["groups"]
            if g.get("provider_id") == "ollama-local"
            for m in g.get("models", [])
        )

        current["path"] = ollama_cfg
        second = cfg.get_available_models()
        assert second["active_provider"] == "ollama-local"
        assert second["default_model"] == "gemma4:26b"
        assert any(
            m["id"] == "gemma4:26b"
            for g in second["groups"]
            if g.get("provider_id") == "ollama-local"
            for m in g.get("models", [])
        )

        current["path"] = tom_cfg
        third = cfg.get_available_models()
        assert third["active_provider"] == "openrouter"
        assert third["default_model"] == "qwen/qwen3.6-plus"
    finally:
        cfg.cfg.clear()
        cfg.cfg.update(old_cfg)
        cfg._cfg_mtime = old_mtime
        cfg._cfg_path = old_path
        cfg.invalidate_models_cache()
