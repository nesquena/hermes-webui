"""Regression coverage for Agent model-metadata reasoning capability lookup."""

import sys
import types

import pytest
from types import SimpleNamespace


def _install_fake_models_dev(monkeypatch, fake_fn):
    fake_agent = types.ModuleType("agent")
    fake_models_dev = types.ModuleType("agent.models_dev")
    setattr(fake_models_dev, "get_model_capabilities", fake_fn)
    setattr(fake_agent, "models_dev", fake_models_dev)
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.models_dev", fake_models_dev)


def test_models_dev_true_returns_full_efforts(monkeypatch):
    _install_fake_models_dev(
        monkeypatch,
        lambda provider, model: SimpleNamespace(supports_reasoning=True),
    )

    import api.config as cfg

    # Since the 2026-09-09 gate (#6018), ultra is GPT-5.6-model-scoped: a
    # metadata-true Grok returns the full ladder MINUS the Codex-only ultra.
    assert cfg._models_dev_reasoning_efforts("grok-4.3", "xai-oauth") == list(
        e for e in cfg.VALID_REASONING_EFFORTS if e != "ultra"
    )


def test_models_dev_false_returns_authoritative_empty(monkeypatch):
    _install_fake_models_dev(
        monkeypatch,
        lambda provider, model: SimpleNamespace(supports_reasoning=False),
    )

    import api.config as cfg

    assert cfg._models_dev_reasoning_efforts("grok-4.20-non-reasoning", "xai-oauth") == []


def test_models_dev_unknown_allows_compatibility_fallback(monkeypatch):
    _install_fake_models_dev(monkeypatch, lambda provider, model: None)

    import api.config as cfg

    # Grok is not GPT-5.6: ultra strips from the compatibility ladder too.
    assert cfg.resolve_model_reasoning_efforts(
        "x-ai/grok-4", provider_id="openrouter"
    ) == [e for e in cfg.VALID_REASONING_EFFORTS if e != "ultra"]


def test_xai_oauth_grok_uses_agent_metadata(monkeypatch):
    seen = []

    def fake_capabilities(provider, model):
        seen.append((provider, model))
        return SimpleNamespace(supports_reasoning=True)

    _install_fake_models_dev(monkeypatch, fake_capabilities)

    import api.config as cfg

    assert cfg.resolve_model_reasoning_efforts(
        "@xai-oauth:grok-4.3", provider_id="xai-oauth"
    ) == [e for e in cfg.VALID_REASONING_EFFORTS if e != "ultra"]
    assert seen == [("xai-oauth", "grok-4.3")]


def test_models_dev_false_suppresses_prefix_heuristic(monkeypatch):
    _install_fake_models_dev(
        monkeypatch,
        lambda provider, model: SimpleNamespace(supports_reasoning=False),
    )

    import api.config as cfg

    assert cfg.resolve_model_reasoning_efforts(
        "x-ai/grok-4-non-reasoning", provider_id="openrouter"
    ) == []


def test_codex_gpt55_uses_models_dev_excluding_unsupported_max(monkeypatch):
    _install_fake_models_dev(
        monkeypatch,
        lambda provider, model: SimpleNamespace(supports_reasoning=True),
    )

    import api.config as cfg

    result = cfg.resolve_model_reasoning_efforts(
        "gpt-5.5", provider_id="openai-codex"
    )
    assert "xhigh" in result
    assert "max" not in result


def test_codex_metadata_false_returns_empty(monkeypatch):
    _install_fake_models_dev(
        monkeypatch,
        lambda provider, model: SimpleNamespace(supports_reasoning=False),
    )

    import api.config as cfg

    assert cfg.resolve_model_reasoning_efforts(
        "gpt-5.5", provider_id="openai-codex"
    ) == []


def test_copilot_gpt55_caps_at_high(monkeypatch):
    import api.config as cfg

    try:
        from hermes_cli.models import github_model_reasoning_efforts
    except ImportError:
        pytest.skip("hermes_cli not available")

    result = cfg.resolve_model_reasoning_efforts(
        "gpt-5.5", provider_id="copilot"
    )
    assert "xhigh" not in result


def test_get_reasoning_status_uses_config_default_model(monkeypatch, tmp_path):
    _install_fake_models_dev(
        monkeypatch,
        lambda provider, model: SimpleNamespace(supports_reasoning=True)
        if (provider, model) == ("xai-oauth", "grok-4.3")
        else None,
    )

    import api.config as cfg

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model:
  default: grok-4.3
  provider: xai-oauth
agent:
  reasoning_effort: medium
display:
  show_reasoning: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)

    status = cfg.get_reasoning_status()

    assert status["reasoning_effort"] == "medium"
    # Grok-4.3 keeps the full ladder minus the GPT-5.6-only ultra tier.
    assert status["supported_efforts"] == [
        e for e in cfg.VALID_REASONING_EFFORTS if e != "ultra"
    ]
    assert status["supports_reasoning_effort"] is True
