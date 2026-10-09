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


def _setup_provider(monkeypatch, tmp_path, provider_cfg, live_ids):
    """Wire config with a single custom-llm provider and a controllable live catalog."""
    import api.config as config

    cfg = {
        "model": {"default": "model-a", "provider": "custom-llm"},
        "providers": {"custom-llm": provider_cfg},
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
    monkeypatch.setattr(
        config, "_read_live_provider_model_ids",
        lambda pid: live_ids if pid == "custom-llm" else [],
    )
    config.invalidate_models_cache()
    return config


def test_discover_models_false_pins_configured_allowlist_despite_discovered_flag(monkeypatch, tmp_path):
    """`discover_models: false` overrides `models_discovered: true` (explicit opt-out wins).

    A provider that persisted a discovered catalog but then pinned it with
    ``discover_models: false`` must keep exactly its configured ``models:`` and NOT
    probe/expose the broader live catalog.
    """
    provider_cfg = {
        "name": "Custom LLM",
        "api": "https://llm.example.com",
        "transport": "chat_completions",
        "models_discovered": True,
        "discover_models": False,
        "models": {
            "model-a": {"supports_vision": True},
            "model-b": {"context_length": 128000},
        },
    }
    live_ids = ["model-a", "model-b", "model-c", "model-d", "model-e"]
    config = _setup_provider(monkeypatch, tmp_path, provider_cfg, live_ids)

    payload = config.get_available_models(force_refresh=True)
    group = _provider_group(payload, "custom-llm")
    ids = [m["id"] for m in group["models"]]

    assert ids == ["model-a", "model-b"]


def test_discover_models_false_string_form_also_pins(monkeypatch, tmp_path):
    """Agent-compatible string opt-out (`discover_models: "false"`) also pins the allowlist."""
    provider_cfg = {
        "name": "Custom LLM",
        "api": "https://llm.example.com",
        "transport": "chat_completions",
        "models_discovered": True,
        "discover_models": "false",
        "models": {"model-a": {}, "model-b": {}},
    }
    live_ids = ["model-a", "model-b", "model-c"]
    config = _setup_provider(monkeypatch, tmp_path, provider_cfg, live_ids)

    payload = config.get_available_models(force_refresh=True)
    group = _provider_group(payload, "custom-llm")
    ids = [m["id"] for m in group["models"]]

    assert ids == ["model-a", "model-b"]


def test_discovered_catalog_survives_transient_live_probe_failure(monkeypatch, tmp_path):
    """A transient empty live probe must not drop a discovered provider's configured models.

    With ``models_discovered: true`` (and discovery allowed), the live catalog is
    authoritative — but when the probe transiently returns nothing, the provider group
    must fall back to the configured discovered IDs rather than vanishing (which would be
    cached empty for up to 24h). A provider absent from the static built-in catalog is
    the exact case #7404's fix must not regress.
    """
    provider_cfg = {
        "name": "Custom LLM",
        "api": "https://llm.example.com",
        "transport": "chat_completions",
        "models_discovered": True,
        "models": {"model-a": {"supports_vision": True}, "model-b": {"context_length": 128000}},
    }
    # Live probe returns NOTHING (transient failure), provider not in _PROVIDER_MODELS.
    config = _setup_provider(monkeypatch, tmp_path, provider_cfg, [])

    payload = config.get_available_models(force_refresh=True)
    group = _provider_group(payload, "custom-llm")
    ids = [m["id"] for m in group["models"]]

    assert ids == ["model-a", "model-b"]


def test_discovered_flag_without_models_key_and_empty_probe_does_not_500(monkeypatch, tmp_path):
    """models_discovered:true with NO models: key + empty live probe must not raise (KeyError->500).

    _provider_models_are_discovered_catalog is True on models_discovered alone, so the
    probe-failure fallback must read models via .get() (absent -> no configured rows),
    degrading to the static catalog rather than crashing /api/models.
    """
    provider_cfg = {
        "name": "Custom LLM",
        "api": "https://llm.example.com",
        "transport": "chat_completions",
        "models_discovered": True,
        # no "models" key at all
    }
    config = _setup_provider(monkeypatch, tmp_path, provider_cfg, [])

    # Must not raise; provider absent from the static catalog degrades to an empty
    # group (filtered out) rather than a 500.
    payload = config.get_available_models(force_refresh=True)
    assert isinstance(payload.get("groups"), list)
