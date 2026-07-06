"""Tests for TTS delegation to hermes-agent's text_to_speech_tool.

After the unification, ALL TTS providers (edge, mistral, openai, etc.)
go through the agent's text_to_speech_tool — no separate code paths.
The provider is selected via tts.provider in config.yaml.
"""
import base64
import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.routes as routes


class _FakeHandler:
    def __init__(self, body: bytes, command="POST", headers=None, client="1.2.3.4"):
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


def _reset_limiter():
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


def _patch_tool(monkeypatch, fake_tts, *, with_check=True):
    """Patch text_to_speech_tool in both sys.modules and api.routes."""
    from tests._tts_helpers import mock_text_to_speech_tool
    if with_check:
        # Wrap to add check_tts_requirements to the namespace
        original = fake_tts

        def wrapper(*args, **kwargs):
            return original(*args, **kwargs)

        # Use the shared helper, then add check_tts_requirements
        import api.routes as routes
        import sys
        from types import SimpleNamespace
        tool = SimpleNamespace(
            text_to_speech_tool=original,
            check_tts_requirements=lambda: True,
        )
        monkeypatch.setitem(sys.modules, "tools.tts_tool", tool)
        monkeypatch.setattr(routes, "text_to_speech_tool", original, raising=False)
    else:
        mock_text_to_speech_tool(monkeypatch, side_effect=fake_tts)


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    from tests._tts_helpers import install_fake_hermes_cli
    install_fake_hermes_cli(monkeypatch)
    import api.auth as _auth
    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    _reset_limiter()
    yield
    _reset_limiter()


def test_tts_get_capability_dispatch_uses_readonly_profile_scope(monkeypatch):
    """Capability discovery reads the active profile config/env, not default."""
    calls = []

    @contextmanager
    def fake_scope(purpose, logger_override=None):
        calls.append(purpose)
        yield

    monkeypatch.setattr("api.profiles.profile_env_for_active_request_readonly", fake_scope)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *a, **k: True)
    monkeypatch.setattr(routes, "_handle_tts_capability", lambda handler: True)

    parsed = SimpleNamespace(path="/api/tts/capability", query="")
    assert routes.handle_get(SimpleNamespace(), parsed) is True
    assert calls == ["/api/tts/capability"]


def test_tts_post_dispatch_uses_profile_scope(monkeypatch):
    """TTS synthesis and config writes run under the active request profile."""
    calls = []

    @contextmanager
    def fake_scope(purpose, logger_override=None):
        calls.append(purpose)
        yield

    monkeypatch.setattr("api.profiles.profile_env_for_active_request", fake_scope)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *a, **k: False)
    monkeypatch.setattr(routes, "_handle_tts", lambda handler, parsed: True)
    monkeypatch.setattr(routes, "_handle_tts_provider", lambda handler, parsed: True)
    monkeypatch.setattr(routes, "_handle_tts_edge_voice", lambda handler, parsed: True)

    for path in ("/api/tts", "/api/tts/provider", "/api/tts/edge/voice"):
        parsed = SimpleNamespace(path=path, query="")
        assert routes.handle_post(SimpleNamespace(headers={}), parsed) is True

    assert calls == ["/api/tts", "/api/tts/provider", "/api/tts/edge/voice"]


def test_profile_runtime_env_pins_config_path_to_profile_home(tmp_path):
    """Profile-scoped TTS/tool calls should not inherit default HERMES_CONFIG_PATH."""
    from api.profiles import get_profile_runtime_env

    home = tmp_path / "profiles" / "voice"
    home.mkdir(parents=True)
    env = get_profile_runtime_env(home)
    assert "HERMES_CONFIG_PATH" in env
    assert env["HERMES_CONFIG_PATH"] == str(home / "config.yaml")


def test_delegation_returns_base64_json(monkeypatch, tmp_path):
    """TTS delegates to text_to_speech_tool and returns JSON with base64 data URL."""
    audio_data = b"\xff\xfb\x90" * 100
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(audio_data)
    output_paths = []

    def fake_tts(text, output_path=None):
        if output_path:
            output_paths.append(output_path)
            with open(output_path, "wb") as f:
                f.write(audio_data)
        return json.dumps({
            "success": True,
            "file_path": str(audio_file),
            "provider": "mistral",
        })

    _patch_tool(monkeypatch, fake_tts)

    h = _post({"text": "hello world"}, client="5.0.0.1")
    routes._handle_tts(h, None)

    assert h.status == 200
    p = h.payload()
    assert p is not None
    assert "audio" in p
    assert p["audio"].startswith("data:audio/mpeg;base64,")
    assert p["content_type"] == "audio/mpeg"
    assert p["size"] == len(audio_data)
    b64_part = p["audio"].split(",", 1)[1]
    assert base64.b64decode(b64_part) == audio_data
    assert output_paths, "handler should pass an output_path to the agent tool"
    assert not Path(output_paths[0]).exists(), "temporary output_path should be cleaned up"
    assert not audio_file.exists(), "returned file_path should be cleaned up when distinct"


