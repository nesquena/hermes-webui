"""Tests for model-aware reasoning effort chip visibility."""

from api import config as cfg


GPT_5_6_MODELS = (
    "gpt-5.6",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)

OPENAI_FAMILY_PROVIDERS = (
    "openai",
    "openai-api",
    "openai-codex",
    "azure",
    "azure-openai",
    "azure-foundry",
)


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
    assert "ultra" not in efforts


def test_openai_codex_gpt56_supports_max_and_ultra_effort_levels():
    efforts = cfg.resolve_model_reasoning_efforts(
        "gpt-5.6-sol",
        provider_id="openai-codex",
    )
    assert "xhigh" in efforts
    assert "max" in efforts
    assert "ultra" in efforts
    assert cfg.coerce_reasoning_effort_for_model(
        "max",
        "gpt-5.6-sol",
        provider_id="openai-codex",
    ) == "max"
    assert cfg.coerce_reasoning_effort_for_model(
        "ultra",
        "gpt-5.6-sol",
        provider_id="openai-codex",
    ) == "ultra"


def test_openai_codex_prefixed_gpt5_supports_reasoning_effort_levels():
    efforts = cfg.resolve_model_reasoning_efforts(
        "@openai-codex:gpt-5.5",
        provider_id="openai-codex",
    )
    assert "medium" in efforts
    assert "high" in efforts
    assert "xhigh" in efforts
    assert "max" not in efforts
    assert "ultra" not in efforts


def test_openai_codex_max_effort_is_clamped_before_streaming():
    assert cfg.coerce_reasoning_effort_for_model(
        "max",
        "gpt-5.5",
        provider_id="openai-codex",
    ) == "xhigh"


def test_openai_family_gpt56_models_expose_and_preserve_max():
    for provider in OPENAI_FAMILY_PROVIDERS:
        for model in GPT_5_6_MODELS:
            efforts = cfg.resolve_model_reasoning_efforts(
                f"@{provider}:{model}",
                provider_id=provider,
            )
            assert "max" in efforts, f"{model} on {provider} must expose max"
            assert cfg.coerce_reasoning_effort_for_model(
                "max",
                f"@{provider}:{model}",
                provider_id=provider,
            ) == "max", f"{model} on {provider} must preserve max"


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
    # #3505 default-deny refinement, tightened by the 2026-08-13 gate: 'max' /
    # 'ultra' are supra-ceiling levels, so on an UNRECOGNIZED provider they
    # degrade to the universally proven 'high' ceiling — nothing proves an
    # unknown OpenAI-compatible endpoint accepts xhigh either. All OTHER
    # levels still preserve verbatim below.
    assert cfg.coerce_reasoning_effort_for_model(
        "max",
        "brand-new-model-2099",
        provider_id="some-custom-provider",
    ) == "high"
    assert cfg.coerce_reasoning_effort_for_model(
        "ultra",
        "brand-new-model-2099",
        provider_id="some-custom-provider",
    ) == "high"
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


def test_provider_config_reasoning_efforts_return_filtered_deduped(monkeypatch):
    original = cfg.cfg.get("providers")
    monkeypatch.setitem(
        cfg.cfg,
        "providers",
        {
            "wandb": {
                "reasoning_efforts": [
                    " none ",
                    "HIGH",
                    "bogus",
                    "high",
                    "xhigh",
                ]
            }
        },
    )
    try:
        assert cfg.resolve_model_reasoning_efforts(
            "zai-org/GLM-5.2",
            provider_id="wandb",
        ) == ["none", "high", "xhigh"]
    finally:
        if original is None:
            cfg.cfg.pop("providers", None)
        else:
            monkeypatch.setitem(cfg.cfg, "providers", original)


def test_provider_config_all_invalid_falls_through(monkeypatch):
    original = cfg.cfg.get("providers")
    monkeypatch.setitem(
        cfg.cfg,
        "providers",
        {"wandb": {"reasoning_efforts": ["bogus", "typo"]}},
    )
    try:
        result = cfg.resolve_model_reasoning_efforts(
            "zai-org/GLM-5.2",
            provider_id="wandb",
        )
        assert result != []
        assert "bogus" not in result
        assert "typo" not in result
    finally:
        if original is None:
            cfg.cfg.pop("providers", None)
        else:
            monkeypatch.setitem(cfg.cfg, "providers", original)


def test_named_custom_provider_config_reasoning_efforts(monkeypatch):
    original = cfg.cfg.get("custom_providers")
    monkeypatch.setitem(
        cfg.cfg,
        "custom_providers",
        [{"name": "llm-proxy", "reasoning_efforts": ["none", "high", "xhigh"]}],
    )
    try:
        assert cfg.resolve_model_reasoning_efforts(
            "some-model",
            provider_id="custom:llm-proxy",
        ) == ["none", "high", "xhigh"]
    finally:
        if original is None:
            cfg.cfg.pop("custom_providers", None)
        else:
            monkeypatch.setitem(cfg.cfg, "custom_providers", original)


