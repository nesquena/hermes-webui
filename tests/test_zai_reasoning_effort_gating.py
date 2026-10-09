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


# ── Three-tier classification: effort / thinking / forced ────────────────────────
# Per docs.z.ai, the three Z.AI capability tiers are distinct:
#   - GLM-5.2+:           effort ladder (max..minimal) AND thinking toggle
#   - GLM-4.5–5.1:        thinking toggle ONLY (no effort ladder)
#   - GLM-4.7:            forced thinking (neither toggle nor ladder)
# Returning [] for the effort ladder must NOT also hide the thinking on/off
# control for the middle tier — otherwise GLM-4.5/4.6/5.0/5.1 users lose the
# working thinking toggle they had before (#6219 round-2 review).


@pytest.mark.parametrize(
    "model_id,expected",
    [
        # GLM-5.2+ → effort ladder + thinking toggle
        ("glm-5.2", "effort"),
        ("glm-5.2-air", "effort"),
        ("glm-5.3", "effort"),
        ("glm-6", "effort"),
        # GLM-4.5 up to (but not including) 5.2 → thinking toggle only
        ("glm-5.1", "thinking"),
        ("glm-5", "thinking"),
        ("glm-5-turbo", "thinking"),
        ("glm-4.5", "thinking"),
        ("glm-4.5-flash", "thinking"),
        ("glm-4.5-air", "thinking"),
        ("glm-4.6", "thinking"),
        # GLM-4.7 family → forced thinking (no toggle, no ladder)
        ("glm-4.7", "forced"),
        ("glm-4.7-air", "forced"),
        # Non-zai / non-glm → defer (None)
        ("gpt-5", None),
        ("claude-sonnet-4.6", None),
    ],
)
def test_zai_glm_classification_three_tiers(model_id, expected):
    cls = cfg._zai_glm_classification(model_id, provider_id="zai")
    assert cls == expected, (
        f"{model_id} via zai should classify as {expected!r}, got {cls!r}"
    )


def test_classification_none_for_non_zai_provider():
    """Aggregators must defer (None) — they route through their own routers."""
    assert cfg._zai_glm_classification("glm-5.2", provider_id="openrouter") is None
    assert cfg._zai_glm_classification("glm-5.2", provider_id="custom:newapi") is None


def test_classification_none_for_non_glm_on_zai():
    """A non-GLM model routed through zai must defer (None)."""
    assert cfg._zai_glm_classification("gpt-5", provider_id="zai") is None
    assert cfg._zai_glm_classification("claude-sonnet-4.6", provider_id="zai") is None


# ── supports_thinking_toggle: chip visibility contract ──────────────────────────


def _reasoning_status(model_id, provider_id="zai"):
    """Helper: call get_reasoning_status with a stub config so it does not depend
    on the active profile's config.yaml."""
    import unittest.mock as mock
    with mock.patch("api.config._load_yaml_config_file", return_value={}):
        return cfg.get_reasoning_status(
            model_id=model_id, provider_id=provider_id
        )


def test_glm_5_2_status_offers_effort_ladder_and_toggle():
    """GLM-5.2: full effort ladder AND thinking toggle both available."""
    st = _reasoning_status("glm-5.2")
    assert st["supports_reasoning_effort"] is True
    assert set(st["supported_efforts"]) == {
        "minimal", "low", "medium", "high", "xhigh", "max"
    }
    assert st["supports_thinking_toggle"] is True


@pytest.mark.parametrize(
    "model_id",
    ["glm-4.6", "glm-4.5", "glm-4.5-flash", "glm-5", "glm-5.1", "glm-5-turbo"],
)
def test_sub_5_2_glm_status_keeps_thinking_toggle_without_effort_ladder(model_id):
    """#6219 round-2: GLM-4.5–5.1 must keep the thinking on/off toggle even though
    the effort ladder is empty — otherwise the composer hides the whole chip and
    silently regresses the working thinking control."""
    st = _reasoning_status(model_id)
    assert st["supported_efforts"] == [], (
        f"{model_id} must not advertise an effort ladder"
    )
    assert st["supports_reasoning_effort"] is False
    assert st["supports_thinking_toggle"] is True, (
        f"{model_id} accepts thinking:{type:enabled|disabled} per Z.AI docs; the "
        "composer must still render an On/None control when the ladder is empty"
    )


def test_glm_4_7_status_offers_neither_toggle_nor_ladder():
    """GLM-4.7 forced thinking: the composer must hide the chip entirely."""
    st = _reasoning_status("glm-4.7")
    assert st["supported_efforts"] == []
    assert st["supports_reasoning_effort"] is False
    assert st["supports_thinking_toggle"] is False, (
        "glm-4.7 forces thinking on — no on/off toggle should be offered"
    )


@pytest.mark.parametrize("alias", ["glm", "z-ai", "z.ai", "zhipu"])
def test_thinking_toggle_aliases_resolve_through_same_gate(alias):
    """All ZAI aliases must produce the same thinking-toggle verdict as native zai."""
    st_glm_4_6 = _reasoning_status("glm-4.6", provider_id=alias)
    st_glm_4_7 = _reasoning_status("glm-4.7", provider_id=alias)
    assert st_glm_4_6["supports_thinking_toggle"] is True
    assert st_glm_4_7["supports_thinking_toggle"] is False


def test_non_zai_status_toggle_defaults_to_effort_capability():
    """For non-zai providers, supports_thinking_toggle must mirror effort
    capability (the prior behavior) — no separate ZAI gate fires."""
    # An effort-capable model on a non-zai provider.
    st = _reasoning_status("gpt-5", provider_id="openai")
    assert st["supports_thinking_toggle"] == st["supports_reasoning_effort"]
    # A non-effort model on a non-zai provider: toggle also False (no ZAI gate).
    st2 = _reasoning_status("gpt-4o", provider_id="openai")
    assert st2["supports_thinking_toggle"] is False
    assert st2["supports_reasoning_effort"] is False


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


