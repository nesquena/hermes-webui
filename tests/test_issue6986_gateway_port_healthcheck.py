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

import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]

COMPOSE_FILES = (
    "docker-compose.two-container.yml",
    "docker-compose.three-container.yml",
)

UNUSABLE_KEY_SHAPES = (
    "",
    " " * 16,
    "abcdefgh",
    "changeme",
    "your_api_key_here",
    "xxxxxxxxxxxxxxxx",
    "ghp_xxxxxxxx",
)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


# The agent's API-server contract (gateway/config.py::load_gateway_config +
# gateway/platforms/api_server.py::APIServerAdapter). The adapter requires a
# usable key (has_usable_secret, min_length=16) before the listener is enabled
# and bound; API_SERVER_ENABLED/HOST alone are not enough.
AGENT_API_ENV = {
    "API_SERVER_ENABLED": "true",
    "API_SERVER_HOST": "0.0.0.0",
    "API_SERVER_PORT": "8642",
    # Interpolated from the caller's environment / .env. Empty default on
    # purpose: an unusable key short-circuits the healthcheck to healthy so the
    # default deployment becomes ready, while a usable key probes /health;
    # host+port alone never enable the API.
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
    """An unusable API_SERVER_KEY (missing, whitespace, <16 chars, placeholder,
    or placeholder shape) must short-circuit the healthcheck to healthy (exit 0)
    so the default deployment becomes ready. The real 8642 probe only runs when a
    usable key is configured. This prevents the default deployment from bricking
    while keeping the probe for keyed setups (#6986, #6987)."""
    posix_shell = shutil.which("sh") or shutil.which("bash")
    if not posix_shell:
        pytest.skip("No POSIX shell available to run healthcheck behavior matrix")

    py_exec = sys.executable.replace("\\", "/")
    if " " in py_exec and not py_exec.startswith('"'):
        py_exec = f'"{py_exec}"'

    for fname in COMPOSE_FILES:
        data = _load(fname)
        agent = data["services"]["hermes-agent"]
        assert "healthcheck" in agent, (
            f"{fname}: hermes-agent must define a healthcheck"
        )
        raw_test = agent["healthcheck"]["test"]
        test_cmd = " ".join(raw_test) if isinstance(raw_test, list) else raw_test
        shell_cmd = (
            raw_test[1]
            if isinstance(raw_test, list) and len(raw_test) > 1 and raw_test[0] == "CMD-SHELL"
            else test_cmd
        )
        # Compose rewrites every `$$` to a literal `$` before handing the command
        # to the shell, so a healthcheck that reads the container runtime env uses
        # `$$VAR`. Run the extracted command the way Compose would: without this,
        # `$$` is the shell's PID and the matrix would measure a different command
        # than the one the container executes.
        shell_cmd = shell_cmd.replace("$$", "$")

        # Closed port fixture: allocate ephemeral port and immediately close socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        closed_url = f"http://127.0.0.1:{closed_port}/health"
        closed_probe_cmd = shell_cmd.replace(
            "http://127.0.0.1:8642/health", closed_url
        ).replace("python3", py_exec)

        # 1. Assert each unusable shape exits 0 against closed port (proves no probe happens)
        for key in UNUSABLE_KEY_SHAPES:
            env = {**os.environ, "API_SERVER_KEY": key}
            res = subprocess.run(
                [posix_shell, "-c", closed_probe_cmd],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            assert res.returncode == 0, (
                f"{fname}: unusable key {key!r} must short-circuit to exit 0 "
                f"without probing (got exit code {res.returncode})"
            )

        # 2. Live server fixture: real 32-hex key exits 0 against live endpoint
        server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
        live_port = server.server_port
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            live_url = f"http://127.0.0.1:{live_port}/health"
            live_probe_cmd = shell_cmd.replace(
                "http://127.0.0.1:8642/health", live_url
            ).replace("python3", py_exec)
            real_key = "0123456789abcdef0123456789abcdef"
            env = {**os.environ, "API_SERVER_KEY": real_key}

            res_live = subprocess.run(
                [posix_shell, "-c", live_probe_cmd],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            assert res_live.returncode == 0, (
                f"{fname}: usable 32-hex key must probe live endpoint and exit 0 "
                f"(got exit code {res_live.returncode})"
            )

            # 3. Same real key against closed port exits non-zero (proves probe is real and gated on usability)
            res_closed = subprocess.run(
                [posix_shell, "-c", closed_probe_cmd],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            assert res_closed.returncode != 0, (
                f"{fname}: usable 32-hex key must exit non-zero against closed port "
                f"(got exit code {res_closed.returncode})"
            )
        finally:
            server.shutdown()
            server.server_close()

        assert "${API_SERVER_KEY}" not in test_cmd, (
            f"{fname}: healthcheck must not use an unescaped ${{API_SERVER_KEY}} — "
            "Compose would interpolate it at config time and bake the key into "
            "docker inspect"
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
