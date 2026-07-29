"""French Edge voices remain migration-only reserved values.

Direct Edge synthesis is retired. These regressions preserve the legacy voice
allowlist used by the reversible migration while proving that no listed or
unlisted French voice can reactivate a WebUI Edge transport.
"""

import io
import json
import sys
from types import SimpleNamespace

import pytest

import api.routes as routes


class _FakeHandler:
    def __init__(self, body: bytes, client="127.0.0.1"):
        self.command = "POST"
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(body))}
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


def _post(body_dict, **kwargs):
    return _FakeHandler(json.dumps(body_dict).encode(), **kwargs)


@pytest.fixture(autouse=True)
def _fresh_tts_limiter(monkeypatch):
    import api.auth as auth

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    monkeypatch.setattr(routes, "_tts_synthesis_limiter", routes._TtsRateLimiter(0))


FRENCH_VOICES = [
    "fr-CA-AntoineNeural",
    "fr-CA-JeanNeural",
    "fr-CA-SylvieNeural",
    "fr-CA-ThierryNeural",
    "fr-FR-DeniseNeural",
    "fr-FR-EloiseNeural",
    "fr-FR-HenriNeural",
]


@pytest.mark.parametrize("voice", FRENCH_VOICES)
def test_french_voice_remains_reserved_for_migration_but_edge_never_synthesizes(
    monkeypatch, voice
):
    calls = []

    class ForbiddenCommunicate:
        def __init__(self, *_args, **_kwargs):
            calls.append("edge")
            raise AssertionError("retired Edge transport was called")

    monkeypatch.setitem(
        sys.modules, "edge_tts", SimpleNamespace(Communicate=ForbiddenCommunicate)
    )
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy engine reached Agent synthesis")
        ),
    )

    assert voice in routes._TTS_EDGE_VOICES
    handler = _post(
        {"engine": "edge", "text": "Bonjour"},
        client=f"127.0.0.{FRENCH_VOICES.index(voice) + 1}",
    )
    routes._handle_tts(handler, None)

    assert handler.status == 409
    assert handler.payload()["code"] == "legacy_tts_migration_required"
    assert calls == []


def test_unlisted_french_locale_is_not_added_to_migration_allowlist(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "edge_tts",
        SimpleNamespace(
            Communicate=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("retired Edge transport was called")
            )
        ),
    )
    assert "fr-BE-CharlineNeural" not in routes._TTS_EDGE_VOICES

    handler = _post(
        {"engine": "edge", "text": "Bonjour"}, client="127.0.0.20"
    )
    routes._handle_tts(handler, None)
    assert handler.status == 409
    assert handler.payload()["code"] == "legacy_tts_migration_required"
