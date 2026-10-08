"""Voice/locale matching for the Edge TTS endpoint.

The reported bug: a message that contains Cyrillic was sent to the default
zh/en Microsoft voice. A voice whose script cannot render Cyrillic either
produces gibberish or silently drops the Russian and reads only the Latin
letters and digits ("reads only English and numbers"). The server must never
hand a non-Russian voice Cyrillic text, and the Russian neural voices must be
selectable in the allowlist.

These exercise ``_handle_tts`` in-process through a fake handler and a fake
``edge_tts`` module — no network and no real synthesis needed, since the voice
resolution and the allowlist check both happen before the edge-tts call.
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
    return _FakeHandler(json.dumps(body_dict).encode(), **kw)


def _reset_limiter():
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


@pytest.fixture(autouse=True)
def _fresh_tts_limiter(monkeypatch):
    # Same isolation as the #2931 module: reset the limiter both sides of each
    # test and pin auth off, since these assertions sit past the rate-limit
    # guard on a path that another suite could otherwise 401 first.
    import api.auth as _auth

    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    _reset_limiter()
    yield
    _reset_limiter()


@pytest.fixture
def capture(monkeypatch):
    """Fake edge_tts that records the (text, voice) it was asked to speak."""
    captured = {}

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            captured["text"] = text
            captured["voice"] = voice
            captured["kwargs"] = kwargs

        def stream_sync(self):
            yield {"type": "audio", "data": b"abc"}

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=FakeCommunicate))
    return captured


# ── The bug: Cyrillic must never reach a non-Russian voice ───────────────────

def test_cyrillic_with_stale_chinese_default_switches_to_russian(capture):
    """The reported shape: a client still sending the old zh default on Russian
    text must get a Russian voice, not Xiaoxiao dropping the Cyrillic."""
    h = _post({"text": "Привет, сэр. Это русский текст.", "voice": "zh-CN-XiaoxiaoNeural"}, client="10.9.0.1")
    routes._handle_tts(h, None)
    assert h.status == 200
    assert capture["voice"] == "ru-RU-DmitryNeural"


def test_cyrillic_without_voice_defaults_to_russian(capture):
    h = _post({"text": "Привет, сэр."}, client="10.9.0.2")
    routes._handle_tts(h, None)
    assert h.status == 200
    assert capture["voice"] == "ru-RU-DmitryNeural"


def test_cyrillic_with_english_voice_switches_to_russian(capture):
    h = _post({"text": "Привет.", "voice": "en-US-AriaNeural"}, client="10.9.0.3")
    routes._handle_tts(h, None)
    assert h.status == 200
    assert capture["voice"] == "ru-RU-DmitryNeural"


# ── No regression: an explicit matching voice is honoured ────────────────────

def test_explicit_russian_voice_is_kept(capture):
    h = _post({"text": "Привет.", "voice": "ru-RU-SvetlanaNeural"}, client="10.9.0.4")
    routes._handle_tts(h, None)
    assert h.status == 200
    assert capture["voice"] == "ru-RU-SvetlanaNeural"


def test_latin_text_keeps_saved_english_voice(capture):
    h = _post({"text": "Hello, sir.", "voice": "en-US-AriaNeural"}, client="10.9.0.5")
    routes._handle_tts(h, None)
    assert h.status == 200
    assert capture["voice"] == "en-US-AriaNeural"


def test_latin_text_without_voice_is_not_forced_to_russian(capture):
    h = _post({"text": "Hello, sir."}, client="10.9.0.6")
    routes._handle_tts(h, None)
    assert h.status == 200
    assert capture["voice"] != "ru-RU-DmitryNeural"


def test_russian_voices_pass_the_allowlist(capture):
    """The Russian neural voices must be selectable, or the fallback 400s."""
    for i, voice in enumerate(("ru-RU-DmitryNeural", "ru-RU-SvetlanaNeural", "ru-RU-DariyaNeural")):
        h = _post({"text": "Привет.", "voice": voice}, client=f"10.9.1.{i}")
        routes._handle_tts(h, None)
        assert h.status == 200, voice
        assert capture["voice"] == voice
