"""Persisted server TTS engine routing coverage for #7391."""

import io
import json
import sys
from types import SimpleNamespace

import pytest

import api.routes as routes


_MISSING = object()


class _FakeHandler:
    def __init__(self, body: bytes, client="10.73.91.1"):
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
        try:
            return json.loads(self.wfile.getvalue().decode("utf-8"))
        except Exception:
            return None


class _AudioResponse:
    headers = {"Content-Type": "audio/mpeg"}

    def __init__(self, audio):
        self.audio = audio

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _size=-1):
        audio, self.audio = self.audio, b""
        return audio


@pytest.fixture(autouse=True)
def _isolated_tts(monkeypatch):
    import api.auth as auth
    import api.config as config

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-elevenlabs")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {
            "elevenlabs": {
                "voice_id": "persistedElevenVoice",
                "model": "eleven_persisted_model",
            },
            "openai": {
                "base_url": "https://tts.example.com/v1/",
                "model": "persisted-openai-model",
                "voice": "persisted-openai-voice",
            },
        }
    })
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter
    yield
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


def _request(monkeypatch, persisted_engine, request_engine=_MISSING):
    captured = {}
    settings = {} if persisted_engine is _MISSING else {"tts_engine": persisted_engine}
    monkeypatch.setattr(routes, "load_settings", lambda: settings)

    class _FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            captured["edge"] = {"text": text, "voice": voice, "kwargs": kwargs}

        def stream_sync(self):
            yield {"type": "audio", "data": b"edge-audio"}

    def _fake_tts_open(req, **_kwargs):
        body = json.loads(req.data.decode("utf-8"))
        if "api.elevenlabs.io" in req.full_url:
            captured["elevenlabs"] = {"url": req.full_url, "body": body}
            return _AudioResponse(b"elevenlabs-audio")
        captured["openai"] = {"url": req.full_url, "body": body}
        return _AudioResponse(b"openai-audio")

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=_FakeCommunicate))
    monkeypatch.setattr(routes, "_tts_open", _fake_tts_open)

    body = {"text": "Hello", "voice": "en-US-AriaNeural"}
    if request_engine is not _MISSING:
        body["engine"] = request_engine
    handler = _FakeHandler(json.dumps(body).encode("utf-8"))
    routes._handle_tts(handler, None)
    return handler, captured


@pytest.mark.parametrize(
    ("persisted_engine", "expected_audio"),
    [
        ("edge", b"edge-audio"),
        ("elevenlabs", b"elevenlabs-audio"),
        ("openai", b"openai-audio"),
    ],
)
@pytest.mark.parametrize("request_engine", [_MISSING, ""])
def test_missing_or_empty_engine_uses_persisted_server_engine(
    monkeypatch, persisted_engine, expected_audio, request_engine
):
    handler, captured = _request(monkeypatch, persisted_engine, request_engine)

    assert handler.status == 200
    assert handler.wfile.getvalue() == expected_audio
    if persisted_engine == "openai":
        assert captured["openai"] == {
            "url": "https://tts.example.com/v1/audio/speech",
            "body": {
                "model": "persisted-openai-model",
                "input": "Hello",
                "voice": "persisted-openai-voice",
            },
        }


@pytest.mark.parametrize("request_engine", [_MISSING, ""])
@pytest.mark.parametrize(
    "persisted_engine", [_MISSING, "", 7, [], {}, "browser", "voicevox_local"]
)
def test_unusable_persisted_engine_defaults_to_edge(monkeypatch, request_engine, persisted_engine):
    handler, captured = _request(monkeypatch, persisted_engine, request_engine)

    assert handler.status == 200
    assert handler.wfile.getvalue() == b"edge-audio"
    assert captured["edge"]["voice"] == "en-US-AriaNeural"


@pytest.mark.parametrize(
    ("persisted_engine", "request_engine", "expected_audio"),
    [
        ("edge", "elevenlabs", b"elevenlabs-audio"),
        ("elevenlabs", "openai", b"openai-audio"),
        ("openai", "edge", b"edge-audio"),
    ],
)
def test_explicit_engine_overrides_each_persisted_server_engine(
    monkeypatch, persisted_engine, request_engine, expected_audio
):
    handler, _captured = _request(monkeypatch, persisted_engine, request_engine)

    assert handler.status == 200
    assert handler.wfile.getvalue() == expected_audio


def test_explicit_engine_still_normalizes_whitespace_and_case(monkeypatch):
    handler, _captured = _request(monkeypatch, "edge", "  OpEnAi  ")

    assert handler.status == 200
    assert handler.wfile.getvalue() == b"openai-audio"


def test_persisted_server_engine_normalizes_whitespace_and_case(monkeypatch):
    handler, _captured = _request(monkeypatch, "  ElEvEnLaBs  ")

    assert handler.status == 200
    assert handler.wfile.getvalue() == b"elevenlabs-audio"


def test_explicit_unknown_engine_keeps_edge_compatibility_behavior(monkeypatch):
    handler, _captured = _request(monkeypatch, "openai", "unknown-engine")

    assert handler.status == 200
    assert handler.wfile.getvalue() == b"edge-audio"


@pytest.mark.parametrize("request_engine", [7, {"engine": "openai"}])
def test_explicit_truthy_non_string_engine_keeps_invalid_body_response(
    monkeypatch, request_engine
):
    handler, _captured = _request(monkeypatch, "openai", request_engine)

    assert handler.status == 400
    assert "invalid request body" in (handler.payload() or {}).get("error", "")


@pytest.mark.parametrize("request_engine", [None, 0, False])
def test_explicit_falsy_non_string_engine_keeps_edge_compatibility_behavior(
    monkeypatch, request_engine
):
    handler, _captured = _request(monkeypatch, "openai", request_engine)

    assert handler.status == 200
    assert handler.wfile.getvalue() == b"edge-audio"
