"""Regression coverage for provider-level config flags in the model picker."""

import pathlib

import api.config as config


def _reset_models_cache():
    config._available_models_cache = None
    config._available_models_cache_ts = 0.0


def _provider_ids(payload: dict) -> set[str]:
    return {str(group.get("provider_id") or "") for group in payload.get("groups", [])}


def test_providers_only_configured_flag_does_not_create_picker_group(monkeypatch):
    """providers.only_configured is a filter flag, not a provider id (#2399)."""
    _reset_models_cache()
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": "openai", "default": "gpt-4o-mini"},
            "providers": {
                "only_configured": True,
                "openai": {"models": ["gpt-4o-mini"]},
            },
        },
        raising=False,
    )
    monkeypatch.setattr(config, "_cfg_has_in_memory_overrides", lambda: True)
    monkeypatch.setattr(
        config,
        "_get_auth_store_path",
        lambda: pathlib.Path("/tmp/hermes-webui-missing-auth-store-issue2399.json"),
    )

    try:
        payload = config.get_available_models()
    finally:
        _reset_models_cache()

    provider_ids = _provider_ids(payload)
    assert "openai" in provider_ids
    assert "only-configured" not in provider_ids
    assert all("Only-Configured" not in str(group.get("provider")) for group in payload["groups"])


def test_unknown_scalar_provider_config_flags_are_ignored(monkeypatch):
    """Unknown scalar siblings under providers must not seed phantom groups."""
    _reset_models_cache()
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": "openai", "default": "gpt-4o-mini"},
            "providers": {
                "future_toggle": "enabled",
                "openai": {"models": ["gpt-4o-mini"]},
            },
        },
        raising=False,
    )
    monkeypatch.setattr(config, "_cfg_has_in_memory_overrides", lambda: True)
    monkeypatch.setattr(
        config,
        "_get_auth_store_path",
        lambda: pathlib.Path("/tmp/hermes-webui-missing-auth-store-issue2399.json"),
    )

    try:
        payload = config.get_available_models()
    finally:
        _reset_models_cache()

    provider_ids = _provider_ids(payload)
    assert "openai" in provider_ids
    assert "future-toggle" not in provider_ids
