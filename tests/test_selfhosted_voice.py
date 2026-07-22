"""Self-hosted STT/TTS: LAN SSRF opt-in, keyless OpenAI TTS, /api/voice/config.

Covers the WebUI-side changes that let operators point STT/TTS at their own
OpenAI-compatible servers on a private LAN, edit the endpoints from the UI, and
forward the browser locale as a transcription language hint.
"""
import io
import json
import shutil
import socket
import subprocess
import textwrap
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


def test_http_allowlisted_hostname_is_rejected_without_http_pinning(monkeypatch):
    """HTTP self-hosting may opt in only literal IP/CIDR targets, never DNS names."""
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "voice.example")

    with pytest.raises(ValueError, match="invalid OpenAI base_url"):
        routes._normalized_openai_tts_base_url("http://voice.example/v1")


def test_http_localhost_remains_allowed_without_lan_optin():
    assert routes._normalized_openai_tts_base_url("http://localhost:8001/v1") == (
        "http://localhost:8001/v1"
    )


def test_https_allowlisted_hostname_remains_accepted_and_pinned(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "voice.example")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("1.1.1.1", 0))],
    )

    assert routes._normalized_openai_tts_base_url("https://voice.example/v1") == (
        "https://voice.example/v1"
    )
    assert routes._tts_resolve_pinned_address("voice.example") == "1.1.1.1"


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


@pytest.mark.parametrize("blocked_address", ["10.0.0.9", "169.254.169.254"])
def test_allowlisted_hostname_cannot_authorize_blocked_resolved_address(monkeypatch, blocked_address):
    """Hostname permission never bypasses the pinned-address SSRF boundary."""
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "voice.example")
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(0, 0, 0, "", (blocked_address, 0))])
    with pytest.raises(ValueError, match="not allowed"):
        routes._tts_resolve_pinned_address("voice.example")


def test_allowlisted_hostname_keeps_public_resolved_address_behavior(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "voice.example")
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(0, 0, 0, "", ("1.1.1.1", 0))])
    assert routes._tts_resolve_pinned_address("voice.example") == "1.1.1.1"


def test_allowlisted_cidr_permits_hostname_resolved_lan_address(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "192.168.1.0/24")
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(0, 0, 0, "", ("192.168.1.50", 0))])
    assert routes._tts_resolve_pinned_address("voice.example") == "192.168.1.50"


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


def test_openai_tts_timeout_configurable(monkeypatch):
    """tts.openai.timeout reaches the proxy request (clamped 1..300);
    default stays 30 — a whole-answer synthesis on a slow self-hosted
    server timed out at the hard 30s before this."""
    import api.config as config
    captured = {}

    def _fake_open(req, timeout=None, **kw):
        captured["timeout"] = timeout
        return _StreamOnceResponse([b"x"], headers={"Content-Type": "audio/wav"})

    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_LAN", "1")
    monkeypatch.setenv("HERMES_WEBUI_TTS_ALLOW_HOSTS", "192.168.1.0/24")
    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {"openai": {"base_url": "http://192.168.1.50:8001/v1",
                           "model": "m", "voice": "v", "timeout": 120}}
    })
    monkeypatch.setattr(routes, "_tts_open", _fake_open)
    h = _post({"text": "Hallo", "engine": "openai"}, client="10.82.0.26")
    routes._handle_tts(h, None)
    assert h.status == 200
    assert captured["timeout"] == 120.0

    monkeypatch.setattr(config, "get_config", lambda: {
        "tts": {"openai": {"base_url": "http://192.168.1.50:8001/v1",
                           "model": "m", "voice": "v", "timeout": 99999}}
    })
    h = _post({"text": "Hallo", "engine": "openai"}, client="10.82.0.27")
    routes._handle_tts(h, None)
    assert captured["timeout"] == 300.0  # clamped


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

    # The agent's ``tools`` package isn't installed in the standalone webui CI —
    # handle_transcribe imports it lazily and degrades to 503 when absent. Inject
    # a fake module (plus a ``tools`` parent stub) so the language-forwarding
    # path is exercised without depending on the agent being importable.
    import sys
    import types
    fake = types.ModuleType("tools.transcription_tools")
    fake.transcribe_audio = _fake_transcribe
    tools_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setattr(tools_pkg, "transcription_tools", fake, raising=False)
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", fake)
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


