"""Tests for model-aware reasoning effort chip visibility."""

import pytest

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
    # #3505 default-deny refinement (maintainer 2026-07-11): 'max' is above the
    # universally safe ceiling, so on an UNRECOGNIZED provider it degrades to
    # xhigh (a truly-unknown provider would 400 on max). All OTHER levels still
    # preserve verbatim below.
    assert cfg.coerce_reasoning_effort_for_model(
        "max",
        "brand-new-model-2099",
        provider_id="some-custom-provider",
    ) == "xhigh"
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


def test_max_effort_degrades_to_top_supported_for_gemini():
    # Gemini's native ladder tops out below 'max'; its adapter would silently
    # treat an unknown 'max' as medium. A stored/CLI 'max' must degrade to the
    # HIGHEST SUPPORTED level, not fall through to a worse one. (#4627 gate)
    #
    # That top level is 'high', not 'xhigh': Google documents
    # minimal/low/medium/high and Gemini has no 'xhigh' either. This assertion
    # previously read 'xhigh' because the ladder itself advertised one — the
    # invariant ("degrade to the top of the real ladder") is unchanged, the
    # ladder is simply accurate now.
    for model in ("gemini-3-pro", "gemini-3-flash"):
        assert cfg.coerce_reasoning_effort_for_model(
            "max", model_id=model, provider_id="gemini"
        ) == "high", f"{model} max must degrade to the top supported level"
        # And the degraded value must be a level the ladder actually offers.
        assert "high" in cfg.resolve_model_reasoning_efforts(
            model, provider_id="gemini"
        ), model


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
    # #3505 default-deny refinement (maintainer call 2026-07-11): 'max' is above
    # the universally safe ceiling, so an unknown/custom provider (empty capability list)
    # must degrade 'max'->'xhigh' rather than send an unsupported level — while all
    # other levels keep the conservative preserve-verbatim behavior.
    assert cfg.coerce_reasoning_effort_for_model(
        "max", model_id="some-unknown-model", provider_id="customprovider"
    ) == "xhigh"
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
def test_grok_rejects_disable_reasoning():
    """Grok 4.5/4.6 have an effort ladder but no off position.

    xAI rejects `reasoning_effort:"none"` on these builds, and the native
    endpoint silently ignores it and leaves reasoning on — so a "None" choice
    would either error or lie about what the model is doing. The capability is
    per-model and separate from "should a control be rendered at all": Grok 4.3
    accepts disabling and keeps its None (see
    `test_grok_disable_capability_is_per_model_not_a_blanket_prefix`).
    """
    for model_id in ("grok-4.6", "grok-4.5"):
        assert cfg.supports_disable_reasoning(model_id) is False, model_id
        assert cfg.coerce_reasoning_effort_for_model("none", model_id) == "", model_id

    # The ladder itself must survive: this is about the off position only.
    assert {"low", "medium", "high"} <= set(
        cfg.resolve_model_reasoning_efforts("grok-4.6")
    )
    for level in ("low", "medium", "high"):
        assert cfg.coerce_reasoning_effort_for_model(level, "grok-4.6") == level

    # `grok-4-fast-reasoning` reasons natively but exposes NO effort dial, so
    # it has no ladder at all — a different condition from "ladder without an
    # off position", and not evidence about the disable capability.
    assert cfg.resolve_model_reasoning_efforts("grok-4-fast-reasoning") == []


def test_models_with_an_off_position_keep_none():
    """The negative side of the gate: don't over-restrict.

    A fix that hides "None" everywhere would break the Z.AI GLM thinking
    toggle, whose entire control is the on/off pair.
    """
    assert cfg.supports_disable_reasoning("glm-4.6", "zai") is True
    assert cfg.coerce_reasoning_effort_for_model("none", "glm-4.6", provider_id="zai") == "none"

    assert cfg.supports_disable_reasoning("claude-opus-5") is True
    assert cfg.coerce_reasoning_effort_for_model("none", "claude-opus-5") == "none"

    # GLM-4.7 forces thinking on: it has no off position either.
    assert cfg.supports_disable_reasoning("glm-4.7", "zai") is False


