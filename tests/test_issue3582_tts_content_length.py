"""Content-Length header present on successful TTS responses (#3582).

The HTTP/1.0 server cannot signal end-of-body via connection close without
triggering a ~31 s client timeout.  Buffering all chunks before writing lets
us include a Content-Length header so the browser can play the audio blob.
"""
import io
import json
import sys
import types

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

    def body(self):
        return self.wfile.getvalue()

    def payload(self):
        try:
            return json.loads(self.body().decode("utf-8"))
        except Exception:
            return None


def _post(body_dict, client="2.3.4.5", **kw):
    body = json.dumps(body_dict).encode()
    return _FakeHandler(body, client=client, **kw)


def _reset_limiter():
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    # Disable auth and reset limiter so guard-rail tests are deterministic.
    import api.auth as _auth
    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    _reset_limiter()
    yield
    _reset_limiter()


def _make_edge_tts_mock(audio_chunks):
    """Return a fake edge_tts module whose Communicate.stream_sync yields chunks."""
    fake_module = types.ModuleType("edge_tts")

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            pass

        def stream_sync(self):
            for data in audio_chunks:
                yield {"type": "audio", "data": data}

    fake_module.Communicate = FakeCommunicate
    return fake_module


def test_content_length_present_and_correct(monkeypatch):
    """Content-Length header matches the actual body written."""
    chunk1 = b"\xff\xfb\x90" * 100
    chunk2 = b"\xff\xfb\x90" * 50
    expected_body = chunk1 + chunk2

    monkeypatch.setitem(sys.modules, "edge_tts", _make_edge_tts_mock([chunk1, chunk2]))

    h = _post({"text": "hello", "voice": "en-US-AriaNeural"}, client="3.0.0.1")
    result = routes._handle_tts(h, None)

    assert result is True
    assert h.status == 200
    assert h.sent_headers.get("Content-Length") == str(len(expected_body))
    assert h.body() == expected_body


def test_content_type_is_audio_mpeg(monkeypatch):
    """Content-Type header must be audio/mpeg."""
    monkeypatch.setitem(sys.modules, "edge_tts", _make_edge_tts_mock([b"\xff\xfb" * 10]))

    h = _post({"text": "world", "voice": "en-US-GuyNeural"}, client="3.0.0.2")
    routes._handle_tts(h, None)

    assert h.sent_headers.get("Content-Type") == "audio/mpeg"


def test_empty_audio_returns_500(monkeypatch):
    """When TTS produces no audio chunks, return 500 before touching the response."""
    monkeypatch.setitem(sys.modules, "edge_tts", _make_edge_tts_mock([]))

    h = _post({"text": "silent", "voice": "en-US-AriaNeural"}, client="3.0.0.3")
    routes._handle_tts(h, None)

    assert h.status == 500
    assert "no audio" in (h.payload() or {}).get("error", "")


def test_content_length_single_chunk(monkeypatch):
    """Single chunk path: Content-Length equals that chunk's length."""
    data = b"A" * 1024
    monkeypatch.setitem(sys.modules, "edge_tts", _make_edge_tts_mock([data]))

    h = _post({"text": "one chunk", "voice": "zh-CN-XiaoxiaoNeural"}, client="3.0.0.4")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert h.sent_headers.get("Content-Length") == "1024"
    assert h.body() == data


def test_non_audio_chunks_ignored(monkeypatch):
    """Metadata/WordBoundary chunks must not contribute to the audio buffer."""
    audio_data = b"\xff\xfb" * 20

    fake_module = types.ModuleType("edge_tts")

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            pass

        def stream_sync(self):
            yield {"type": "WordBoundary", "data": b"ignored"}
            yield {"type": "audio", "data": audio_data}
            yield {"type": "SessionEnd", "data": None}

    fake_module.Communicate = FakeCommunicate
    monkeypatch.setitem(sys.modules, "edge_tts", fake_module)

    h = _post({"text": "mixed chunks", "voice": "en-US-AriaNeural"}, client="3.0.0.5")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert h.body() == audio_data
    assert h.sent_headers.get("Content-Length") == str(len(audio_data))