def test_response_splitting_preference_wired():
    """Preferences → Response splitting (punctuation | paragraphs | none)
    exists, persists, and drives chunked TTS playback in both playback paths."""
    ui = (_STATIC / "ui.js").read_text(encoding="utf-8")
    boot = (_STATIC / "boot.js").read_text(encoding="utf-8")
    panels = (_STATIC / "panels.js").read_text(encoding="utf-8")
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="settingsTtsSplit"' in html
    for val in ("punctuation", "paragraphs", "none"):
        assert f'value="{val}"' in html
    assert "_ttsSplitMode" in ui and "_ttsChunksFor" in ui
    assert "_playServerTtsChunks" in ui
    assert "_ttsQueueToken" in ui
    # voice mode uses the shared chunked players
    assert "_playServerTtsChunks(chunks" in boot
    assert "_speakBrowserChunk" in boot
    # preference persists via localStorage + server speech settings
    assert "hermes-tts-split" in panels
    assert "tts_split:'hermes-tts-split'" in panels
    assert "['tts_split','hermes-tts-split']" in boot


def test_stt_request_format_field_wired():
    """STT Request format (multipart | JSON base64) is exposed in the
    self-hosted section and persisted to stt.openai.request_format."""
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    panels = (_STATIC / "panels.js").read_text(encoding="utf-8")
    assert 'id="settingsSttRequestFormat"' in html
    assert 'value="json"' in html
    assert "settingsSttRequestFormat" in panels
    assert "request_format" in vc._STT_STR_FIELDS


_WEBM = b'\x1aE\xdf\xa3' + b'\x00' * 12   # EBML/WebM magic
_WAV = b'RIFF\x00\x00\x00\x00WAVE'        # RIFF..WAVE
_OGG = b'OggS' + b'\x00' * 12


def test_stt_mime_types_allowlist_enforced(monkeypatch):
    """stt.mime_types is enforced (415) by SNIFFING the real container — a
    renamed extension cannot bypass it (the parse_multipart part type is not
    available, so filename-only enforcement was a bypassable sham)."""
    import api.config as config
    import api.upload as upload

    monkeypatch.setattr(config, "get_config",
                        lambda: {"stt": {"mime_types": "audio/webm,audio/ogg"}})
    # real webm bytes accepted
    assert upload._stt_mime_rejection({"file": ("v.webm", _WEBM)}, "v.webm") is None
    # WAV bytes rejected even though allowlist lacks wav
    rej = upload._stt_mime_rejection({"file": ("v.wav", _WAV)}, "v.wav")
    assert rej and "not in the allowed types" in rej
    # BYPASS ATTEMPT: WAV content renamed to .webm is still rejected (content wins)
    rej2 = upload._stt_mime_rejection({"file": ("evil.webm", _WAV)}, "evil.webm")
    assert rej2 and "not in the allowed types" in rej2
    # wildcard token
    monkeypatch.setattr(config, "get_config", lambda: {"stt": {"mime_types": "audio/*"}})
    assert upload._stt_mime_rejection({"file": ("v.ogg", _OGG)}, "v.ogg") is None
    # empty allowlist accepts anything
    monkeypatch.setattr(config, "get_config", lambda: {"stt": {}})
    assert upload._stt_mime_rejection({"file": ("v.wav", _WAV)}, "v.wav") is None


def test_sniff_audio_mime_covers_common_containers():
    import api.upload as upload
    assert upload._sniff_audio_mime(_WEBM) == "audio/webm"
    assert upload._sniff_audio_mime(_WAV) == "audio/wav"
    assert upload._sniff_audio_mime(_OGG) == "audio/ogg"
    assert upload._sniff_audio_mime(b'ID3\x04junk') == "audio/mpeg"
    assert upload._sniff_audio_mime(b'\x00\x00\x00\x20ftypM4A ') == "audio/mp4"
    assert upload._sniff_audio_mime(b'random') == ""


