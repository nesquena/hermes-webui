"""OpenAI-compatible TTS endpoint and UI wiring coverage for #4982."""
import io
import json
import socket
import ssl
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


def _http_response_bytes(status_code: int, body=b"", *, reason="OK", headers=None):
    hdr = {"Content-Length": str(len(body)), "Content-Type": "audio/mpeg"}
    if headers:
        hdr.update(headers)
    lines = [f"HTTP/1.1 {status_code} {reason}\r\n"]
    for key, value in hdr.items():
        lines.append(f"{key}: {value}\r\n")
    lines.append("\r\n")
    return "".join(lines).encode("utf-8") + body


class _FakeSocketForHttps:
    def __init__(self, response_body: bytes):
        self.writes = []
        self.response = io.BytesIO(response_body)
        self.closed = False

    def sendall(self, data):
        self.writes.append(data)

    def setsockopt(self, *_args, **_kwargs):
        return None

    def makefile(self, *_args, **_kwargs):
        return self.response

    def shutdown(self, *_args, **_kwargs):
        return None

    def close(self):
        self.closed = True


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


def test_tts_resolve_pinned_address_accepts_public_ip(monkeypatch):
    def _fake_getaddrinfo(*_args, **_kwargs):
        return [(0, 0, 0, "", ("1.1.1.1", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    assert routes._tts_resolve_pinned_address("1.1.1.1") == "1.1.1.1"


def test_tts_resolve_pinned_address_rejects_blocked_target(monkeypatch):
    def _fake_getaddrinfo(*_args, **_kwargs):
        return [(0, 0, 0, "", ("10.0.0.5", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    with pytest.raises(ValueError, match="not allowed"):
        routes._tts_resolve_pinned_address("public.example.com")


def test_tts_resolve_pinned_address_rejects_mixed_addresses(monkeypatch):
    def _fake_getaddrinfo(*_args, **_kwargs):
        return [
            (0, 0, 0, "", ("203.0.113.10", 0)),
            (0, 0, 0, "", ("127.0.0.1", 0)),
        ]
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    with pytest.raises(ValueError, match="not allowed"):
        routes._tts_resolve_pinned_address("public.example.com")


def test_openai_tts_does_not_connect_to_rebound_private_address(monkeypatch):
    host = "rebind-openai.example.com"
    counts = {"getaddrinfo": 0}
    created = []

    def _fake_getaddrinfo(*_args, **_kwargs):
        counts["getaddrinfo"] += 1
        if counts["getaddrinfo"] == 1:
            return [(0, 0, 0, "", ("1.1.1.1", 443))]
        return [(0, 0, 0, "", ("169.254.169.254", 443))]

    def _fake_create_connection(address, *args, **_kwargs):
        dial_host, dial_port = address
        try:
            socket.inet_aton(dial_host)
            resolved = dial_host
        except OSError:
            resolved = socket.getaddrinfo(dial_host, dial_port)[0][4][0]
        created.append((resolved, dial_port))
        raise AssertionError(f"connect should not run with this test; got {(resolved, dial_port)}")

    def _fake_wrap_socket(_context, sock, *args, **kwargs):
        return sock

    import api.config as config

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", _fake_wrap_socket)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {"openai": {"base_url": f"https://{host}/v1"}}
    })

    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.8")
    routes._handle_tts(h, None)

    assert counts["getaddrinfo"] == 2
    assert created == []
    assert h.status == 502
    assert "OpenAI TTS generation failed" in (h.payload() or {}).get("error", "")


def test_openai_tts_pinned_connection_preserves_host_and_sni(monkeypatch):
    host = "static-openai.example.com"
    response_bytes = _http_response_bytes(200, b"audio-openai")
    fake_socket = _FakeSocketForHttps(response_bytes)
    observed = {}
    created = []

    def _fake_getaddrinfo(*_args, **_kwargs):
        return [(0, 0, 0, "", ("1.1.1.1", 443))]

    def _fake_create_connection(address, *_args, **_kwargs):
        created.append(address)
        return fake_socket

    def _fake_wrap_socket(_context, sock, *args, server_hostname=None, **_kwargs):
        observed["server_hostname"] = server_hostname
        return sock

    import api.config as config

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", _fake_wrap_socket)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {"openai": {"base_url": f"https://{host}/v1"}}
    })
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.9")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert h.sent_headers["Content-Type"] == "audio/mpeg"
    assert h.wfile.getvalue() == b"audio-openai"
    assert created == [("1.1.1.1", 443)]
    assert observed["server_hostname"] == host
    sent = b"".join(fake_socket.writes).decode("utf-8", "replace")
    assert f"Host: {host}" in sent


def test_openai_tts_rejects_redirect_with_pinned_opener(monkeypatch):
    host = "redirect-openai.example.com"
    response_bytes = _http_response_bytes(302, headers={"Location": "http://169.254.169.254/v1/audio/speech"}, reason="Found")
    fake_socket = _FakeSocketForHttps(response_bytes)
    created = []

    def _fake_getaddrinfo(*_args, **_kwargs):
        return [(0, 0, 0, "", ("1.1.1.1", 443))]

    def _fake_create_connection(address, *_args, **_kwargs):
        created.append(address)
        return fake_socket

    def _fake_wrap_socket(_context, sock, *args, **kwargs):
        return sock

    import api.config as config

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", _fake_wrap_socket)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {"openai": {"base_url": f"https://{host}/v1"}}
    })
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.10")
    routes._handle_tts(h, None)

    assert h.status in (500, 502)
    assert created == [("1.1.1.1", 443)]
    assert "OpenAI TTS generation failed" in (h.payload() or {}).get("error", "")


def test_openai_tts_ignores_https_proxy_and_dials_pinned_target(monkeypatch):
    host = "proxy-safe-openai.example.com"
    response_bytes = _http_response_bytes(200, b"audio-openai")
    fake_socket = _FakeSocketForHttps(response_bytes)
    created = []

    def _fake_getaddrinfo(target_host, *_args, **_kwargs):
        if target_host == host:
            return [(0, 0, 0, "", ("1.1.1.1", 443))]
        if target_host == "127.0.0.1":
            return [(0, 0, 0, "", ("127.0.0.1", 8888))]
        raise AssertionError(f"unexpected DNS lookup for {target_host}")

    def _fake_create_connection(address, *_args, **_kwargs):
        created.append(address)
        return fake_socket

    def _fake_wrap_socket(_context, sock, *args, **kwargs):
        return sock

    import api.config as config

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", _fake_wrap_socket)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:8888")
    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {"openai": {"base_url": f"https://{host}/v1"}}
    })
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.11")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert h.wfile.getvalue() == b"audio-openai"
    assert created == [("1.1.1.1", 443)]


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
