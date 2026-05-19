from __future__ import annotations


def test_keyless_named_custom_provider_uses_placeholder_and_generic_custom(monkeypatch):
    import api.streaming as streaming

    monkeypatch.setattr(
        streaming,
        "resolve_custom_provider_connection",
        lambda provider: (None, "http://gpu.local:8000/v1"),
    )

    provider, api_key, base_url = streaming._resolve_custom_provider_runtime_overrides(
        "custom:gpu-local-8000", None, None
    )

    assert provider == "custom"
    assert api_key == "dummy-key"
    assert base_url == "http://gpu.local:8000/v1"


def test_named_custom_provider_preserves_configured_key(monkeypatch):
    import api.streaming as streaming

    monkeypatch.setattr(
        streaming,
        "resolve_custom_provider_connection",
        lambda provider: ("real-key", "http://gpu.local:8000/v1"),
    )

    provider, api_key, base_url = streaming._resolve_custom_provider_runtime_overrides(
        "custom:gpu-local-8000", None, None
    )

    assert provider == "custom"
    assert api_key == "real-key"
    assert base_url == "http://gpu.local:8000/v1"


def test_named_custom_provider_keeps_existing_runtime_base_url(monkeypatch):
    import api.streaming as streaming

    monkeypatch.setattr(
        streaming,
        "resolve_custom_provider_connection",
        lambda provider: (None, "http://config.example/v1"),
    )

    provider, api_key, base_url = streaming._resolve_custom_provider_runtime_overrides(
        "custom:runtime-local", None, "http://runtime.example/v1"
    )

    assert provider == "custom"
    assert api_key == "dummy-key"
    assert base_url == "http://runtime.example/v1"


def test_non_custom_provider_is_unchanged(monkeypatch):
    import api.streaming as streaming

    called = False

    def _unexpected(provider):
        nonlocal called
        called = True
        return (None, None)

    monkeypatch.setattr(streaming, "resolve_custom_provider_connection", _unexpected)

    provider, api_key, base_url = streaming._resolve_custom_provider_runtime_overrides(
        "openrouter", None, None
    )

    assert (provider, api_key, base_url) == ("openrouter", None, None)
    assert called is False


def test_custom_provider_env_name_is_posix_safe():
    import api.config as config

    assert config._api_key_env_name("custom:gpu.local-8000") == "CUSTOM_GPU_LOCAL_8000_API_KEY"
    assert config._api_key_env_name("custom:10.8.71.41:8080") == "CUSTOM_10_8_71_41_8080_API_KEY"
    assert config._api_key_env_name("custom/foo bar") == "CUSTOM_FOO_BAR_API_KEY"


def test_resolve_custom_provider_connection_prefers_sanitized_env(monkeypatch):
    import api.config as config

    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "custom_providers": [
                {"name": "gpu.local-8000", "base_url": "http://gpu.local:8000/v1"},
            ],
        },
    )
    monkeypatch.setenv("CUSTOM_GPU_LOCAL_8000_API_KEY", "sanitized-key")
    monkeypatch.setenv("CUSTOM:GPU.LOCAL-8000_API_KEY", "legacy-key")

    api_key, base_url = config.resolve_custom_provider_connection("custom:gpu.local-8000")

    assert api_key == "sanitized-key"
    assert base_url == "http://gpu.local:8000/v1"


def test_resolve_custom_provider_connection_falls_back_to_legacy_env(monkeypatch, caplog):
    import logging
    import api.config as config

    config._LEGACY_CUSTOM_API_KEY_ENV_WARNED.clear()
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "custom_providers": [
                {"name": "gpu.local-8000", "base_url": "http://gpu.local:8000/v1"},
            ],
        },
    )
    monkeypatch.delenv("CUSTOM_GPU_LOCAL_8000_API_KEY", raising=False)
    monkeypatch.setenv("CUSTOM:GPU.LOCAL-8000_API_KEY", "legacy-key")

    with caplog.at_level(logging.WARNING, logger="api.config"):
        api_key, _base_url = config.resolve_custom_provider_connection("custom:gpu.local-8000")

    assert api_key == "legacy-key"
    assert "CUSTOM_GPU_LOCAL_8000_API_KEY" in caplog.text
