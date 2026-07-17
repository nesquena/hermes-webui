"""Self-hosted STT/TTS: LAN SSRF opt-in, keyless OpenAI TTS, /api/voice/config.

Covers the WebUI-side changes that let operators point STT/TTS at their own
OpenAI-compatible servers on a private LAN, edit the endpoints from the UI, and
forward the browser locale as a transcription language hint.
"""
import io
import json
import socket
from pathlib import Path

import pytest

import api.routes as routes
import api.voice_config as vc

_STATIC = Path(__file__).resolve().parent.parent / "static"


class _FakeHandler:
    def __init__(self, body: bytes = b"", command: str = "POST", headers=None, client="1.2.3.4"):
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

    def __exit__(self, *_a):
        return False

    def read(self, _size=-1):
        return self._chunks.pop(0) if self._chunks else b""


def _post(body_dict, **kw):
    return _FakeHandler(json.dumps(body_dict).encode(), **kw)


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    import api.auth as _auth
    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    for var in ("VOICE_TOOLS_OPENAI_KEY", "OPENAI_API_KEY",
                "HERMES_WEBUI_TTS_ALLOW_LAN", "HERMES_WEBUI_TTS_ALLOW_HOSTS",
                "HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE"):
        monkeypatch.delenv(var, raising=False)
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter
    yield


# ── SSRF LAN opt-in ─────────────────────────────────────────────────────────

def test_lan_base_url_rejected_without_optin():
    with pytest.raises(ValueError):
        routes._normalized_openai_tts_base_url("http://192.168.1.50:8001/v1")


def test_lan_base_url_allowed_with_optin(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "192.168.1.0/24")
    out = routes._normalized_openai_tts_base_url("http://192.168.1.50:8001/v1")
    assert out == "http://192.168.1.50:8001/v1"


def test_gate_without_hosts_is_fail_closed(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")  # no _ALLOW_HOSTS
    with pytest.raises(ValueError):
        routes._normalized_openai_tts_base_url("http://192.168.1.50:8001/v1")


def test_hosts_without_gate_is_fail_closed(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "192.168.1.50")  # no gate
    with pytest.raises(ValueError):
        routes._normalized_openai_tts_base_url("http://192.168.1.50:8001/v1")


def test_non_allowlisted_lan_still_blocked(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "192.168.1.50")
    with pytest.raises(ValueError):
        routes._normalized_openai_tts_base_url("http://10.0.0.9:8001/v1")


def test_pinned_https_lan_allowed_when_allowlisted(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "192.168.1.0/24")
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(0, 0, 0, "", ("192.168.1.50", 0))])
    assert routes._tts_resolve_pinned_address("192.168.1.50") == "192.168.1.50"


# ── keyless self-hosted OpenAI TTS + content-type forwarding ────────────────

def test_openai_tts_keyless_self_hosted_uses_placeholder(monkeypatch):
    import api.config as config
    captured = {}

    def _fake_open(req, **kw):
        captured["auth"] = req.headers.get("Authorization")
        captured["url"] = req.full_url
        return _StreamOnceResponse([b"WAVDATA"], headers={"Content-Type": "audio/wav"})

    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "192.168.1.0/24")
    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {"openai": {"base_url": "http://192.168.1.50:8001/v1",
                           "model": "qwen3-tts", "voice": "Serena"}}
    })
    monkeypatch.setattr(routes, "_tts_open", _fake_open)
    h = _post({"text": "Hallo", "engine": "openai"}, client="10.82.0.21")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert captured["auth"] == "Bearer sk-no-key-required"
    assert captured["url"] == "http://192.168.1.50:8001/v1/audio/speech"
    # upstream WAV type is forwarded, not mislabelled as audio/mpeg
    assert h.sent_headers["Content-Type"] == "audio/wav"


def test_openai_tts_reads_config_api_key(monkeypatch):
    import api.config as config
    captured = {}

    def _fake_open(req, **kw):
        captured["auth"] = req.headers.get("Authorization")
        return _StreamOnceResponse([b"x"], headers={"Content-Type": "audio/mpeg"})

    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {"openai": {"base_url": "https://tts.example.com/v1",
                           "api_key": "sk-config-key", "model": "m", "voice": "v"}}
    })
    monkeypatch.setattr(routes, "_tts_open", _fake_open)
    h = _post({"text": "Hi", "engine": "openai"}, client="10.82.0.22")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert captured["auth"] == "Bearer sk-config-key"