def test_voice_reply_tts_toggle_present():
    """Voice mode has a spoken-reply toggle: STT+LLM with or without TTS.
    Persisted per browser; when off, _speakResponse re-arms the mic instead
    of synthesizing."""
    boot = (_STATIC / "boot.js").read_text(encoding="utf-8")
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    i18n = (_STATIC / "i18n.js").read_text(encoding="utf-8")
    assert 'id="btnVoiceReplyToggle"' in html
    assert "hermes-voice-reply-tts" in boot
    assert "_voiceReplyTts" in boot
    # when off, _speakResponse skips synthesis and re-listens
    assert "if(!_voiceReplyTts){" in boot
    # i18n key present in every locale (parity)
    assert i18n.count("voice_reply_toggle:") == i18n.count("voice_mode_toggle:")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for voice-mode runtime tests")
def test_completion_beep_suppressed_in_voice_mode():
    """Drive the real voice-mode state export and completion-chime body.

    A text marker cannot prove that the chime reads the same active state as the
    voice system. This harness executes the actual voice-mode IIFE, activates it
    through its rendered button handler, and then calls the actual completion
    sound function. Deactivation must preserve the ordinary non-voice chime.
    """
    boot = (_STATIC / "boot.js").read_text(encoding="utf-8")
    messages = (_STATIC / "messages.js").read_text(encoding="utf-8")
    voice_start = boot.index("(function(){", boot.index("// ── Turn-based voice mode"))
    voice_end = boot.index("\n})();\nfunction _currentSessionIsReusableEmptyChat", voice_start) + len("\n})();")
    voice_mode = boot[voice_start:voice_end]
    sound_start = messages.index("function playNotificationSound")
    sound_end = messages.index("\n}", sound_start) + len("\n}")
    completion_sound = messages[sound_start:sound_end]

    harness = textwrap.dedent(
        """
        let oscillatorStarts = 0;
        const classes = () => ({ add() {}, remove() {} });
        const elements = {
          btnVoiceMode: { style: {}, classList: classes(), onclick: null },
          voiceModeBar: { style: {} },
          voiceModeIndicator: { className: '' },
          voiceModeLabel: { textContent: '' },
          btnMic: { style: {} },
          msg: { value: '' },
        };
        const window = {
          _soundEnabled: true,
          SpeechRecognition: class { start() {} abort() {} },
          speechSynthesis: {},
          AudioContext: class {
            constructor() { this.currentTime = 0; this.destination = {}; }
            createOscillator() {
              return {
                connect() {}, type: '',
                frequency: { setValueAtTime() {} },
                start() { oscillatorStarts += 1; }, stop() {}, onended: null,
              };
            }
            createGain() {
              return {
                connect() {},
                gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} },
              };
            }
            close() {}
          },
        };
        const navigator = { mediaDevices: null };
        const localStorage = { getItem() { return null; }, setItem() {} };
        const S = { busy: false };
        const document = { querySelectorAll() { return []; } };
        const $ = id => elements[id] || null;
        const t = key => key;
        const showToast = () => {};
        const autoResize = () => {};
        const stopTTS = () => {};
        const _micOriginNeedsSecureContext = () => false;
        const _setButtonTooltip = () => {};
        %s
        %s
        elements.btnVoiceMode.onclick();
        if (typeof window._voiceModeActive !== 'function' || !window._voiceModeActive()) {
          throw new Error('voice mode did not expose its active state');
        }
        playNotificationSound();
        const whileVoiceActive = oscillatorStarts;
        elements.btnVoiceMode.onclick();
        playNotificationSound();
        console.log(JSON.stringify({ whileVoiceActive, afterDeactivate: oscillatorStarts }));
        """
    ) % (voice_mode, completion_sound)
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip())
    assert observed == {"whileVoiceActive": 0, "afterDeactivate": 1}


def test_voice_mode_thinking_watchdog_present():
    """Voice mode recovers from a 'thinking' turn that never reaches the
    done→autoRead hook (dropped stream / cancel / error) instead of hanging."""
    boot = (_STATIC / "boot.js").read_text(encoding="utf-8")
    assert "_armThinkingWatchdog" in boot
    assert "_clearThinkingWatchdog" in boot
    # watchdog polls S.busy and recovers to listening / speaks the reply
    assert "S.busy" in boot
    # armed on send, cleared on speak + deactivate
    assert boot.count("_clearThinkingWatchdog()") >= 2


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
