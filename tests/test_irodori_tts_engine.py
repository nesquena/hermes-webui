"""Coverage for the Irodori TTS engine on /api/tts."""

import io
import json

import pytest

import api.routes as routes


class _FakeHandler:
    def __init__(self, body: bytes, command: str = "POST", headers=None, client="9.9.9.9"):
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


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    import api.auth as _auth

    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    monkeypatch.delenv("IRODORI_API_KEY", raising=False)
    monkeypatch.delenv("IRODORI_TTS_ALLOW_REMOTE_API_KEY", raising=False)
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter
    yield
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


def test_irodori_rejects_invalid_speed(monkeypatch):
    monkeypatch.setattr(
        routes,
        "_resolve_irodori_tts_settings",
        lambda: {
            "base_url": "http://127.0.0.1:8088",
            "model": "irodori-tts",
            "voice": "none",
            "speed": 1.0,
            "api_key": "",
        },
    )
    h = _post({"text": "hello", "engine": "irodori", "speed": 9.9}, client="9.9.9.1")
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "speed" in (h.payload() or {}).get("error", "")


def test_irodori_rejects_invalid_voice(monkeypatch):
    monkeypatch.setattr(
        routes,
        "_resolve_irodori_tts_settings",
        lambda: {
            "base_url": "http://127.0.0.1:8088",
            "model": "irodori-tts",
            "voice": "none",
            "speed": 1.0,
            "api_key": "",
        },
    )
    h = _post({"text": "hello", "engine": "irodori", "voice": "../../etc/passwd"}, client="9.9.9.2")
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "voice" in (h.payload() or {}).get("error", "")


def test_irodori_happy_path_streams_mp3(monkeypatch):
    monkeypatch.setattr(
        routes,
        "_resolve_irodori_tts_settings",
        lambda: {
            "base_url": "http://127.0.0.1:8088",
            "model": "irodori-tts",
            "voice": "hakua",
            "speed": 1.0,
            "api_key": "local-key",
        },
    )
    captured = {}

    class _Resp:
        def __init__(self):
            self._chunks = [b"ID3fakeaudio", b""]
            self._i = 0

        def read(self, n=-1):
            c = self._chunks[self._i] if self._i < len(self._chunks) else b""
            self._i += 1
            return c

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=120):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    import urllib.request as _ur

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    h = _post({"text": "こんにちは", "engine": "irodori", "voice": "hakua", "speed": 1.1}, client="9.9.9.3")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert h.sent_headers.get("Content-Type") == "audio/mpeg"
    assert h.wfile.getvalue() == b"ID3fakeaudio"
    assert captured["url"] == "http://127.0.0.1:8088/v1/audio/speech"
    assert captured["auth"] == "Bearer local-key"
    assert captured["body"] == {
        "model": "irodori-tts",
        "input": "こんにちは",
        "voice": "hakua",
        "response_format": "mp3",
        "speed": 1.1,
    }


def test_irodori_remote_api_key_not_sent_without_opt_in(monkeypatch):
    monkeypatch.setattr(
        routes,
        "_resolve_irodori_tts_settings",
        lambda: {
            "base_url": "https://example.com:8088",
            "model": "irodori-tts",
            "voice": "none",
            "speed": 1.0,
            "api_key": "secret",
        },
    )

    class _Resp:
        def __init__(self):
            self._chunks = [b"audio", b""]
            self._i = 0

        def read(self, n=-1):
            c = self._chunks[self._i] if self._i < len(self._chunks) else b""
            self._i += 1
            return c

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    captured = {}

    def _fake_urlopen(req, timeout=120):
        captured["auth"] = req.get_header("Authorization")
        return _Resp()

    import urllib.request as _ur

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    h = _post({"text": "hello", "engine": "irodori"}, client="9.9.9.4")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert captured["auth"] is None


def test_normalize_irodori_base_url_strips_openai_suffixes():
    assert routes._normalize_irodori_base_url("http://127.0.0.1:8088/v1/audio/speech") == "http://127.0.0.1:8088"
    assert routes._normalize_irodori_base_url("http://127.0.0.1:8088/v1") == "http://127.0.0.1:8088"
