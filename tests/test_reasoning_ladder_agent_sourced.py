"""The reasoning-effort ladder is sourced from the agent, and ceilings cap
everything above the documented top — not just the literal 'max'.

Bug being fixed: the WebUI hardcoded its own six-level ladder in
api/config.py and shipped eight static dropdown rows in static/index.html,
so when Hermes Agent released 'ultra' above 'max' (the level the TUI's
`hermes model` picker offers via hermes_constants.VALID_REASONING_EFFORTS),
the composer dropdown still topped out at Max. The ladder is now imported
from hermes_constants (fallback for standalone installs keeps the current
agent set), and every "strip the above-ceiling level" rule — provider
ceilings, the Z.AI max-capped tier, and the coercion default-deny — is
expressed as "everything above the ceiling" so the NEXT level the agent
ships is capped/surfaced correctly without another WebUI edit.
"""

import pytest

from api import config as cfg


# ── _efforts_above: the generic ceiling helper ───────────────────────────────

def test_efforts_above_xhigh_includes_max_and_ultra():
    above = cfg._efforts_above("xhigh")
    assert "max" in above and "ultra" in above
    assert "xhigh" not in above and "high" not in above


def test_efforts_above_max_includes_only_rungs_above_max():
    above = cfg._efforts_above("max")
    assert "ultra" in above
    assert "max" not in above and "xhigh" not in above


def test_efforts_above_unknown_ceiling_is_empty():
    assert cfg._efforts_above("nonexistent-level") == frozenset()


# ── Provider ceilings cap 'ultra' exactly like 'max' ─────────────────────────

def test_gemini_ceiling_strips_ultra_and_max_keeps_xhigh():
    efforts = cfg._filter_reasoning_efforts_for_provider(
        list(cfg.VALID_REASONING_EFFORTS), "gemini-4-argon", "gemini"
    )
    assert "xhigh" in efforts
    assert "max" not in efforts, "existing ceiling: Gemini tops out at xhigh"
    assert "ultra" not in efforts, (
        "the ceiling is 'tops out at xhigh' — a level above max must be "
        f"stripped with it; got {efforts!r}"
    )


def test_pre_gpt56_ceiling_strips_ultra_and_max():
    efforts = cfg._filter_reasoning_efforts_for_provider(
        list(cfg.VALID_REASONING_EFFORTS), "gpt-5.5", "openai"
    )
    assert "xhigh" in efforts and "max" not in efforts and "ultra" not in efforts


def test_legacy_claude_ceiling_strips_ultra_and_max():
    efforts = cfg._filter_reasoning_efforts_for_provider(
        list(cfg.VALID_REASONING_EFFORTS), "claude-sonnet-4-5", "anthropic"
    )
    assert "xhigh" in efforts and "max" not in efforts and "ultra" not in efforts


def test_uncapped_reasoning_lane_reaches_ultra():
    # A provider with no ceiling rule keeps the full agent ladder — this is
    # the user-facing fix: reasoning-capable routes now advertise the agent's
    # current ceiling instead of the stale hardcoded 'max'.
    efforts = cfg._filter_reasoning_efforts_for_provider(
        list(cfg.VALID_REASONING_EFFORTS), "qwen3.8-flash", "deepseek"
    )
    assert efforts == list(cfg.VALID_REASONING_EFFORTS)
    assert "ultra" in efforts


def test_zai_effort_tier_caps_at_max_not_ultra():
    # Z.AI documents its ladder top as 'max' (GLM-5.2+); 'ultra' is a
    # Hermes-internal rung above it and must not be advertised as a native
    # Z.AI wire level.
    efforts = cfg._filter_reasoning_efforts_for_provider(
        list(cfg.VALID_REASONING_EFFORTS), "glm-5.3", "zai"
    )
    assert "max" in efforts
    assert "ultra" not in efforts, f"zai tier caps at max; got {efforts!r}"


# ── Coercion agrees with the widened ladder ─────────────────────────────────

def test_ultra_degrades_on_capped_models():
    # Ceiling excludes ultra (and max) → descend to xhigh, never escalate.
    for model, provider in (
        ("gemini-4-argon", "gemini"),
        ("gpt-5.5", "openai"),
        ("claude-sonnet-4-5", "anthropic"),
    ):
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", model_id=model, provider_id=provider
        ) == "xhigh", f"ultra must degrade to the capped ceiling on {model}"


def test_ultra_degrades_on_unknown_provider():
    # #3505 default-deny applies to everything above the universally-safe
    # xhigh ceiling — ultra included.
    assert cfg.coerce_reasoning_effort_for_model(
        "ultra", model_id="some-unknown-model", provider_id="customprovider"
    ) == "xhigh"


def test_ultra_preserved_when_supported():
    # Adaptive Claude (4.6+) is uncapped at max and the agent clamps any
    # above-max Hermes-internal rung at the wire — same as the CLI picker.
    assert cfg.coerce_reasoning_effort_for_model(
        "ultra", model_id="claude-opus-4.6", provider_id="anthropic"
    ) == "ultra"


# ── Persistence accepts the agent ceiling ────────────────────────────────────

def test_set_reasoning_effort_accepts_ultra():
    import unittest.mock as mock

    saved = {}

    def fake_save(path, data):
        saved.update(data.get("agent", {}) or {})

    with mock.patch("api.config._save_yaml_config_file", side_effect=fake_save), \
         mock.patch("api.config.reload_config"):
        cfg.set_reasoning_effort("ultra")
    assert saved.get("reasoning_effort") == "ultra"


def test_set_reasoning_effort_still_rejects_garbage():
    import unittest.mock as mock

    with mock.patch("api.config._save_yaml_config_file"), \
         mock.patch("api.config.reload_config"):
        with pytest.raises(ValueError):
            cfg.set_reasoning_effort("banana")
