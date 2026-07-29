"""Retirement and SSRF-boundary assertions for WebUI OpenAI TTS.

OpenAI-compatible URL/credential handling now belongs exclusively to Hermes
Agent. The WebUI rejects the reserved legacy engine before DNS, socket, config,
or HTTP access and rejects every per-request provider override in Agent mode.
"""

from __future__ import annotations

import io
import json
import socket
import urllib.request

import pytest

import api.config as config
import api.routes as routes


class Handler:
    def __init__(self, body):
        encoded = json.dumps(body).encode("utf-8")
        self.command = "POST"
        self.rfile = io.BytesIO(encoded)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(encoded))}
        # Reach the retired-engine/request-schema boundary deterministically;
        # remote auth-disabled denial is covered by the route security suite.
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _forbid_boundary_access(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("DNS called")),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("socket called")),
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("HTTP called")),
    )
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("config read")),
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://169.254.169.254/v1",
        "https://user:pass@api.example.com/v1",
        "https://10.0.0.5/v1",
        "https://127.0.0.1/v1",
        "https://[::1]/v1",
        "https://public.example.com/v1",
    ],
)
def test_openai_legacy_engine_rejects_before_ssrf_or_credential_boundary(
    monkeypatch, base_url
):
    _forbid_boundary_access(monkeypatch)
    handler = Handler(
        {
            "engine": "openai",
            "text": "hello",
            "base_url": base_url,
            "api_key": "must-not-leak",
            "voice": "alloy",
        }
    )
    routes._handle_tts(handler, None)
    assert handler.status == 409
    assert handler.payload()["code"] == "legacy_tts_migration_required"
    assert "must-not-leak" not in handler.wfile.getvalue().decode("utf-8")


@pytest.mark.parametrize(
    "override",
    [
        {"provider": "openai"},
        {"base_url": "https://api.example.com/v1"},
        {"api_key": "secret"},
        {"voice": "alloy"},
        {"model": "tts-1"},
    ],
)
def test_agent_mode_rejects_openai_overrides_before_worker(monkeypatch, override):
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    handler = Handler({"engine": "agent", "text": "hello", **override})
    routes._handle_tts(handler, None)
    assert handler.status == 400
    assert handler.payload()["code"] == "invalid_request"


def test_openai_webui_network_and_ssrf_helpers_are_absent():
    for name in (
        "_tts_open",
        "_tts_addr_is_blocked",
        "_tts_resolve_pinned_address",
        "_normalized_openai_tts_base_url",
        "_PinnedHTTPSConnection",
        "_PinnedHTTPSHandler",
    ):
        assert not hasattr(routes, name)
