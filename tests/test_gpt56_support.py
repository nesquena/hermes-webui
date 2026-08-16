"""GPT-5.6 model catalog and reasoning capability contracts."""

import re
from pathlib import Path

import pytest

from api import config


GPT56_MODEL_IDS = {
    "gpt-5.6-sol",
    "gpt-5.6-sol-pro",
    "gpt-5.6-terra",
    "gpt-5.6-terra-pro",
    "gpt-5.6-luna",
    "gpt-5.6-luna-pro",
}
GPT56_ULTRA_MODEL_IDS = {
    "gpt-5.6-sol",
    "gpt-5.6-sol-pro",
    "gpt-5.6-terra",
    "gpt-5.6-terra-pro",
}


def test_openai_api_fallback_includes_all_gpt56_models():
    model_ids = {entry["id"] for entry in config._PROVIDER_MODELS["openai-api"]}

    assert GPT56_MODEL_IDS <= model_ids


def test_bare_openai_fallback_excludes_gpt56_models():
    model_ids = {entry["id"] for entry in config._PROVIDER_MODELS["openai"]}

    assert GPT56_MODEL_IDS.isdisjoint(model_ids)


def test_codex_fallback_includes_all_gpt56_models():
    model_ids = {entry["id"] for entry in config._PROVIDER_MODELS["openai-codex"]}

    assert GPT56_MODEL_IDS <= model_ids


def test_nous_fallback_includes_all_gpt56_models():
    model_ids = {entry["id"] for entry in config._PROVIDER_MODELS["nous"]}

    assert {f"@nous:openai/{model_id}" for model_id in GPT56_MODEL_IDS} <= model_ids


def test_openrouter_fallback_includes_all_gpt56_models():
    model_ids = {
        entry["id"]
        for entry in config._FALLBACK_MODELS
        if entry["provider"] == "OpenRouter"
    }

    assert {f"openai/{model_id}" for model_id in GPT56_MODEL_IDS} <= model_ids


def test_bare_openai_catalog_excludes_gpt56_models():
    model_ids = {
        entry["id"]
        for entry in config._FALLBACK_MODELS
        if entry["provider"] == "OpenAI"
    }

    assert {f"openai/{model_id}" for model_id in GPT56_MODEL_IDS}.isdisjoint(model_ids)


def test_static_model_fallback_includes_all_gpt56_models():
    html = (Path(__file__).parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    group = re.search(
        r'<optgroup label="OpenAI API" data-provider="openai-api">(.*?)</optgroup>',
        html,
        re.S,
    )

    assert group is not None
    for model_id in GPT56_MODEL_IDS:
        assert f'value="{model_id}"' in group.group(1)
        assert f'value="openai/{model_id}"' not in html


def test_live_openai_api_catalog_is_augmented_with_gpt56(monkeypatch):
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": "openai-api", "default": "gpt-5.4"},
            "providers": {"openai-api": {}},
        },
    )
    monkeypatch.setattr(
        config,
        "_read_live_provider_model_ids",
        lambda provider_id: ["gpt-5.4"] if provider_id == "openai-api" else [],
    )
    config.invalidate_models_cache()

    try:
        result = config.get_available_models()
    finally:
        config.invalidate_models_cache()
    openai_api_group = next(
        group for group in result["groups"] if group["provider_id"] == "openai-api"
    )

    assert GPT56_MODEL_IDS <= {entry["id"] for entry in openai_api_group["models"]}


@pytest.mark.parametrize(
    ("model_id", "provider", "supports_ultra"),
    [
        ("gpt-5.6-sol", "openai-api", True),
        ("openai/gpt-5.6-terra-pro", "openrouter", True),
        ("@nous:openai/gpt-5.6-luna", "nous", False),
        ("gpt-5.6-luna-pro", "openai-codex", False),
    ],
)
def test_gpt56_exposes_its_exact_reasoning_efforts(
    monkeypatch, model_id, provider, supports_ultra
):
    # GPT-5.6 is newer than models.dev metadata in some installations. Its
    # first-party capability contract must win over a stale negative result.
    monkeypatch.setattr(config, "_models_dev_reasoning_efforts", lambda *_args: [])
    expected = ["none", "low", "medium", "high", "xhigh", "max"]
    if supports_ultra:
        expected.append("ultra")

    assert config.resolve_model_reasoning_efforts(
        model_id, provider_id=provider
    ) == expected


