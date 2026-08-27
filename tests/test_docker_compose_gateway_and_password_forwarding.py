"""Regression coverage for two Docker-compose gaps found in the field.

1. Gateway API not reachable out of the box. The two/three-container compose
   files set ``HERMES_API_URL=http://hermes-agent:8642`` on the WebUI but
   never configured the agent to listen on 8642. The agent image only starts
   its API-server listener when ``API_SERVER_KEY`` is a usable value (>=16
   chars); ``API_SERVER_ENABLED`` alone does nothing. The compose files now
   forward ``API_SERVER_KEY`` from ``.env`` and bind the listener on
   0.0.0.0, and the WebUI receives the matching
   ``HERMES_WEBUI_GATEWAY_API_KEY`` so its health probe authenticates.

2. ``HERMES_WEBUI_PASSWORD`` set in ``.env`` had no effect. Docker Compose
   uses ``.env`` only for variable interpolation — a value is not injected
   into a container unless the compose file references it. All three compose
   files now forward ``HERMES_WEBUI_PASSWORD`` into the WebUI service.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

MULTI = ("docker-compose.two-container.yml", "docker-compose.three-container.yml")
ALL = ("docker-compose.yml",) + MULTI


# ── 1: gateway API server forwarding (multi-container only) ────────────────


def test_agent_service_enables_api_server():
    """The agent service must carry the API-server env block so a user who
    sets API_SERVER_KEY in .env gets a listening 8642 without editing the
    compose file."""
    for fn in MULTI:
        src = (REPO / fn).read_text(encoding="utf-8")
        assert "- API_SERVER_ENABLED=true" in src, (
            f"{fn}: agent must declare API_SERVER_ENABLED."
        )
        assert "- API_SERVER_HOST=0.0.0.0" in src, (
            f"{fn}: API server must bind 0.0.0.0 — the default 127.0.0.1 is "
            "unreachable from the WebUI container over the compose network."
        )
        assert "- API_SERVER_KEY=${API_SERVER_KEY:-}" in src, (
            f"{fn}: agent must forward API_SERVER_KEY from .env so the gateway "
            "API listener can start (the agent requires a usable key, >=16 chars)."
        )


def test_webui_service_forwards_gateway_api_key():
    """The WebUI must receive the same API_SERVER_KEY so its /health/detailed
    probe authenticates instead of 401ing into 'Gateway endpoint not
    reachable'."""
    for fn in MULTI:
        src = (REPO / fn).read_text(encoding="utf-8")
        assert "- HERMES_WEBUI_GATEWAY_API_KEY=${API_SERVER_KEY:-}" in src, (
            f"{fn}: WebUI must forward HERMES_WEBUI_GATEWAY_API_KEY from the "
            "same API_SERVER_KEY so the gateway health probe authenticates."
        )


# ── 2: HERMES_WEBUI_PASSWORD forwarding (all compose files) ────────────────


def test_webui_service_forwards_password():
    """HERMES_WEBUI_PASSWORD set in .env must reach the WebUI container.
    .env is only for compose interpolation; without this forwarding line the
    password silently does nothing."""
    for fn in ALL:
        src = (REPO / fn).read_text(encoding="utf-8")
        assert "- HERMES_WEBUI_PASSWORD=${HERMES_WEBUI_PASSWORD:-}" in src, (
            f"{fn}: WebUI must forward HERMES_WEBUI_PASSWORD from .env — "
            "setting it only in .env does nothing unless the compose file "
            "references it."
        )


def test_env_example_documents_api_server_key():
    """.env.docker.example must tell users the API server needs a usable
    API_SERVER_KEY (>=16 chars) and that it is forwarded automatically."""
    example = (REPO / ".env.docker.example").read_text(encoding="utf-8")
    assert "API_SERVER_KEY" in example, (
        ".env.docker.example must document API_SERVER_KEY."
    )
    assert ">=16" in example, (
        ".env.docker.example must state the API_SERVER_KEY length floor "
        "(>=16 chars) — shorter keys are silently ignored by the agent."
    )
    assert "HERMES_WEBUI_GATEWAY_API_KEY" in example, (
        ".env.docker.example must mention that the WebUI key is forwarded "
        "automatically from the same API_SERVER_KEY."
    )
