"""Grok identity, provider ceilings and the real Off switch (#6497 re-review, 2026-11-23).

Three findings from the maintainer's re-review at ``3c1277a1b1a7``:

1. A Grok-looking provider name filtered an unrelated model. ``_normalize_grok_model_id``
   scans the whole hint for ``grok`` and only the first colon of the provider hint is
   stripped, so ``@custom:grok-proxy:deepseek-v4`` was classified by its provider slug:
   the public resolver returned no efforts, and a version-shaped slug such as
   ``grok-4.5-gateway`` imposed that family's cap. The identity used for the Grok
   decision has to be the MODEL only, the same one on the filter, the coercion and the
   status path.
2. Saved efforts ignored narrower provider declarations. The Grok branch of
   ``coerce_reasoning_effort_for_model`` returned before consulting
   ``resolve_model_reasoning_efforts``, so a provider declaring only ``low`` still
   coerced a saved ``high`` to ``high`` (and a saved ``max`` to ``high``/``xhigh``).
3. The Off switch was advertised for prefixes whose wire has no disable field. Measured
   against the installed Agent (``agent/transports/codex.py::_resolve_reasoning`` with
   ``is_xai_responses``): every Grok route answers ``("none" if "none" in vocabulary else
   None, False)`` and neither ``XAI_LEGACY_EFFORTS`` nor ``XAI_GROK46_EFFORTS`` carries
   ``none``, so no Grok request can turn reasoning off — the model default stays on.

Ground truth on this machine (Agent ``agent/reasoning_effort.py:48``)::

    XAI_GROK46_EFFORTS = ("low", "medium", "high", "xhigh")
    XAI_LEGACY_EFFORTS = ("low", "medium", "high")

module: probe_grok_off_switch (see tests below) asserts that measurement whenever the
installed Agent is importable and skips otherwise (CI installs no Agent).
"""

from __future__ import annotations

import os

import pytest

from api import config as cfg

FULL = ["minimal", "low", "medium", "high", "xhigh", "max"]
LEGACY = ["low", "medium", "high"]
FAMILY_46 = ["low", "medium", "high", "xhigh"]


def _install_config(tmp_path, monkeypatch, yaml_text: str):
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    previous = os.environ.get("HERMES_CONFIG_PATH")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(path))
    cfg.reload_config()
    yield path
    if previous is None:
        os.environ.pop("HERMES_CONFIG_PATH", None)
    else:
        os.environ["HERMES_CONFIG_PATH"] = previous
    cfg.reload_config()


@pytest.fixture()
def provider_store_low(tmp_path, monkeypatch):
    """``providers:`` store declaring a single effort for grok-4.6."""
    yield from _install_config(
        tmp_path,
        monkeypatch,
        """
providers:
  xai:
    models:
      grok-4.6:
        reasoning_efforts: ["low"]
""",
    )


@pytest.fixture()
def custom_store_low(tmp_path, monkeypatch):
    """``custom_providers:`` store declaring a single effort for grok-4.6."""
    yield from _install_config(
        tmp_path,
        monkeypatch,
        """
custom_providers:
  - name: proxy
    base_url: "https://proxy.example/v1"
    model: grok-4.6
    reasoning_efforts: ["low"]
""",
    )


@pytest.fixture()
def empty_store(tmp_path, monkeypatch):
    yield from _install_config(tmp_path, monkeypatch, "model:\n  provider: xai\n")


# ── 1. The identity is the MODEL, not the provider slug ─────────────────────


def test_hint_does_not_change_a_non_grok_answer(empty_store):
    """``@custom:grok-proxy:<non-Grok>`` must answer exactly like the bare id."""
    bare = cfg.resolve_model_reasoning_efforts("deepseek-v4", provider_id="custom:grok-proxy")
    hinted = cfg.resolve_model_reasoning_efforts(
        "@custom:grok-proxy:deepseek-v4", provider_id="custom:grok-proxy"
    )
    assert bare, "control: the bare non-Grok id has a ladder"
    assert hinted == bare


def test_grok_looking_provider_slug_does_not_cap_the_model(empty_store):
    """A version-shaped slug (``grok-4.5-gateway``) is not a Grok model."""
    assert (
        cfg._filter_reasoning_efforts_for_provider(
            FULL, "@custom:grok-4.5-gateway:deepseek-v4", "custom:grok-4.5-gateway"
        )
        == FULL
    )


