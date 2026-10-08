"""Coverage for the MiniMax TTS engine on the /api/tts endpoint.

Exercises the engine routing + guard rails of _handle_tts's minimax branch
in-process via a fake handler. The happy path mocks routes._tts_open so no
real MiniMax network call is made; the rejection paths (missing key, bad
voice_id, upstream error, non-hex audio) bail before / after the call
deterministically.
"""
from __future__ import annotations

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


def test_minimax_missing_key_returns_503(monkeypatch, tmp_path):
    """No MINIMAX_API_KEY (env or .env) → 503, no network call."""
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    import api.profiles as profiles
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    h = _post({"text": "hello", "engine": "minimax"}, client="9.9.9.1")
    routes._handle_tts(h, None)
    assert h.status == 503
    assert "MINIMAX_API_KEY" in (h.payload() or {}).get("error", "")


def test_minimax_rejects_unsafe_voice_id_in_request(monkeypatch):
    """A voice_id containing JSON-smuggling characters → 400 before any call."""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-minimax-test")

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("MiniMax upstream called")

    monkeypatch.setattr(routes, "_tts_open", _fail_if_called)
    h = _post(
        {"text": "hello", "engine": "minimax", "voice_id": 'evil","injected":1'},
        client="9.9.9.2",
    )
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "voice_id" in (h.payload() or {}).get("error", "")


def test_minimax_overlong_text_rejected_before_engine(monkeypatch):
    """The shared 5000-char cap applies to the minimax engine too (no call)."""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-minimax-test")

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("MiniMax upstream called")

    monkeypatch.setattr(routes, "_tts_open", _fail_if_called)
    h = _post({"text": "x" * 5001, "engine": "minimax"}, client="9.9.9.3")
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "too long" in (h.payload() or {}).get("error", "")