def test_openai_tts_env_key_not_sent_to_lan_target(monkeypatch):
    """A real env OpenAI key (set for chat) must never travel to a
    self-hosted LAN target — the placeholder Bearer is sent instead."""
    import api.config as config
    captured = {}

    def _fake_open(req, **kw):
        captured["auth"] = req.headers.get("Authorization")
        return _StreamOnceResponse([b"x"], headers={"Content-Type": "audio/wav"})

    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-chat-key")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "192.168.1.0/24")
    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {"openai": {"base_url": "http://192.168.1.50:8001/v1",
                           "model": "m", "voice": "v"}}
    })
    monkeypatch.setattr(routes, "_tts_open", _fake_open)
    h = _post({"text": "Hallo", "engine": "openai"}, client="10.82.0.23")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert captured["auth"] == "Bearer sk-no-key-required"


def test_openai_tts_env_key_still_used_for_public_host(monkeypatch):
    import api.config as config
    captured = {}

    def _fake_open(req, **kw):
        captured["auth"] = req.headers.get("Authorization")
        return _StreamOnceResponse([b"x"], headers={"Content-Type": "audio/mpeg"})

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key")
    monkeypatch.setattr(config, "get_config", lambda: {"tts": {}})
    monkeypatch.setattr(routes, "_tts_open", _fake_open)
    h = _post({"text": "Hi", "engine": "openai"}, client="10.82.0.24")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert captured["auth"] == "Bearer sk-env-key"


def test_openai_tts_merges_extra_params(monkeypatch):
    """tts.extra_params reach the upstream JSON body; core fields win."""
    import api.config as config
    captured = {}

    def _fake_open(req, **kw):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _StreamOnceResponse([b"x"], headers={"Content-Type": "audio/wav"})

    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "192.168.1.0/24")
    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {
            "extra_params": {"speed": 1.2, "seed": 7, "model": "evil-override"},
            "openai": {"base_url": "http://192.168.1.50:8001/v1",
                       "model": "qwen3-tts", "voice": "spk"},
        }
    })
    monkeypatch.setattr(routes, "_tts_open", _fake_open)
    h = _post({"text": "Hallo", "engine": "openai"}, client="10.82.0.25")
    routes._handle_tts(h, None)

    assert h.status == 200
    assert captured["body"]["speed"] == 1.2
    assert captured["body"]["seed"] == 7
    assert captured["body"]["model"] == "qwen3-tts"  # core field wins
    assert captured["body"]["voice"] == "spk"


# ── /api/voice/config ───────────────────────────────────────────────────────

def test_voice_config_get_redacts_key(monkeypatch):
    import api.config as config
    monkeypatch.setattr(config, "get_config", lambda: {
        "stt": {"provider": "openai",
                "openai": {"base_url": "http://h:5094/v1", "model": "nemo",
                           "api_key": "secret", "language": "de"}},
        "tts": {"provider": "openai",
                "openai": {"base_url": "http://h:7036/v1", "model": "qwen3-tts",
                           "voice": "Serena"}},
    })
    h = _FakeHandler(command="GET")
    vc.handle_voice_config_get(h)
    body = h.payload()
    assert body["ok"] is True
    assert body["stt"]["base_url"] == "http://h:5094/v1"
    assert body["stt"]["language"] == "de"
    assert body["stt"]["api_key_set"] is True
    assert "api_key" not in body["stt"]
    assert body["tts"]["voice"] == "Serena"
    assert body["tts"]["api_key_set"] is False
    assert body["writable"] is False


def test_voice_config_post_blocked_without_optin(monkeypatch):
    h = _post({"stt": {"base_url": "http://h:5094/v1"}}, client="10.82.0.30")
    vc.handle_voice_config_post(h)
    assert h.status == 403


