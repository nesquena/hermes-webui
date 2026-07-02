"""Regression coverage for Copilot model settings vs picker catalog.

Hermes config uses providers.<provider>.models as a per-model settings map for
some built-in providers. WebUI must not mistake that for a picker allowlist for
Copilot, or a single reasoning_effort entry collapses the model dropdown.
"""


def _provider_group(payload: dict, provider_id: str) -> dict:
    for group in payload.get("groups", []):
        if group.get("provider_id") == provider_id:
            return group
    raise AssertionError(f"provider group {provider_id!r} not found: {payload.get('groups')!r}")


def test_copilot_provider_models_settings_do_not_replace_live_catalog(monkeypatch, tmp_path):
    import api.config as config

    cfg = {
        "model": {"default": "gpt-5.5", "provider": "copilot"},
        "providers": {
            "copilot": {
                "name": "GitHub Copilot",
                "api": "https://api.githubcopilot.com",
                "transport": "chat_completions",
                "models": {
                    "claude-opus-4.8": {"reasoning_effort": "xhigh"},
                },
            }
        },
    }

    monkeypatch.setattr(config, "cfg", cfg, raising=False)
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: {"test": "fingerprint"})
    monkeypatch.setattr(config, "reload_config_if_stale", lambda: None)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "_cfg_mtime", 0.0, raising=False)
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda pid: ["gpt-5.5", "claude-opus-4.8", "gpt-5.4"] if pid == "copilot" else [])

    config.invalidate_models_cache()
    payload = config.get_available_models(force_refresh=True)
    group = _provider_group(payload, "copilot")
    ids = [m["id"] for m in group["models"]]

    assert ids == ["gpt-5.5", "claude-opus-4.8", "gpt-5.4"]


def test_unknown_duplicate_copilot_provider_config_is_not_rendered(monkeypatch, tmp_path):
    import api.config as config

    cfg = {
        "model": {"default": "gpt-5.5", "provider": "copilot"},
        "providers": {
            "copilot": {"models": {"claude-opus-4.8": {}}},
            "copilot-2": {"name": "copilot", "models": {"gpt-5.5": {}}},
        },
    }

    monkeypatch.setattr(config, "cfg", cfg, raising=False)
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: {"test": "fingerprint"})
    monkeypatch.setattr(config, "reload_config_if_stale", lambda: None)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "_cfg_mtime", 0.0, raising=False)
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda pid: ["gpt-5.5", "claude-opus-4.8"] if pid == "copilot" else [])

    config.invalidate_models_cache()
    payload = config.get_available_models(force_refresh=True)
    provider_ids = [g.get("provider_id") for g in payload.get("groups", [])]

    assert "copilot" in provider_ids
    assert "copilot-2" not in provider_ids