def test_named_custom_provider_model_reasoning_efforts_take_precedence(monkeypatch):
    original = cfg.cfg.get("custom_providers")
    monkeypatch.setitem(
        cfg.cfg,
        "custom_providers",
        [
            {
                "name": "llm-proxy",
                "reasoning_efforts": ["high"],
                "models": {
                    "Inkling": {
                        "reasoning_efforts": ["none", "low", "medium", "high", "xhigh"]
                    }
                },
            }
        ],
    )
    try:
        assert cfg.resolve_model_reasoning_efforts(
            "inkling",
            provider_id="custom:llm-proxy",
        ) == ["none", "low", "medium", "high", "xhigh"]
        assert cfg.resolve_model_reasoning_efforts(
            "another-model",
            provider_id="custom:llm-proxy",
        ) == ["high"]
    finally:
        if original is None:
            cfg.cfg.pop("custom_providers", None)
        else:
            monkeypatch.setitem(cfg.cfg, "custom_providers", original)


def test_model_only_top_tiers_remain_authoritative_in_public_filter(monkeypatch):
    monkeypatch.setitem(cfg.cfg, "custom_providers", [{
        "name": "llm-proxy",
        "models": {"inkling": {"reasoning_efforts": ["high", "max", "ultra"]}},
    }])
    assert cfg.resolve_model_reasoning_efforts(
        "inkling", provider_id="custom:llm-proxy"
    ) == ["high", "max", "ultra"]
    for effort in ("max", "ultra"):
        assert cfg.coerce_reasoning_effort_for_model(
            effort, "inkling", provider_id="custom:llm-proxy"
        ) == effort


def test_actual_metadata_ladder_retains_max(monkeypatch):
    monkeypatch.setattr(
        cfg, "_models_dev_reasoning_efforts",
        lambda *args, **kwargs: ["minimal", "low", "medium", "high", "xhigh", "max"],
    )
    efforts = cfg.resolve_model_reasoning_efforts("glm-5.2-nvfp4", provider_id="actual")
    assert "max" in efforts
    assert cfg.coerce_reasoning_effort_for_model(
        "max", "glm-5.2-nvfp4", provider_id="actual"
    ) == "max"


def test_copilot_standalone_fallback_caps_gpt56(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fail_cli_models(name, *args, **kwargs):
        if name == "hermes_cli.models":
            raise ImportError("forced standalone fallback")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_cli_models)
    efforts = cfg.resolve_model_reasoning_efforts("gpt-5.6-sol", provider_id="copilot")
    assert efforts == ["minimal", "low", "medium", "high"]
    for effort in ("xhigh", "max", "ultra"):
        assert cfg.coerce_reasoning_effort_for_model(
            effort, "gpt-5.6-sol", provider_id="copilot"
        ) == "high"


def test_model_reasoning_efforts_fall_back_to_provider_when_invalid(monkeypatch):
    original = cfg.cfg.get("providers")
    monkeypatch.setitem(
        cfg.cfg,
        "providers",
        {
            "wandb": {
                "reasoning_efforts": ["none", "high"],
                "models": {"inkling": {"reasoning_efforts": ["bogus", "typo"]}},
            }
        },
    )
    try:
        assert cfg.resolve_model_reasoning_efforts(
            "inkling",
            provider_id="wandb",
        ) == ["none", "high"]
    finally:
        if original is None:
            cfg.cfg.pop("providers", None)
        else:
            monkeypatch.setitem(cfg.cfg, "providers", original)


def test_acp_guards_win_over_configured_reasoning_efforts(monkeypatch):
    original = cfg.cfg.get("providers")
    monkeypatch.setitem(
        cfg.cfg,
        "providers",
        {"copilot-acp": {"reasoning_efforts": ["high"]}},
    )
    try:
        assert cfg.resolve_model_reasoning_efforts(
            "some-model",
            provider_id="copilot-acp",
        ) == []
    finally:
        if original is None:
            cfg.cfg.pop("providers", None)
        else:
            monkeypatch.setitem(cfg.cfg, "providers", original)


def test_nested_route_deny_wins_over_configured_reasoning_efforts(monkeypatch):
    original = cfg.cfg.get("custom_providers")
    monkeypatch.setitem(
        cfg.cfg,
        "custom_providers",
        [
            {
                "name": "agg",
                "reasoning_efforts": ["low", "high"],
                "models": {
                    "vertex/gemini-image-1.0": {"reasoning_efforts": ["high"]},
                    "vertex/gemini-embedding-001": {"reasoning_efforts": ["high"]},
                },
            }
        ],
    )
    try:
        for model in ("vertex/gemini-image-1.0", "vertex/gemini-embedding-001"):
            assert cfg.resolve_model_reasoning_efforts(
                model,
                provider_id="custom:agg",
            ) == []
    finally:
        if original is None:
            cfg.cfg.pop("custom_providers", None)
        else:
            monkeypatch.setitem(cfg.cfg, "custom_providers", original)


