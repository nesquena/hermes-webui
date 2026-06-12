"""Coverage for French voices in the Edge TTS allowlist.

Mirrors the in-process / mocked-edge_tts pattern from
test_issue2931_edge_tts_endpoint.py to assert each French voice in the
allowlist reaches synthesis (HTTP 200) and that an unlisted French locale
(fr-BE) is still rejected at the allowlist (HTTP 400).
"""
import io
import json
import sys
from types import SimpleNamespace

import pytest

import api.routes as routes


class _FakeHandler:
    def __init__(self, body: bytes, command: str = "POST", headers=None, client="1.2.3.4"):
        self.command = command
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = headers or {}
        self.headers.setdefault("Content-Length", str(len(body)))
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
        try:
            return json.loads(self.wfile.getvalue().decode("utf-8"))
        except Exception:
            return None


def _post(body_dict, **kw):
    body = json.dumps(body_dict).encode()
    return _FakeHandler(body, **kw)


def _reset_limiter():
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


@pytest.fixture(autouse=True)
def _fresh_tts_limiter(monkeypatch):
    import api.auth as _auth
    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    _reset_limiter()
    yield
    _reset_limiter()


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
def test_french_voice_in_allowlist_reaches_synthesis(monkeypatch, voice):
    captured = {}

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            captured["text"] = text
            captured["voice"] = voice

        def stream_sync(self):
            yield {"type": "audio", "data": b"abc"}

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=FakeCommunicate))

    # Unique client per voice so the 2s per-client rate limiter does not throttle
    # successive parametrized runs in this module.
    client = f"10.99.0.{FRENCH_VOICES.index(voice) + 1}"
    h = _post({"text": "Bonjour", "voice": voice}, client=client)
    routes._handle_tts(h, None)

    assert h.status == 200, h.payload()
    assert captured["voice"] == voice


def test_unlisted_french_locale_still_rejected():
    # fr-BE is a real Edge voice locale but is intentionally not in the
    # allowlist; it must still be rejected at the voice check before any
    # synthesis is attempted.
    h = _post({"text": "Bonjour", "voice": "fr-BE-CharlineNeural"}, client="10.99.1.1")
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "invalid voice" in (h.payload() or {}).get("error", "")