def test_grok_looking_provider_slug_does_not_impose_the_off_rule(empty_store):
    """``none`` stays available for a non-Grok model behind such a slug."""
    assert (
        cfg.coerce_reasoning_effort_for_model(
            "none", "@custom:grok-4.5-gateway:deepseek-v4", provider_id="custom:grok-4.5-gateway"
        )
        == "none"
    )


@pytest.mark.parametrize(
    "model,lane,expected",
    [
        ("@custom:grok-proxy:grok-4.6", "custom:grok-proxy", FAMILY_46),
        ("@custom:agg:grok-4.5", "custom:agg", LEGACY),
        ("@custom:grok-proxy:grok-4.3", "custom:grok-proxy", LEGACY),
    ],
)
def test_a_real_grok_model_keeps_its_ladder_behind_a_custom_hint(model, lane, expected, empty_store):
    """Control: the repair must not stop classifying the model itself."""
    assert cfg.resolve_model_reasoning_efforts(model, provider_id=lane) == expected


# ── 2. Coercion honours the provider declaration ────────────────────────────


@pytest.mark.parametrize("fixture_name", ["provider_store_low", "custom_store_low"])
@pytest.mark.parametrize("requested", ["high", "medium", "xhigh", "max"])
def test_coerce_clamps_to_the_declared_ceiling(requested, fixture_name, request):
    """A saved level above the provider declaration lands on the declared one."""
    request.getfixturevalue(fixture_name)
    lane = "xai" if fixture_name == "provider_store_low" else "custom:proxy"
    assert cfg.resolve_model_reasoning_efforts("grok-4.6", provider_id=lane) == ["low"]
    assert cfg.coerce_reasoning_effort_for_model(requested, "grok-4.6", provider_id=lane) == "low"


@pytest.mark.parametrize("fixture_name", ["provider_store_low", "custom_store_low"])
def test_coerce_keeps_a_level_inside_the_declaration(fixture_name, request):
    request.getfixturevalue(fixture_name)
    lane = "xai" if fixture_name == "provider_store_low" else "custom:proxy"
    assert cfg.coerce_reasoning_effort_for_model("low", "grok-4.6", provider_id=lane) == "low"


def test_coerce_without_a_declaration_keeps_the_family_ceiling(empty_store):
    """Control: no provider declaration means the model ladder decides."""
    assert cfg.coerce_reasoning_effort_for_model("max", "grok-4.6", provider_id="xai") == "xhigh"
    assert cfg.coerce_reasoning_effort_for_model("xhigh", "grok-4.5", provider_id="xai") == "high"


# ── 3. The Off switch follows the request builder ───────────────────────────


@pytest.mark.parametrize(
    "model", ["grok-4.3", "grok-3-mini", "grok-4.20-multi-agent", "grok-4.5", "grok-4.6"]
)
def test_no_grok_prefix_advertises_an_off_switch(model, empty_store):
    profile = cfg._grok_reasoning_profile(model)
    assert profile is not None, model
    assert profile["can_disable"] is False, model


@pytest.mark.parametrize(
    "model", ["grok-4.3", "grok-3-mini", "grok-4.20-multi-agent", "grok-4.5", "grok-4.6"]
)
def test_a_stored_off_becomes_the_provider_default(model, empty_store):
    """Nothing goes on the wire: the model's default effort stays on."""
    assert cfg.coerce_reasoning_effort_for_model("none", model, provider_id="xai") == ""
    status = cfg.get_reasoning_status(model_id=model, provider_id="xai")
    assert status["supports_thinking_toggle"] is False
    assert "none" not in status["supported_efforts"]


def test_measured_against_the_installed_request_builder():
    """The Off switch exists only where the Agent's wire vocabulary carries ``none``.

    Skipped when the installed Agent is not importable (the CI environment), so the
    mirror keeps its own expectation there via the cases above.
    """
    transport = pytest.importorskip("agent.transports.codex")
    for model in ("grok-4.3", "grok-3-mini", "grok-4.20-multi-agent", "grok-4.5", "grok-4.6"):
        effort, enabled = transport._resolve_reasoning(
            model, {"is_xai_responses": True, "reasoning_config": {"enabled": False}}
        )
        profile = cfg._grok_reasoning_profile(model)
        assert profile is not None, model
        # ``None`` means "send no reasoning field", i.e. the default effort stays on.
        assert profile["can_disable"] is (effort == "none"), model
        assert enabled is False
