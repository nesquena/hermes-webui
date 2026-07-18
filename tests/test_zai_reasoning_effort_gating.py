"""Regression tests: ZAI (Z.AI / Zhipu / GLM) reasoning-effort per-model gating.

Z.AI's official API (docs.z.ai) defines two distinct parameters:

* ``thinking: {"type": "enabled"|"disabled"}`` — the reasoning on/off toggle,
  supported by GLM-4.5 and above (with GLM-4.7 using *forced* thinking that
  cannot be disabled).
* ``reasoning_effort`` — the effort intensity (max/xhigh/high/medium/low/
  minimal/none), supported by **GLM-5.2 and above ONLY**.

Before this fix, hermes-webui advertised the full 6-level ``reasoning_effort``
ladder (plus ``none``) for *every* GLM model, because ``_candidate_supports_reasoning``
has an unconditional ``glm`` token match and ``_filter_reasoning_efforts_for_provider``
had no ZAI branch. Six of seven catalog models therefore showed a selector whose
values Z.AI documents as GLM-5.2-exclusive, and GLM-4.7 (forced thinking) showed
a ``none`` option that has no effect.

These tests pin the corrected behaviour: the intensity ladder is offered only for
GLM-5.2+ via the native ``zai`` provider, and the entire ladder (including
``none``) is dropped for earlier GLM models and for the forced-thinking GLM-4.7.
Aggregator providers (openrouter, kilocode, custom:...) are intentionally
untouched because they route through their own routers, not Z.AI's native docs.
"""

import pytest

import api.config as cfg


# ── GLM-5.2: full ladder preserved (the only model that supports reasoning_effort) ─

def test_glm_5_2_native_zai_keeps_full_ladder():
    efforts = cfg.resolve_model_reasoning_efforts("glm-5.2", provider_id="zai")
    # Z.AI's accepted values match VALID_REASONING_EFFORTS exactly.
    assert set(efforts) == {"minimal", "low", "medium", "high", "xhigh", "max"}


def test_glm_5_2_preserves_none_sentinel():
    # The 'none' UI sentinel (turn reasoning off) must survive filtering when a
    # provider config or models.dev source emits it alongside the ladder. The
    # default heuristic path does NOT emit 'none', so we inject it via provider
    # config to exercise the preservation branch in resolve_model_reasoning_efforts
    # (which re-attaches 'none' after _filter_reasoning_efforts_for_provider runs).
    import unittest.mock as mock

    raw_with_none = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    with mock.patch(
        "api.config._resolve_model_reasoning_efforts_impl",
        return_value=raw_with_none,
    ):
        efforts = cfg.resolve_model_reasoning_efforts("glm-5.2", provider_id="zai")
    assert "none" in efforts, (
        "glm-5.2 filter returns the full ladder unchanged, so a raw 'none' "
        f"sentinel must survive re-attachment; got {efforts!r}"
    )


@pytest.mark.parametrize(
    "model_id",
    ["glm-5.3", "glm-5.2-air", "glm-6-pro", "glm-6", "glm-5.2.1"],
)
def test_future_glm_5_2_plus_keeps_full_ladder(model_id):
    """Forward-compat: any GLM >= 5.2 must keep the full ladder."""
    efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id="zai")
    assert set(efforts) >= {"low", "medium", "high", "max"}


# ── Bug 1: pre-5.2 GLM models must NOT advertise reasoning_effort ────────────────

@pytest.mark.parametrize(
    "model_id",
    [
        "glm-5.1",
        "glm-5",
        "glm-5-turbo",
        "glm-4.5",
        "glm-4.5-flash",
    ],
)
def test_pre_5_2_glm_drops_reasoning_effort_ladder(model_id):
    """Bug 1: these GLM models do not support reasoning_effort per Z.AI docs."""
    efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id="zai")
    assert efforts == [], (
        f"{model_id} via native zai must not advertise reasoning_effort "
        f"(Z.AI: reasoning_effort is GLM-5.2+ only); got {efforts!r}"
    )


# ── Bug 3: GLM-4.7 forced thinking — no ladder AND no 'none' ─────────────────────

def test_glm_4_7_forced_thinking_drops_entire_ladder():
    """Bug 3: GLM-4.7 uses forced thinking and cannot be disabled."""
    efforts = cfg.resolve_model_reasoning_efforts("glm-4.7", provider_id="zai")
    assert efforts == [], (
        "glm-4.7 uses forced thinking per Z.AI docs — no reasoning_effort "
        f"and no 'none'; got {efforts!r}"
    )


def test_glm_4_7_air_forced_thinking_drops_entire_ladder():
    efforts = cfg.resolve_model_reasoning_efforts("glm-4.7-air", provider_id="zai")
    assert efforts == []


# ── Aliases resolve through the same gate ────────────────────────────────────────