def test_nested_route_deny_wins_for_provider_qualified_hinted_model(monkeypatch):
    # Regression for the deeper bypass: a provider-qualified hint like
    # "@custom:agg:vertex/gemini-image-1.0" must strip BOTH the "@custom:"
    # wrapper AND the named-provider slug "agg:" before the nested-route
    # deny check runs. A naive first-colon split only strips "@custom:",
    # leaving "agg:vertex/gemini-image-1.0" — which no longer starts with
    # "vertex/gemini-" — so the deny is missed and the configured
    # ["low", "high"] leaks through on an image/embedding route.
    original = cfg.cfg.get("custom_providers")
    monkeypatch.setitem(
        cfg.cfg,
        "custom_providers",
        [{"name": "agg", "reasoning_efforts": ["low", "high"]}],
    )
    try:
        for model in (
            "@custom:agg:vertex/gemini-image-1.0",
            "@custom:agg:vertex/gemini-embedding-001",
        ):
            assert cfg.resolve_model_reasoning_efforts(
                model,
                provider_id="custom:agg",
            ) == []
    finally:
        if original is None:
            cfg.cfg.pop("custom_providers", None)
        else:
            monkeypatch.setitem(cfg.cfg, "custom_providers", original)


def test_nested_route_deny_is_boundary_based_not_prefix_based():
    # Structural regression test for the underlying invariant, independent
    # of any particular wrapper/strip scheme: _nested_route_reasoning_denied
    # must catch the vertex/gemini- or gemini_cli/gemini- route no matter
    # how many opaque wrapper layers precede it in the raw string, as long
    # as the route starts at a non-alphanumeric boundary. This was bypassed
    # twice via different prefix-stripping edge cases (PR #5313) before the
    # check itself was made boundary-based instead of prefix-based, so no
    # future wrapper scheme can reintroduce the same class of bug.
    denied_cases = [
        "vertex/gemini-image-1.0",
        "vertex/gemini-embedding-001",
        "@custom:agg:vertex/gemini-image-1.0",
        "agg:vertex/gemini-image-1.0",  # the literal leftover fragment from the historical bug
        "gemini_cli/gemini-imagine-2",
        "outer:inner:vertex/gemini-image-1.0",  # hypothetical deeper future nesting
        "@custom:outer:@custom:inner:vertex/gemini-embedding-001",
    ]
    for model in denied_cases:
        assert cfg._nested_route_reasoning_denied(model) is True, model

    allowed_cases = [
        "vertex/gemini-2.5-pro",
        "notvertex/gemini-image-1.0",  # embedded in a larger token — must NOT match
        "somegemini_cli/gemini-image-1",  # same — embedded substring, not a boundary
        "",
    ]
    for model in allowed_cases:
        assert cfg._nested_route_reasoning_denied(model) is False, model


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


def test_get_reasoning_status_for_reasoning_capable_model_has_no_max():
    status = cfg.get_reasoning_status(
        model_id="gpt-5.5",
        provider_id="openai-codex",
    )
    assert status["supported_efforts"] == ["minimal", "low", "medium", "high", "xhigh"]
    assert status["supports_reasoning_effort"] is True
    assert "max" not in status["supported_efforts"]


def test_get_reasoning_status_coerces_stale_max_to_xhigh(monkeypatch):
    """A previously-saved `agent.reasoning_effort: max` (no longer a valid effort)
    must be reported as the coerced `xhigh`, not the raw stale `max`, so the
    boot/status/chip read paths agree with what streaming actually sends."""
    monkeypatch.setattr(
        cfg,
        "_load_yaml_config_file",
        lambda *a, **k: {"agent": {"reasoning_effort": "max"}},
    )
    status = cfg.get_reasoning_status(
        model_id="gpt-5.5",
        provider_id="openai-codex",
    )
    assert status["reasoning_effort"] == "xhigh"
    assert status["reasoning_effort"] != "max"


def test_max_effort_degrades_to_xhigh_for_gemini():
    # Gemini's native ladder tops out below 'max'; its adapter would silently
    # treat an unknown 'max' as medium. A stored/CLI 'max' must degrade to xhigh
    # (the highest supported), not fall through to a worse level. (#4627 gate)
    for model in ("gemini-3-pro", "gemini-3-flash"):
        assert cfg.coerce_reasoning_effort_for_model(
            "max", model_id=model, provider_id="gemini"
        ) == "xhigh", f"{model} max must degrade to xhigh"


def test_max_effort_degrades_to_xhigh_for_pre_adaptive_anthropic():
    # Pre-adaptive Claude (3.7 / 4.0-4.5) uses manual thinking whose budget table
    # lacks 'max' and falls back to 8k; 'max' must degrade to xhigh instead. (#4627 gate)
    for model in (
        "claude-3-7-sonnet", "claude-sonnet-4-5", "claude-haiku-4-5",
        # date-stamped legacy IDs the Anthropic adapter uses
        "claude-3-opus-20240229", "claude-3-5-sonnet-20241022",
        "claude-sonnet-4-20250514", "claude-opus-4-20250514",
    ):
        assert cfg.coerce_reasoning_effort_for_model(
            "max", model_id=model, provider_id="anthropic"
        ) == "xhigh", f"{model} max must degrade to xhigh"