def test_gpt56_codex_sol_exposes_supported_max_and_ultra(monkeypatch):
    monkeypatch.setattr(config, "_models_dev_reasoning_efforts", lambda *_args: [])

    assert config.resolve_model_reasoning_efforts(
        "gpt-5.6-sol", provider_id="openai-codex"
    ) == ["none", "low", "medium", "high", "xhigh", "max", "ultra"]


def test_gpt56_max_is_preserved_for_direct_openai(monkeypatch):
    monkeypatch.setattr(config, "_models_dev_reasoning_efforts", lambda *_args: [])

    assert config.coerce_reasoning_effort_for_model(
        "max", "gpt-5.6-sol", provider_id="openai-api"
    ) == "max"


def test_gpt56_max_is_preserved_for_codex(monkeypatch):
    monkeypatch.setattr(config, "_models_dev_reasoning_efforts", lambda *_args: [])

    assert config.coerce_reasoning_effort_for_model(
        "max", "gpt-5.6-sol", provider_id="openai-codex"
    ) == "max"


@pytest.mark.parametrize("provider_id", ["openai-api", "openai-codex", "openrouter"])
@pytest.mark.parametrize("model_id", sorted(GPT56_ULTRA_MODEL_IDS))
def test_sol_and_terra_ultra_is_preserved(monkeypatch, provider_id, model_id):
    monkeypatch.setattr(config, "_models_dev_reasoning_efforts", lambda *_args: [])

    assert config.coerce_reasoning_effort_for_model(
        "ultra", model_id, provider_id=provider_id
    ) == "ultra"


@pytest.mark.parametrize("provider_id", ["openai-api", "openai-codex", "openrouter"])
@pytest.mark.parametrize("model_id", ["gpt-5.6-luna", "gpt-5.6-luna-pro"])
def test_luna_ultra_degrades_to_max(monkeypatch, provider_id, model_id):
    monkeypatch.setattr(config, "_models_dev_reasoning_efforts", lambda *_args: [])

    assert config.coerce_reasoning_effort_for_model(
        "ultra", model_id, provider_id=provider_id
    ) == "max"


@pytest.mark.parametrize("provider_id", ["openai-api", "openai-codex"])
def test_gpt56_minimal_degrades_to_none(monkeypatch, provider_id):
    monkeypatch.setattr(config, "_models_dev_reasoning_efforts", lambda *_args: [])

    assert config.coerce_reasoning_effort_for_model(
        "minimal", "gpt-5.6-sol", provider_id=provider_id
    ) == "none"


def test_openrouter_older_reasoning_models_preserve_max_but_not_ultra():
    efforts = config._heuristic_reasoning_efforts(
        "openai/gpt-5.5", "openrouter"
    )

    assert efforts == ["minimal", "low", "medium", "high", "xhigh", "max"]
    assert "max" in efforts
    assert "ultra" not in efforts


def test_unknown_gpt56_variant_does_not_inherit_max_or_ultra(monkeypatch):
    monkeypatch.setattr(config, "_models_dev_reasoning_efforts", lambda *_args: None)

    for model_id in ("gpt-5.6", "gpt-5.6-mini", "gpt-5.6-sol-preview"):
        efforts = config.resolve_model_reasoning_efforts(
            model_id, provider_id="openai-api"
        )
        assert "max" not in efforts
        assert "ultra" not in efforts


def test_unknown_gpt56_variant_ultra_degrades_to_xhigh(monkeypatch):
    monkeypatch.setattr(config, "_models_dev_reasoning_efforts", lambda *_args: None)

    assert config.coerce_reasoning_effort_for_model(
        "ultra", "gpt-5.6-sol-preview", provider_id="openai-api"
    ) == "xhigh"


def test_reasoning_dropdown_includes_max_and_ultra():
    html = (Path(__file__).parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'data-effort="max"' in html
    assert 'data-effort="ultra"' in html


def test_openrouter_preserves_max_when_metadata_advertises_it(monkeypatch):
    monkeypatch.setattr(
        config,
        "_models_dev_reasoning_efforts",
        lambda *_args: ["minimal", "low", "medium", "high", "xhigh", "max"],
    )

    assert config.resolve_model_reasoning_efforts(
        "openai/gpt-5.4", provider_id="openrouter"
    )[-1] == "max"
    assert config.coerce_reasoning_effort_for_model(
        "max", "openai/gpt-5.4", provider_id="openrouter"
    ) == "max"


def test_generic_partial_capabilities_do_not_disable_reasoning(monkeypatch):
    monkeypatch.setattr(
        config,
        "_models_dev_reasoning_efforts",
        lambda *_args: ["none", "high"],
    )

    assert config.coerce_reasoning_effort_for_model(
        "low", "custom-model", provider_id="custom"
    ) == "low"
