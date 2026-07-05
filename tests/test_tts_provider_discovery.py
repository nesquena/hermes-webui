"""Tests for individual TTS provider auto-discovery in /api/tts/capability.

Each provider has its own check (library importable + credentials present).
These tests verify the auto-discovery logic for all 10 providers:

  edge, mistral, openai, elevenlabs, neutts, kittentts, piper,
  gemini, minimax, xai

For each provider we test two paths:
  1. Provider appears in available_providers when its check passes
  2. Provider does NOT appear when its check fails
"""
import io
import json
import sys
from types import SimpleNamespace

import api.routes as routes
from tests._tts_helpers import install_fake_hermes_cli


# Path setup — api/config.py already adds the agent dir to sys.path at
# module import time via _discover_agent_dir(), so we don't need to do it
# again here. The mock below patches sys.modules['tools.tts_tool'] directly.

import pytest


@pytest.fixture(autouse=True)
def _setup_hermes_cli(monkeypatch):
    install_fake_hermes_cli(monkeypatch)
    yield


class _FakeHandler:
    def __init__(self, command="GET", headers=None, client="127.0.0.1"):
        self.command = command
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.headers = headers or {}
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


def _make_tts_tool_module(**overrides):
    """Create a fake tools.tts_tool module with stubbed provider checks.

    Each provider's check function defaults to returning False (not available).
    Pass overrides={'mistral': True} to make a specific provider available.
    """
    defaults = {
        "check_tts_requirements": lambda: True,
        "_import_mistral_client": lambda: None,
        "_import_openai_client": lambda: None,
        "_has_openai_audio_backend": lambda: False,
        "_import_elevenlabs": lambda: None,
        "_check_neutts_available": lambda: False,
        "_check_kittentts_available": lambda: False,
        "_check_piper_available": lambda: False,
        "get_env_value": lambda key: "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _setup_environ(monkeypatch, env_vars):
    """Set env vars via monkeypatch and clear them after the test."""
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)


def _call_capability(monkeypatch, tts_tool_module, env_vars=None, config=None):
    """Call _handle_tts_capability with mocked config and tts_tool module.

    Args:
        config: Optional config dict to use. Defaults to edge with empty voice.
    """
    if env_vars is None:
        env_vars = {}
    if config is None:
        config = {"tts": {"provider": "edge", "edge": {"voice": ""}}}
    _setup_environ(monkeypatch, env_vars)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: config,
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "tools.tts_tool", tts_tool_module)
    # Mock xai_http import path used by the handler
    fake_xai = SimpleNamespace(resolve_xai_http_credentials=lambda: {"api_key": ""})
    monkeypatch.setitem(sys.modules, "tools.xai_http", fake_xai)

    h = _FakeHandler()
    routes._handle_tts_capability(h)
    return h.payload()


# ── Edge TTS ──────────────────────────────────────────────────────────


def test_edge_appears_when_edge_tts_importable(monkeypatch):
    """Edge TTS appears when the edge_tts package is importable."""
    # Create a fake edge_tts module that's importable
    fake_edge_tts = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge_tts)

    tts_tool = _make_tts_tool_module()
    p = _call_capability(monkeypatch, tts_tool)
    assert "edge" in p["available_providers"]


def test_edge_absent_when_edge_tts_not_importable(monkeypatch):
    """Edge TTS does NOT appear when edge_tts import fails."""
    # Remove edge_tts from sys.modules and make import raise
    monkeypatch.delitem(sys.modules, "edge_tts", raising=False)
    # Patch the import to raise ImportError
    import builtins
    real_import = builtins.__import__

    def _mock_import(name, *args, **kwargs):
        if name == "edge_tts":
            raise ImportError("no edge_tts")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _mock_import)

    tts_tool = _make_tts_tool_module()
    p = _call_capability(monkeypatch, tts_tool)
    assert "edge" not in p["available_providers"]


# ── Mistral ───────────────────────────────────────────────────────────


def test_mistral_appears_when_api_key_set(monkeypatch):
    """Mistral appears when MISTRAL_API_KEY is set."""
    tts_tool = _make_tts_tool_module(
        get_env_value=lambda key: "test-key" if key == "MISTRAL_API_KEY" else ""
    )
    p = _call_capability(monkeypatch, tts_tool, {"MISTRAL_API_KEY": "test-key"})
    assert "mistral" in p["available_providers"]


