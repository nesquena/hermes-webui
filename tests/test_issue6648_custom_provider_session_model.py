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

    profile_cfg = {"custom_providers": [{"name": "Sensenova Primary"}]}
    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
        profile_config=profile_cfg,
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
        profile_config={"custom_providers": [{"name": "Sensenova Primary"}]},
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
        profile_config={"custom_providers": [{"name": "Sensenova Primary"}]},
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


def test_provider_name_collisions_never_normalized(monkeypatch):
    """Non-custom qualifiers are never normalized, even when the lookup would
    resolve them to a custom slug (#6718 re-gate, CORE finding 1).

    Regression cases from the review:
      - ``@openai:gpt-5.5`` with a named ``openai`` provider must reach OpenAI
        as the bare ``gpt-5.5``, never ``openai/gpt-5.5``;
      - ``custom/x`` under an ``moa`` provider must not be rerouted to moa.
    """
    import api.config as config
    import api.routes as routes

    # The lookup resolves ANY qualifier to a custom slug — the gate must reject
    # non-``custom:`` qualifiers before the lookup is even consulted.
    monkeypatch.setattr(
        config,
        "_named_custom_provider_slug_for_provider",
        lambda provider, config_obj=None: "custom:" + str(provider).removeprefix("custom:"),
    )

    assert routes._normalize_custom_qualified_session_model("@openai:gpt-5.5", "openai") is None
    assert routes._normalize_custom_qualified_session_model("@moa:ensemble", "moa") is None
    assert routes._normalize_custom_qualified_session_model("custom/x", "moa") is None
    assert routes._normalize_custom_qualified_session_model("custom/x", None) is None


def test_session_profile_config_is_source_of_truth(monkeypatch):
    """The lookup must run against the session's OWN profile config — the
    ``config_obj`` threaded into the helper must be the session profile dict
    (#6718 re-gate, CORE finding 2)."""
    import api.config as config
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }
    captured = {}

    def _fake_named_slug(provider, config_obj=None):
        captured["config_obj"] = config_obj
        if config_obj is session_cfg and provider == "custom:sensenova-primary":
            return "custom:sensenova-primary"
        return ""

    monkeypatch.setattr(config, "_named_custom_provider_slug_for_provider", _fake_named_slug)

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
        profile_config=session_cfg,
    )

    assert changed is True
    assert captured["config_obj"] is session_cfg
    assert effective == "sensenova-primary/deepseek-v4-flash", effective
    assert provider == "sensenova-primary", provider


def test_slug_missing_from_session_profile_not_leaked(monkeypatch):
    """A slug defined only in another profile (global cfg) must NOT remap a
    session whose own profile no longer defines it (#6718 re-gate, CORE
    finding 2: no global fallback when a profile config was supplied)."""
    import api.config as config
    import api.routes as routes

    # Global config still defines the slug...
    monkeypatch.setattr(
        config,
        "cfg",
        {"custom_providers": [{"name": "Sensenova Primary"}]},
    )

    # ...but the session's own profile does not. The REAL helper must not fall
    # back to the global cfg once a profile config dict was supplied.
    session_cfg = {"custom_providers": []}

    result = routes._normalize_custom_qualified_session_model(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
        profile_config=session_cfg,
    )
    assert result is None


def test_configured_qualified_value_drives_normalization():
    """End-to-end through the REAL helper: the canonical slug comes from the
    profile config entry's qualified value (custom_providers), resolved under
    the session's own profile config."""
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:deepseek-v4-flash",
        "custom:sensenova-primary",
        profile_config=session_cfg,
    )

    assert changed is True
    assert effective == "sensenova-primary/deepseek-v4-flash", effective
    assert provider == "sensenova-primary", provider


