"""Retirement and request-boundary coverage for the former Edge endpoint.

Edge remains a reserved migration ID. It is never an executable WebUI engine;
provider selection and synthesis are owned by Hermes Agent.
"""

from __future__ import annotations

import io
import json

import pytest

import api.routes as routes
from api.agent_tts import AgentTtsAudio


class Handler:
    def __init__(self, body, *, command="POST", client="127.0.0.1", headers=None):
        encoded = json.dumps(body).encode("utf-8")
        self.command = command
        self.rfile = io.BytesIO(encoded)
        self.wfile = io.BytesIO()
        self.headers = dict(headers or {})
        self.headers.setdefault("Content-Length", str(len(encoded)))
        self.client_address = (client, 12345)
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


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    monkeypatch.setattr(routes, "_tts_synthesis_limiter", routes._TtsRateLimiter(0))


def test_tts_requires_post():
    handler = Handler({"engine": "agent", "text": "hello"}, command="GET")
    routes._handle_tts(handler, None)
    assert handler.status == 405


def test_missing_engine_is_invalid_and_does_not_assume_edge(monkeypatch):
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    handler = Handler({"text": "hello"})
    routes._handle_tts(handler, None)
    assert handler.status == 400
    assert handler.payload()["code"] == "invalid_request"


def test_edge_is_reserved_migration_state_with_no_agent_dispatch(monkeypatch):
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    handler = Handler(
        {
            "engine": "edge",
            "text": "hello",
            "voice": "en-US-AriaNeural",
            "rate": "+10%",
            "pitch": "-5Hz",
        }
    )
    routes._handle_tts(handler, None)
    assert handler.status == 409
    assert handler.payload()["code"] == "legacy_tts_migration_required"


def test_agent_rejects_edge_voice_and_prosody_overrides(monkeypatch):
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    handler = Handler(
        {
            "engine": "agent",
            "text": "hello",
            "voice": "en-US-AriaNeural",
            "rate": "+10%",
        }
    )
    routes._handle_tts(handler, None)
    assert handler.status == 400


def test_agent_rate_limit_uses_raw_peer_not_spoofed_forwarded_for(monkeypatch):
    # Authorization/trusted-proxy behavior has dedicated route tests. Isolate
    # this assertion to limiter ownership of the raw socket peer.
    monkeypatch.setattr(routes, "_tts_gate_allows", lambda _handler: True)
    monkeypatch.setattr(routes, "_tts_synthesis_limiter", routes._TtsRateLimiter(60))
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_a, **_k: AgentTtsAudio(
            b"RIFF\x08\0\0\0WAVEdata", "audio/wav", "edge"
        ),
    )
    first = Handler(
        {"engine": "agent", "text": "one"},
        client="10.0.0.4",
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    second = Handler(
        {"engine": "agent", "text": "two"},
        client="10.0.0.4",
        headers={"X-Forwarded-For": "203.0.113.11"},
    )
    routes._handle_tts(first, None)
    routes._handle_tts(second, None)
    assert first.status == 200
    assert second.status == 429


def test_agent_requires_nonempty_text_before_worker(monkeypatch):
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    handler = Handler({"engine": "agent", "text": "   "})
    routes._handle_tts(handler, None)
    assert handler.status == 400
