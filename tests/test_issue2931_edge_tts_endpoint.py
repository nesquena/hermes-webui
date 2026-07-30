"""Validation + security-path coverage for the Edge TTS endpoint (#2931).

These exercise the guard rails of _handle_tts (method, input cap, voice
allowlist, rate limiting) in-process via a fake handler — no network and no
real edge-tts synthesis required, since every rejection happens before the
edge_tts import / Communicate call.
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
    # Drop any limiter state carried between tests so rate-limit assertions are
    # deterministic regardless of run order.
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


@pytest.fixture(autouse=True)
def _fresh_tts_limiter(monkeypatch):
    # The limiter is a function-attribute singleton that persists across the
    # whole test session; reset it before AND after every test in this module so
    # neither prior suite state nor these tests leak rate-limit state.
    # Also force auth OFF: these tests exercise the method/length/voice/rate-limit
    # guards, which sit before the auth check. Another test in the full suite can
    # leave is_auth_enabled() True globally, which would 401 these requests before
    # they reach the path under test. Pin it False so the assertions are
    # deterministic regardless of suite order.
    import api.auth as _auth
    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    _reset_limiter()
    yield
    _reset_limiter()


def test_tts_requires_post():
    h = _post({"text": "hello"}, command="GET")
    routes._handle_tts(h, None)
    assert h.status == 405


def test_tts_requires_text():
    h = _post({"text": "   "})
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "text is required" in (h.payload() or {}).get("error", "")


def test_tts_rejects_overlong_text():
    h = _post({"text": "x" * 5001}, client="10.0.0.1")
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "too long" in (h.payload() or {}).get("error", "")


def test_tts_rejects_unknown_voice():
    h = _post({"text": "hello", "voice": "evil-voice-injection"}, client="10.0.0.2")
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "invalid voice" in (h.payload() or {}).get("error", "")


def test_tts_rejects_invalid_rate_before_engine():
    h = _post({"text": "hello", "voice": "en-US-AriaNeural", "rate": "<break/>"}, client="10.0.0.6")
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "invalid rate" in (h.payload() or {}).get("error", "")


def test_tts_rejects_invalid_pitch_before_engine():
    h = _post({"text": "hello", "voice": "en-US-AriaNeural", "pitch": "+500Hz"}, client="10.0.0.7")
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "invalid pitch" in (h.payload() or {}).get("error", "")


def test_tts_accepts_ui_prosody_shape(monkeypatch):
    captured = {}

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            captured["text"] = text
            captured["voice"] = voice
            captured["kwargs"] = kwargs

        def stream_sync(self):
            yield {"type": "audio", "data": b"abc"}

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=FakeCommunicate))

    h = _post(
        {
            "text": "hello",
            "voice": "en-US-AriaNeural",
            "rate": "+10%",
            "pitch": "-5Hz",
        },
        client="10.0.0.8",
    )
    routes._handle_tts(h, None)

    assert h.status == 200
    assert captured["text"] == "hello"
    assert captured["voice"] == "en-US-AriaNeural"
    assert captured["kwargs"] == {"rate": "+10%", "pitch": "-5Hz"}


def test_tts_rate_limits_second_immediate_request():
    # The limiter runs (and records the client) BEFORE the voice allowlist and
    # before any edge-tts synthesis. Use an invalid voice so the first request
    # still registers with the limiter but returns at the allowlist (400) without
    # making a real network call; the second immediate request from the SAME
    # client is then throttled (429). Unique client IP avoids any cross-test key
    # collision (the autouse fixture also resets the limiter each test).
    h1 = _post({"text": "hello", "voice": "not-a-real-voice"}, client="10.0.0.3")
    routes._handle_tts(h1, None)
    assert h1.status == 400  # rejected at allowlist, limiter recorded the client
    h2 = _post({"text": "hello", "voice": "not-a-real-voice"}, client="10.0.0.3")
    routes._handle_tts(h2, None)
    assert h2.status == 429


def test_tts_rate_limit_ignores_spoofed_forwarded_for_by_default():
    h1 = _post(
        {"text": "hello", "voice": "not-a-real-voice"},
        headers={"X-Forwarded-For": "203.0.113.10"},
        client="10.0.0.4",
    )
    routes._handle_tts(h1, None)
    assert h1.status == 400

    h2 = _post(
        {"text": "hello", "voice": "not-a-real-voice"},
        headers={"X-Forwarded-For": "203.0.113.11"},
        client="10.0.0.4",
    )
    routes._handle_tts(h2, None)
    assert h2.status == 429


def test_tts_rate_limit_can_trust_forwarded_for_when_opted_in(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")

    h1 = _post(
        {"text": "hello", "voice": "not-a-real-voice"},
        headers={"X-Forwarded-For": "203.0.113.12"},
        client="10.0.0.5",
    )
    routes._handle_tts(h1, None)
    assert h1.status == 400

    h2 = _post(
        {"text": "hello", "voice": "not-a-real-voice"},
        headers={"X-Forwarded-For": "203.0.113.13"},
        client="10.0.0.5",
    )
    routes._handle_tts(h2, None)
    assert h2.status == 400
