"""Regression tests for #6986 — multi-container WebUI must reach the
hermes-agent gateway on port 8642.

Issue #6986: in the two- and three-container compose setups the WebUI failed
to reach `hermes-agent:8642` ("can't reach hermes-agent", failed to detect
`run_agents`). Two config gaps caused it:

1. The `hermes-agent` service never enabled the gateway API listener. The
   agent only binds port 8642 when the API server platform is configured with
   a usable `API_SERVER_KEY` (>=16 chars, per the startup guard
   `has_usable_secret` in the agent's `gateway/platforms/api_server.py`);
   `API_SERVER_ENABLED` + `API_SERVER_HOST` alone silently leave the port
   unbound, so the WebUI gets "Connection refused".
2. The WebUI (and dashboard) used a bare `depends_on` that only waits for the
   container to start, not for the gateway to actually listen on 8642 — the
   health probe raced the agent boot and reported the gateway as down.

This module parses the effective Compose model (not source text) and pins the
real Agent contract together: the `API_SERVER_*` variables, WebUI key
forwarding, the healthcheck, and the `service_healthy` dependency.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]

COMPOSE_FILES = (
    "docker-compose.two-container.yml",
    "docker-compose.three-container.yml",
)

# The agent's API-server contract (gateway/config.py::load_gateway_config +
# gateway/platforms/api_server.py::APIServerAdapter). The adapter requires a
# usable key (has_usable_secret, min_length=16) before the listener is enabled
# and bound; API_SERVER_ENABLED/HOST alone are not enough.
AGENT_API_ENV = {
    "API_SERVER_ENABLED": "true",
    "API_SERVER_HOST": "0.0.0.0",
    "API_SERVER_PORT": "8642",
    # Interpolated from the caller's environment / .env. Empty default on
    # purpose: a missing/weak key must leave the service unhealthy, never
    # silently "enabled" by host+port alone.
    "API_SERVER_KEY": "${API_SERVER_KEY:-}",
}


def _load(fname: str) -> dict:
    return yaml.safe_load((REPO / fname).read_text(encoding="utf-8"))


def _env_map(service: dict) -> dict[str, str]:
    env: dict[str, str] = {}
    for entry in service.get("environment") or []:
        key, _, value = entry.partition("=")
        env[key] = value
    return env


def test_agent_service_enables_api_server_with_real_contract():
    """The hermes-agent service must enable the gateway API with the real
    Agent contract vars (API_SERVER_*), not the unsupported HERMES_GATEWAY_*
    names. The agent consumes no HERMES_GATEWAY_* variable, so asserting the
    parseable model (not source text) is what proves the effective container
    can actually bind 8642 (#6986)."""
    for fname in COMPOSE_FILES:
        data = _load(fname)
        agent_env = _env_map(data["services"]["hermes-agent"])
        for name, expected in AGENT_API_ENV.items():
            assert agent_env.get(name) == expected, (
                f"{fname}: hermes-agent must set {name}={expected!r} so the "
                "gateway API listener binds per the Agent contract"
            )
        assert "HERMES_GATEWAY_HOST" not in agent_env, (
            f"{fname}: HERMES_GATEWAY_HOST is not consumed by the Agent — "
            "the real contract is API_SERVER_HOST"
        )
        assert "HERMES_GATEWAY_PORT" not in agent_env, (
            f"{fname}: HERMES_GATEWAY_PORT is not consumed by the Agent — "
            "the real contract is API_SERVER_PORT"
        )


def test_webui_forwards_gateway_api_key():
    """The WebUI service must forward the same API_SERVER_KEY value so its
    gateway health probe can authenticate (must match the agent's key)."""
    for fname in COMPOSE_FILES:
        data = _load(fname)
        agent_env = _env_map(data["services"]["hermes-agent"])
        webui_env = _env_map(data["services"]["hermes-webui"])
        assert "HERMES_WEBUI_GATEWAY_API_KEY" in webui_env, (
            f"{fname}: hermes-webui must forward HERMES_WEBUI_GATEWAY_API_KEY "
            "so the gateway health probe can authenticate"
        )
        assert (
            webui_env["HERMES_WEBUI_GATEWAY_API_KEY"] == agent_env["API_SERVER_KEY"]
        ), (
            f"{fname}: HERMES_WEBUI_GATEWAY_API_KEY must forward the same "
            "value as the agent's API_SERVER_KEY"
        )
        assert webui_env.get("HERMES_API_URL") == "http://hermes-agent:8642", (
            f"{fname}: hermes-webui must point HERMES_API_URL at the agent "
            "gateway over the compose network"
        )


def test_missing_key_short_circuits_healthcheck_to_healthy():
    """A missing or weak API_SERVER_KEY must short-circuit the healthcheck to
    healthy (exit 0) so the default no-key deployment becomes ready. The real
    8642 probe only runs when a usable key is configured. This prevents the
    default deployment from bricking while keeping the probe for keyed setups
    (#6986, #6987)."""
    for fname in COMPOSE_FILES:
        data = _load(fname)
        agent = data["services"]["hermes-agent"]
        assert "healthcheck" in agent, (
            f"{fname}: hermes-agent must define a healthcheck"
        )
        test_cmd = " ".join(agent["healthcheck"]["test"])
        # The healthcheck must short-circuit when API_SERVER_KEY is empty
        assert "[ -z \"${API_SERVER_KEY}\" ]" in test_cmd, (
            f"{fname}: healthcheck must short-circuit when API_SERVER_KEY is empty"
        )
        # The real probe must still be present for when a key IS configured
        assert "8642" in test_cmd, (
            f"{fname}: healthcheck must probe the gateway port 8642 when key is set"
        )
        assert "health" in test_cmd, (
            f"{fname}: healthcheck should hit a gateway health endpoint when key is set"
        )
        # The setup message must exist in the compose comments and state the
        # real requirement (usable key >=16 chars) — never claim host+port
        # alone enable the API.
        src = (REPO / fname).read_text(encoding="utf-8")
        assert "API_SERVER_KEY" in src
        assert "16" in src, (
            f"{fname}: comment must document the >=16 char API_SERVER_KEY requirement"
        )
        assert "never enable" in src, (
            f"{fname}: comment must state that host+port alone never enable the API"
        )
        # Document the short-circuit behavior
        assert "short-circuit" in src.lower(), (
            f"{fname}: comment must document the short-circuit behavior for missing key"
        )


def test_webui_and_dashboard_wait_for_healthy_agent():
    """The WebUI (and dashboard, in the three-container file) must start only
    after the agent gateway is healthy — a bare depends_on starts too early
    and the WebUI reports "can't reach hermes-agent" (#6986)."""
    two = _load("docker-compose.two-container.yml")
    dep = two["services"]["hermes-webui"].get("depends_on")
    assert isinstance(dep, dict), (
        "two-container: hermes-webui depends_on must be the map form"
    )
    assert dep.get("hermes-agent", {}).get("condition") == "service_healthy", (
        "two-container: hermes-webui must depend on hermes-agent with "
        "condition: service_healthy"
    )

    three = _load("docker-compose.three-container.yml")
    for svc in ("hermes-webui", "hermes-dashboard"):
        dep = three["services"][svc].get("depends_on")
        assert isinstance(dep, dict), (
            f"three-container: {svc} depends_on must be the map form"
        )
        assert dep.get("hermes-agent", {}).get("condition") == "service_healthy", (
            f"three-container: {svc} must depend on hermes-agent with "
            "condition: service_healthy"
        )


def test_docker_docs_cover_port_8642_troubleshooting():
    """docs/docker.md must document the 8642 failure mode with the real Agent
    contract: host-side checks use the published loopback endpoint (the
    service-name URL is Compose-network DNS and does not resolve from the
    host), and a usable API_SERVER_KEY is required."""
    src = (REPO / "docs" / "docker.md").read_text(encoding="utf-8")
    assert "8642" in src
    assert "#6986" in src
    assert "service_healthy" in src
    # Real Agent contract in the docs, not the unsupported names.
    assert "API_SERVER_KEY" in src
    assert "HERMES_GATEWAY_HOST" not in src, (
        "docs/docker.md must not recommend HERMES_GATEWAY_HOST — the Agent "
        "consumes no such variable"
    )
    assert "HERMES_GATEWAY_PORT" not in src, (
        "docs/docker.md must not recommend HERMES_GATEWAY_PORT — the Agent "
        "consumes no such variable"
    )
    # Host-side check uses the published loopback endpoint.
    assert "http://127.0.0.1:8642/health" in src, (
        "docs/docker.md must probe the published loopback endpoint from the host"
    )
    # The service-name URL is documented for in-container use only, and the
    # docs must state that host+port alone never enable the API.
    assert "Compose-network DNS" in src, (
        "docs/docker.md must explain the service-name URL is Compose-network DNS"
    )
    assert "never enable" in src, (
        "docs/docker.md must state that host and port alone never enable the API"
    )
