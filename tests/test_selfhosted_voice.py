"""Self-hosted STT/TTS: LAN SSRF opt-in, keyless OpenAI TTS, /api/voice/config.

Covers the WebUI-side changes that let operators point STT/TTS at their own
OpenAI-compatible servers on a private LAN, edit the endpoints from the UI, and
forward the browser locale as a transcription language hint.
"""
import io
import json
import os
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
    from api import operator_env as _opmod
    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    for var in ("VOICE_TOOLS_OPENAI_KEY", "OPENAI_API_KEY",
                "HERMES_WEBUI_TTS_ALLOW_LAN", "HERMES_WEBUI_TTS_ALLOW_HOSTS",
                "HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE"):
        monkeypatch.delenv(var, raising=False)
    # The snapshot is taken once at import, so it survives between tests and
    # has to be reset here — otherwise one test's opt-in silently grants the
    # next one operator authority it never asked for.
    _saved_operator_env = dict(_opmod._STARTUP_ENV)
    for _key in ("HERMES_WEBUI_TTS_ALLOW_LAN", "HERMES_WEBUI_TTS_ALLOW_HOSTS",
                 "HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE"):
        _opmod._STARTUP_ENV[_key] = ""
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter
    yield
    _opmod._STARTUP_ENV.clear()
    _opmod._STARTUP_ENV.update(_saved_operator_env)


def set_operator_env(**overrides):
    """Set operator-only controls the way an operator does — at startup.

    ``monkeypatch.setenv`` deliberately no longer reaches these: a profile's
    dotenv file is projected into ``os.environ`` process-wide, so reading them
    live would let any profile grant itself operator authority. That
    immutability is the property under test, which means a test that wants the
    control ON has to write the startup snapshot instead of the environment.
    """
    from api.operator_env import _set_startup_env_for_tests

    _set_startup_env_for_tests(**overrides)



# ── SSRF LAN opt-in ─────────────────────────────────────────────────────────

def test_lan_base_url_rejected_without_optin():
    with pytest.raises(ValueError):
        routes._normalized_openai_tts_base_url("http://192.168.1.50:8001/v1")


def test_lan_base_url_allowed_with_optin(monkeypatch):
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="192.168.1.0/24")
    out = routes._normalized_openai_tts_base_url("http://192.168.1.50:8001/v1")
    assert out == "http://192.168.1.50:8001/v1"


def test_http_allowlisted_hostname_is_rejected_without_http_pinning(monkeypatch):
    """HTTP self-hosting may opt in only literal IP/CIDR targets, never DNS names."""
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="voice.example")

    with pytest.raises(ValueError, match="invalid OpenAI base_url"):
        routes._normalized_openai_tts_base_url("http://voice.example/v1")


def test_http_localhost_remains_allowed_without_lan_optin():
    assert routes._normalized_openai_tts_base_url("http://localhost:8001/v1") == (
        "http://localhost:8001/v1"
    )


def test_https_allowlisted_hostname_remains_accepted_and_pinned(monkeypatch):
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="voice.example")
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
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")  # no _ALLOW_HOSTS
    with pytest.raises(ValueError):
        routes._normalized_openai_tts_base_url("http://192.168.1.50:8001/v1")


def test_hosts_without_gate_is_fail_closed(monkeypatch):
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="192.168.1.50")  # no gate
    with pytest.raises(ValueError):
        routes._normalized_openai_tts_base_url("http://192.168.1.50:8001/v1")


def test_non_allowlisted_lan_still_blocked(monkeypatch):
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="192.168.1.50")
    with pytest.raises(ValueError):
        routes._normalized_openai_tts_base_url("http://10.0.0.9:8001/v1")


def test_pinned_https_lan_allowed_when_allowlisted(monkeypatch):
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="192.168.1.0/24")
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(0, 0, 0, "", ("192.168.1.50", 0))])
    assert routes._tts_resolve_pinned_address("192.168.1.50") == "192.168.1.50"


@pytest.mark.parametrize("blocked_address", ["10.0.0.9", "169.254.169.254"])
def test_allowlisted_hostname_cannot_authorize_blocked_resolved_address(monkeypatch, blocked_address):
    """Hostname permission never bypasses the pinned-address SSRF boundary."""
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="voice.example")
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(0, 0, 0, "", (blocked_address, 0))])
    with pytest.raises(ValueError, match="not allowed"):
        routes._tts_resolve_pinned_address("voice.example")


def test_allowlisted_hostname_keeps_public_resolved_address_behavior(monkeypatch):
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="voice.example")
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(0, 0, 0, "", ("1.1.1.1", 0))])
    assert routes._tts_resolve_pinned_address("voice.example") == "1.1.1.1"


def test_allowlisted_cidr_permits_hostname_resolved_lan_address(monkeypatch):
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="192.168.1.0/24")
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

    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="192.168.1.0/24")
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
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="192.168.1.0/24")
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

    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="192.168.1.0/24")
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

    set_operator_env(HERMES_WEBUI_TTS_ALLOW_LAN="1")
    set_operator_env(HERMES_WEBUI_TTS_ALLOW_HOSTS="192.168.1.0/24")
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
    # `language` sits in config.yaml but is NOT surfaced: no built-in agent STT
    # provider reads stt.openai.language, so echoing it back into an editable
    # field would advertise a control that changes nothing.
    assert "language" not in body["stt"]
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
    set_operator_env(HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE="1")
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
    """A blank api_key field on a save that does NOT move the endpoint keeps
    the stored key — the redacted GET never returns it, so the round-trip must
    not wipe it."""
    import api.config as config
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "stt:\n  openai:\n    base_url: http://h:5094/v1\n    api_key: sk-existing\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg))
    set_operator_env(HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE="1")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg)

    h = _post({"stt": {"base_url": "http://h:5094/v1", "model": "nemo"}}, client="10.82.0.32")
    vc.handle_voice_config_post(h)
    assert cfg.read_text(encoding="utf-8").count("sk-existing") == 1