def test_capability_probe_does_not_mutate_core_globals():
    """The probe must answer without touching shared module state.

    An earlier probe installed `mock.patch.object` stand-ins for the core's
    token and fetch helpers. `patch.object` restores whatever the attribute
    held when it was ENTERED, so two concurrent probes interleaving as
    A-enter, B-enter, A-exit, B-exit left B writing A's probe lambda back as
    the permanent value — permanently collapsing core catalog discovery to a
    synthetic probe model. The server is threaded, so this was reachable.
    """
    try:
        import hermes_cli.models as copilot_models
    except ImportError:
        pytest.skip("hermes_cli not available")

    watched = (
        "github_model_reasoning_efforts",
        "fetch_github_model_catalog",
        "_resolve_copilot_catalog_api_key",
        "_cached_copilot_reasoning_token",
    )
    before = {
        name: getattr(copilot_models, name, None)
        for name in watched
    }

    for _ in range(5):
        cfg._copilot_core_reads_catalog_unprompted(copilot_models)

    for name in watched:
        assert getattr(copilot_models, name, None) is before[name], (
            f"{name} was replaced by the capability probe"
        )


def test_copilot_ladders_survive_without_hermes_cli(monkeypatch):
    """No installed core at all must not suppress the Copilot controls.

    The wire-agreement gate exists to avoid rendering a control the runtime
    will silently ignore. When no core is importable there is no runtime to
    disagree with — a standalone WebUI, or CI — so the heuristic is the only
    answer available and must stand.

    This is a real regression that reached CI: the first version of the gate
    imported hermes_cli and failed closed, which erased every Copilot ladder
    in an environment that has no core installed, `gpt-5.5` included.
    """
    import builtins

    real_import = builtins.__import__

    def _no_hermes_cli(name, *args, **kwargs):
        if name.startswith("hermes_cli"):
            raise ImportError("simulated: no hermes_cli installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_hermes_cli)

    for model_id, provider_id in (
        ("gpt-5.5", "copilot"),
        ("gpt-5.5", "github-copilot"),
        ("gemini-3.6-flash", "copilot"),
        ("claude-opus-5", "copilot"),
    ):
        efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)
        assert efforts, (
            f"{model_id} via {provider_id}: the ladder vanished with no core "
            "installed — the wire-agreement gate must not fire when there is "
            "no runtime to disagree with"
        )
        assert {"low", "medium", "high"} <= set(efforts), (model_id, efforts)


def test_grok_disable_capability_is_per_model_not_a_blanket_prefix():
    """Only the Grok builds that reject `none` lose it — 4.3 keeps working.

    A blanket `grok` prefix-deny is a silent regression, not a cosmetic one:
    with `None` removed, the installed xAI transport defaults reasoning back
    on at medium, so a user who picks "None" on Grok 4.3 silently gets medium
    reasoning. The Agent core records the live verification: grok-4.5 rejects
    `reasoning_effort: "none"` "unlike grok-4.3", and 4.6 is its successor.
    """
    # Verified to REJECT disabling.
    for model_id in ("grok-4.5", "grok-4.6", "x-ai/grok-4.6"):
        assert cfg.supports_disable_reasoning(model_id) is False, model_id
        assert cfg.coerce_reasoning_effort_for_model("none", model_id) == "", model_id

    # Verified to ACCEPT disabling — must keep None.
    for model_id in ("grok-4.3", "grok-3-mini", "x-ai/grok-4.3"):
        assert cfg.supports_disable_reasoning(model_id) is True, model_id
        assert cfg.coerce_reasoning_effort_for_model("none", model_id) == "none", model_id

    # The effort ladder itself is unaffected on both sides.
    for model_id in ("grok-4.3", "grok-4.6"):
        assert {"low", "medium", "high"} <= set(
            cfg.resolve_model_reasoning_efforts(model_id)
        ), model_id


@pytest.mark.parametrize(
    "model_id,is_gemini",
    [
        # Real Gemini ids, at every nesting depth.
        ("gemini-3.6-flash", True),
        ("google/gemini-2.5-pro", True),
        ("vertex/gemini-3-pro-preview", True),
        ("gemini", True),
        # Models that merely CONTAIN the word — an unrestricted substring
        # match clamped these to Gemini's ladder and dropped xhigh/max.
        ("notgemini-deepseek-r1", False),
        ("acme-gemini-router/deepseek-r1", False),
        ("gemini-router/deepseek-r1", False),
        ("deepseek-r1", False),
    ],
)
def test_gemini_detection_matches_model_shaped_segments(model_id, is_gemini):
    assert cfg._is_gemini_id(model_id) is is_gemini, model_id


