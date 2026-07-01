"""OpenAI-compatible TTS endpoint and UI wiring coverage for #4982."""
import io
import json
from pathlib import Path

import pytest

import api.routes as routes


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


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


class _StreamOnceResponse:
    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _size=-1):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


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
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _reset_limiter()
    yield
    _reset_limiter()


def test_openai_tts_no_key_returns_503(monkeypatch):
    import api.onboarding as onboarding

    monkeypatch.setattr(onboarding, "_load_env_file", lambda *_args, **_kwargs: {})
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.1")
    routes._handle_tts(h, None)
    assert h.status == 503
    assert "OpenAI API key not configured" in (h.payload() or {}).get("error", "")


def test_openai_tts_success_returns_audio(monkeypatch):
    captured = {}

    def _fake_urlopen(req, timeout=0):
        captured["auth"] = req.headers.get("Authorization")
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _StreamOnceResponse([b"audio-openai"])

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(routes, "_tts_open", lambda req, **kw: _fake_urlopen(req))
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.2")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert h.sent_headers["Content-Type"] == "audio/mpeg"
    assert h.wfile.getvalue() == b"audio-openai"
    assert captured["auth"] == "Bearer sk-openai"
    assert captured["url"] == "https://api.openai.com/v1/audio/speech"
    assert captured["body"] == {"model": "gpt-4o-mini-tts", "input": "Hello", "voice": "alloy"}


def test_openai_tts_prefers_voice_tools_key_over_openai_key(monkeypatch):
    captured = {}

    def _fake_urlopen(req, timeout=0):
        captured["auth"] = req.headers.get("Authorization")
        return _StreamOnceResponse([b"preferred"])

    monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "sk-voice-tools")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(routes, "_tts_open", lambda req, **kw: _fake_urlopen(req))
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.3")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert captured["auth"] == "Bearer sk-voice-tools"


def test_openai_tts_config_overrides(monkeypatch):
    captured = {}
    import api.config as config

    def _fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _StreamOnceResponse([b"custom"])

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {"openai": {"base_url": "https://custom.example.com/v1/", "model": "tts-custom", "voice": "nova"}}
    })
    monkeypatch.setattr(routes, "_tts_open", lambda req, **kw: _fake_urlopen(req))
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.4")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert captured["url"] == "https://custom.example.com/v1/audio/speech"
    assert captured["body"] == {"model": "tts-custom", "input": "Hello", "voice": "nova"}


@pytest.mark.parametrize("base_url", [
    "http://169.254.169.254/v1",
    "https://user:pass@api.example.com/v1",
    "http://user:pass@localhost:8080/v1",
    # SSRF: https to private / link-local / loopback / reserved literal IPs must
    # be rejected even though the scheme is https (regression for #5079 gate).
    "https://169.254.169.254/v1",
    "https://10.0.0.5/v1",
    "https://192.168.1.10/v1",
    "https://127.0.0.1/v1",
    "https://[::1]/v1",
])
def test_openai_tts_rejects_invalid_base_url_config(monkeypatch, base_url):
    import api.config as config

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {"openai": {"base_url": base_url}}
    })
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.5")
    routes._handle_tts(h, None)

    assert h.status == 400
    assert "base_url" in (h.payload() or {}).get("error", "")


def test_openai_tts_rejects_non_audio_upstream_response(monkeypatch):
    def _fake_urlopen(_req, timeout=0):
        return _StreamOnceResponse(
            [b'{"error":"nope"}'],
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(routes, "_tts_open", lambda req, **kw: _fake_urlopen(req))
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.6")
    routes._handle_tts(h, None)

    assert h.status == 502
    assert "OpenAI TTS generation failed" in (h.payload() or {}).get("error", "")


def test_openai_tts_does_not_follow_upstream_redirect(monkeypatch):
    # SSRF / credential-leak regression (#5079 gate): an upstream redirect must
    # NOT be followed — following it would carry the Authorization bearer to the
    # redirect target and could bounce the request to a private/link-local host
    # after base-url validation already passed. The no-redirect opener raises,
    # which surfaces as a 502 (generation failed), never a second request.
    import urllib.error

    def _redirecting_open(req, **kw):
        # Simulate urllib raising on a redirect the way HTTPRedirectHandler would
        # when redirect_request() raises (the no-redirect handler's behavior).
        raise urllib.error.HTTPError(
            req.full_url, 302, "Found",
            {"Location": "http://169.254.169.254/v1/audio/speech"}, None,
        )

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(routes, "_tts_open", _redirecting_open)
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.9")
    routes._handle_tts(h, None)

    assert h.status in (500, 502)
    assert "OpenAI TTS generation failed" in (h.payload() or {}).get("error", "")


def test_openai_tts_rejects_oversized_upstream_audio(monkeypatch):
    def _fake_urlopen(_req, timeout=0):
        return _StreamOnceResponse([b"1234", b"5"], headers={"Content-Type": "audio/mpeg"})

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(routes, "_TTS_PROXY_MAX_BYTES", 4)
    monkeypatch.setattr(routes, "_tts_open", lambda req, **kw: _fake_urlopen(req))
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.7")
    routes._handle_tts(h, None)

    assert h.status == 502
    assert "OpenAI TTS generation failed" in (h.payload() or {}).get("error", "")


def test_openai_option_in_html():
    src = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert '<option value="openai">OpenAI TTS (server)</option>' in src


def test_openai_voice_placeholder_in_panels():
    src = (STATIC_DIR / "panels.js").read_text(encoding="utf-8")
    assert "engine==='openai'" in src
    assert 'OpenAI voice (server-configured)' in src


def test_play_openai_tts_exists_in_ui_js():
    src = (STATIC_DIR / "ui.js").read_text(encoding="utf-8")
    assert 'function _playOpenaiTts(text, btn)' in src
    assert "body:JSON.stringify({text:text, engine:'openai'})" in src


def test_boot_js_handles_openai_engine():
    src = (STATIC_DIR / "boot.js").read_text(encoding="utf-8")
    assert 'if(engine==="openai")' in src
    assert "body: JSON.stringify({text: clean, engine: 'openai'})" in src