def test_minimax_happy_path_decodes_hex_and_streams_mp3(monkeypatch):
    """With a key + valid config, the branch hits MiniMax, hex-decodes
    data.audio, and returns audio/mpeg with Content-Length matching the
    decoded byte count."""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-minimax-test")
    import api.config as _cfg
    monkeypatch.setattr(_cfg, "get_config", lambda: {
        "tts": {"minimax": {"model": "speech-2.8-hd", "voice_id": "English_expressive_narrator"}}
    })

    # MP3 frames start with either an ID3 tag or 0xFFEx sync. Use ID3 + payload
    # so the test would fail loudly if hex decoding or frame assembly broke.
    real_audio = b"ID3\x03\x00" + b"\x00" * 16 + b"\xff\xfb\x90\x00" + b"\x00" * 32
    audio_hex = real_audio.hex()

    upstream_payload = json.dumps({
        "data": {"audio": audio_hex, "status": 2},
        "extra_info": {"audio_length": 11124, "audio_size": len(real_audio)},
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }).encode("utf-8")

    captured = {}

    class _Resp:
        def __init__(self):
            self._chunks = [upstream_payload, b""]
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
        captured["authorization"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(routes, "_tts_open", _fake_tts_open)

    h = _post({"text": "hello world", "engine": "minimax"}, client="9.9.9.4")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert h.sent_headers.get("Content-Type") == "audio/mpeg"
    assert h.sent_headers.get("Content-Length") == str(len(real_audio))
    assert h.wfile.getvalue() == real_audio
    # Confirm we hit the right URL with the right auth and voice.
    assert captured["url"] == "https://api.minimax.io/v1/t2a_v2"
    assert captured["authorization"] == "Bearer sk-minimax-test"
    assert captured["body"]["voice_setting"]["voice_id"] == "English_expressive_narrator"
    assert captured["body"]["model"] == "speech-2.8-hd"
    assert captured["body"]["stream"] is False
    assert captured["body"]["output_format"] == "hex"


def test_minimax_upstream_error_status_returns_502(monkeypatch):
    """base_resp.status_code != 0 → 502 with the upstream message surfaced."""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-minimax-test")

    upstream_payload = json.dumps({
        "data": {},
        "base_resp": {"status_code": 1002, "status_msg": "invalid voice_id"},
    }).encode("utf-8")

    class _Resp:
        def __init__(self):
            self._chunks = [upstream_payload, b""]
            self._i = 0
        def read(self, n=-1):
            c = self._chunks[self._i] if self._i < len(self._chunks) else b""
            self._i += 1
            return c
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(routes, "_tts_open", lambda *a, **kw: _Resp())
    h = _post({"text": "hello", "engine": "minimax"}, client="9.9.9.5")
    routes._handle_tts(h, None)
    assert h.status == 502
    err = (h.payload() or {}).get("error", "")
    assert "invalid voice_id" in err


def test_minimax_non_hex_audio_returns_502(monkeypatch):
    """data.audio that's not valid hex → 502."""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-minimax-test")

    upstream_payload = json.dumps({
        "data": {"audio": "not-actually-hex-zzzz", "status": 2},
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }).encode("utf-8")

    class _Resp:
        def __init__(self):
            self._chunks = [upstream_payload, b""]
            self._i = 0
        def read(self, n=-1):
            c = self._chunks[self._i] if self._i < len(self._chunks) else b""
            self._i += 1
            return c
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(routes, "_tts_open", lambda *a, **kw: _Resp())
    h = _post({"text": "hello", "engine": "minimax"}, client="9.9.9.6")
    routes._handle_tts(h, None)
    assert h.status == 502
    err = (h.payload() or {}).get("error", "")
    assert "invalid audio" in err


def test_minimax_default_voice_is_known_good(monkeypatch):
    """Regression guard: the hardcoded default voice (used when no
    tts.minimax.voice_id is set) must be one that actually exists on
    MiniMax's platform. English_Exciting_Actor appears in MiniMax's
    docs as a sample but returns 'voice id not exist' (status_code 2054)
    from the API. The current default is English_expressive_narrator,
    which was verified to return audio on 2026-07-07.
    """
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-minimax-test")
    # Empty config — exercises the hardcoded default.
    import api.config as _cfg
    monkeypatch.setattr(_cfg, "get_config", lambda: {"tts": {"minimax": {}}})

    captured = {}
    audio_hex = b"ID3ok".hex()

    class _Resp:
        def __init__(self):
            self._chunks = [
                json.dumps({
                    "data": {"audio": audio_hex, "status": 2},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }).encode("utf-8"),
                b"",
            ]
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
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(routes, "_tts_open", _fake_tts_open)
    h = _post({"text": "hello", "engine": "minimax"}, client="9.9.9.8")
    routes._handle_tts(h, None)
    assert h.status == 200
    # If you ever change the default, first verify the new voice_id
    # actually works against the real MiniMax API (curl test) and
    # then update this assertion + the dropdown in panels.js.
    assert captured["body"]["voice_setting"]["voice_id"] == "English_expressive_narrator"


def test_minimax_per_request_voice_id_overrides_config(monkeypatch):
    """When synthesize() passes voice_id in opts, it shows up in the request body."""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-minimax-test")
    import api.config as _cfg
    monkeypatch.setattr(_cfg, "get_config", lambda: {
        "tts": {"minimax": {"voice_id": "English_Graceful_Lady"}}
    })
    captured = {}
    audio_hex = b"ID3hi".hex()

    class _Resp:
        def __init__(self):
            self._chunks = [
                json.dumps({
                    "data": {"audio": audio_hex, "status": 2},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }).encode("utf-8"),
                b"",
            ]
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
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(routes, "_tts_open", _fake_tts_open)

    h = _post(
        {"text": "hello", "engine": "minimax", "voice_id": "English_Cute_Girl"},
        client="9.9.9.7",
    )
    routes._handle_tts(h, None)

    assert h.status == 200
    assert captured["body"]["voice_setting"]["voice_id"] == "English_Cute_Girl"