def test_max_effort_preserved_for_adaptive_anthropic_and_deepseek():
    # Adaptive Claude (4.6+) and DeepSeek genuinely support 'max' — it must NOT degrade.
    for model in ("claude-opus-4.6", "claude-sonnet-4.6", "claude-opus-4.7", "claude-opus-latest"):
        assert cfg.coerce_reasoning_effort_for_model(
            "max", model_id=model, provider_id="anthropic"
        ) == "max", f"{model} must preserve max"
    assert cfg.coerce_reasoning_effort_for_model(
        "max", model_id="deepseek-reasoner", provider_id="deepseek"
    ) == "max"


def test_max_degrades_for_pre_gpt56_and_o_series_across_openai_family_lanes():
    # GPT-5 models before 5.6 cap at xhigh, while o1/o3/o4 cap at high, across
    # direct OpenAI, ChatGPT/Codex, and Azure provider aliases.
    for provider in OPENAI_FAMILY_PROVIDERS:
        for model in ("gpt-5", "gpt-5.1", "gpt-5.5"):
            assert cfg.coerce_reasoning_effort_for_model(
                "max", model_id=model, provider_id=provider
            ) == "xhigh", f"{model} on {provider} must degrade max->xhigh"
        for model in ("o1", "o3-mini", "o4-mini"):
            assert cfg.coerce_reasoning_effort_for_model(
                "max", model_id=model, provider_id=provider
            ) == "high", f"{model} on {provider} must degrade max->high"


def test_max_degrades_for_azure_bedrock_hosted_legacy_claude():
    # Legacy Claude via Azure Foundry / Bedrock is still pre-adaptive; the ceiling
    # follows the model, not just the provider name. (#4627 re-gate)
    for prov in ("azure-foundry", "bedrock"):
        assert cfg.coerce_reasoning_effort_for_model(
            "max", model_id="claude-sonnet-4-20250514", provider_id=prov
        ) == "xhigh", f"legacy Claude on {prov} must degrade max->xhigh"
    # adaptive Claude via azure preserves max
    assert cfg.coerce_reasoning_effort_for_model(
        "max", model_id="claude-opus-4.6", provider_id="azure-foundry"
    ) == "max"


def test_max_degrades_on_unknown_provider_but_other_levels_preserved():
    # #3505 default-deny refinement, tightened by the 2026-08-13 gate: max and
    # ultra sit above the universal ceiling, so an unknown/custom provider
    # (empty capability list, no explicit allowlist) must degrade both to the
    # universally proven 'high' — an unknown OpenAI-compatible endpoint has no
    # authority proving xhigh support. All other levels keep the conservative
    # preserve-verbatim behavior.
    assert cfg.coerce_reasoning_effort_for_model(
        "max", model_id="some-unknown-model", provider_id="customprovider"
    ) == "high"
    assert cfg.coerce_reasoning_effort_for_model(
        "ultra", model_id="some-unknown-model", provider_id="customprovider"
    ) == "high"
    # other levels still preserved verbatim for an unknown provider
    for eff in ("minimal", "low", "medium", "high", "xhigh"):
        assert cfg.coerce_reasoning_effort_for_model(
            eff, model_id="some-unknown-model", provider_id="customprovider"
        ) == eff, f"{eff} must be preserved verbatim on unknown provider (#3505)"


def test_max_only_offered_in_ui_when_actually_supported():
    # The dropdown gates on resolve_model_reasoning_efforts(): 'max' appears ONLY
    # for models whose supported list includes it (adaptive Claude, DeepSeek), and
    # is absent for legacy/capped models and unknown providers.
    assert "max" in cfg.resolve_model_reasoning_efforts("claude-opus-4.6", provider_id="anthropic")
    assert "max" in cfg.resolve_model_reasoning_efforts("deepseek-reasoner", provider_id="deepseek")
    assert "max" not in cfg.resolve_model_reasoning_efforts("claude-sonnet-4-5", provider_id="anthropic")
    assert "max" not in cfg.resolve_model_reasoning_efforts("gpt-5.1", provider_id="openai")
    assert "max" not in cfg.resolve_model_reasoning_efforts("gemini-3-pro", provider_id="gemini")


def test_datestamped_claude3_not_reasoning_capable_heuristic():
    # A bare, date-stamped Claude 3.0 id must NOT be treated as reasoning-capable
    # by the heuristic. The minor-version capture previously used `(\d+)`, which
    # swallowed the 8-digit date stamp ("...-20240229") as the minor version so
    # `major==3 and minor>=7` wrongly matched — surfacing reasoning-effort
    # controls for models that don't support them. Claude 3.0/3.5 have no
    # extended-thinking support; only 3.7+ (and 4.x) do.
    for model in (
        "claude-3-opus-20240229",
        "claude-3-sonnet-20240229",
        "claude-3-haiku-20240307",
        "claude-3-opus",
        "claude-3-5-sonnet-20241022",
    ):
        assert cfg._candidate_supports_reasoning(model) is False, (
            f"{model} must not be reasoning-capable (Claude 3.0/3.5 excluded)"
        )
    # 3.7+ and 4.x (including date-stamped builds) stay reasoning-capable.
    for model in (
        "claude-3-7-sonnet",
        "claude-3-7-sonnet-20250219",
        "claude-sonnet-4-5",
        "claude-opus-4-20250514",
        "claude-opus-4.6",
    ):
        assert cfg._candidate_supports_reasoning(model) is True, (
            f"{model} must remain reasoning-capable"
        )


