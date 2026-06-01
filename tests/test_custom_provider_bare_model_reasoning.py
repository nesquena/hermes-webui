"""Regression tests: custom providers with non-slash model names expose reasoning efforts.

Custom API aggregators (e.g. New API, One API) route requests using their own
naming conventions — bare names like ``deepseek-v4-flash`` or dot-separated
names like ``moonshotai.kimi-k2.5`` — rather than the OpenRouter-style
``vendor/model`` slash format that the heuristic prefix list was written for.

Before this fix, ``resolve_model_reasoning_efforts`` always returned ``[]`` for
these combinations, hiding the reasoning effort selector in the UI even though
the underlying models fully support thinking/reasoning.
"""

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
