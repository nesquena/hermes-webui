"""Coverage for the ElevenLabs TTS engine on the /api/ttt endpoint (#3510).

Exercises the engine routing + guard rails of _handle_tts's elevenlabs branch
in-process via a fake handler. The happy path mocks routes._tts_open so no real
ElevenLabs network call is made; the rejection paths (missing key, bad config
voice_id) bail before any network call.
"""
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
    # Auth + limiter sit before the engine branch; pin them off/clean so these
    # assertions are deterministic regardless of suite order.
    import api.auth as _auth
    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter
    yield
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


def test_elevenlabs_missing_key_returns_503(monkeypatch, tmp_path):
    """No ELEVENLABS_API_KEY (env or .env) → 503, no network call."""
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    # Point the .env fallback at an empty home so no key is found.
    import api.profiles as profiles
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    h = _post({"text": "hello", "engine": "elevenlabs"}, client="9.9.9.1")
    routes._handle_tts(h, None)
    assert h.status == 503
    assert "ELEVENLABS_API_KEY" in (h.payload() or {}).get("error", "")


def test_elevenlabs_rejects_traversal_voice_id_in_config(monkeypatch, tmp_path):
    """A config voice_id that isn't a safe path segment → 400 before any call."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test")
    monkeypatch.setattr(routes, "get_config", lambda: {"tts": {"elevenlabs": {"voice_id": "../../etc/passwd"}}}, raising=False)
    # If get_config isn't a routes-level name, patch api.config.
    import api.config as _cfg
    monkeypatch.setattr(_cfg, "get_config", lambda: {"tts": {"elevenlabs": {"voice_id": "../../etc/passwd"}}})
    h = _post({"text": "hello", "engine": "elevenlabs"}, client="9.9.9.2")
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "voice_id" in (h.payload() or {}).get("error", "")


def test_elevenlabs_happy_path_streams_mp3(monkeypatch):
    """With a key + valid config, the branch calls ElevenLabs and returns audio/mpeg."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test")
    import api.config as _cfg
    monkeypatch.setattr(_cfg, "get_config", lambda: {"tts": {"elevenlabs": {"voice_id": "pNInz6obpgDQGcFmaJgB", "model": "eleven_multilingual_v2"}}})

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

    def _fake_tts_open(req, timeout=30, opener_factory=None, **_kw):
        captured["url"] = req.full_url
        captured["xi_api_key"] = req.get_header("Xi-api-key")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    # The ElevenLabs branch now routes through `_tts_open`.
    monkeypatch.setattr(routes, "_tts_open", _fake_tts_open)

    h = _post({"text": "hello world", "engine": "elevenlabs"}, client="9.9.9.3")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert h.sent_headers.get("Content-Type") == "audio/mpeg"
    assert h.wfile.getvalue() == b"ID3fakeaudio"
    # Confirmed it hit the configured voice + sent the key header + capped text.
    assert "pNInz6obpgDQGcFmaJgB" in captured["url"]
    assert captured["xi_api_key"] == "sk-test"
    assert captured["body"]["text"] == "hello world"


