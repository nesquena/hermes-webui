"""Tests for model-aware reasoning effort chip visibility."""

from api import config as cfg


def test_cursor_acp_models_do_not_support_reasoning_effort_levels():
    assert cfg.resolve_model_reasoning_efforts(
        "cursor/composer-2.5",
        provider_id="cursor-acp",
    ) == []


def test_openai_codex_gpt5_supports_reasoning_effort_levels():
    efforts = cfg.resolve_model_reasoning_efforts(
        "gpt-5.5",
        provider_id="openai-codex",
    )
    assert "medium" in efforts
    assert "high" in efforts
    assert "xhigh" in efforts
    assert "max" not in efforts


def test_openai_codex_prefixed_gpt5_supports_reasoning_effort_levels():
    efforts = cfg.resolve_model_reasoning_efforts(
        "@openai-codex:gpt-5.5",
        provider_id="openai-codex",
    )
    assert "medium" in efforts
    assert "high" in efforts
    assert "xhigh" in efforts
    assert "max" not in efforts


def test_openai_codex_max_effort_is_clamped_before_streaming():
    assert cfg.coerce_reasoning_effort_for_model(
        "max",
        "gpt-5.5",
        provider_id="openai-codex",
    ) == "xhigh"


def test_unsupported_xhigh_degrades_to_high_not_disabled():
    # o1/o3/o4 on openai-codex cap at low/medium/high. A configured xhigh (or
    # max) must clamp DOWN to the highest supported level (high), not silently
    # disable reasoning by returning "".
    assert cfg.coerce_reasoning_effort_for_model(
        "xhigh",
        "o3-mini",
        provider_id="openai-codex",
    ) == "high"
    assert cfg.coerce_reasoning_effort_for_model(
        "max",
        "o3-mini",
        provider_id="openai-codex",
    ) == "high"


def test_coerce_never_escalates_above_configured_effort():
    # A supported lower effort is returned verbatim; coercion only degrades.
    assert cfg.coerce_reasoning_effort_for_model(
        "low",
        "gpt-5.5",
        provider_id="openai-codex",
    ) == "low"


def test_coerce_preserves_effort_for_unrecognized_model():
    # #3505 review: resolve_model_reasoning_efforts() returns [] for BOTH
    # known-unsupported AND simply-unrecognized models (custom providers,
    # aggregator-rewritten ids, brand-new releases). Coercion must NOT silently
    # drop a configured effort just because we don't recognize the model — that
    # would be a behavior change vs sending it verbatim (master). Preserve the
    # configured level for an empty/unknown capability set; the provider stays
    # the final authority. The known-bad CLAMP paths return a NON-empty set, so
    # they are unaffected (covered by the openai-codex tests above).
    assert cfg.coerce_reasoning_effort_for_model(
        "high",
        "some-unknown-model-xyz",
        provider_id="some-custom-provider",
    ) == "high"
    assert cfg.coerce_reasoning_effort_for_model(
        "max",
        "brand-new-model-2099",
        provider_id="some-custom-provider",
    ) == "max"
    # 'none' / unset still pass through unchanged for unknown models.
    assert cfg.coerce_reasoning_effort_for_model(
        "none", "some-unknown-model-xyz", provider_id="custom"
    ) == "none"
    assert cfg.coerce_reasoning_effort_for_model(
        "", "some-unknown-model-xyz", provider_id="custom"
    ) == ""


def test_github_copilot_gpt5_supports_reasoning_effort_levels():
    efforts = cfg.resolve_model_reasoning_efforts(
        "gpt-5.5",
        provider_id="github-copilot",
    )
    assert "medium" in efforts
    assert "high" in efforts


def test_openrouter_anthropic_models_keep_reasoning_effort_levels():
    efforts = cfg.resolve_model_reasoning_efforts(
        "anthropic/claude-sonnet-4.5",
        provider_id="openrouter",
    )
    assert "medium" in efforts
    assert "high" in efforts


def test_non_reasoning_http_models_hide_reasoning_effort_levels():
    assert cfg.resolve_model_reasoning_efforts(
        "meta-llama/llama-3.1-8b-instruct",
        provider_id="openrouter",
    ) == []


def test_get_reasoning_status_includes_supported_efforts(monkeypatch):
    monkeypatch.setattr(
        cfg,
        "resolve_model_reasoning_efforts",
        lambda *a, **k: ["low", "medium", "high"],
    )
    status = cfg.get_reasoning_status(
        model_id="gpt-5.5",
        provider_id="openai-codex",
    )
    assert status["supported_efforts"] == ["low", "medium", "high"]
    assert status["supports_reasoning_effort"] is True
    assert status["reasoning_default_on"] is True


