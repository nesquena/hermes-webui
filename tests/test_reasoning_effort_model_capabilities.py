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


def test_openai_codex_prefixed_gpt5_supports_reasoning_effort_levels():
    efforts = cfg.resolve_model_reasoning_efforts(
        "@openai-codex:gpt-5.5",
        provider_id="openai-codex",
    )
    assert "medium" in efforts
    assert "high" in efforts


def test_litellm_responses_codex_gpt5_exposes_high_and_xhigh_only():
    assert cfg.resolve_model_reasoning_efforts(
        "codex/gpt-5.5-fast",
        provider_id="litellm-responses",
    ) == ["high", "xhigh"]


def test_litellm_responses_prefixed_codex_gpt5_exposes_high_and_xhigh_only():
    assert cfg.resolve_model_reasoning_efforts(
        "@litellm-responses:codex/gpt-5.5",
        provider_id="litellm-responses",
    ) == ["high", "xhigh"]


def test_xai_grok_models_expose_high_and_xhigh_only():
    assert cfg.resolve_model_reasoning_efforts(
        "grok-4.20-reasoning",
        provider_id="xai",
    ) == ["high", "xhigh"]


def test_litellm_chat_xai_grok_models_expose_high_and_xhigh_only():
    assert cfg.resolve_model_reasoning_efforts(
        "@litellm-chat:xai/grok-4.20-reasoning",
        provider_id="litellm-chat",
    ) == ["high", "xhigh"]


def test_xai_oauth_grok_models_expose_high_and_xhigh_only():
    assert cfg.resolve_model_reasoning_efforts(
        "grok-4.20-reasoning",
        provider_id="xai-oauth",
    ) == ["high", "xhigh"]


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