@pytest.mark.parametrize("alias", ["glm", "z-ai", "z.ai", "zhipu"])
def test_zai_aliases_resolve_through_same_gate(alias):
    """_resolve_provider_alias must funnel all ZAI aliases to the same gate."""
    efforts_5_2 = cfg.resolve_model_reasoning_efforts("glm-5.2", provider_id=alias)
    efforts_5_1 = cfg.resolve_model_reasoning_efforts("glm-5.1", provider_id=alias)
    efforts_4_7 = cfg.resolve_model_reasoning_efforts("glm-4.7", provider_id=alias)
    assert set(efforts_5_2) == {
        "minimal", "low", "medium", "high", "xhigh", "max"
    }
    assert efforts_5_1 == []
    assert efforts_4_7 == []


# ── Aggregator / custom providers are intentionally UNAFFECTED ───────────────────
# Z.AI's per-model docs apply to the native zai endpoint only. Third-party routers
# (openrouter, kilocode, custom:...) have their own mappings and must keep the
# existing family-level reasoning capability so the selector still appears there.

@pytest.mark.parametrize(
    "model_id, provider_id",
    [
        ("glm-5.1:free", "kilocode"),
        ("glm-6-pro", "custom:newapi"),
        ("glm-4.5-flash", "openrouter"),
    ],
)
def test_aggregator_providers_keep_family_reasoning(model_id, provider_id):
    efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)
    assert set(efforts) >= {"low", "medium", "high"}, (
        f"{model_id} via aggregator {provider_id} must keep family-level reasoning"
    )


# ── Non-GLM models on zai provider are untouched ─────────────────────────────────

def test_non_glm_model_on_zai_provider_unaffected():
    # A non-GLM model id routed through the zai provider must not be gated
    # by the GLM-specific branch — the gate keys on "glm" in the bare id, so
    # non-GLM models fall through unchanged. (The OpenAI-family ceiling does NOT
    # fire here because that branch is keyed on provider, not model family.)
    efforts = cfg.resolve_model_reasoning_efforts("gpt-5", provider_id="zai")
    assert set(efforts) == {"minimal", "low", "medium", "high", "xhigh", "max"}


# ── Coercion agrees with advertising (UI/coercion invariant) ─────────────────────
# The ZAI gate returns a KNOWN-empty list for pre-5.2 GLM (distinct from the
# ambiguous empty list returned for genuinely-unknown models, which preserves
# the configured effort verbatim per #3505). So any stored effort level — not
# just 'max' — must coerce to "" (send no reasoning_effort field) for these
# models, matching the UI showing no options. Without this, a stored 'high' or
# 'medium' would be forwarded to Z.AI and silently ignored.

@pytest.mark.parametrize(
    "model_id",
    ["glm-5.1", "glm-5", "glm-5-turbo", "glm-4.5", "glm-4.5-flash", "glm-4.7"],
)
def test_coerce_any_stored_level_to_empty_for_pre_5_2_glm(model_id):
    """Bug 2 (coercion gap): all levels, not just 'max', must coerce to ''."""
    for level in ["max", "xhigh", "high", "medium", "low", "minimal"]:
        coerced = cfg.coerce_reasoning_effort_for_model(
            level, model_id, provider_id="zai"
        )
        assert coerced == "", (
            f"{model_id} via zai is known not to support reasoning_effort; stored "
            f"'{level}' must coerce to '' (send no field), got {coerced!r}"
        )


def test_coerce_preserves_levels_for_glm_5_2():
    """GLM-5.2 accepts the full ladder — all stored levels preserve verbatim."""
    for level in ["max", "xhigh", "high", "medium", "low", "minimal"]:
        coerced = cfg.coerce_reasoning_effort_for_model(
            level, "glm-5.2", provider_id="zai"
        )
        assert coerced == level, (
            f"glm-5.2 supports '{level}'; got {coerced!r}"
        )


@pytest.mark.parametrize("alias", ["glm", "z-ai", "z.ai", "zhipu"])
def test_coerce_zai_aliases_resolve_through_same_gate(alias):
    """All ZAI aliases must hit the same coercion gate as native 'zai'."""
    coerced = cfg.coerce_reasoning_effort_for_model(
        "high", "glm-5.1", provider_id=alias
    )
    assert coerced == "", (
        f"alias '{alias}' must resolve to zai and coerce 'high' to '' for glm-5.1"
    )


def test_coerce_unchanged_for_unknown_non_zai_models():
    """Regression guard for #3505: a genuinely-unknown model on a non-zai provider
    must STILL preserve the configured effort verbatim (the ZAI gate must not
    bleed into the ambiguous-empty path)."""
    coerced = cfg.coerce_reasoning_effort_for_model(
        "high", "some-brand-new-model-9999", provider_id="custom:myrouter"
    )
    # custom: provider is not zai → ZAI gate returns None → #3505 preserve path.
    assert coerced == "high"