def test_qwen_prefixed_alias_reasoning_detection():
    """Prefixed Qwen IDs (e.g. New-API aliases) must still be detected.

    Regression: "al-qwen3.8-max-preview" normalizes to tokens
    ["al", "qwen3", "8", ...] where "qwen" is NOT a standalone token,
    so the old exact-membership check silently failed.
    """
    # Prefixed Qwen 3+ → reasoning-capable
    for model in (
        "al-qwen3.8-max-preview",
        "al-qwen3.7-max",
        "al-qwen3.7-plus",
        "al-qwen3.6-flash",
        "sn-qwen3-235b-a22b",
    ):
        assert cfg._candidate_supports_reasoning(model) is True, (
            f"{model} must be reasoning-capable (prefixed Qwen 3+)"
        )
    # Bare Qwen 3+ → still works
    for model in (
        "qwen3-235b-a22b",
        "qwen3-32b",
    ):
        assert cfg._candidate_supports_reasoning(model) is True, (
            f"{model} must be reasoning-capable (bare Qwen 3+)"
        )
    # Qwen 2.x → excluded regardless of prefix
    for model in (
        "al-qwen2.5-72b-instruct",
        "qwen2.5-7b-instruct",
        "qwen2-72b",
    ):
        assert cfg._candidate_supports_reasoning(model) is False, (
            f"{model} must NOT be reasoning-capable (Qwen 2.x excluded)"
        )
    # Hybrid IDs with embedded Qwen 2.x must NOT be shadowed by the Qwen
    # branch — they must fall through to the DeepSeek detector.
    for model in (
        "deepseek-r1-distill-qwen2.5-bakeneko-32b",
        "rinna/deepseek-r1-distill-qwen2.5-bakeneko-32b",
    ):
        assert cfg._candidate_supports_reasoning(model) is True, (
            f"{model} must remain reasoning-capable (DeepSeek-R1 hybrid, "
            f"Qwen 2.x must not shadow the DeepSeek detector)"
        )

# ── PR #6018: max/ultra coercion leak closures ───────────────────────────────

def test_named_custom_provider_hints_keep_model_scoped_effort_ceilings(monkeypatch):
    original = cfg.cfg.get("custom_providers")
    monkeypatch.setitem(
        cfg.cfg,
        "custom_providers",
        [{"name": "frontier-gw", "reasoning_efforts": ["high", "xhigh", "max", "ultra"]}],
    )
    try:
        cases = (
            ("@custom:frontier-gw:gpt-5.5", "xhigh", False),
            ("@custom:frontier-gw:o3", "high", False),
            ("@custom:frontier-gw:gpt-5.6-sol", "ultra", True),
            ("@custom:frontier-gw:unknown-frontier-model", "ultra", True),
        )
        for model, expected, keeps_top_tiers in cases:
            efforts = cfg.resolve_model_reasoning_efforts(model, provider_id="custom:frontier-gw")
            assert ("max" in efforts and "ultra" in efforts) is keeps_top_tiers, model
            assert cfg.coerce_reasoning_effort_for_model(
                "ultra", model, provider_id="custom:frontier-gw"
            ) == expected, model
            inferred_efforts = cfg.resolve_model_reasoning_efforts(model)
            assert ("max" in inferred_efforts and "ultra" in inferred_efforts) is keeps_top_tiers
            assert cfg.coerce_reasoning_effort_for_model("ultra", model) == expected
    finally:
        if original is None:
            cfg.cfg.pop("custom_providers", None)
        else:
            monkeypatch.setitem(cfg.cfg, "custom_providers", original)


def test_custom_provider_default_denies_max_ultra_for_recognized_models():
    # Finding 1: a recognized reasoning-capable model family behind a custom /
    # unknown provider must NOT inherit the generic max/ultra tiers by default —
    # the provider's native ladder is unknown, so the top tiers are denied.
    for model in (
        "kimi-k2.5", "moonshotai.kimi-k2.5", "deepseek-v4-flash",
        "zai-org/GLM-5.2", "minimax-m3-pro",
    ):
        efforts = cfg.resolve_model_reasoning_efforts(model, provider_id="custom:unlisted")
        assert efforts, f"{model} should still expose the standard ladder"
        assert "xhigh" in efforts
        assert "max" not in efforts and "ultra" not in efforts, (
            f"{model} via unrecognized custom provider must default-deny max/ultra"
        )
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", model, provider_id="custom:unlisted"
        ) == "xhigh"
        assert cfg.coerce_reasoning_effort_for_model(
            "max", model, provider_id="custom:unlisted"
        ) == "xhigh"