def test_voice_config_post_drops_the_key_when_the_endpoint_host_changes(monkeypatch, tmp_path):
    """Repointing base_url at a different server must not carry the old key.

    The reproduced failure: a blank-key save that changed base_url to a LAN box
    kept `sk-existing`, and the TTS/STT path then shipped that credential — in
    cleartext over plain http — to a server it was never issued for. The key is
    dropped and the response says so.
    """
    import api.config as config
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "tts:\n  openai:\n    base_url: https://api.openai.com/v1\n    api_key: sk-existing\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg))
    set_operator_env(HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE="1")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg)

    h = _post({"tts": {"base_url": "http://192.168.1.50:7036/v1"}}, client="10.82.0.32")
    vc.handle_voice_config_post(h)
    text = cfg.read_text(encoding="utf-8")
    assert "sk-existing" not in text
    assert "192.168.1.50" in text
    assert h.payload()["api_key_dropped"]["tts"] is True


def test_voice_config_post_can_clear_a_stored_key(monkeypatch, tmp_path):
    """`api_key_clear` is the ONLY way to remove a key from the UI.

    Before it existed the redacted GET returned a boolean, a blank field meant
    "keep", and there was consequently no request an operator could send that
    deleted a key they had stopped trusting.
    """
    import api.config as config
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "tts:\n  openai:\n    base_url: http://h:7036/v1\n    api_key: sk-existing\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg))
    set_operator_env(HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE="1")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg)

    h = _post({"tts": {"base_url": "http://h:7036/v1", "api_key_clear": True}},
              client="10.82.0.32")
    vc.handle_voice_config_post(h)
    assert "sk-existing" not in cfg.read_text(encoding="utf-8")
    assert h.payload()["tts"]["api_key_set"] is False


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
    # The gate requires a LISTENING leg only — browser recognizer or capture.
    # It no longer hard-requires browser SpeechRecognition, and no longer
    # requires speechSynthesis either: server TTS can supply the speaking leg,
    # and silent-reply mode needs none. That is resolved at activation.
    assert "_canRecordAudio" in src
    assert "if(!hasSR&&!_canRecordAudio) return;" in src
    assert "||!hasTTS) return;" not in src, (
        "speechSynthesis must not gate voice mode; confirmed server TTS is a leg"
    )
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


def test_unsupported_stt_controls_are_not_exposed():
    """Controls the agent never reads must not be offered.

    `stt.openai.language`, `request_format` and `response_format` were editable
    in Settings and persisted to config.yaml, but `tools/transcription_tools.py`
    reads none of them for a built-in provider: `_transcribe_openai` hardcodes
    its response format and only forwards `language` for PLUGIN providers. An
    operator who set a language got no error and no effect — the setting looked
    applied and was not. The live language hint is the per-request `language`
    field on /api/transcribe, which IS feature-detected and consumed.
    """
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    panels = (_STATIC / "panels.js").read_text(encoding="utf-8")
    for gone in ("settingsSttRequestFormat", "settingsSttLanguage", "settingsSttResponseFormat"):
        assert gone not in html, gone
        assert gone not in panels, gone
    for gone in ("request_format", "language", "response_format"):
        assert gone not in vc._STT_STR_FIELDS, gone
    # The per-request hint stays.
    import api.upload as upload
    assert upload._normalize_transcribe_language("de-DE") == "de"


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
    # When off, _speakResponse skips synthesis and re-listens. It consults the
    # EFFECTIVE state: the stored preference AND a runtime suppression used
    # when no speaking leg is available.
    assert "if(!_voiceReplyEffective()){" in boot
    assert "function _voiceReplyEffective()" in boot
    # The runtime suppression must never be persisted — a TTS server that is
    # briefly unreachable would otherwise become the user's saved choice.
    fallback = boot[boot.index("_voiceReplySilentFallback=!_speakingLegAvailable()"):][:400]
    assert "localStorage.setItem('hermes-voice-reply-tts'" not in fallback, (
        "the silent fallback must not write the persisted preference"
    )
    # i18n key present in every locale (parity)
    assert i18n.count("voice_reply_toggle:") == i18n.count("voice_mode_toggle:")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for voice-mode runtime tests")
def test_completion_beep_suppressed_in_voice_mode():
    """Drive the real voice-mode state export and completion-chime body.

    A text marker cannot prove that the chime reads the same active state as the
    voice system. This harness executes the actual voice-mode IIFE, activates it
    through its rendered button handler, and then calls the actual completion
    sound function.

    Suppression is OWNED, not global: only the session voice mode is holding a
    turn in goes quiet. A different session finishing while the user talks must
    still chime — a plain "voice mode is on" check silenced every background
    task the user was waiting on. Deactivation restores the ordinary chime.
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
        const S = { busy: false, session: { session_id: 'voice-sid' } };
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
        // Activation is async: it resolves the listening leg before the first
        // listen, so the active flag is set after the capability probe settles
        // rather than synchronously on the click.
        const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setTimeout(r, 0)); };
        (async () => {
          elements.btnVoiceMode.onclick();
          await settle();
          if (typeof window._voiceModeActive !== 'function' || !window._voiceModeActive()) {
            throw new Error('voice mode did not expose its active state');
          }
          playNotificationSound('voice-sid');
          const whileVoiceActive = oscillatorStarts;
          playNotificationSound('some-other-sid');
          const otherSessionWhileVoiceActive = oscillatorStarts - whileVoiceActive;
          elements.btnVoiceMode.onclick();
          await settle();
          playNotificationSound('voice-sid');
          console.log(JSON.stringify({
            whileVoiceActive,
            otherSessionWhileVoiceActive,
            afterDeactivate: oscillatorStarts - whileVoiceActive - otherSessionWhileVoiceActive,
          }));
        })().catch(e => { console.error(e.message); process.exit(1); });
        """
    ) % (voice_mode, completion_sound)
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip())
    assert observed == {
        "whileVoiceActive": 0,
        "otherSessionWhileVoiceActive": 1,
        "afterDeactivate": 1,
    }


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
                  "settingsSttApiKeyClear", "settingsTtsBaseUrl", "settingsTtsVoiceId",
                  "settingsTtsApiKeyClear", "settingsVoiceEndpointsSave"):
        assert el_id in src, el_id


