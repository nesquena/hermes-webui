"""Grok effort table: parity with the installed Agent (#6497 review, 2026-10-29).

The maintainer measured ``_filter_reasoning_efforts_for_provider`` against the
Agent's own contract and asked for the table to become the test:

    model                   provider       Agent sends
    grok-4.3 / grok-4.5     xai            low medium high
    grok-4.3 / grok-4.5     custom:proxy   low medium high
    grok-3-mini             both           low medium high
    grok-4.20-multi-agent   both           low medium high
    grok-4.5x               both           low medium high
    grok-4.6                both           low medium high xhigh
    grok-4 / grok-4-fast    xai            no dial
    grok-4.7 / grok-5       both           no dial

Ground truth (this machine, Agent ``agent/model_metadata.py:377`` /
``agent/reasoning_effort.py:48``):
``_GROK_EFFORT_CAPABLE_PREFIXES = ("grok-3-mini", "grok-4.20-multi-agent",
"grok-4.3", "grok-4.5", "grok-4.6")`` matched as prefixes after the vendor
prefix is stripped, ``XAI_GROK46_EFFORTS`` for the 4.6 family and
``XAI_LEGACY_EFFORTS`` otherwise, and no ``reasoning`` field at all for every
other Grok id. ``x-ai/grok-4.5`` must resolve through the vendor prefix.
"""

import pytest

from api import config as cfg

LEGACY = ["low", "medium", "high"]
FAMILY_46 = ["low", "medium", "high", "xhigh"]
#: Every lane the WebUI can carry a model id on: the two xAI credential lanes
#: and a custom proxy that serves the same id (the review's ``custom:proxy``).
LANES = ("xai", "xai-oauth", "custom:proxy")

DIAL_ROWS = [
    ("grok-4.3", LEGACY),
    ("grok-4.5", LEGACY),
    ("grok-4.5-latest", LEGACY),
    ("grok-4-5-20250930", LEGACY),  # delimiter-separated suffix keeps the 4.5 prefix
    ("grok-3-mini", LEGACY),
    ("grok-4.20-multi-agent", LEGACY),
    ("grok-4.5x", LEGACY),
    ("x-ai/grok-4.5", LEGACY),
    ("@xai:grok-4.5", LEGACY),
    ("Grok-4.5", LEGACY),
    ("grok-4.6", FAMILY_46),
    ("grok-4.6-0712", FAMILY_46),
]

NO_DIAL_ROWS = [
    "grok-4",
    "grok4",
    "grok-4-fast",
    "grok-4-45",
    "grok-4.7",
    "grok-5",
    "grok-45",
    "grok45",
    "grok-3",
    "grok-2",
    "grok-beta",
    "notgrok-4.5",
]

FULL = ["minimal", "low", "medium", "high", "xhigh", "max"]


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("model,expected", DIAL_ROWS)
def test_effort_ladder_matches_the_agent_table(model, expected, lane):
    assert cfg.resolve_model_reasoning_efforts(model, provider_id=lane) == expected


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("model", NO_DIAL_ROWS)
def test_ids_the_agent_sends_no_effort_for_get_no_dial(model, lane):
    assert cfg.resolve_model_reasoning_efforts(model, provider_id=lane) == []


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("model,expected", DIAL_ROWS)
def test_filter_is_the_single_decision_point(model, expected, lane):
    """The ceiling must hold whatever ladder the metadata source hands in."""
    assert cfg._filter_reasoning_efforts_for_provider(FULL, model, lane) == expected


def test_non_grok_models_keep_the_full_ladder():
    """Control: the Grok branch must not cap anything outside the family."""
    assert cfg._filter_reasoning_efforts_for_provider(FULL, "deepseek-v4", "custom:proxy") == FULL
    assert cfg._filter_reasoning_efforts_for_provider(FULL, "kimi-k3", "xai") == FULL


@pytest.mark.parametrize("model,expected", DIAL_ROWS)
def test_profile_answers_the_dial_question(model, expected):
    profile = cfg._grok_reasoning_profile(model)
    assert profile is not None, model
    assert cfg._candidate_supports_reasoning(model) is True, model
    assert cfg.resolve_model_reasoning_efforts(model, provider_id="xai") == expected


@pytest.mark.parametrize("model", NO_DIAL_ROWS)
def test_profile_is_absent_where_the_agent_has_no_dial(model):
    assert cfg._grok_reasoning_profile(model) is None, model
    assert cfg._candidate_supports_reasoning(model) is False, model


@pytest.mark.parametrize(
    "model,lane,requested,expected",
    [
        # inside the ladder: untouched
        ("grok-4.6", "xai", "xhigh", "xhigh"),
        ("grok-4.6", "xai", "high", "high"),
        ("grok-4.5", "xai", "medium", "medium"),
        # above the ceiling: nearest weaker supported level
        ("grok-4.6", "xai", "max", "xhigh"),
        ("grok-4.5", "xai", "xhigh", "high"),
        ("grok-4.5", "custom:proxy", "max", "high"),
        # below the floor: the Agent's clamp_effort returns the weakest supported level
        ("grok-4.6", "xai", "minimal", "low"),
        ("grok-4.5", "xai", "minimal", "low"),
        # no dial at all: nothing the Agent would send, so no field is sent
        ("grok-4", "xai", "high", ""),
        ("grok-4.7", "xai", "xhigh", ""),
        ("grok-4-fast", "xai", "low", ""),
    ],
)
def test_coerce_lands_inside_the_agent_ladder(model, lane, requested, expected):
    assert cfg.coerce_reasoning_effort_for_model(requested, model, provider_id=lane) == expected


@pytest.mark.parametrize("model", ["grok-4.5", "grok-4.6", "grok-4.5x"])
@pytest.mark.parametrize("lane", LANES)
def test_stored_none_becomes_provider_default_where_xai_rejects_it(model, lane):
    """`grok-4.5/4.6 ... REJECT "none", unlike grok-4.3` (agent/model_metadata.py:377)."""
    assert cfg.coerce_reasoning_effort_for_model("none", model, provider_id=lane) == ""


def test_grok_43_keeps_the_real_off_switch():
    """The 4.3 prefix is the one Grok id whose wire accepts 'none'."""
    assert cfg.coerce_reasoning_effort_for_model("none", "grok-4.3", provider_id="xai") == "none"


@pytest.mark.parametrize("model,lane,expected", [
    ("grok-4.5", "xai", LEGACY),
    ("grok-4.5", "custom:proxy", LEGACY),
    ("grok-4.6", "xai", FAMILY_46),
    ("grok-4", "xai", []),
])
def test_status_reports_the_same_ladder(model, lane, expected):
    status = cfg.get_reasoning_status(model_id=model, provider_id=lane)
    assert status["supported_efforts"] == expected
    assert status["supports_reasoning_effort"] is bool(expected)


@pytest.mark.parametrize("model", ["grok-4.5", "grok-4.6", "grok-4", "grok-4.7"])
def test_status_hides_the_chip_or_the_dead_none_option(model):
    status = cfg.get_reasoning_status(model_id=model, provider_id="xai")
    assert status["supports_thinking_toggle"] is False
    assert "none" not in status["supported_efforts"]