def test_mistral_absent_when_no_api_key(monkeypatch):
    """Mistral does NOT appear when MISTRAL_API_KEY is not set."""
    tts_tool = _make_tts_tool_module(
        get_env_value=lambda key: "" if key == "MISTRAL_API_KEY" else ""
    )
    p = _call_capability(monkeypatch, tts_tool)
    assert "mistral" not in p["available_providers"]


# ── OpenAI ────────────────────────────────────────────────────────────


def test_openai_appears_when_audio_backend_available(monkeypatch):
    """OpenAI appears when _has_openai_audio_backend returns True."""
    tts_tool = _make_tts_tool_module(_has_openai_audio_backend=lambda: True)
    p = _call_capability(monkeypatch, tts_tool)
    assert "openai" in p["available_providers"]


def test_openai_absent_when_no_audio_backend(monkeypatch):
    """OpenAI does NOT appear when _has_openai_audio_backend is False."""
    tts_tool = _make_tts_tool_module(_has_openai_audio_backend=lambda: False)
    p = _call_capability(monkeypatch, tts_tool)
    assert "openai" not in p["available_providers"]


# ── ElevenLabs ────────────────────────────────────────────────────────


def test_elevenlabs_appears_when_api_key_set(monkeypatch):
    """ElevenLabs appears when ELEVENLABS_API_KEY is set."""
    tts_tool = _make_tts_tool_module(
        get_env_value=lambda key: "test-key" if key == "ELEVENLABS_API_KEY" else ""
    )
    p = _call_capability(monkeypatch, tts_tool, {"ELEVENLABS_API_KEY": "test-key"})
    assert "elevenlabs" in p["available_providers"]


def test_elevenlabs_absent_when_no_api_key(monkeypatch):
    """ElevenLabs does NOT appear when ELEVENLABS_API_KEY is not set."""
    tts_tool = _make_tts_tool_module(
        get_env_value=lambda key: "" if key == "ELEVENLABS_API_KEY" else ""
    )
    p = _call_capability(monkeypatch, tts_tool)
    assert "elevenlabs" not in p["available_providers"]


# ── Local providers (NeuTTS, KittenTTS, Piper) ───────────────────────


def test_neutts_appears_when_available(monkeypatch):
    """NeuTTS appears when _check_neutts_available returns True."""
    tts_tool = _make_tts_tool_module(_check_neutts_available=lambda: True)
    p = _call_capability(monkeypatch, tts_tool)
    assert "neutts" in p["available_providers"]


def test_neutts_absent_when_not_available(monkeypatch):
    """NeuTTS does NOT appear when _check_neutts_available is False."""
    tts_tool = _make_tts_tool_module(_check_neutts_available=lambda: False)
    p = _call_capability(monkeypatch, tts_tool)
    assert "neutts" not in p["available_providers"]


def test_kittentts_appears_when_available(monkeypatch):
    """KittenTTS appears when _check_kittentts_available returns True."""
    tts_tool = _make_tts_tool_module(_check_kittentts_available=lambda: True)
    p = _call_capability(monkeypatch, tts_tool)
    assert "kittentts" in p["available_providers"]


def test_kittentts_absent_when_not_available(monkeypatch):
    """KittenTTS does NOT appear when _check_kittentts_available is False."""
    tts_tool = _make_tts_tool_module(_check_kittentts_available=lambda: False)
    p = _call_capability(monkeypatch, tts_tool)
    assert "kittentts" not in p["available_providers"]


def test_piper_appears_when_available(monkeypatch):
    """Piper appears when _check_piper_available returns True."""
    tts_tool = _make_tts_tool_module(_check_piper_available=lambda: True)
    p = _call_capability(monkeypatch, tts_tool)
    assert "piper" in p["available_providers"]


def test_piper_absent_when_not_available(monkeypatch):
    """Piper does NOT appear when _check_piper_available is False."""
    tts_tool = _make_tts_tool_module(_check_piper_available=lambda: False)
    p = _call_capability(monkeypatch, tts_tool)
    assert "piper" not in p["available_providers"]


