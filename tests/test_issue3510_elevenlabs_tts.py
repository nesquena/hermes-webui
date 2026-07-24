"""Retirement assertions for the former WebUI ElevenLabs transport.

The security boundary is now schema-level rejection: the reserved legacy ID
must never inspect credentials/configuration or issue an outbound request.
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
        # Reach the legacy-engine boundary under auth-disabled local-origin policy.
        # Public remote peers are rejected earlier with local_origin_required.
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


def _forbid_legacy_io(monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("HTTP called")),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("socket called")),
    )
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("config read")),
    )


@pytest.mark.parametrize(
    "body",
    [
        {"engine": "elevenlabs", "text": "hello"},
        {
            "engine": "elevenlabs",
            "text": "hello",
            "voice_id": "../../etc/passwd",
            "api_key": "must-not-be-read",
        },
        {
            "engine": "elevenlabs",
            "text": "x" * 10000,
            "base_url": "http://169.254.169.254",
        },
    ],
)
def test_elevenlabs_legacy_requests_return_409_before_any_boundary_access(
    monkeypatch, body
):
    _forbid_legacy_io(monkeypatch)
    handler = Handler(body)
    routes._handle_tts(handler, None)
    assert handler.status == 409
    assert handler.payload()["code"] == "legacy_tts_migration_required"
    assert "must-not-be-read" not in handler.wfile.getvalue().decode("utf-8")


def test_elevenlabs_webui_transport_helpers_are_absent():
    assert not hasattr(routes, "_tts_open")
    assert not hasattr(routes, "_buffer_tts_audio_response")
    assert not hasattr(routes, "_NoRedirectTtsHandler")