def test_delegation_preserves_non_mp3_content_type(monkeypatch, tmp_path):
    """Returned audio file extension controls the data URL MIME type."""
    audio_data = b"OggS" + (b"\x00" * 32)
    audio_file = tmp_path / "clip.ogg"
    audio_file.write_bytes(audio_data)

    def fake_tts(text, output_path=None):
        if output_path:
            Path(output_path).write_bytes(b"fallback mp3 data")
        return json.dumps({
            "success": True,
            "file_path": str(audio_file),
            "provider": "custom",
        })

    _patch_tool(monkeypatch, fake_tts)

    h = _post({"text": "hello"}, client="5.0.0.9")
    routes._handle_tts(h, None)

    assert h.status == 200
    p = h.payload()
    assert p is not None
    assert p["content_type"] == "audio/ogg"
    assert p["audio"].startswith("data:audio/ogg;base64,")
    assert base64.b64decode(p["audio"].split(",", 1)[1]) == audio_data
    assert not audio_file.exists()


def test_tts_content_type_helper_covers_common_agent_formats():
    """Agent/plugin providers can return more than MP3/WAV."""
    cases = {
        "speech.mp3": "audio/mpeg",
        "speech.mpeg": "audio/mpeg",
        "speech.wav": "audio/wav",
        "speech.ogg": "audio/ogg",
        "speech.opus": "audio/ogg",
        "speech.aac": "audio/aac",
        "speech.flac": "audio/flac",
        "speech.m4a": "audio/mp4",
        "speech.webm": "audio/webm",
        "speech": "audio/mpeg",
    }
    for filename, content_type in cases.items():
        assert routes._tts_content_type_for_path(filename) == content_type


def test_delegation_works_for_edge_provider(monkeypatch, tmp_path):
    """Edge TTS also goes through the agent delegation (no separate path)."""
    audio_data = b"\x00" * 50
    audio_file = tmp_path / "edge.mp3"
    audio_file.write_bytes(audio_data)

    def fake_tts(text, output_path=None):
        return json.dumps({
            "success": True,
            "file_path": str(audio_file),
            "provider": "edge",
        })

    _patch_tool(monkeypatch, fake_tts)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"tts": {"provider": "edge"}},
        raising=False,
    )

    h = _post({"text": "hello"}, client="5.0.0.2")
    routes._handle_tts(h, None)

    assert h.status == 200
    p = h.payload()
    assert p is not None
    assert p["audio"].startswith("data:audio/mpeg;base64,")


def test_delegation_returns_503_when_agent_missing(monkeypatch):
    """When text_to_speech_tool can't be imported, TTS returns 503."""
    # Remove from sys.modules and make the import fail
    monkeypatch.delitem(sys.modules, "tools.tts_tool", raising=False)
    import builtins
    real_import = builtins.__import__

    def _mock_import(name, *args, **kwargs):
        if name == "tools.tts_tool":
            raise ImportError("no agent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _mock_import)

    h = _post({"text": "hello"}, client="5.0.0.3")
    routes._handle_tts(h, None)

    assert h.status == 503
    assert "unavailable" in (h.payload() or {}).get("error", "").lower()


def test_delegation_returns_500_on_tts_failure(monkeypatch):
    """When text_to_speech_tool returns success=False, TTS returns 500."""
    def fake_tts(text, output_path=None):
        return json.dumps({"success": False, "error": "provider timeout"})

    _patch_tool(monkeypatch, fake_tts)

    h = _post({"text": "hello"}, client="5.0.0.4")
    routes._handle_tts(h, None)

    assert h.status == 500
    assert "provider timeout" in (h.payload() or {}).get("error", "")


def test_delegation_handles_provider_in_response(monkeypatch, tmp_path):
    """TTS works regardless of which provider the agent uses."""
    audio_data = b"x" * 32
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(audio_data)

    def fake_tts(text, output_path=None):
        return json.dumps({
            "success": True,
            "file_path": str(audio_file),
            "provider": "openai",  # arbitrary provider
        })

    _patch_tool(monkeypatch, fake_tts)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"tts": {"provider": "openai"}},
        raising=False,
    )

    h = _post({"text": "hello"}, client="5.0.0.5")
    routes._handle_tts(h, None)
    assert h.status == 200