# ── Round-3: GLM-4.7 forced + stored 'none' (gap #2) ────────────────────────────
# A forced-thinking model cannot have reasoning disabled, so a stored 'none'
# must coerce to '' (provider default = thinking on) and the 'none' sentinel
# must NOT appear in supported_efforts even when the raw source lists it.


@pytest.mark.parametrize("model_id", ["glm-4.7", "glm-4.7-air"])
def test_coerce_stored_none_to_empty_for_forced_glm(model_id):
    """Gap #2: GLM-4.7 + stored 'none' must coerce to '' (forced thinking)."""
    coerced = cfg.coerce_reasoning_effort_for_model(
        "none", model_id, provider_id="zai"
    )
    assert coerced == "", (
        f"{model_id} forces thinking on — stored 'none' must coerce to '' so "
        f"streaming does not build disabled reasoning; got {coerced!r}"
    )


@pytest.mark.parametrize("model_id", ["glm-5.2", "glm-4.6", "glm-5.1"])
def test_coerce_preserves_none_for_non_forced_glm(model_id):
    """Regression guard: non-forced GLM models MUST still accept 'none'."""
    coerced = cfg.coerce_reasoning_effort_for_model(
        "none", model_id, provider_id="zai"
    )
    assert coerced == "none", (
        f"{model_id} is not forced-thinking — 'none' must be preserved; "
        f"got {coerced!r}"
    )


def test_resolve_does_not_reattach_none_for_forced_glm():
    """Gap #2 part B: when the raw source lists 'none', the resolver must NOT
    reattach it to a forced-thinking model's supported options."""
    import unittest.mock as mock
    raw_with_none = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    with mock.patch(
        "api.config._resolve_model_reasoning_efforts_impl",
        return_value=raw_with_none,
    ):
        for model_id in ["glm-4.7", "glm-4.7-air"]:
            sup = cfg.resolve_model_reasoning_efforts(model_id, provider_id="zai")
            assert sup == [], (
                f"{model_id} is forced-thinking — supported_efforts must be [] "
                f"even when the raw source lists 'none'; got {sup}"
            )


def test_resolve_reattaches_none_for_thinking_tier():
    """Regression guard: GLM-4.6 (thinking tier) MUST still get 'none' in its
    supported list when the raw source lists it — the thinking tier CAN turn
    thinking off, only the forced tier cannot."""
    import unittest.mock as mock
    raw_with_none = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    with mock.patch(
        "api.config._resolve_model_reasoning_efforts_impl",
        return_value=raw_with_none,
    ):
        sup = cfg.resolve_model_reasoning_efforts("glm-4.6", provider_id="zai")
        assert sup == ["none"], (
            f"glm-4.6 (thinking tier) must keep [none] when raw source lists it; "
            f"got {sup}"
        )


def test_get_reasoning_status_forced_glm_with_stored_none_reports_default():
    """End-to-end gap #2: GLM-4.7 with agent.reasoning_effort=none configured
    must report reasoning_effort='' (default = thinking on), not 'none'."""
    import unittest.mock as mock
    with mock.patch(
        "api.config._load_yaml_config_file",
        return_value={"agent": {"reasoning_effort": "none"}},
    ):
        st = cfg.get_reasoning_status(model_id="glm-4.7", provider_id="zai")
    assert st["reasoning_effort"] == "", (
        f"forced-thinking glm-4.7 with stored 'none' must report '' (default = "
        f"thinking on); got {st['reasoning_effort']!r}"
    )
    assert st["supports_thinking_toggle"] is False
    assert st["supported_efforts"] == []


# ── Round-3: set_reasoning_effort accepts empty (gap #1 backend) ────────────────
# The Default/On re-enable path POSTs effort:'' to clear the override. The
# backend must accept empty (not 400) and remove the agent.reasoning_effort key.


def test_set_reasoning_effort_accepts_empty_as_clear():
    """Gap #1 backend: empty effort clears the override (Default/On path)."""
    import unittest.mock as mock
    saved = {}

    def fake_save(path, data):
        saved.update(data.get("agent", {}) or {})

    with mock.patch("api.config._save_yaml_config_file", side_effect=fake_save), \
         mock.patch("api.config.reload_config"):
        result = cfg.set_reasoning_effort("", model_id="glm-4.6", provider_id="zai")
    # Empty effort must remove the key entirely (not write empty string).
    assert "reasoning_effort" not in saved, (
        f"empty effort must clear agent.reasoning_effort; saved agent cfg={saved}"
    )
    assert result["reasoning_effort"] == ""


def test_set_reasoning_effort_still_rejects_invalid():
    """Regression guard: invalid effort values must still raise ValueError."""
    import unittest.mock as mock
    with mock.patch("api.config._save_yaml_config_file"), \
         mock.patch("api.config.reload_config"):
        with pytest.raises(ValueError):
            cfg.set_reasoning_effort("banana", model_id="glm-4.6", provider_id="zai")


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_set_reasoning_effort_still_accepts_valid_levels(effort):
    """Regression guard: all valid levels + none must still save correctly."""
    import unittest.mock as mock
    saved = {}

    def fake_save(path, data):
        saved.update(data.get("agent", {}) or {})

    with mock.patch("api.config._save_yaml_config_file", side_effect=fake_save), \
         mock.patch("api.config.reload_config"):
        cfg.set_reasoning_effort(effort, model_id="glm-4.6", provider_id="zai")
    assert saved.get("reasoning_effort") == effort