def test_non_gemini_model_named_like_gemini_keeps_its_full_ladder():
    """The misclassification had a visible cost: levels silently disappeared."""
    efforts = cfg.resolve_model_reasoning_efforts(
        "notgemini-deepseek-r1", provider_id="custom:litellm"
    )
    # Whatever the heuristic decides, it must NOT be Gemini's clamped ladder.
    assert efforts != ["minimal", "low", "medium", "high"], efforts


def test_native_gemini_offers_only_levels_the_adapter_distinguishes():
    """Every offered native level must produce a distinct wire request.

    The installed adapter collapses efforts when building `thinkingConfig`:
    Gemini 2.5 maps every level to `{"includeThoughts": True}`, 3.x Pro maps
    minimal/low/medium to `low`, and 3.x Flash maps minimal/low to `low`.
    Offering an aliased level lets a user pick a depth that never reaches the
    model. Asserted against the adapter's own function so this test tracks the
    runtime rather than a copy of its rules.
    """
    try:
        from agent.transports.chat_completions import (
            _build_gemini_thinking_config as build,
        )
    except ImportError:
        pytest.skip("agent transport not available")

    for model_id in (
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-3-pro-preview",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
    ):
        ladder = cfg.resolve_model_reasoning_efforts(model_id, provider_id="gemini")
        wires = [repr(build(model_id, {"effort": lv})) for lv in ladder]
        assert len(set(wires)) == len(ladder), (
            f"{model_id}: native ladder {ladder} collapses to "
            f"{len(set(wires))} distinct request(s) — aliased levels offered"
        )

    # Gemini 2.5 distinguishes nothing, so it must offer no intensity control.
    assert cfg.resolve_model_reasoning_efforts("gemini-2.5-pro", provider_id="gemini") == []


def test_aggregator_gemini_keeps_the_published_ladder():
    """Only the NATIVE path is narrowed — aggregators skip that adapter."""
    assert cfg.resolve_model_reasoning_efforts(
        "google/gemini-2.5-pro", provider_id="openrouter"
    ) == ["low", "medium", "high"]
    assert cfg.resolve_model_reasoning_efforts(
        "gemini-3.6-flash", provider_id="custom:litellm"
    ) == ["minimal", "low", "medium", "high"]


@pytest.mark.parametrize(
    ("model_id", "capable"),
    [
        # A date stamp must not be absorbed into the version number. These are
        # `grok-4` plus a date, which a bare `startswith("grok-4-5")` reads as
        # the effort-capable 4.5 build -- the composer would then offer levels
        # the model rejects and the request fails.
        ("grok-4-520250101", False),
        ("grok-4-620250101", False),
        ("grok-4-320250101", False),
        ("grok-4-20250101", False),
        # Genuinely dated capable builds must keep their ladder: the guard is
        # about the version boundary, not about rejecting dates.
        ("grok-4.5-20250101", True),
        ("grok-4.6-20250101", True),
        ("grok-3-mini-20250101", True),
        # Undated baselines, unchanged.
        ("grok-4", False),
        ("grok-3", False),
        ("grok-4.5", True),
        ("grok-4.6", True),
        ("grok-4.3", True),
    ],
)
def test_grok_version_prefix_does_not_absorb_date_stamps(model_id, capable):
    """Effort capability is decided on a version boundary, not a raw prefix.

    Mirrors the `(?!\\d)` date-stamp guard the Claude branch already applies:
    the character after a matched version prefix must not be a digit, or an
    aggregator id like `grok-4-520250101` silently promotes `grok-4` to 4.5.
    """
    normalized = model_id.replace(".", "-")
    assert cfg._grok_supports_effort(normalized) is capable, (model_id, capable)

    efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id="custom:newapi")
    if capable:
        assert efforts, f"{model_id} should expose a ladder"
        assert "xhigh" not in efforts and "max" not in efforts, (model_id, efforts)
    else:
        assert efforts == [], (
            f"{model_id} rejects reasoning_effort but a ladder was offered: {efforts}"
        )