# ── Gate follow-ups (authoritative gate certificate on 86e0b149) ────────────


def _install_fake_transcription_tools(monkeypatch, fake_mod):
    """Put a stand-in `tools.transcription_tools` on sys.modules.

    The standalone WebUI has no `tools` package at all, and
    `_stt_provider_capability` reaches it with `import tools.x as y`, which
    needs the PARENT package bound too — a submodule-only entry is enough for
    `from tools.x import y` but not for this form.
    """
    import sys
    import types

    tools_pkg = sys.modules.get("tools")
    if tools_pkg is None:
        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setattr(tools_pkg, "transcription_tools", fake_mod, raising=False)
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", fake_mod)


class _RecordingScope:
    """Context manager that records whether it was entered."""

    def __init__(self):
        self.entered = 0
        self.purposes = []

    def __call__(self, purpose="", **_kw):
        self.purposes.append(purpose)
        return self

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *_a):
        return False


def test_transcribe_runs_inside_the_request_profile_scope(monkeypatch, tmp_path):
    """The agent STT call must be bound to the profile that issued the request.

    `transcribe_audio` resolves its provider, config.yaml and credentials
    through the agent's ambient helpers (process env, `get_hermes_home()`). A
    WebUI request carries its profile in a cookie that becomes a thread-local,
    which those helpers do not consult — so a named profile's Dictate ran on the
    ROOT profile's provider, endpoint and API key.
    """
    import api.profiles as profiles
    import api.upload as upload

    scope = _RecordingScope()
    monkeypatch.setattr(profiles, "profile_env_for_active_request", scope)

    inside = {}

    def _transcribe(path, **_kw):
        inside["entered_at_call_time"] = scope.entered
        return {"success": True, "transcript": "hallo"}

    import types

    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.transcribe_audio = _transcribe
    _install_fake_transcription_tools(monkeypatch, fake_mod)

    body, headers = _multipart_audio(b"OggS" + b"\x00" * 12, "voice-input.ogg")
    h = _FakeHandler(body=body, headers=headers)
    upload.handle_transcribe(h)

    assert h.payload() == {"ok": True, "transcript": "hallo"}
    assert inside.get("entered_at_call_time") == 1, "transcribe ran outside the profile scope"
    assert scope.purposes == ["/api/transcribe"]


def test_stt_capability_probe_runs_inside_the_request_profile_scope(monkeypatch):
    """Same binding for the probe: the browser decides whether to route voice
    mode at the server from this answer, so it must describe the REQUEST's
    profile, not the root profile."""
    import api.profiles as profiles
    import api.upload as upload

    scope = _RecordingScope()
    monkeypatch.setattr(profiles, "profile_env_for_active_request", scope)

    seen = {}

    def _probe(_module):
        seen["entered"] = scope.entered
        return True, "openai"

    import types

    monkeypatch.setattr(upload, "_stt_provider_capability_from_module", _probe)
    _install_fake_transcription_tools(monkeypatch, types.ModuleType("tools.transcription_tools"))

    h = _FakeHandler(command="GET")
    upload.handle_transcribe_capability(h)
    assert h.payload()["available"] is True
    assert seen.get("entered") == 1, "capability probe ran outside the profile scope"


def _multipart_audio(data: bytes, filename: str, extra_fields=None):
    boundary = "----voicetest"
    parts = []
    for key, value in (extra_fields or {}).items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return body, {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }


def test_configured_mime_allowlist_rejects_an_unclassifiable_upload(monkeypatch):
    """An extensionless payload with unrecognised magic bytes must be REJECTED
    when an allowlist is configured.

    Accepting it made `stt.mime_types` advisory: the one upload nothing can
    classify — exactly what a caller sends to get past a type filter — walked
    straight through. Operators who want everything accepted leave the setting
    unset.
    """
    import api.config as config
    import api.upload as upload

    monkeypatch.setattr(config, "get_config", lambda: {"stt": {"mime_types": "audio/webm"}})
    files = {"file": ("blob", b"\x00\x01\x02\x03not-a-known-container")}
    rejection = upload._stt_mime_rejection(files, "blob")
    assert rejection, "unclassifiable upload passed a configured allowlist"
    assert "audio/webm" in rejection

    # A real WebM still gets through, and an unset allowlist accepts anything.
    assert upload._stt_mime_rejection({"file": ("blob", b"\x1aE\xdf\xa3" + b"\x00" * 12)}, "blob") is None
    monkeypatch.setattr(config, "get_config", lambda: {"stt": {}})
    assert upload._stt_mime_rejection(files, "blob") is None


def test_unknown_magic_with_an_allowed_suffix_is_rejected(monkeypatch):
    """Unidentifiable bytes named ``*.webm`` must not inherit ``audio/webm``.

    The previous head fell back to ``mimetypes.guess_type(safe_name)`` whenever
    the magic bytes were unrecognised. The filename is fully attacker-supplied,
    so that fallback handed out exactly the type the allowlist was configured
    to accept: arbitrary non-audio content called ``payload.webm`` passed an
    ``audio/webm`` allowlist. The extensionless case was already covered — this
    is the one that made the suffix worth forging.
    """
    import api.config as config
    import api.upload as upload

    monkeypatch.setattr(config, "get_config",
                        lambda: {"stt": {"mime_types": "audio/webm,audio/ogg"}})

    not_audio = b"\x00\x01\x02\x03definitely-not-a-media-container" + b"\xff" * 32
    for name in ("payload.webm", "payload.ogg", "payload.WEBM", "payload.mp3"):
        rejection = upload._stt_mime_rejection({"file": (name, not_audio)}, name)
        assert rejection, f"{name} inherited an allowed type from its suffix"
        assert "audio/webm" in rejection

    # The suffix must not decide the accept case either: real WebM bytes are
    # admitted under a name whose suffix is not in the allowlist at all.
    real_webm = b"\x1aE\xdf\xa3" + b"\x00" * 12
    assert upload._stt_mime_rejection({"file": ("recording.txt", real_webm)},
                                      "recording.txt") is None

    # And a wildcard allowlist still lets the unidentifiable payload through.
    monkeypatch.setattr(config, "get_config", lambda: {"stt": {"mime_types": "*/*"}})
    assert upload._stt_mime_rejection({"file": ("payload.webm", not_audio)},
                                      "payload.webm") is None


