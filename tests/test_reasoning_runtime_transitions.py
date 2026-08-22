"""#6018 final gate — clamp reasoning after Agent fallback/model transitions."""

import sys
from types import SimpleNamespace

import pytest

from api.agent_runtime import _destination_aware_ai_agent_class


class _MinimalAgent:
    def __init__(self, *, model, provider, base_url, reasoning_config):
        self.model = model
        self.provider = provider
        self.base_url = base_url
        self.reasoning_config = reasoning_config


def _guarded_agent(model="gpt-5.6-sol", effort="ultra"):
    guarded = _destination_aware_ai_agent_class(_MinimalAgent)
    return guarded(
        model=model,
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        reasoning_config={"enabled": True, "effort": effort},
    )


def test_transition_assignments_follow_destination_model_ceiling():
    agent = _guarded_agent()
    assert agent.reasoning_config["effort"] == "ultra"

    # Both installed-Agent transition chokepoints assign reasoning_config only
    # after updating agent.model/provider. The shared assignment guard therefore
    # clamps fallback and /model writes against the destination.
    agent.model = "gpt-5.5"
    agent.reasoning_config = {"enabled": True, "effort": "ultra"}
    assert agent.reasoning_config["effort"] == "xhigh"

    agent.model = "o3"
    agent.reasoning_config = {"enabled": True, "effort": "ultra"}
    assert agent.reasoning_config["effort"] == "high"


def test_gpt56_ultra_to_gpt55_fallback_emits_xhigh_on_real_agent_transport(
    monkeypatch,
):
    """Compose the installed Agent resolver and Responses wire transport."""
    constants = pytest.importorskip("hermes_constants")
    codex = pytest.importorskip("agent.transports.codex")
    # ResponsesApiTransport imports this one identity constant lazily from the
    # heavyweight run_agent module. Supply only that production input so the
    # wire test does not require the Agent's unrelated terminal/browser deps.
    monkeypatch.setitem(
        sys.modules,
        "run_agent",
        SimpleNamespace(DEFAULT_AGENT_IDENTITY="Hermes Agent"),
    )

    cfg = {"agent": {"reasoning_effort": "ultra"}}
    agent = _guarded_agent(
        effort=constants.resolve_reasoning_config(cfg, "gpt-5.6-sol")["effort"]
    )
    assert agent.reasoning_config["effort"] == "ultra"

    # Production fallback activation updates the route, then assigns the result
    # of resolve_reasoning_config(load_config(), agent.model).
    agent.model = "gpt-5.5"
    agent.reasoning_config = constants.resolve_reasoning_config(cfg, agent.model)
    assert agent.reasoning_config["effort"] == "xhigh"

    wire = codex.ResponsesApiTransport().build_kwargs(
        agent.model,
        [{"role": "user", "content": "fallback probe"}],
        reasoning_config=agent.reasoning_config,
        provider=agent.provider,
        base_url=agent.base_url,
        is_codex_backend=True,
    )
    assert wire["reasoning"]["effort"] == "xhigh"
    assert wire["reasoning"]["effort"] != "ultra"