def test_gpt_5_6_first_party_fallback_is_exact_and_includes_azure_foundry(monkeypatch):
    monkeypatch.setattr(cfg, "_models_dev_reasoning_efforts", lambda *_args: [])
    for provider in (
        "openai-codex", "openai", "azure-foundry",
        "azure", "azure-ai-foundry", "azure-ai",
    ):
        assert cfg._resolve_provider_alias(provider) in {
            "openai-codex", "openai", "azure-foundry"
        }
        for model in ("gpt-5.6", "gpt-5.6-sol"):
            for candidate in (model, f"@{provider}:{model}"):
                efforts = cfg.resolve_model_reasoning_efforts(candidate, provider_id=provider)
                assert "max" in efforts and "ultra" in efforts
                assert cfg.coerce_reasoning_effort_for_model(
                    "ultra", candidate, provider_id=provider
                ) == "ultra"
        for lookalike in ("not-gpt-5.6", "gpt-5.60"):
            for candidate in (lookalike, f"@{provider}:{lookalike}"):
                efforts = cfg.resolve_model_reasoning_efforts(candidate, provider_id=provider)
                assert "max" not in efforts and "ultra" not in efforts
                assert cfg.coerce_reasoning_effort_for_model(
                    "ultra", candidate, provider_id=provider
                ) not in {"max", "ultra"}


def test_gpt_5_6_status_matches_resolver_and_coercer_for_azure_aliases(monkeypatch):
    monkeypatch.setattr(cfg, "_models_dev_reasoning_efforts", lambda *_args: [])
    monkeypatch.setattr(cfg, "_load_yaml_config_file", lambda *_args: {
        "agent": {"reasoning_effort": "ultra"},
    })
    for provider in ("azure-foundry", "azure", "azure-ai-foundry", "azure-ai"):
        positive = cfg.get_reasoning_status(
            model_id="gpt-5.6-sol", provider_id=provider
        )
        assert "ultra" in positive["supported_efforts"]
        assert positive["reasoning_effort"] == "ultra"

        for lookalike in ("not-gpt-5.6", "gpt-5.60"):
            rejected = cfg.get_reasoning_status(
                model_id=lookalike, provider_id=provider
            )
            assert "max" not in rejected["supported_efforts"]
            assert "ultra" not in rejected["supported_efforts"]
            assert rejected["reasoning_effort"] not in {"max", "ultra"}


def test_azure_aliases_keep_lower_model_ceilings(monkeypatch):
    monkeypatch.setattr(
        cfg, "_models_dev_reasoning_efforts",
        lambda *_args: list(cfg.VALID_REASONING_EFFORTS),
    )
    for provider in ("azure-foundry", "azure", "azure-ai-foundry", "azure-ai"):
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", "gpt-5.5", provider_id=provider
        ) == "xhigh"
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", "o3", provider_id=provider
        ) == "high"


def test_reasoning_context_fallback_accepts_legacy_string_model_config(monkeypatch):
    monkeypatch.setitem(cfg.cfg, "model", "gpt-5.6-sol")
    monkeypatch.setattr(
        cfg, "resolve_model_provider", lambda *_args: (_ for _ in ()).throw(RuntimeError("probe"))
    )
    assert cfg._resolve_reasoning_context("gpt-5.6-sol", None, None) == (
        "gpt-5.6-sol", "", None
    )


def test_custom_provider_explicit_allowlist_authorizes_max_ultra(monkeypatch):
    # Finding 1 (exception): an explicit provider reasoning_efforts allowlist
    # is the operator's authorization — max/ultra survive when listed.
    original = cfg.cfg.get("custom_providers")
    monkeypatch.setitem(
        cfg.cfg,
        "custom_providers",
        [{"name": "frontier-gw", "reasoning_efforts": ["high", "xhigh", "max", "ultra"]}],
    )
    try:
        assert cfg.resolve_model_reasoning_efforts(
            "some-model", provider_id="custom:frontier-gw"
        ) == ["high", "xhigh", "max", "ultra"]
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", "some-model", provider_id="custom:frontier-gw"
        ) == "ultra"
        assert cfg.coerce_reasoning_effort_for_model(
            "max", "some-model", provider_id="custom:frontier-gw"
        ) == "max"
    finally:
        if original is None:
            cfg.cfg.pop("custom_providers", None)
        else:
            monkeypatch.setitem(cfg.cfg, "custom_providers", original)


def test_aggregator_routes_cap_older_gpt5_and_o_series():
    # Finding 2: model-scoped ceilings follow the MODEL across aggregator lanes.
    # Older GPT-5 + o-series via OpenRouter/Nous must never see max/ultra.
    for prov in ("openrouter", "nous"):
        for model in ("openai/gpt-5.5", "openai/gpt-5.1"):
            assert cfg.coerce_reasoning_effort_for_model(
                "ultra", model, provider_id=prov
            ) == "xhigh", f"{model} via {prov}: ultra must downgrade to xhigh"
            assert cfg.coerce_reasoning_effort_for_model(
                "max", model, provider_id=prov
            ) == "xhigh"
            efforts = cfg.resolve_model_reasoning_efforts(model, provider_id=prov)
            assert "max" not in efforts and "ultra" not in efforts
        for model in ("openai/o3", "openai/o4-mini"):
            assert cfg.coerce_reasoning_effort_for_model(
                "ultra", model, provider_id=prov
            ) == "high", f"{model} via {prov}: ultra must downgrade to high"
            assert cfg.coerce_reasoning_effort_for_model(
                "xhigh", model, provider_id=prov
            ) == "high"
            efforts = cfg.resolve_model_reasoning_efforts(model, provider_id=prov)
            assert set(efforts) <= {"low", "medium", "high"}
        # GPT-5.6 via the same aggregator keeps the generic top tiers — the
        # ceiling is model-scoped, not aggregator-scoped.
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", "openai/gpt-5.6-sol", provider_id=prov
        ) == "ultra"


