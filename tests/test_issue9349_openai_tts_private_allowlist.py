"""#9349: explicit, default-deny allowlist for private/LAN/Tailscale TTS hosts.

The OpenAI-compatible TTS proxy historically refuses every non-global address
(including Tailscale's 100.64.0.0/10). This suite pins the contract of the
opt-in allowlist: with no allowlist the private address is still refused, and
only an exact hostname match in ``tts.openai.allow_private_hosts`` opens the
SSRF guard for that host.
"""
import io
import json
import socket

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
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    _reset_limiter()
    yield
    _reset_limiter()


def _openai_config(**openai_cfg):
    return {"tts": {"openai": dict(openai_cfg)}}


def _install_openai_config(monkeypatch, **openai_cfg):
    import api.config as config

    monkeypatch.setattr(config, "get_config", lambda: _openai_config(**openai_cfg))


def _stub_tts_open(monkeypatch, captured=None):
    def _fake_urlopen(req, timeout=0):
        if captured is not None:
            captured["url"] = req.full_url
            captured["auth"] = req.headers.get("Authorization")
            captured["body"] = json.loads(req.data.decode("utf-8"))
        return _StreamOnceResponse([b"audio-private"])
    monkeypatch.setattr(routes, "_tts_open", lambda req, **kw: _fake_urlopen(req))


def test_private_base_url_rejected_without_allowlist(monkeypatch):
    _install_openai_config(monkeypatch, base_url="https://100.64.12.34/v1")
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.9")
    routes._handle_tts(h, None)

    assert h.status == 400
    assert "base_url" in (h.payload() or {}).get("error", "")


def test_allowlisted_private_https_base_url_is_accepted(monkeypatch):
    _install_openai_config(
        monkeypatch,
        base_url="https://100.64.12.34/v1",
        allow_private_hosts=["100.64.12.34"],
    )
    captured = {}
    _stub_tts_open(monkeypatch, captured)

    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.10")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert h.sent_headers["Content-Type"] == "audio/mpeg"
    assert h.wfile.getvalue() == b"audio-private"
    assert captured["url"] == "https://100.64.12.34/v1/audio/speech"
    assert captured["auth"] == "Bearer sk-openai"


def test_allowlisted_private_http_base_url_is_accepted(monkeypatch):
    _install_openai_config(
        monkeypatch,
        base_url="http://192.168.1.50:8880/v1",
        allow_private_hosts=["192.168.1.50"],
    )
    captured = {}
    _stub_tts_open(monkeypatch, captured)

    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.11")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert captured["url"] == "http://192.168.1.50:8880/v1/audio/speech"


def test_allowlist_does_not_cover_other_private_hosts(monkeypatch):
    # The allowlist is an exact-host approval, not a "allow the private network"
    # switch: a different private host stays refused.
    _install_openai_config(
        monkeypatch,
        base_url="https://192.168.1.60/v1",
        allow_private_hosts=["100.64.12.34"],
    )
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.12")
    routes._handle_tts(h, None)

    assert h.status == 400
    assert "base_url" in (h.payload() or {}).get("error", "")


def test_allowlist_is_exact_not_a_range(monkeypatch):
    _install_openai_config(
        monkeypatch,
        base_url="https://100.64.0.2/v1",
        allow_private_hosts=["100.64.0.1"],
    )
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.13")
    routes._handle_tts(h, None)

    assert h.status == 400


def test_allowlist_must_be_a_list_of_strings(monkeypatch):
    # A malformed (string) value must fail closed, not be treated as approval.
    _install_openai_config(
        monkeypatch,
        base_url="https://100.64.12.34/v1",
        allow_private_hosts="100.64.12.34",
    )
    h = _post({"text": "Hello", "engine": "openai"}, client="10.82.0.14")
    routes._handle_tts(h, None)

    assert h.status == 400


def test_pinned_resolution_allows_allowlisted_private_host(monkeypatch):
    def _fake_getaddrinfo(*_args, **_kwargs):
        return [(0, 0, 0, "", ("192.168.1.50", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    assert routes._tts_resolve_pinned_addresses(
        "kokoro.lan", None, allow_private_hosts=["kokoro.lan"]
    ) == ["192.168.1.50"]


def test_pinned_resolution_still_rejects_unlisted_private_host(monkeypatch):
    def _fake_getaddrinfo(*_args, **_kwargs):
        return [(0, 0, 0, "", ("192.168.1.50", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    with pytest.raises(ValueError, match="not allowed"):
        routes._tts_resolve_pinned_addresses(
            "kokoro.lan", None, allow_private_hosts=["other.lan"]
        )


def test_normalized_base_url_allows_allowlisted_private_host():
    assert (
        routes._normalized_openai_tts_base_url(
            "https://100.64.12.34/v1", allow_private_hosts=["100.64.12.34"]
        )
        == "https://100.64.12.34/v1"
    )
    # Default (no allowlist) is unchanged.
    with pytest.raises(ValueError, match="invalid OpenAI base_url"):
        routes._normalized_openai_tts_base_url("https://100.64.12.34/v1")