# ── Gemini ────────────────────────────────────────────────────────────


def test_gemini_appears_when_gemini_key_set(monkeypatch):
    """Gemini appears when GEMINI_API_KEY is set."""
    tts_tool = _make_tts_tool_module(
        get_env_value=lambda key: "test-key" if key == "GEMINI_API_KEY" else ""
    )
    p = _call_capability(monkeypatch, tts_tool, {"GEMINI_API_KEY": "test-key"})
    assert "gemini" in p["available_providers"]


def test_gemini_appears_when_google_key_set(monkeypatch):
    """Gemini also appears when GOOGLE_API_KEY is set (alternate env var)."""
    tts_tool = _make_tts_tool_module(
        get_env_value=lambda key: "test-key" if key == "GOOGLE_API_KEY" else ""
    )
    p = _call_capability(monkeypatch, tts_tool, {"GOOGLE_API_KEY": "test-key"})
    assert "gemini" in p["available_providers"]


def test_gemini_absent_when_no_keys(monkeypatch):
    """Gemini does NOT appear when neither GEMINI_API_KEY nor GOOGLE_API_KEY is set."""
    tts_tool = _make_tts_tool_module(
        get_env_value=lambda key: "" if key in ("GEMINI_API_KEY", "GOOGLE_API_KEY") else ""
    )
    p = _call_capability(monkeypatch, tts_tool)
    assert "gemini" not in p["available_providers"]


# ── MiniMax ───────────────────────────────────────────────────────────


def test_minimax_appears_when_key_set(monkeypatch):
    """MiniMax appears when MINIMAX_API_KEY is set."""
    tts_tool = _make_tts_tool_module(
        get_env_value=lambda key: "test-key" if key == "MINIMAX_API_KEY" else ""
    )
    p = _call_capability(monkeypatch, tts_tool, {"MINIMAX_API_KEY": "test-key"})
    assert "minimax" in p["available_providers"]


def test_minimax_absent_when_no_key(monkeypatch):
    """MiniMax does NOT appear when MINIMAX_API_KEY is not set."""
    tts_tool = _make_tts_tool_module(
        get_env_value=lambda key: "" if key == "MINIMAX_API_KEY" else ""
    )
    p = _call_capability(monkeypatch, tts_tool)
    assert "minimax" not in p["available_providers"]


# ── xAI ───────────────────────────────────────────────────────────────


def test_xai_appears_when_credentials_resolved(monkeypatch):
    """xAI appears when resolve_xai_http_credentials returns an api_key."""
    fake_xai = SimpleNamespace(
        resolve_xai_http_credentials=lambda: {"api_key": "test-key"}
    )
    monkeypatch.setitem(sys.modules, "tools.xai_http", fake_xai)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"tts": {"provider": "edge", "edge": {"voice": ""}}},
        raising=False,
    )
    tts_tool = _make_tts_tool_module()
    monkeypatch.setitem(sys.modules, "tools.tts_tool", tts_tool)

    h = _FakeHandler()
    routes._handle_tts_capability(h)
    p = h.payload()
    assert "xai" in p["available_providers"]


def test_xai_absent_when_no_credentials(monkeypatch):
    """xAI does NOT appear when resolve_xai_http_credentials returns no api_key."""
    fake_xai = SimpleNamespace(resolve_xai_http_credentials=lambda: {"api_key": ""})
    monkeypatch.setitem(sys.modules, "tools.xai_http", fake_xai)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"tts": {"provider": "edge", "edge": {"voice": ""}}},
        raising=False,
    )
    tts_tool = _make_tts_tool_module()
    monkeypatch.setitem(sys.modules, "tools.tts_tool", tts_tool)

    h = _FakeHandler()
    routes._handle_tts_capability(h)
    p = h.payload()
    assert "xai" not in p["available_providers"]


# ── config_voice field ──────────────────────────────────────────────


def test_config_voice_returned_for_edge_provider(monkeypatch):
    """config_voice is populated when tts.edge.voice is set in config."""
    tts_tool = _make_tts_tool_module()
    p = _call_capability(
        monkeypatch, tts_tool,
        config={"tts": {"provider": "edge", "edge": {"voice": "en-US-AriaNeural"}}}
    )
    assert p["config_voice"] == "en-US-AriaNeural"


