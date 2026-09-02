"""Regression coverage for #7404: models_discovered suppresses live catalog.

When a provider sets ``models_discovered: true`` in its config alongside a
``models:`` dict of per-model metadata, WebUI must not treat the dict as a
strict allowlist.  It should fall through to the live ``/v1/models`` probe,
matching the upstream Hermes Agent behaviour.
"""


def _provider_group(payload: dict, provider_id: str) -> dict:
    for group in payload.get("groups", []):
        if group.get("provider_id") == provider_id:
            return group
    raise AssertionError(f"provider group {provider_id!r} not found: {payload.get('groups')!r}")


def test_models_discovered_skips_config_allowlist_and_uses_live_catalog(monkeypatch, tmp_path):
    import api.config as config

    cfg = {
        "model": {"default": "model-a", "provider": "custom-llm"},
        "providers": {
            "custom-llm": {
                "name": "Custom LLM",
                "api": "https://llm.example.com",
                "transport": "chat_completions",
                "models_discovered": True,
                "models": {
                    "model-a": {"supports_vision": True},
                    "model-b": {"context_length": 128000},
                },
            }
        },
    }

    live_ids = ["model-a", "model-b", "model-c", "model-d", "model-e"]

    monkeypatch.setattr(config, "cfg", cfg, raising=False)
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: {"test": "fingerprint"})
    monkeypatch.setattr(config, "reload_config_if_stale", lambda: None)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "_cfg_mtime", 0.0, raising=False)
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(
        config, "_read_live_provider_model_ids",
        lambda pid: live_ids if pid == "custom-llm" else [],
    )

    config.invalidate_models_cache()
    payload = config.get_available_models(force_refresh=True)
    group = _provider_group(payload, "custom-llm")
    ids = [m["id"] for m in group["models"]]

    assert ids == live_ids