@pytest.mark.parametrize(
    "upstream",
    [
        "audio/mpeg\r\nSet-Cookie: injected=1",
        "audio/mpeg\nX-Injected: 1",
        "audio/mpeg; charset=x\r\nSet-Cookie: y=1",
        "audio/ wav",
        "audio/mpeg:8080",
        "text/html",
        "audio/",
    ],
)
def test_upstream_audio_content_type_cannot_inject_headers(upstream):
    """/api/tts forwards the TTS server's audio type; the old check was
    `startswith("audio/")`, which passes a CRLF payload through to
    `send_header` verbatim — a hostile or compromised TTS endpoint could set
    cookies and CORS headers on a same-origin WebUI response."""
    assert routes._safe_forwarded_audio_type(upstream) == ""


@pytest.mark.parametrize(
    "upstream,expected",
    [
        ("audio/wav", "audio/wav"),
        ("audio/mpeg", "audio/mpeg"),
        ("audio/ogg; codecs=opus", "audio/ogg"),
        ("  audio/x-wav  ", "audio/x-wav"),
    ],
)
def test_legitimate_upstream_audio_types_still_forward(upstream, expected):
    assert routes._safe_forwarded_audio_type(upstream) == expected


def test_voice_config_write_holds_the_shared_config_lock(monkeypatch, tmp_path):
    """The read-modify-write must run under the same `_cfg_lock` every other
    config.yaml writer holds.

    Without it two acknowledged saves — voice endpoints in one tab, the model
    picker in another — each read the pre-write file and the second
    `os.replace` silently discarded the first, both answered 200.
    """
    import api.config as config

    cfg = tmp_path / "config.yaml"
    cfg.write_text("tts:\n  openai:\n    model: old\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg))
    set_operator_env(HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE="1")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg)

    observed = {}
    real_write = vc._write_config_atomic

    def _spy(path, dump, data):
        observed["locked"] = config._cfg_lock.locked()
        return real_write(path, dump, data)

    monkeypatch.setattr(vc, "_write_config_atomic", _spy)

    h = _post({"tts": {"model": "new"}}, client="10.82.0.32")
    vc.handle_voice_config_post(h)
    assert h.payload()["ok"] is True
    assert observed.get("locked") is True, "config.yaml was rewritten without _cfg_lock"


def test_config_backup_inherits_the_source_mode_and_never_widens_it(tmp_path):
    """A hardened 0600 config.yaml must not produce a 0644 backup next to it.

    Reproduced by the gate: the backup was written with `Path.write_bytes`,
    which applies the umask — so the API key the operator had locked down to
    owner-only sat in a world-readable sibling file.
    """
    import os

    cfg = tmp_path / "config.yaml"
    cfg.write_text("tts:\n  openai:\n    api_key: sk-secret\n", encoding="utf-8")
    os.chmod(cfg, 0o600)

    vc._backup_config(cfg)
    backups = list(tmp_path.glob("config.yaml" + vc._BACKUP_SUFFIX + "*"))
    assert len(backups) == 1
    assert "sk-secret" in backups[0].read_text(encoding="utf-8")
    assert (backups[0].stat().st_mode & 0o777) == 0o600, oct(backups[0].stat().st_mode & 0o777)


def test_config_backups_are_pruned(tmp_path):
    """Each backup is a full copy of a file that can hold API keys, so they are
    capped instead of accumulating forever."""
    import os

    cfg = tmp_path / "config.yaml"
    for i in range(vc._MAX_CONFIG_BACKUPS + 4):
        cfg.write_text(f"tts:\n  openai:\n    model: m{i}\n", encoding="utf-8")
        # Distinct mtimes so each write earns its own backup name.
        os.utime(cfg, (1_700_000_000 + i, 1_700_000_000 + i))
        vc._backup_config(cfg)
    backups = sorted(tmp_path.glob("config.yaml" + vc._BACKUP_SUFFIX + "*"))
    assert len(backups) == vc._MAX_CONFIG_BACKUPS, [b.name for b in backups]


def test_saving_through_a_symlinked_config_updates_the_referent(monkeypatch, tmp_path):
    """A symlinked config.yaml must keep being a symlink.

    The hand-rolled `mkstemp`+`os.replace` here replaced the LINK with a regular
    file: an operator pointing ~/.hermes/config.yaml at a managed dotfiles repo
    had the link severed and their real config left un-updated, so the next
    `git status` showed nothing and the agent read a different file than the UI
    had just written.
    """
    import api.config as config

    real = tmp_path / "managed" / "config.yaml"
    real.parent.mkdir()
    real.write_text("tts:\n  openai:\n    model: old\n", encoding="utf-8")
    link = tmp_path / "config.yaml"
    link.symlink_to(real)

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(link))
    set_operator_env(HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE="1")
    monkeypatch.setattr(config, "_get_config_path", lambda: link)

    h = _post({"tts": {"model": "new"}}, client="10.82.0.32")
    vc.handle_voice_config_post(h)
    assert h.payload()["ok"] is True
    assert link.is_symlink(), "the symlink was replaced by a regular file"
    assert "new" in real.read_text(encoding="utf-8"), "the referent was not updated"


def _voice_mode_source():
    boot = (_STATIC / "boot.js").read_text(encoding="utf-8")
    start = boot.index("(function(){", boot.index("// ── Turn-based voice mode"))
    end = boot.index("\n})();\nfunction _currentSessionIsReusableEmptyChat", start) + len("\n})();")
    return boot[start:end]


