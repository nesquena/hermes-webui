"""Raw Agent TTS response length and type contract."""

from __future__ import annotations

import io
import json

import pytest

import api.routes as routes
from api.agent_tts import AgentTtsAudio, AgentTtsError


class Handler:
    def __init__(self, text="hello"):
        encoded = json.dumps({"engine": "agent", "text": text}).encode("utf-8")
        self.command = "POST"
        self.rfile = io.BytesIO(encoded)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(encoded))}
        self.client_address = ("3.0.0.1", 12345)
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


@pytest.mark.parametrize(
    ("data", "mime", "provider"),
    [
        (b"ID3\4\0\0\0\0\0\0audio", "audio/mpeg", "openai"),
        (b"RIFF\x08\0\0\0WAVEdata", "audio/wav", "edge"),
        (b"OggSfixture", "audio/ogg", "piper"),
        (b"fLaCfixture", "audio/flac", "command-provider"),
        (b"ADIFfixture", "audio/aac", "xai"),
    ],
)
def test_content_length_and_type_match_validated_agent_bytes(
    monkeypatch, data, mime, provider
):
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_a, **_k: AgentTtsAudio(data, mime, provider),
    )
    handler = Handler()
    result = routes._handle_tts(handler, None)

    assert result is True
    assert handler.status == 200
    assert handler.wfile.getvalue() == data
    assert handler.sent_headers["Content-Type"] == mime
    assert handler.sent_headers["Content-Length"] == str(len(data))
    assert handler.sent_headers["Cache-Control"] == "no-store"
    assert handler.sent_headers["X-Content-Type-Options"] == "nosniff"


def test_empty_or_invalid_agent_artifact_maps_before_response_bytes(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AgentTtsError(
            "tts_artifact_invalid", 502, "Agent TTS produced invalid audio."
        )

    monkeypatch.setattr(routes, "synthesize_agent_tts", fail)
    handler = Handler()
    routes._handle_tts(handler, None)

    assert handler.status == 502
    assert handler.sent_headers["Content-Type"].startswith("application/json")
    assert handler.payload()["code"] == "tts_artifact_invalid"
