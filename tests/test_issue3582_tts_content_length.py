"""TTS response is valid JSON with base64 audio data URL.

After the TTS delegation refactor, all providers (edge, mistral, etc.)
go through the agent's text_to_speech_tool and return JSON in the
same shape:

    {"audio": "data:audio/mpeg;base64,...", "content_type": "audio/mpeg", "size": N}
"""
import base64
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
    """Disable auth, reset limiter, and mock the agent's text_to_speech_tool."""
    import api.auth as _auth
    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    _reset_limiter()
    yield
    _reset_limiter()


def _mock_tool_returning(monkeypatch, audio_bytes):
    """Mock text_to_speech_tool to write audio_bytes and return success."""
    def fake_tool(text, output_path):
        with open(output_path, "wb") as f:
            f.write(audio_bytes)
        return json.dumps({"success": True, "file_path": output_path})

    tool = SimpleNamespace(text_to_speech_tool=fake_tool)
    monkeypatch.setitem(sys.modules, "tools.tts_tool", tool)


def _decode_audio_data_url(payload):
    """Extract and base64-decode the audio data URL from a TTS response payload."""
    assert payload is not None, "response is not valid JSON"
    audio_url = payload.get("audio")
    assert audio_url is not None
    assert audio_url.startswith("data:audio/mpeg;base64,")
    b64 = audio_url.split(",", 1)[1]
    return base64.b64decode(b64)


def test_response_is_json_with_audio_data_url(monkeypatch):
    """TTS returns JSON with a base64 data URL for all providers."""
    expected = b"\xff\xfb\x90" * 100 + b"\xff\xfb\x90" * 50
    _mock_tool_returning(monkeypatch, expected)

    h = _post({"text": "hello"}, client="3.0.0.1")
    routes._handle_tts(h, None)

    assert h.status == 200
    payload = h.payload()
    assert payload is not None
    assert payload.get("content_type") == "audio/mpeg"
    assert payload.get("size") == len(expected)
    assert _decode_audio_data_url(payload) == expected


def test_content_type_is_audio_mpeg(monkeypatch):
    """Content-Type field in the JSON response is always audio/mpeg."""
    _mock_tool_returning(monkeypatch, b"\xff\xfb" * 10)

    h = _post({"text": "world"}, client="3.0.0.2")
    routes._handle_tts(h, None)

    payload = h.payload()
    assert payload is not None
    assert payload.get("content_type") == "audio/mpeg"


def test_empty_audio_returns_500(monkeypatch):
    """When text_to_speech_tool reports success=False, return 500."""
    tool = SimpleNamespace(text_to_speech_tool=lambda **kw: json.dumps({
        "success": False, "error": "TTS produced no audio"
    }))
    monkeypatch.setitem(sys.modules, "tools.tts_tool", tool)

    h = _post({"text": "silent"}, client="3.0.0.3")
    routes._handle_tts(h, None)

    assert h.status == 500
    assert "no audio" in (h.payload() or {}).get("error", "")


def test_size_matches_audio_data(monkeypatch):
    """The reported size equals the actual decoded audio length."""
    data = b"A" * 1024
    _mock_tool_returning(monkeypatch, data)

    h = _post({"text": "one chunk"}, client="3.0.0.4")
    routes._handle_tts(h, None)

    assert h.status == 200
    payload = h.payload()
    assert payload is not None
    assert payload.get("size") == 1024
    assert _decode_audio_data_url(payload) == data


def test_non_audio_chunks_not_applicable(monkeypatch):
    """After delegation, the agent handles chunking — we just return its file.

    This test verifies the data URL is the complete file the agent produced,
    not partial chunks (chunking is the agent's concern, not ours).
    """
    audio_data = b"\xff\xfb" * 20
    _mock_tool_returning(monkeypatch, audio_data)

    h = _post({"text": "complete audio"}, client="3.0.0.5")
    routes._handle_tts(h, None)

    assert h.status == 200
    payload = h.payload()
    assert payload is not None
    assert _decode_audio_data_url(payload) == audio_data
    assert payload.get("size") == len(audio_data)