_VOICE_HARNESS = textwrap.dedent(
    """
    // Minimal browser for the voice-mode IIFE. `hasSR` is controlled by whether
    // SpeechRecognition exists — the Firefox case is SR ABSENT.
    const toasts = [];
    const classes = () => ({ add() {}, remove() {} });
    const elements = {
      btnVoiceMode: { style: {}, classList: classes(), onclick: null },
      voiceModeBar: { style: {} },
      voiceModeIndicator: { className: '' },
      voiceModeLabel: { textContent: '' },
      btnMic: { style: {} },
      msg: { value: '' },
      btnVoiceReplyToggle: null,
    };
    const $ = (id) => elements[id] || null;
    const t = (k) => k;
    const showToast = (m) => { toasts.push(String(m)); };
    const autoResize = () => {};
    const stopTTS = () => {};
    const send = () => {};
    const _micOriginNeedsSecureContext = () => false;
    const _setButtonTooltip = () => {};
    const _micToastKeyForRecognitionError = () => null;
    const S = { busy: false, session: { session_id: 'sid' } };
    const localStorage = {
      _v: { 'hermes-voice-mode-button': 'true' },
      getItem(k) { return Object.prototype.hasOwnProperty.call(this._v, k) ? this._v[k] : null; },
      setItem(k, v) { this._v[k] = String(v); },
    };
    const document = { querySelectorAll: () => [] };
    // Bounded fake clock: timers run in scheduling order, never more than
    // MAX_TICKS of them, so a retry loop that never stops still terminates the
    // test instead of hanging it.
    const MAX_TICKS = 400;
    let queue = [], ticks = 0;
    const setTimeout = (fn) => { queue.push(fn); return queue.length; };
    const clearTimeout = () => {};
    const setInterval = () => 0;
    const clearInterval = () => {};
    const requestAnimationFrame = () => 0;
    const cancelAnimationFrame = () => {};
    const yieldToLoop = () => new Promise((r) => globalThis.setImmediate(r));
    async function drain() {
      // Rounds, because a drained queue is not the same as a settled system:
      // an async continuation (getUserMedia, the fetch chain) schedules the
      // NEXT timer only after the event loop turns.
      for (let round = 0; round < 80 && ticks < MAX_TICKS; round++) {
        while (queue.length && ticks < MAX_TICKS) {
          const fn = queue.shift();
          ticks += 1;
          try { fn(); } catch (_) {}
          await yieldToLoop();
        }
        await yieldToLoop();
        if (!queue.length) {
          await yieldToLoop();
          if (!queue.length) break;
        }
      }
    }

    let transcribePosts = 0;
    const fetch = (url) => {
      if (String(url).indexOf('transcribe/capability') >= 0) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, available: CAPABILITY }) });
      }
      if (String(url).indexOf('api/transcribe') >= 0) {
        transcribePosts += 1;
        if (TRANSCRIBE_OK) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, transcript: 'hallo' }) });
        }
        return Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({ error: 'unavailable' }) });
      }
      return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
    };

    // MediaRecorder that produces one utterance immediately on start().
    class MediaRecorder {
      static isTypeSupported() { return true; }
      constructor() { this.state = 'inactive'; this.mimeType = 'audio/webm'; }
      start() {
        this.state = 'recording';
        setTimeout(() => {
          this.state = 'inactive';
          if (this.ondataavailable) this.ondataavailable({ data: { size: 4096, type: 'audio/webm' } });
          if (this.onstop) this.onstop();
        });
      }
      stop() { this.state = 'inactive'; }
    }
    class Blob { constructor(parts, opts) { this.size = 4096; this.type = (opts && opts.type) || ''; } }
    class File extends Blob {}
    class FormData { append() {} }
    // Every getUserMedia() call is registered so the test can prove that each
    // stream it handed out was eventually stopped. An orphaned stream is a
    // microphone left open in a real browser.
    const streams = [];
    const navigator = {
      mediaDevices: {
        getUserMedia: () => {
          const id = streams.length + 1;
          const stream = { id, stopped: false, getTracks: () => [{ stop() { stream.stopped = true; } }] };
          streams.push(stream);
          return Promise.resolve(stream);
        },
      },
    };
    const window = {
      MediaRecorder,
      speechSynthesis: {},
      SpeechRecognition: HAS_SR ? class { start() {} abort() {} } : undefined,
    };
    if (!HAS_SR) delete window.SpeechRecognition;
    const performance = { now: () => 0 };
    """
)


