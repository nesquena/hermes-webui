"""Regression coverage for GPT-5.6 max-reasoning capability policy."""

import pytest

from api import config as cfg
from api.gateway_chat import _gateway_reasoning_effort_for_request


KNOWN_GPT_5_6_MODELS = (
    "gpt-5.6-sol",
    "gpt-5.6-sol-pro",
    "gpt-5.6-terra",
    "gpt-5.6-terra-pro",
    "gpt-5.6-luna",
    "gpt-5.6-luna-pro",
)
OPENAI_FAMILY_PROVIDERS = (
    "openai-codex",
    "openai",
    "openai-api",
    "azure-foundry",
    "azure-openai",
    "azure",
)


@pytest.mark.parametrize("model_id", KNOWN_GPT_5_6_MODELS)
@pytest.mark.parametrize("provider_id", OPENAI_FAMILY_PROVIDERS)
def test_known_gpt_5_6_models_advertise_max_on_openai_family(model_id, provider_id):
    assert "max" in cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)


@pytest.mark.parametrize(
    "model_id,provider_id",
    [
        ("@openai-codex:gpt-5.6-sol", "openai-codex"),
        ("@openai:gpt-5.6-terra-pro", "openai"),
        ("openai/gpt-5.6-luna", "openai-api"),
        ("azure/gpt-5.6-luna-pro", "azure-foundry"),
    ],
)
def test_known_gpt_5_6_prefixed_and_provider_hinted_ids_advertise_max(model_id, provider_id):
    assert "max" in cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)


@pytest.mark.parametrize("model_id", ["gpt-5", "gpt-5.1", "gpt-5.5", "gpt-5-pro"])
def test_older_gpt_5_models_keep_xhigh_ceiling(model_id):
    efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id="openai")
    assert "xhigh" in efforts
    assert "max" not in efforts
    assert cfg.coerce_reasoning_effort_for_model("max", model_id, provider_id="openai") == "xhigh"


@pytest.mark.parametrize("model_id", ["o1", "o3-mini", "o4-mini"])
def test_o_series_models_keep_high_ceiling(model_id):
    efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id="openai-codex")
    assert efforts == ["low", "medium", "high"]
    assert cfg.coerce_reasoning_effort_for_model(
        "max", model_id, provider_id="openai-codex"
    ) == "high"


def test_unknown_gpt_5_6_variant_does_not_gain_max():
    model_id = "gpt-5.6-nebula"
    efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id="openai")
    assert "max" not in efforts
    assert cfg.coerce_reasoning_effort_for_model("max", model_id, provider_id="openai") == "xhigh"


def test_configured_provider_effort_list_still_passes_through_model_ceiling(monkeypatch):
    monkeypatch.setitem(
        cfg.cfg,
        "providers",
        {"openai": {"reasoning_efforts": ["none", "high", "xhigh", "max"]}},
    )
    assert cfg.resolve_model_reasoning_efforts(
        "gpt-5.6-sol", provider_id="openai"
    ) == ["none", "high", "xhigh", "max"]
    assert cfg.resolve_model_reasoning_efforts(
        "gpt-5.6-nebula", provider_id="openai"
    ) == ["none", "high", "xhigh"]


def test_status_metadata_advertises_and_preserves_max_for_known_gpt_5_6(monkeypatch):
    monkeypatch.setattr(
        cfg,
        "_load_yaml_config_file",
        lambda *_args, **_kwargs: {"agent": {"reasoning_effort": "max"}},
    )
    status = cfg.get_reasoning_status(
        model_id="gpt-5.6-terra-pro", provider_id="openai-codex"
    )
    assert "max" in status["supported_efforts"]
    assert status["supports_reasoning_effort"] is True
    assert status["reasoning_effort"] == "max"


@pytest.mark.parametrize("model_id", KNOWN_GPT_5_6_MODELS)
def test_max_is_preserved_by_coercion_and_gateway_streaming(model_id):
    assert cfg.coerce_reasoning_effort_for_model(
        "max", model_id, provider_id="openai-codex"
    ) == "max"
    gateway_cfg = {"agent": {"reasoning_effort": "max"}}
    assert _gateway_reasoning_effort_for_request(
        gateway_cfg, model=model_id, model_provider="openai-codex"
    ) == "max"


@pytest.mark.parametrize(
    "model_id,provider_id",
    [
        ("gemini-3-pro", "gemini"),
        ("claude-sonnet-4-5", "anthropic"),
    ],
)
def test_other_existing_model_ceilings_remain_unchanged(model_id, provider_id):
    assert "max" not in cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)
    assert cfg.coerce_reasoning_effort_for_model(
        "max", model_id, provider_id=provider_id
    ) == "xhigh"
