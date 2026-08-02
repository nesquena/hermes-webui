"""Regression tests for issue #6648.

Sessions created with a custom OpenAI-compatible provider persist the model as
``@custom:<slug>:<model>`` (or ``custom/<model>``). The Hermes runtime does not
recognize a provider named ``custom:<something>`` — it only knows the canonical
provider slug (e.g. ``sensenova-primary``). Without normalization every request
fails with "Unknown provider 'custom:...'" and exhausts the full fallback chain,
making WebUI responses 10-30x slower than Hermes Desktop.

The fix normalizes to ``<canonical-slug>/<bare_model>`` (single API call) when
the qualifier matches a NAMED ``custom_providers`` entry, and preserves the
current behavior otherwise.
"""


def test_custom_qualified_model_normalized_to_canonical_slug(monkeypatch):
    """@custom:sensenova-primary:deepseek-v4-flash -> sensenova-primary/deepseek-v4-flash."""
    import api.config as config
    import api.routes as routes

    def _fake_named_slug(provider, config_obj=None):
        return "custom:sensenova-primary" if provider == "custom:sensenova-primary" else ""

    monkeypatch.setattr(config, "_named_custom_provider_slug_for_provider", _fake_named_slug)

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
    )

    assert changed is True
    assert effective == "sensenova-primary/deepseek-v4-flash", effective
    assert provider == "sensenova-primary", provider


def test_custom_qualified_model_routes_in_single_path(monkeypatch):
    """Provider must come back canonical (no 'custom:' prefix) so the runtime resolves it."""
    import api.config as config
    import api.routes as routes

    monkeypatch.setattr(
        config,
        "_named_custom_provider_slug_for_provider",
        lambda provider, config_obj=None: "custom:sensenova-primary"
        if provider == "custom:sensenova-primary"
        else "",
    )

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:deepseek-v4-flash",
        "custom:sensenova-primary",
    )

    assert changed is True
    assert provider == "sensenova-primary"
    assert effective == "sensenova-primary/deepseek-v4-flash"
    assert "custom:" not in provider
    assert "custom:" not in effective.split("/", 1)[0]


def test_slash_custom_form_normalized(monkeypatch):
    """Persisted 'custom/<model>' form with a custom:<slug> provider is also normalized."""
    import api.config as config
    import api.routes as routes

    monkeypatch.setattr(
        config,
        "_named_custom_provider_slug_for_provider",
        lambda provider, config_obj=None: "custom:sensenova-primary"
        if provider == "custom:sensenova-primary"
        else "",
    )

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "custom/deepseek-v4-flash",
        "custom:sensenova-primary",
    )

    assert changed is True
    assert effective == "sensenova-primary/deepseek-v4-flash", effective
    assert provider == "sensenova-primary"


def test_unmatched_qualifier_preserved(monkeypatch):
    """A custom: qualifier that matches NO named entry stays untouched (no rerouting)."""
    import api.config as config
    import api.routes as routes

    monkeypatch.setattr(
        config,
        "_named_custom_provider_slug_for_provider",
        lambda provider, config_obj=None: "",
    )

    result = routes._normalize_custom_qualified_session_model(
        "@custom:unknown-slug:gpt-5.4-mini",
        None,
    )
    assert result is None


def test_non_custom_model_untouched(monkeypatch):
    """Regular @provider:model and bare models never hit the custom normalizer path."""
    import api.config as config
    import api.routes as routes

    monkeypatch.setattr(
        config,
        "_named_custom_provider_slug_for_provider",
        lambda provider, config_obj=None: "",
    )

    assert routes._normalize_custom_qualified_session_model("@deepseek:deepseek-v4-pro", None) is None
    assert routes._normalize_custom_qualified_session_model("gpt-5.4", None) is None