def test_copilot_fallback_caps_max_ultra():
    # The standalone fallback mirrors Agent's static high ceiling for every
    # Copilot GPT-5 model, including GPT-5.6.
    expected = ["minimal", "low", "medium", "high"]
    assert cfg._heuristic_reasoning_efforts("gpt-5.5", "copilot") == expected
    assert cfg._heuristic_reasoning_efforts("gpt-5.6-sol", "copilot") == expected
    assert cfg._heuristic_reasoning_efforts("o3", "github-copilot") == ["low", "medium", "high"]


def test_metadata_unavailable_fallback_applies_gpt56_check(monkeypatch):
    # Finding 4: when capability metadata has no answer, the fallback branches
    # returning the expanded global effort list must still apply the GPT-5.6
    # model check and the unknown-provider max/ultra default-deny.
    monkeypatch.setattr(cfg, "_models_dev_reasoning_efforts", lambda *a, **k: None)
    efforts = cfg.resolve_model_reasoning_efforts("openai/gpt-5.5", provider_id="openrouter")
    assert "xhigh" in efforts
    assert "max" not in efforts and "ultra" not in efforts
    efforts56 = cfg.resolve_model_reasoning_efforts("openai/gpt-5.6-sol", provider_id="openrouter")
    assert "max" in efforts56 and "ultra" in efforts56
    # Same gap closed for heuristic-recognized families on unknown providers.
    custom = cfg.resolve_model_reasoning_efforts("kimi-k2.5", provider_id="custom:unlisted")
    assert "xhigh" in custom
    assert "max" not in custom and "ultra" not in custom


def test_negative_metadata_does_not_hide_first_party_gpt56_contract(monkeypatch):
    monkeypatch.setattr(cfg, "_models_dev_reasoning_efforts", lambda *a, **k: [])
    efforts = cfg.resolve_model_reasoning_efforts(
        "gpt-5.6-sol", provider_id="openai-codex"
    )
    assert "max" in efforts and "ultra" in efforts
    assert cfg.coerce_reasoning_effort_for_model(
        "ultra", "gpt-5.6-sol", provider_id="openai-codex"
    ) == "ultra"



# --- 2026-08-13 gate regressions (#6018) -----------------------------------


def test_unknown_provider_top_tiers_land_on_universal_high_ceiling(tmp_path, monkeypatch):
    # Gate blocker 1: for unknown/custom providers WITHOUT an explicit
    # provider/model allowlist, max AND ultra must land on the universally
    # proven 'high' ceiling — an unknown OpenAI-compatible endpoint has no
    # authority proving xhigh support.
    # Resolver: no ladder is advertised at all for the unknown model.
    assert cfg.resolve_model_reasoning_efforts(
        "frontier-model-x", provider_id="unknown-gw"
    ) == []
    # Coercion: both supra-ceiling tiers degrade to high, not xhigh.
    for eff in ("max", "ultra"):
        assert cfg.coerce_reasoning_effort_for_model(
            eff, "frontier-model-x", provider_id="unknown-gw"
        ) == "high", f"{eff} must land on the universal high ceiling"
    # Wire: the /api/reasoning persistence path reports the coerced high, so
    # what boot/status/chip read agrees with what streaming would send.
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text("agent: {}\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(cfg, "reload_config", lambda: None)
    status = cfg.set_reasoning_effort(
        "ultra", model_id="frontier-model-x", provider_id="unknown-gw"
    )
    assert status["reasoning_effort"] == "high"
    status = cfg.get_reasoning_status(
        model_id="frontier-model-x", provider_id="unknown-gw"
    )
    assert status["reasoning_effort"] == "high"


def test_unknown_provider_allowlists_stay_authoritative_over_high_ceiling(monkeypatch):
    # Gate blocker 1 (control): explicit provider- and model-level allowlists
    # remain authoritative — an authorized top tier survives verbatim instead
    # of landing on the universal high ceiling.
    original = cfg.cfg.get("custom_providers")
    monkeypatch.setitem(cfg.cfg, "custom_providers", [
        {"name": "authorized-gw", "reasoning_efforts": ["high", "max", "ultra"]},
        {"name": "model-gw", "models": {"inkling": {"reasoning_efforts": ["high", "max"]}}},
    ])
    try:
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", "some-model", provider_id="custom:authorized-gw"
        ) == "ultra"
        assert cfg.coerce_reasoning_effort_for_model(
            "max", "inkling", provider_id="custom:model-gw"
        ) == "max"
        # The model-level allowlist does not leak to sibling models: an
        # unlisted model on the same unknown gateway still lands on high.
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", "other-model", provider_id="custom:model-gw"
        ) == "high"
    finally:
        if original is None:
            cfg.cfg.pop("custom_providers", None)
        else:
            monkeypatch.setitem(cfg.cfg, "custom_providers", original)


