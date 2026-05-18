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
