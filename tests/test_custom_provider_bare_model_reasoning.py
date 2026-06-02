"""Regression tests: custom providers with non-slash model names expose reasoning efforts.

Custom API aggregators (e.g. New API, One API) route requests using their own
naming conventions — bare names like ``deepseek-v4-flash`` or dot-separated
names like ``moonshotai.kimi-k2.5`` — rather than the OpenRouter-style
``vendor/model`` slash format that the heuristic prefix list was written for.

Before this fix, ``resolve_model_reasoning_efforts`` always returned ``[]`` for
these combinations, hiding the reasoning effort selector in the UI even though
the underlying models fully support thinking/reasoning.
"""

import pytest

import api.config as cfg


# ── bare model names (no slash or dot prefix) ────────────────────────────────

def test_deepseek_v4_flash_bare_name_custom_provider():
    efforts = cfg.resolve_model_reasoning_efforts(
        "deepseek-v4-flash",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}, (
        "deepseek-v4-flash via custom provider should expose reasoning efforts"
    )


def test_deepseek_r1_bare_name_custom_provider():
    efforts = cfg.resolve_model_reasoning_efforts(
        "deepseek-r1",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}


@pytest.mark.parametrize(
    "model_id",
    [
        "deepseek.v3.2",
        "deepseek_v3_2",
        "vendor.deepseek.v3.2",
        "deepseek.v4-flash",
        "deepseek_v4_flash",
    ],
)
def test_deepseek_separator_variants_custom_provider(model_id):
    efforts = cfg.resolve_model_reasoning_efforts(
        model_id,
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}, (
        f"{model_id} via custom provider should expose reasoning efforts"
    )


# ── dot-separated model names (vendor.model) ─────────────────────────────────

def test_kimi_dot_separated_custom_provider():
    efforts = cfg.resolve_model_reasoning_efforts(
        "moonshotai.kimi-k2.5",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}, (
        "moonshotai.kimi-k2.5 via custom provider should expose reasoning efforts"
    )


def test_qwen3_dot_separated_custom_provider():
    efforts = cfg.resolve_model_reasoning_efforts(
        "qwen.qwen3-vl-235b-a22b-instruct",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}


# ── "thinking" keyword in model name ─────────────────────────────────────────

def test_thinking_keyword_in_model_name_custom_provider():
    efforts = cfg.resolve_model_reasoning_efforts(
        "vendor.some-model-thinking-preview",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}, (
        "model name containing 'thinking' should always expose reasoning efforts"
    )


def test_reasoning_keyword_in_model_name_custom_provider():
    efforts = cfg.resolve_model_reasoning_efforts(
        "vendor.model-reasoning-v1",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}


# ── non-reasoning models must stay hidden ─────────────────────────────────────

def test_plain_llm_bare_name_custom_provider_no_reasoning():
    assert cfg.resolve_model_reasoning_efforts(
        "llama-3.1-8b-instruct",
        provider_id="custom:newapi",
    ) == [], (
        "generic llama model via custom provider should NOT expose reasoning efforts"
    )


def test_plain_llm_dot_separated_custom_provider_no_reasoning():
    assert cfg.resolve_model_reasoning_efforts(
        "meta.llama-3.1-70b",
        provider_id="custom:newapi",
    ) == []


@pytest.mark.parametrize(
    "model_id",
    [
        "thinkinghub.llama-3.1-70b",
        "reasoninghub.llama-3.1-70b",
    ],
)
def test_vendor_prefix_keyword_does_not_trigger_reasoning(model_id):
    assert cfg.resolve_model_reasoning_efforts(
        model_id,
        provider_id="custom:newapi",
    ) == []


# ── slash-prefixed names must still work (no regression) ─────────────────────

def test_deepseek_slash_prefix_still_works():
    efforts = cfg.resolve_model_reasoning_efforts(
        "deepseek/deepseek-v4-flash",
        provider_id="custom:newapi",
    )
    assert set(efforts) >= {"low", "medium", "high"}


def test_openrouter_slash_prefix_unaffected():
    efforts = cfg.resolve_model_reasoning_efforts(
        "anthropic/claude-sonnet-4.5",
        provider_id="openrouter",
    )
    assert set(efforts) >= {"low", "medium", "high"}


def test_generalized_model_families_and_suffixed_ids():
    test_models = [
        # GPT
        ("gpt-5.5", "custom:newapi"),
        ("gpt-6-ultra", "custom:newapi"),
        # Claude
        ("claude-sonnet-4-6-free", "opencode-zen"),
        ("claude-opus-4-7:free", "kilocode"),
        ("claude-sonnet-3-7-free", "opencode-zen"),
        # Qwen
        ("qwen-3-coder-free", "opencode-zen"),
        ("qwen-4-coder:free", "opencode-zen"),
        # Minimax
        ("minimax-m2.5-free", "opencode-zen"),
        ("minimax-m3-pro", "custom:newapi"),
        # Mimo
        ("mimo-v2.5-free", "opencode-zen"),
        ("mimo-v3-pro", "custom:newapi"),
        # GLM
        ("glm-5.1:free", "kilocode"),
        ("glm-6-pro", "custom:newapi"),
        # Step
        ("step-1.5:free", "kilocode"),
        ("step-2-pro", "custom:newapi"),
        # DeepSeek
        ("deepseek-v5-free", "custom:newapi"),
        ("deepseek-r3:free", "kilocode"),
        # Kimi
        ("kimi-k2.6-free", "opencode-zen"),
        ("kimi-k3-pro:free", "kilocode")
    ]

    for model_id, provider_id in test_models:
        efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)
        assert set(efforts) >= {"low", "medium", "high"}, (
            f"Failed: {model_id} via {provider_id} should resolve reasoning support"
        )


def test_unsupported_model_families_and_versions():
    unsupported_models = [
        # GPT: only 5+ supports reasoning_effort — gpt-4o/4.1/3.5 must be excluded
        ("gpt-4o", "opencode-zen"),
        ("gpt-4o-mini", "opencode-zen"),
        ("gpt-4.1", "kilocode"),
        ("gpt-4-turbo", "custom:newapi"),
        ("gpt-3.5-turbo", "opencode-zen"),
        # Claude
        ("claude-sonnet-3.5", "opencode-zen"),
        ("claude-opus-3-5-free", "kilocode"),
        # Qwen
        ("qwen-2.5-coder-free", "opencode-zen"),
        ("qwen-2-7b-instruct", "custom:newapi"),
    ]

    for model_id, provider_id in unsupported_models:
        efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)
        assert efforts == [], (
            f"Failed: {model_id} via {provider_id} should NOT resolve reasoning support"
        )