def test_voice_config_post_writes_and_preserves_comments(monkeypatch, tmp_path):
    import api.config as config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# my hand-written config\n"
        "stt:\n"
        "  provider: local  # keep this comment\n"
        "tts:\n"
        "  provider: edge\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE", "1")
    # Force get_config() to read our temp file fresh.
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg)

    h = _post({
        "stt": {"provider": "openai", "base_url": "http://192.168.1.50:5094/v1",
                "model": "nemotron", "api_key": "sk-stt", "language": "de"},
        "tts": {"provider": "openai", "base_url": "http://192.168.1.50:7036/v1",
                "model": "qwen3-tts", "voice": "Serena"},
    }, client="10.82.0.31")
    vc.handle_voice_config_post(h)
    assert h.status in (None, 200), h.payload()
    assert h.payload()["ok"] is True

    written = cfg.read_text(encoding="utf-8")
    # values always written
    assert "http://192.168.1.50:5094/v1" in written
    assert "sk-stt" in written
    assert "qwen3-tts" in written
    # a backup of the pre-write file was created
    assert list(tmp_path.glob("config.yaml.voicebak-*"))
    # comment preservation is best-effort: guaranteed only when ruamel is present
    # (the live WebUI runtime ships it via the agent venv).
    try:
        import ruamel.yaml  # noqa: F401
        has_ruamel = True
    except Exception:
        has_ruamel = False
    if has_ruamel:
        assert "hand-written config" in written
        assert "keep this comment" in written


def test_voice_config_post_keeps_existing_key_when_absent(monkeypatch, tmp_path):
    import api.config as config
    cfg = tmp_path / "config.yaml"
    cfg.write_text("stt:\n  openai:\n    api_key: sk-existing\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE", "1")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg)

    h = _post({"stt": {"base_url": "http://h:5094/v1"}}, client="10.82.0.32")
    vc.handle_voice_config_post(h)
    assert cfg.read_text(encoding="utf-8").count("sk-existing") == 1


# ── TTS capability ──────────────────────────────────────────────────────────

def test_tts_capability_shape(monkeypatch):
    import api.config as config
    monkeypatch.setattr(config, "get_config", lambda: {"tts": {"provider": "edge"}})
    h = _FakeHandler(command="GET")
    vc.handle_tts_capability(h)
    body = h.payload()
    assert body["ok"] is True
    assert set(("available", "provider")).issubset(body.keys())


# ── STT language passthrough ────────────────────────────────────────────────

import api.upload as upload


@pytest.mark.parametrize("raw,expected", [
    ("de-DE", "de"), ("en_US", "en"), ("de", "de"), (b"fr-FR", "fr"),
    ("", ""), (None, ""), ("   ", ""), ("123", ""), ("zh-Hans-CN", "zh"),
])
def test_normalize_transcribe_language(raw, expected):
    assert upload._normalize_transcribe_language(raw) == expected


def test_transcribe_forwards_language(monkeypatch, tmp_path):
    calls = {}

    def _fake_transcribe(path, language=None):
        calls["language"] = language
        return {"success": True, "transcript": "hallo"}

    import tools.transcription_tools as tt
    monkeypatch.setattr(tt, "transcribe_audio", _fake_transcribe)
    monkeypatch.setattr(
        upload, "parse_multipart",
        lambda *a, **k: ({"language": "de-DE"}, {"file": ("a.webm", b"RIFFxxxx")}),
    )

    h = _FakeHandler(b"body", command="POST",
                     headers={"Content-Type": "multipart/form-data; boundary=x",
                              "Content-Length": "4"})
    upload.handle_transcribe(h)
    assert h.payload().get("transcript") == "hallo"
    assert calls["language"] == "de"


# ── Frontend wiring (static source presence) ────────────────────────────────

def test_boot_js_voice_mode_uses_server_stt():
    src = (_STATIC / "boot.js").read_text(encoding="utf-8")
    # voice mode gate no longer hard-requires browser SpeechRecognition
    assert "_canRecordAudio" in src
    assert "if((!hasSR&&!_canRecordAudio)||!hasTTS) return;" in src
    # server-STT listening leg present and wired to /api/transcribe
    assert "_startListeningServer" in src
    assert "_useServerStt" in src
    assert "_probeVoiceServerStt" in src
    assert "form.append('language'" in src


def test_panels_js_wires_voice_config():
    src = (_STATIC / "panels.js").read_text(encoding="utf-8")
    assert "_wireVoiceEndpoints" in src
    assert "api/voice/config" in src


def test_index_html_has_voice_endpoint_fields():
    src = (_STATIC / "index.html").read_text(encoding="utf-8")
    for el_id in ("settingsVoiceEndpoints", "settingsSttBaseUrl", "settingsSttModel",
                  "settingsSttLanguage", "settingsTtsBaseUrl", "settingsTtsVoiceId",
                  "settingsVoiceEndpointsSave"):
        assert el_id in src, el_id