def test_get_reasoning_status_unrecognized_model_still_offers_efforts(monkeypatch):
    """Unrecognized models get the full effort list but reasoning_default_on=False."""
    monkeypatch.setattr(
        cfg,
        "resolve_model_reasoning_efforts",
        lambda *a, **k: [],
    )
    # Hermetic guard: with resolve_model_reasoning_efforts -> [], the gate's
    # `elif` branch fires and calls _models_dev_reasoning_efforts. Mock it to
    # None so the test is independent of live models.dev catalog state.
    monkeypatch.setattr(
        cfg,
        "_models_dev_reasoning_efforts",
        lambda *a, **k: None,
    )
    status = cfg.get_reasoning_status(
        model_id="some-unknown-model",
        provider_id="custom:myproxy",
    )
    assert len(status["supported_efforts"]) > 0, (
        "Unrecognized models should still expose effort levels"
    )
    assert status["supports_reasoning_effort"] is True
    assert status["reasoning_default_on"] is False


def test_get_reasoning_status_recognized_model_default_on(monkeypatch):
    """Recognized reasoning models have reasoning_default_on=True."""
    monkeypatch.setattr(
        cfg,
        "resolve_model_reasoning_efforts",
        lambda *a, **k: list(cfg.VALID_REASONING_EFFORTS),
    )
    status = cfg.get_reasoning_status(
        model_id="deepseek-r1",
        provider_id="custom:newapi",
    )
    assert status["reasoning_default_on"] is True
    assert status["supports_reasoning_effort"] is True


def test_get_reasoning_status_cursor_acp_not_supported():
    """Cursor/Copilot ACP models explicitly return supports_reasoning_effort=False."""
    status = cfg.get_reasoning_status(
        model_id="cursor/composer-2.5",
        provider_id="cursor-acp",
    )
    assert status["supports_reasoning_effort"] is False
    assert status["reasoning_default_on"] is False
    assert status["supported_efforts"] == []


def test_get_reasoning_status_explicitly_unsupported_via_metadata(monkeypatch):
    """Models explicitly marked unsupported in capabilities dev metadata return supports_reasoning_effort=False."""
    monkeypatch.setattr(
        cfg,
        "_models_dev_reasoning_efforts",
        lambda *a, **k: [],
    )
    status = cfg.get_reasoning_status(
        model_id="gpt-4o",
        provider_id="openai",
    )
    assert status["supports_reasoning_effort"] is False
    assert status["reasoning_default_on"] is False
    assert status["supported_efforts"] == []


def test_get_reasoning_status_copilot_disagreement_authoritative(monkeypatch):
    """Authoritative supported efforts from copilot resolver should not be overridden by standard catalog fallback."""
    monkeypatch.setattr(
        cfg,
        "resolve_model_reasoning_efforts",
        lambda model_id, provider_id, **k: ["medium", "high"] if provider_id == "copilot" else [],
    )
    called_metadata_check = False

    def mock_metadata_efforts(model_id, provider_id):
        nonlocal called_metadata_check
        called_metadata_check = True
        return []

    monkeypatch.setattr(
        cfg,
        "_models_dev_reasoning_efforts",
        mock_metadata_efforts,
    )
    status = cfg.get_reasoning_status(
        model_id="copilot/gpt-4o",
        provider_id="copilot",
    )
    assert status["supports_reasoning_effort"] is True
    assert status["supported_efforts"] == ["medium", "high"]
    assert not called_metadata_check, "Should not query models.dev metadata since resolver returned success"


def test_models_dev_reasoning_efforts_precedence_loop(monkeypatch):
    """The bare-model metadata lookup should try standard providers in a deterministic order."""
    import sys
    from types import ModuleType
    called_providers = []

    def mock_get_model_capabilities(provider, model):
        called_providers.append(provider)
        return None

    mock_agent = ModuleType("agent")
    mock_models_dev = ModuleType("agent.models_dev")
    mock_models_dev.get_model_capabilities = mock_get_model_capabilities

    monkeypatch.setitem(sys.modules, "agent", mock_agent)
    monkeypatch.setitem(sys.modules, "agent.models_dev", mock_models_dev)

    cfg._models_dev_reasoning_efforts("gpt-4o", "custom-proxy")

    expected_order = [
        "openai", "anthropic", "gemini", "google", "deepseek",
        "xai", "mistral", "copilot", "openrouter"
    ]
    assert called_providers == ["custom-proxy"] + expected_order