def test_elevenlabs_overlong_text_rejected_before_engine(monkeypatch):
    """The shared 5000-char cap applies to the elevenlabs engine too (no call)."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test")

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("ElevenLabs upstream called")

    monkeypatch.setattr(routes, "_tts_open", _fail_if_called)
    h = _post({"text": "x" * 5001, "engine": "elevenlabs"}, client="9.9.9.4")
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "too long" in (h.payload() or {}).get("error", "")


def test_elevenlabs_rejects_oversized_upstream_audio(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test")
    import api.config as _cfg
    monkeypatch.setattr(_cfg, "get_config", lambda: {
        "tts": {"elevenlabs": {"voice_id": "pNInz6obpgDQGcFmaJgB", "model": "eleven_multilingual_v2"}}
    })

    class _Resp:
        def __init__(self):
            self.headers = {"Content-Type": "audio/mpeg"}
            self._chunks = [b"1234", b"5", b""]
            self._i = 0
        def read(self, n=-1):
            c = self._chunks[self._i] if self._i < len(self._chunks) else b""
            self._i += 1
            return c
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _fake_tts_open(req, timeout=30, opener_factory=None, **_kw):
        return _Resp()

    monkeypatch.setattr(routes, "_TTS_PROXY_MAX_BYTES", 4)
    monkeypatch.setattr(routes, "_tts_open", _fake_tts_open)

    h = _post({"text": "hello world", "engine": "elevenlabs"}, client="9.9.9.5")
    routes._handle_tts(h, None)

    assert h.status == 502
    assert "ElevenLabs TTS generation failed" in (h.payload() or {}).get("error", "")


def test_elevenlabs_tts_does_not_follow_upstream_redirect(monkeypatch):
    # Regression proof: a fake redirect response should fail at _tts_open, not be followed.
    from urllib.request import HTTPRedirectHandler

    class _Resp:
        def __init__(self):
            self._chunks = [b"ID3fakeaudio", b""]
            self._i = 0
        def info(self):
            return {}
        def read(self, n=-1):
            c = self._chunks[self._i] if self._i < len(self._chunks) else b""
            self._i += 1
            return c
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _fake_tts_open(req, timeout=30, opener_factory=None, **_kw):
        assert opener_factory is not None
        opener = opener_factory()
        redirect_handler = next(
            (handler for handler in opener.handlers if isinstance(handler, HTTPRedirectHandler)),
            None,
        )
        assert redirect_handler is not None
        with pytest.raises(ValueError):
            redirect_handler.redirect_request(
                req,
                None,
                302,
                "Found",
                {"Location": "http://169.254.169.254/latest/meta-data"},
                "http://169.254.169.254/latest/meta-data",
            )
        raise ValueError("redirect blocked 302")

    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test")
    import api.config as _cfg
    monkeypatch.setattr(_cfg, "get_config", lambda: {"tts": {"elevenlabs": {"voice_id": "pNInz6obpgDQGcFmaJgB", "model": "eleven_multilingual_v2"}}})
    monkeypatch.setattr(routes, "_tts_open", _fake_tts_open)
    h = _post({"text": "hello world", "engine": "elevenlabs"}, client="9.9.9.6")
    routes._handle_tts(h, None)

    assert h.status == 502
    assert "ElevenLabs TTS generation failed" in (h.payload() or {}).get("error", "")


def test_elevenlabs_tts_uses_no_proxy_opener(monkeypatch):
    captured = {}

    class _Resp:
        def __init__(self):
            self.headers = {"Content-Type": "audio/mpeg"}
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

    def _fake_tts_open(req, timeout=30, opener_factory=None, **_kw):
        captured["opener_factory"] = opener_factory
        opener = opener_factory()
        captured["opener_handlers"] = opener.handlers
        return _Resp()

    from urllib.request import HTTPRedirectHandler

    original_proxy_handler = routes.ProxyHandler

    def _spy_proxy_handler(proxies=None, **_kw):
        captured["proxy_init_kwargs"] = {"proxies": proxies}
        return original_proxy_handler(proxies)

    monkeypatch.setattr(routes, "ProxyHandler", _spy_proxy_handler)

    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test")
    import api.config as _cfg
    monkeypatch.setattr(_cfg, "get_config", lambda: {"tts": {"elevenlabs": {"voice_id": "pNInz6obpgDQGcFmaJgB", "model": "eleven_multilingual_v2"}}})
    monkeypatch.setattr(routes, "_tts_open", _fake_tts_open)

    h = _post({"text": "hello world", "engine": "elevenlabs"}, client="9.9.9.7")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert h.wfile.getvalue() == b"ID3fakeaudio"
    assert captured["proxy_init_kwargs"] == {"proxies": {}}
    assert any(isinstance(handler, HTTPRedirectHandler) for handler in captured["opener_handlers"])