def test_config_voice_empty_when_edge_voice_unset(monkeypatch):
    """config_voice is empty string when tts.edge.voice is not set."""
    tts_tool = _make_tts_tool_module()
    p = _call_capability(
        monkeypatch, tts_tool,
        config={"tts": {"provider": "edge", "edge": {}}}
    )
    assert p["config_voice"] == ""


def test_config_voice_empty_for_non_edge_provider(monkeypatch):
    """config_voice is empty when provider is not edge (only edge has config_voice)."""
    tts_tool = _make_tts_tool_module()
    p = _call_capability(
        monkeypatch, tts_tool,
        config={"tts": {"provider": "mistral", "edge": {"voice": "en-US-AriaNeural"}}}
    )
    assert p["config_voice"] == ""


# ── Combined scenarios ────────────────────────────────────────────────


def test_all_providers_appear_when_all_available(monkeypatch):
    """All 10 providers appear when every check passes."""
    fake_edge_tts = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge_tts)
    tts_tool = _make_tts_tool_module(
        get_env_value=lambda key: "test-key" if key in (
            "MISTRAL_API_KEY", "ELEVENLABS_API_KEY",
            "GEMINI_API_KEY", "MINIMAX_API_KEY"
        ) else "",
        _has_openai_audio_backend=lambda: True,
        _check_neutts_available=lambda: True,
        _check_kittentts_available=lambda: True,
        _check_piper_available=lambda: True,
    )
    fake_xai = SimpleNamespace(
        resolve_xai_http_credentials=lambda: {"api_key": "test-key"}
    )
    monkeypatch.setitem(sys.modules, "tools.xai_http", fake_xai)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"tts": {"provider": "edge", "edge": {"voice": ""}}},
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "tools.tts_tool", tts_tool)

    h = _FakeHandler()
    routes._handle_tts_capability(h)
    p = h.payload()

    expected = {"edge", "mistral", "openai", "elevenlabs",
                "neutts", "kittentts", "piper",
                "gemini", "minimax", "xai"}
    assert set(p["available_providers"]) == expected


def test_no_providers_when_all_checks_fail(monkeypatch):
    """No providers appear when every check fails (edge_tts missing, no keys)."""
    # Remove edge_tts to make import fail
    import builtins
    real_import = builtins.__import__

    def _mock_import(name, *args, **kwargs):
        if name == "edge_tts":
            raise ImportError("no edge_tts")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _mock_import)
    monkeypatch.delitem(sys.modules, "edge_tts", raising=False)

    tts_tool = _make_tts_tool_module(
        get_env_value=lambda key: "",
        _has_openai_audio_backend=lambda: False,
        _check_neutts_available=lambda: False,
        _check_kittentts_available=lambda: False,
        _check_piper_available=lambda: False,
    )
    fake_xai = SimpleNamespace(resolve_xai_http_credentials=lambda: {"api_key": ""})
    monkeypatch.setitem(sys.modules, "tools.xai_http", fake_xai)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"tts": {"provider": "edge", "edge": {"voice": ""}}},
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "tools.tts_tool", tts_tool)

    h = _FakeHandler()
    routes._handle_tts_capability(h)
    p = h.payload()
    assert p["available_providers"] == []


def test_failure_of_one_check_does_not_block_others(monkeypatch):
    """If Mistral check fails, other providers can still appear."""
    fake_edge_tts = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge_tts)

    # Make mistral check raise an exception
    def _bad_mistral():
        raise RuntimeError("mistral broken")

    tts_tool = _make_tts_tool_module(
        _import_mistral_client=_bad_mistral,
        get_env_value=lambda key: "",
        _check_neutts_available=lambda: True,  # this one works
    )
    p = _call_capability(monkeypatch, tts_tool)
    # Mistral should not crash the whole endpoint
    assert "mistral" not in p["available_providers"]
    # Other providers should still be discoverable
    assert "edge" in p["available_providers"]
    assert "neutts" in p["available_providers"]