def _run_voice_harness(*, has_sr: bool, capability: bool, epilogue: str, transcribe_ok: bool = False):
    src = (
        _VOICE_HARNESS.replace("HAS_SR", "true" if has_sr else "false")
        .replace("CAPABILITY", "true" if capability else "false")
        .replace("TRANSCRIBE_OK", "true" if transcribe_ok else "false")
        + _voice_mode_source()
        + "\n"
        + textwrap.dedent(epilogue)
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", src], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_voice_mode_refuses_to_start_without_a_confirmed_listening_leg():
    """No SpeechRecognition AND no server STT means there is no way to listen.

    Voice mode used to treat "the browser has no recognizer" as proof that the
    server must be handling STT (`_voiceServerStt || !hasSR`), so on Firefox it
    activated against an endpoint it had never confirmed — the button lit up,
    the mic opened, and every utterance went nowhere.
    """
    observed = _run_voice_harness(
        has_sr=False,
        capability=False,
        epilogue="""
        (async () => {
          await elements.btnVoiceMode.onclick();
          await drain();
          console.log(JSON.stringify({
            active: window._voiceModeActive(),
            posts: transcribePosts,
            toasted: toasts.indexOf('voice_stt_unavailable') >= 0,
          }));
        })();
        """,
    )
    assert observed == {"active": False, "posts": 0, "toasted": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_voice_mode_starts_when_server_stt_is_confirmed():
    """The gate must not block the case it exists for."""
    observed = _run_voice_harness(
        has_sr=False,
        capability=True,
        epilogue="""
        (async () => {
          await elements.btnVoiceMode.onclick();
          console.log(JSON.stringify({ active: window._voiceModeActive() }));
        })();
        """,
    )
    assert observed == {"active": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_failing_server_stt_deactivates_instead_of_retrying_forever():
    """A browser with no recognizer had NO exit from a failing /api/transcribe.

    Every rejected upload scheduled another listen 800 ms later, forever: the
    mic stayed hot, the same 503 came back each round, and nothing on screen
    explained it. Failures are now counted and voice mode switches itself off
    with a message.
    """
    observed = _run_voice_harness(
        has_sr=False,
        capability=True,
        epilogue="""
        (async () => {
          await elements.btnVoiceMode.onclick();
          await drain();
          console.log(JSON.stringify({
            active: window._voiceModeActive(),
            posts: transcribePosts,
            toasted: toasts.indexOf('voice_stt_unavailable') >= 0,
            ticksExhausted: ticks >= MAX_TICKS,
          }));
        })();
        """,
    )
    assert observed["active"] is False, "voice mode stayed on against a failing STT server"
    assert observed["ticksExhausted"] is False, "the retry loop never terminated"
    assert observed["posts"] == 3, observed
    assert observed["toasted"] is True


def test_voice_endpoint_labels_are_associated_and_translatable():
    """Every input in the endpoints block needs a real label association.

    The block shipped 13 bare `<label>` elements with no `for`, so a screen
    reader announced "edit text" with no name, and clicking a label did not
    focus its field. The strings were also hardcoded English inside a UI that
    translates everything else.
    """
    import re

    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    start = html.index('<details id="settingsVoiceEndpoints"')
    block = html[start:html.index("</details>", start)]

    control_ids = set(re.findall(r'<(?:input|select)[^>]*\bid="([^"]+)"', block))
    labelled = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', block))
    # Checkbox controls are wrapped by their own <label>, which is an equally
    # valid association; exclude them from the `for=` requirement.
    wrapped = set(re.findall(r'<label[^>]*>\s*<input[^>]*\bid="([^"]+)"', block))
    missing = control_ids - labelled - wrapped
    assert not missing, f"controls with no label association: {sorted(missing)}"

    for label in re.findall(r"<label[^>]*>", block):
        if 'for="' not in label:
            continue
        assert "data-i18n" in label or "data-i18n" in block, label
    # And the visible strings go through i18n rather than being baked in.
    for key in ("voice_endpoints_title", "voice_endpoints_base_url",
                "voice_endpoints_api_key", "voice_endpoints_save"):
        assert f'data-i18n="{key}"' in block, key


def test_every_voice_i18n_key_exists_in_every_locale():
    """A key referenced from JS but missing in a locale renders as the raw key.

    The endpoints UI added strings mid-file; parity is enforced here rather
    than discovered by a user on a non-English locale seeing
    `voice_endpoints_saving` in their status line.
    """
    import re

    i18n = (_STATIC / "i18n.js").read_text(encoding="utf-8")
    locale_count = len(re.findall(r"^\s*session_deleted: ", i18n, re.M))
    assert locale_count >= 15, locale_count
    referenced = set()
    for name in ("boot.js", "panels.js"):
        src = (_STATIC / name).read_text(encoding="utf-8")
        referenced |= set(re.findall(r"t\('(voice_endpoints_[a-z0-9_]+|voice_stt_unavailable)'\)", src))
    assert referenced, "no voice keys referenced — the scan is broken, not the locales"
    for key in sorted(referenced):
        found = len(re.findall(r"^\s*%s: " % re.escape(key), i18n, re.M))
        assert found == locale_count, f"{key}: {found} of {locale_count} locales"


def test_voice_endpoints_refetch_on_every_settings_load():
    """A one-shot `_wired` guard left the PREVIOUS profile's endpoints on screen.

    Voice endpoints live in the active profile's config.yaml. Wiring and
    fetching behind the same guard meant a profile switch redisplayed stale base
    URLs and `api_key_set` flags — and saving from that form wrote them into the
    new profile. Handlers stay one-shot; the fetch does not.
    """
    panels = (_STATIC / "panels.js").read_text(encoding="utf-8")
    start = panels.index("function _wireVoiceEndpoints(")
    body = panels[start:panels.index("\n    // Populate voice selector", start)]
    guard = body.index("box._wired=true")
    refresh_call = body.rindex("refresh();")
    assert refresh_call > guard, "refresh() must run outside the one-shot guard"
    assert "if(!box._wired)" in body, "handler installation must still be one-shot"
    # The guard must not short-circuit the whole function any more.
    assert "if(!box || box._wired) return;" not in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_a_second_click_during_the_capability_probe_does_not_start_twice():
    """Activation is async, so the click handler's `_voiceModeActive` check no
    longer covers start-up.

    While the capability probe is awaited the flag is still false, so a second
    click ran the whole activation body a SECOND time. The two flows then raced
    over one set of module-level capture handles: `_vmStream`, `_vmRecorder` and
    `_vmMaxTimer` were overwritten by the later flow, leaving the earlier
    MediaStream with no reachable reference — nothing could stop it, so the
    microphone stayed open with the tab indicator lit for the rest of the
    session. They also shared `_vmSttFailures`, so the bounded-retry contract
    tripped on interleaved counts from two flows.

    Found by adversarial review; introduced by this round's own async change.
    """
    observed = _run_voice_harness(
        has_sr=False,
        capability=True,
        # STT must SUCCEED here, or the bounded-retry deactivation would end the
        # run for us and the assertions would hold with the race still present.
        transcribe_ok=True,
        epilogue="""
        (async () => {
          // Two clicks in the same tick — the second lands while the first is
          // still awaiting the capability probe.
          const first = elements.btnVoiceMode.onclick();
          const second = elements.btnVoiceMode.onclick();
          await first; await second;
          await drain();
          console.log(JSON.stringify({
            activations: toasts.filter((m) => m === 'voice_mode_active').length,
            orphanedStreams: streams.filter((s) => !s.stopped).length,
          }));
        })();
        """,
    )
    assert observed["activations"] == 1, "the activation body ran more than once"
    assert observed["orphanedStreams"] == 0, "a MediaStream was left running with no way to stop it"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_deactivating_while_the_probe_is_in_flight_does_not_resurrect_voice_mode():
    """The other half of the same async window.

    `_deactivate()` (the user, or the Preferences toggle switching the feature
    off) can land while an activation is still awaiting its capability probe.
    Without the generation check the awaited activation resumed afterwards and
    turned voice mode back on — mic open, against the user's last instruction.
    """
    observed = _run_voice_harness(
        has_sr=False,
        capability=True,
        transcribe_ok=True,
        epilogue="""
        (async () => {
          const starting = elements.btnVoiceMode.onclick();
          // Switch it off again before the probe resolves.
          window._voiceModeDeactivate();
          await starting;
          await drain();
          console.log(JSON.stringify({
            active: window._voiceModeActive(),
            orphanedStreams: streams.filter((s) => !s.stopped).length,
          }));
        })();
        """,
    )
    assert observed["active"] is False, "a superseded activation turned voice mode back on"
    assert observed["orphanedStreams"] == 0


@pytest.mark.parametrize(
    "before,after,same",
    [
        ("https://tts.example/v1", "https://tts.example:443/v1", True),
        ("http://box:80/v1", "http://box/v1", True),
        ("http://Box.Local:7036/v1", "http://box.local:7036/v1/", True),
        ("https://tts.example/v1", "https://tts.example/v2", True),
        ("https://tts.example/v1", "https://other.example/v1", False),
        ("https://tts.example/v1", "http://tts.example/v1", False),
        ("http://box:7036/v1", "http://box:7037/v1", False),
    ],
)
def test_endpoint_identity_ignores_only_what_does_not_change_the_server(before, after, same):
    """What counts as "the same endpoint" for the key-retention rule.

    Too strict and an operator loses their key for adding a trailing slash or
    an explicit :443; too loose and a key travels to a genuinely different
    server. Scheme, host and effective port decide; path and case do not.
    """
    assert (vc._base_url_host(before) == vc._base_url_host(after)) is same


# ── Operator boundary: a profile may not grant itself these controls ────────


class TestOperatorControlsAreNotProfileSettable:
    """The voice write gate and the TTS LAN allowlist are deployment posture.

    Both were previously read from the live environment. ``_reload_dotenv()``
    projects the active profile's dotenv file into ``os.environ`` process-wide,
    so a profile could set them — enabling server-config writes and widening
    the SSRF guard to targets of its choosing, for every profile the process
    serves concurrently.
    """

    OPERATOR_KEYS = (
        "HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE",
        "HERMES_WEBUI_TTS_ALLOW_LAN",
        "HERMES_WEBUI_TTS_ALLOW_HOSTS",
    )

    def test_a_profile_dotenv_cannot_enable_any_of_them(self, tmp_path):
        """The reported bypass, end to end through the real projection path."""
        from api.profiles import _reload_dotenv

        home = tmp_path / "profiles" / "tenant"
        home.mkdir(parents=True)
        (home / ".env").write_text(
            "HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE=1\n"
            "HERMES_WEBUI_TTS_ALLOW_LAN=1\n"
            "HERMES_WEBUI_TTS_ALLOW_HOSTS=10.0.0.0/8\n"
            "TENANT_HARMLESS_VALUE=projected\n",
            encoding="utf-8",
        )
        try:
            _reload_dotenv(home)
            # The projection itself must refuse the operator keys...
            for key in self.OPERATOR_KEYS:
                assert os.environ.get(key) is None, f"{key} reached the live environment"
            # ...and it must still work for ordinary profile values.
            assert os.environ.get("TENANT_HARMLESS_VALUE") == "projected"
            # The consumers stay closed.
            assert vc._voice_config_writable() is False
            enabled, networks, names = routes._tts_lan_allowlist()
            assert enabled is False
            assert not networks and not names
            assert routes._tts_addr_in_lan_allowlist("10.1.2.3") is False
        finally:
            for key in (*self.OPERATOR_KEYS, "TENANT_HARMLESS_VALUE"):
                os.environ.pop(key, None)

    def test_a_live_environment_write_does_not_reach_them(self, monkeypatch):
        """Second line of defence: the value is read from the startup snapshot.

        This is what still holds if a future projection path forgets to consult
        the protected/blocked lists.
        """
        for key in self.OPERATOR_KEYS:
            monkeypatch.setenv(key, "1" if key != "HERMES_WEBUI_TTS_ALLOW_HOSTS" else "10.0.0.0/8")
        assert vc._voice_config_writable() is False
        assert routes._tts_lan_allowlist()[0] is False

    def test_the_operator_startup_value_still_enables_them(self):
        """The opt-in must keep working when it comes from the right place."""
        set_operator_env(
            HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE="1",
            HERMES_WEBUI_TTS_ALLOW_LAN="1",
            HERMES_WEBUI_TTS_ALLOW_HOSTS="10.0.0.0/8",
        )
        assert vc._voice_config_writable() is True
        enabled, networks, _names = routes._tts_lan_allowlist()
        assert enabled is True
        assert routes._tts_addr_in_lan_allowlist("10.1.2.3") is True

    def test_a_profile_dotenv_cannot_clear_an_operator_opt_in(self, tmp_path):
        """Altering counts as much as enabling — narrowing hides a real grant."""
        from api.profiles import _reload_dotenv

        set_operator_env(
            HERMES_WEBUI_TTS_ALLOW_LAN="1",
            HERMES_WEBUI_TTS_ALLOW_HOSTS="10.0.0.0/8",
        )
        home = tmp_path / "profiles" / "tenant"
        home.mkdir(parents=True)
        (home / ".env").write_text(
            "HERMES_WEBUI_TTS_ALLOW_LAN=0\n"
            "HERMES_WEBUI_TTS_ALLOW_HOSTS=192.168.0.0/16\n",
            encoding="utf-8",
        )
        try:
            _reload_dotenv(home)
            enabled, _networks, _names = routes._tts_lan_allowlist()
            assert enabled is True, "a profile cleared an operator opt-in"
            assert routes._tts_addr_in_lan_allowlist("10.1.2.3") is True
            assert routes._tts_addr_in_lan_allowlist("192.168.1.5") is False, (
                "a profile substituted its own allowlist"
            )
        finally:
            for key in self.OPERATOR_KEYS:
                os.environ.pop(key, None)

    def test_operator_only_keys_are_in_both_profile_lists(self):
        """The snapshot is defence in depth; these lists are the first line."""
        from api.operator_env import OPERATOR_ONLY_ENV_KEYS
        from api.profiles import _BLOCKED_RUNTIME_ENV_KEYS, _PROTECTED_ENV_KEYS

        assert OPERATOR_ONLY_ENV_KEYS <= set(_PROTECTED_ENV_KEYS)
        assert OPERATOR_ONLY_ENV_KEYS <= set(_BLOCKED_RUNTIME_ENV_KEYS)

    def test_the_gateway_parity_projection_drops_them(self):
        from api.profiles import filter_runtime_env_for_gateway_parity

        out = filter_runtime_env_for_gateway_parity({
            "HERMES_WEBUI_TTS_ALLOW_LAN": "1",
            "HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE": "1",
            "TENANT_HARMLESS_VALUE": "ok",
        })
        assert "HERMES_WEBUI_TTS_ALLOW_LAN" not in out
        assert "HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE" not in out
        assert out["TENANT_HARMLESS_VALUE"] == "ok"

    def test_the_snapshot_module_refuses_undeclared_keys(self):
        """A caller must not route an ordinary setting through this module."""
        from api.operator_env import operator_env

        with pytest.raises(KeyError):
            operator_env("OPENAI_API_KEY")


# ── Re-gate 2026-07-26: lifecycle ownership and shared TTS resolution ───────


def test_recorder_and_transcription_are_lifecycle_owned():
    """Blocker 2: old callbacks must not consume the new lifecycle.

    `MediaRecorder.ondataavailable/onstop` closed over no owner, so a recorder
    from a deactivated listen pushed audio into the new chunk buffer and its
    onstop tore down the new stream. The transcription flow checked only
    `_voiceModeActive` after its awaits, so an old transcript could be typed
    into and sent from a lifecycle that started after the recording was made.
    """
    boot = (_STATIC / "boot.js").read_text(encoding="utf-8")
    assert "_vmRecorder.ondataavailable=(ev)=>{\n      if(gen!==_vmListenGen) return;" in boot
    assert "_vmRecorder.onstop=()=>{ _vmFinishServerUtterance(gen); };" in boot
    assert "async function _vmFinishServerUtterance(listenGen){" in boot
    assert "const _stillOurs=()=>" in boot
    finish = boot[boot.index("async function _vmFinishServerUtterance("):][:4000]
    # No resume point in the transcription flow may re-arm on the bare flag.
    assert "if(_voiceModeActive) _startListening(); },250);" not in finish
    assert "if(_voiceModeActive) _startListening(); },800);" not in finish


def test_browser_recognition_is_owned_and_bounded():
    """Blocker 2, browser leg: callbacks were unowned and retries unbounded."""
    boot = (_STATIC / "boot.js").read_text(encoding="utf-8")
    start = boot.index("  function _startListening(){")
    body = boot[start:start + 5200]
    assert "const gen=++_vmListenGen;" in body
    assert "const _ours=()=>gen===_vmListenGen&&_voiceModeActive;" in body
    for handler in ("_recognition.onresult", "_recognition.onend", "_recognition.onerror"):
        idx = body.index(handler)
        assert "if(!_ours()) return;" in body[idx:idx + 260], f"{handler} is unowned"
    # `network` and a synchronous start() failure retried forever.
    assert body.count("_vmNoteSttFailure()") >= 2, "browser-leg retries are still unbounded"
    # ...but an idle no-speech/aborted outcome must not consume the budget.
    nospeech = body[body.index("event.error==='no-speech'"):][:400]
    assert "_vmNoteSttFailure" not in nospeech


def test_activation_resolves_the_listening_leg_first():
    """Blocker 4: with a browser recognizer the server probe was fire-and-forget,
    so the first utterance could go to the cloud recognizer while the configured
    self-hosted probe was still in flight."""
    boot = (_STATIC / "boot.js").read_text(encoding="utf-8")
    act = boot[boot.index("  async function _activate(){"):][:3000]
    assert "const _sttReady=await _probeVoiceServerStt();" in act
    assert "_probeVoiceServerStt();\n    }" not in act, "a fire-and-forget probe remains"


def test_tts_capability_shares_the_route_resolution(monkeypatch):
    """Blocker 5: the probe answered from a different implementation.

    It asked the optional Agent helper while `/api/tts` runs the WebUI's own
    engines, so it could promise a leg the route then refused with 503.
    """
    import api.routes as _routes
    import api.voice_config as _vc

    assert callable(getattr(_routes, "tts_engine_available", None))
    src = __import__("inspect").getsource(_vc._tts_provider_capability)
    assert "tts_engine_available" in src
    assert "check_tts_requirements" not in src, "the divergent helper is still consulted"

    # browser is client-side only and can never be a server leg.
    assert _routes.tts_engine_available("browser") is False
    assert _routes.tts_engine_available("nonsense-engine") is False

    # elevenlabs depends on the same key the route requires.
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    monkeypatch.setattr(_routes, "_load_env_file", lambda *_a, **_k: {}, raising=False)
    assert _routes.tts_engine_available("elevenlabs") is False
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")
    assert _routes.tts_engine_available("elevenlabs") is True


def test_tts_capability_reports_a_per_engine_map(monkeypatch):
    """A client whose selected engine differs from the configured default has to
    be able to ask about the engine it will actually call."""
    import api.config as _config
    import api.voice_config as _vc

    monkeypatch.setattr(_config, "get_config", lambda: {"tts": {"provider": "edge"}})
    available, provider, engines = _vc._tts_provider_capability()
    assert provider == "edge"
    assert set(engines) == {"edge", "openai", "elevenlabs"}
    assert isinstance(available, bool)