def test_colon_suffixed_model_id_normalized(monkeypatch):
    """#6718 (greptile-apps[bot]): a model id that itself contains ':' (e.g.
    ``@custom:sensenova-primary:model:free`` where the model is ``model:free``)
    must still normalize. ``_split_provider_qualified_model`` rsplit's on the
    LAST ':', so the naive qualifier ``custom:sensenova-primary:model`` matches
    no configured slug; the normalizer must walk back to the configured prefix
    and re-attach the eaten segments to the model."""
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:model:free",
        None,
        profile_config=session_cfg,
    )
    assert changed is True
    assert effective == "sensenova-primary/model:free", effective
    assert provider == "sensenova-primary", provider


def test_colon_suffixed_model_id_multi_segment(monkeypatch):
    """Multi-colon model id: ``@custom:sensenova-primary:a:b:c`` -> model ``a:b:c``."""
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:a:b:c",
        None,
        profile_config=session_cfg,
    )
    assert changed is True
    assert effective == "sensenova-primary/a:b:c", effective
    assert provider == "sensenova-primary", provider


def test_endpoint_derived_host_port_slug_not_walked_back():
    """Endpoint-derived ``custom:<host>:<port>`` slugs must stay unchanged —
    the colon walk-back must NOT eat ``8080:Qwen3`` into the model (#1776 form)."""
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }

    assert (
        routes._normalize_custom_qualified_session_model(
            "@custom:10.8.71.41:8080:Qwen3",
            None,
            profile_config=session_cfg,
        )
        is None
    )


def test_profile_config_none_fails_closed_against_global(monkeypatch):
    """A slug configured only in the module-global config must NOT normalize
    when ``profile_config=None`` — the ``None`` state is reachable from
    /api/session/new and chat/start when the profile YAML is absent/unreadable,
    and the helper chain must fail closed instead of falling back to
    ``api.config.cfg`` (#6718 re-gate, CORE 2 follow-up).

    The REAL helper is used end-to-end (no slug-lookup monkeypatch): the global
    config defines the slug, so any global fallback would resolve it.
    """
    import api.config as config
    import api.routes as routes

    # Only the module-global config defines the slug.
    monkeypatch.setattr(
        config,
        "cfg",
        {"custom_providers": [{"name": "Sensenova Primary"}]},
    )
    # Keep the resolver out of the real (network-backed) catalog — the
    # assertion here is about fail-closed normalization, not catalog resolution.
    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda *a, **k: {"default_model": "gpt-5.4"},
    )

    # Direct helper call with profile_config=None → fail closed.
    assert (
        routes._normalize_custom_qualified_session_model(
            "@custom:sensenova-primary:deepseek-v4-flash",
            None,
            profile_config=None,
        )
        is None
    )

    # The resolver entry point without a profile config also fails closed.
    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
        profile_config=None,
    )
    assert changed is False
    assert effective == "@custom:sensenova-primary:deepseek-v4-flash", effective


def test_session_new_request_threads_profile_config(monkeypatch):
    """``_session_model_state_from_request`` (the /api/session/new and
    /api/session/update entry point) must thread the session profile config
    into the normalizer — the lookup sees the session's own config dict
    (#6718 re-gate, CORE 2 follow-up)."""
    import api.config as config
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }
    captured = {}

    def _fake_named_slug(provider, config_obj=None):
        captured["config_obj"] = config_obj
        if config_obj is session_cfg and provider == "custom:sensenova-primary":
            return "custom:sensenova-primary"
        return ""

    monkeypatch.setattr(config, "_named_custom_provider_slug_for_provider", _fake_named_slug)

    model, provider = routes._session_model_state_from_request(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
        profile_config=session_cfg,
    )

    assert captured.get("config_obj") is session_cfg
    assert model == "sensenova-primary/deepseek-v4-flash", model
    assert provider == "sensenova-primary", provider


def test_session_new_request_e2e_profile_config():
    """End-to-end through the REAL helper: a named-custom model on /api/session/new
    normalizes to the canonical slug under the session's own profile config."""
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }

    model, provider = routes._session_model_state_from_request(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
        profile_config=session_cfg,
    )

    assert model == "sensenova-primary/deepseek-v4-flash", model
    assert provider == "sensenova-primary", provider