def test_pre_adaptive_claude_ceiling_follows_model_across_aggregators():
    # Gate blocker 2: the pre-adaptive Claude ceiling is model-scoped and must
    # apply on OpenRouter/Nous and every routed lane, for prefixed and bare
    # legacy IDs alike — not only on the named Anthropic/cloud-host lanes.
    legacy_ids = (
        "anthropic/claude-sonnet-4.5",
        "claude-sonnet-4-5",
        "anthropic/claude-3-7-sonnet",
        "claude-3-5-sonnet-20241022",
    )
    for prov in ("openrouter", "nous"):
        for model in legacy_ids:
            efforts = cfg.resolve_model_reasoning_efforts(model, provider_id=prov)
            assert "max" not in efforts and "ultra" not in efforts, (
                f"{model} via {prov} must cap below max/ultra, got {efforts}"
            )
            if efforts:  # date-stamped Claude 3.x is heuristic-denied entirely
                assert "xhigh" in efforts, (model, prov, efforts)
            for eff in ("max", "ultra"):
                assert cfg.coerce_reasoning_effort_for_model(
                    eff, model, provider_id=prov
                ) == "xhigh", f"{eff} for {model} via {prov} must degrade to xhigh"
        # Qualified @provider:model form resolves the same ceiling.
        qualified = f"@{prov}:anthropic/claude-sonnet-4.5"
        q_efforts = cfg.resolve_model_reasoning_efforts(qualified, provider_id=prov)
        assert "max" not in q_efforts and "ultra" not in q_efforts
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", qualified, provider_id=prov
        ) == "xhigh"
        # Control: adaptive Claude via the same aggregator keeps the top tiers
        # — the ceiling is model-scoped, not lane-scoped.
        adaptive = cfg.resolve_model_reasoning_efforts(
            "anthropic/claude-opus-4.6", provider_id=prov
        )
        assert "max" in adaptive and "ultra" in adaptive
    # The same model-scoped ceiling holds on unknown/custom gateway lanes.
    assert cfg.coerce_reasoning_effort_for_model(
        "ultra", "claude-sonnet-4-5", provider_id="custom:some-gw"
    ) == "xhigh"


def test_ai_gateway_alias_family_recognized_with_claude_max_contract():
    # Gate blocker 3: ai-gateway is a registered production provider that
    # forwards reasoning config. Every installed Agent alias must resolve to
    # the canonical slug, preserve adaptive Claude 'max', and map the
    # Codex-only 'ultra' tier down to the wire 'max' instead of xhigh.
    aliases = ("ai-gateway", "vercel", "vercel-ai-gateway", "ai_gateway", "aigateway")
    for alias in aliases:
        assert cfg._resolve_provider_alias(alias) == "ai-gateway", alias
        for model in ("anthropic/claude-opus-4.6", "claude-opus-4.6"):
            efforts = cfg.resolve_model_reasoning_efforts(model, provider_id=alias)
            assert "max" in efforts, (alias, model, efforts)
            assert "ultra" not in efforts, (
                f"ultra is Codex product-only; {alias} wire ladder tops at max"
            )
            assert cfg.coerce_reasoning_effort_for_model(
                "max", model, provider_id=alias
            ) == "max", (alias, model)
            assert cfg.coerce_reasoning_effort_for_model(
                "ultra", model, provider_id=alias
            ) == "max", f"ultra must map to max on {alias}, not degrade to xhigh"
        # Qualified @provider:model form keeps the same contract.
        qualified = f"@{alias}:anthropic/claude-opus-4.6"
        q_efforts = cfg.resolve_model_reasoning_efforts(qualified, provider_id=alias)
        assert "max" in q_efforts and "ultra" not in q_efforts, (alias, q_efforts)
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", qualified, provider_id=alias
        ) == "max"
        # Model-scoped ceilings still ride through the gateway: pre-adaptive
        # Claude caps at xhigh, older GPT-5 at xhigh, o-series at high.
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", "anthropic/claude-sonnet-4.5", provider_id=alias
        ) == "xhigh"
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", "openai/gpt-5.5", provider_id=alias
        ) == "xhigh"
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", "openai/o3", provider_id=alias
        ) == "high"


def test_ai_gateway_status_reports_wire_max_for_stored_ultra(monkeypatch):
    # Gate blocker 3 (status boundary): a stored 'ultra' surfaces as the wire
    # 'max' for Claude 4.6 behind every ai-gateway alias, so the chip and the
    # streamed value agree.
    monkeypatch.setattr(cfg, "_load_yaml_config_file", lambda *_args: {
        "agent": {"reasoning_effort": "ultra"},
    })
    for alias in ("ai-gateway", "vercel", "vercel-ai-gateway", "ai_gateway", "aigateway"):
        status = cfg.get_reasoning_status(
            model_id="anthropic/claude-opus-4.6", provider_id=alias
        )
        assert "max" in status["supported_efforts"], alias
        assert "ultra" not in status["supported_efforts"], alias
        assert status["reasoning_effort"] == "max", alias
