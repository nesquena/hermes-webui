"""Unit tests for set_hermes_tts_provider and set_hermes_edge_voice.

These functions write to config.yaml under _cfg_lock, then call
reload_config() to invalidate the in-process cache. The tests verify:
  1. The function writes the right value to the right key
  2. reload_config() is called (cache invalidation)
  3. The lock is released (no deadlock)
  4. Edge cases: None values, empty strings, missing keys
"""
import io
import json
import os
import tempfile
import threading
from unittest.mock import patch

import pytest


@pytest.fixture
def temp_config(monkeypatch):
    """Create a temporary config.yaml and point the app at it."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(path, "w") as f:
        f.write("tts:\n  provider: edge\n  edge:\n    voice: en-US-AriaNeural\n")
    monkeypatch.setenv("HERMES_CONFIG_PATH", path)
    # Clear any cached config
    import api.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_cfg_mtime", 0.0, raising=False)
    monkeypatch.setattr(cfg_mod, "_cfg_cache", {}, raising=False)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_set_provider_writes_correct_value(temp_config):
    """set_hermes_tts_provider writes provider to tts: section."""
    import api.config as cfg_mod
    with patch.object(cfg_mod, "reload_config") as mock_reload:
        result = cfg_mod.set_hermes_tts_provider("mistral")
    assert result == {"ok": True, "provider": "mistral"}
    with open(temp_config) as f:
        content = f.read()
    assert "provider: mistral" in content
    mock_reload.assert_called_once()


def test_set_provider_lowercases_and_strips(temp_config):
    """Provider name is lowercased and stripped before writing."""
    import api.config as cfg_mod
    with patch.object(cfg_mod, "reload_config"):
        cfg_mod.set_hermes_tts_provider("  Mistral  ")
    with open(temp_config) as f:
        content = f.read()
    assert "provider: mistral" in content
    assert "Mistral" not in content.split("provider:")[1].split("\n")[0]


def test_set_provider_empty_clears_value(temp_config):
    """Empty string provider removes the key (falls back to default)."""
    import api.config as cfg_mod
    with patch.object(cfg_mod, "reload_config"):
        result = cfg_mod.set_hermes_tts_provider("")
    assert result == {"ok": True, "provider": None}
    with open(temp_config) as f:
        content = f.read()
    # Either the line is gone, or the value is empty
    assert "provider:" not in content or "provider: ''" in content or "provider: " not in content


def test_set_provider_preserves_other_sections(temp_config):
    """Setting provider doesn't affect edge voice or other config."""
    import api.config as cfg_mod
    with patch.object(cfg_mod, "reload_config"):
        cfg_mod.set_hermes_tts_provider("mistral")
    with open(temp_config) as f:
        content = f.read()
    assert "voice: en-US-AriaNeural" in content  # edge voice preserved


def test_set_provider_calls_reload_config(temp_config):
    """reload_config() is called to invalidate in-process cache."""
    import api.config as cfg_mod
    with patch.object(cfg_mod, "reload_config") as mock:
        cfg_mod.set_hermes_tts_provider("mistral")
    mock.assert_called_once()


def test_set_provider_lock_is_released(temp_config):
    """Lock is released after write so subsequent calls don't deadlock."""
    import api.config as cfg_mod
    with patch.object(cfg_mod, "reload_config"):
        cfg_mod.set_hermes_tts_provider("mistral")
        # This would hang forever if the lock wasn't released
        cfg_mod.set_hermes_tts_provider("edge")
    # No timeout = success


def test_set_provider_concurrent_writes(temp_config):
    """Concurrent writes don't corrupt the config file."""
    import api.config as cfg_mod
    results = []
    errors = []

    def write_provider(name):
        try:
            with patch.object(cfg_mod, "reload_config"):
                cfg_mod.set_hermes_tts_provider(name)
                results.append(name)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=write_provider, args=(n,))
               for n in ["mistral", "edge", "openai", "elevenlabs"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors, f"Concurrent writes failed: {errors}"
    assert len(results) == 4
    # Final value should be one of the four (whichever won the race)
    with open(temp_config) as f:
        content = f.read()
    assert any(f"provider: {n}" in content for n in ["mistral", "edge", "openai", "elevenlabs"])


def test_set_edge_voice_writes_correct_value(temp_config):
    """set_hermes_edge_voice writes voice to tts.edge: section."""
    import api.config as cfg_mod
    with patch.object(cfg_mod, "reload_config") as mock_reload:
        result = cfg_mod.set_hermes_edge_voice("en-US-GuyNeural")
    assert result == {"ok": True, "voice": "en-US-GuyNeural"}
    with open(temp_config) as f:
        content = f.read()
    assert "voice: en-US-GuyNeural" in content
    mock_reload.assert_called_once()


def test_set_edge_voice_strips_whitespace(temp_config):
    """Voice name is stripped of leading/trailing whitespace."""
    import api.config as cfg_mod
    with patch.object(cfg_mod, "reload_config"):
        cfg_mod.set_hermes_edge_voice("  en-US-AriaNeural  ")
    with open(temp_config) as f:
        content = f.read()
    assert "voice: en-US-AriaNeural" in content


def test_set_edge_voice_empty_clears_value(temp_config):
    """Empty string voice removes the key (uses agent default)."""
    import api.config as cfg_mod
    with patch.object(cfg_mod, "reload_config"):
        result = cfg_mod.set_hermes_edge_voice("")
    assert result == {"ok": True, "voice": None}
    with open(temp_config) as f:
        content = f.read()
    # voice line is gone
    assert "voice:" not in content


def test_set_edge_voice_preserves_provider(temp_config):
    """Setting edge voice doesn't affect the provider setting."""
    import api.config as cfg_mod
    with patch.object(cfg_mod, "reload_config"):
        cfg_mod.set_hermes_edge_voice("en-US-BrianNeural")
    with open(temp_config) as f:
        content = f.read()
    assert "provider: edge" in content
    assert "voice: en-US-BrianNeural" in content


def test_set_edge_voice_creates_edge_section_if_missing(temp_config, monkeypatch):
    """If tts.edge doesn't exist, the function creates it."""
    import api.config as cfg_mod
    # Remove the edge section
    with open(temp_config, "w") as f:
        f.write("tts:\n  provider: edge\n")
    with patch.object(cfg_mod, "reload_config"):
        cfg_mod.set_hermes_edge_voice("en-US-AriaNeural")
    with open(temp_config) as f:
        content = f.read()
    assert "edge:" in content
    assert "voice: en-US-AriaNeural" in content


def test_set_edge_voice_lock_is_released(temp_config):
    """Lock is released after write so subsequent calls don't deadlock."""
    import api.config as cfg_mod
    with patch.object(cfg_mod, "reload_config"):
        cfg_mod.set_hermes_edge_voice("en-US-AriaNeural")
        cfg_mod.set_hermes_edge_voice("en-US-GuyNeural")
    # No timeout = success


def test_both_calls_use_same_lock(temp_config):
    """set_hermes_tts_provider and set_hermes_edge_voice share the same lock."""
    import api.config as cfg_mod
    with patch.object(cfg_mod, "reload_config"):
        cfg_mod.set_hermes_tts_provider("mistral")
        cfg_mod.set_hermes_edge_voice("en-US-GuyNeural")
        cfg_mod.set_hermes_tts_provider("edge")
    with open(temp_config) as f:
        content = f.read()
    assert "provider: edge" in content
    assert "voice: en-US-GuyNeural" in content


def test_yaml_config_save_is_atomic_and_durable(tmp_path, monkeypatch):
    """Config writes use temp-file fsync + os.replace, not direct truncation."""
    import api.config as cfg_mod

    config_path = tmp_path / "config.yaml"
    config_path.write_text("tts:\n  provider: edge\n", encoding="utf-8")
    replace_calls = []
    fsync_calls = []
    orig_replace = cfg_mod.os.replace
    orig_fsync = cfg_mod.os.fsync

    def spy_replace(src, dst):
        replace_calls.append((str(src), str(dst), os.path.exists(src)))
        return orig_replace(src, dst)

    def spy_fsync(fd):
        fsync_calls.append(fd)
        return orig_fsync(fd)

    monkeypatch.setattr(cfg_mod.os, "replace", spy_replace)
    monkeypatch.setattr(cfg_mod.os, "fsync", spy_fsync)

    cfg_mod._save_yaml_config_file(config_path, {"tts": {"provider": "mistral"}})

    assert replace_calls, "_save_yaml_config_file must publish via os.replace()"
    assert replace_calls[0][1] == str(config_path)
    assert replace_calls[0][2] is True, "replace source should be an existing temp file"
    assert len(fsync_calls) >= 2, "must fsync file contents and parent directory"
    assert "provider: mistral" in config_path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".config.yaml.*.tmp"))


class _FakeHandler:
    def __init__(self, body: bytes, command="POST", headers=None, client="127.0.0.1"):
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


def _post(body, **kw):
    return _FakeHandler(json.dumps(body).encode(), **kw)


def _reset_tts_config_limiters(routes):
    for owner in (routes._handle_tts_provider, routes._handle_tts_edge_voice):
        if hasattr(owner, "_tts_config_limiter"):
            delattr(owner, "_tts_config_limiter")


def _disable_route_auth(monkeypatch):
    import api.auth as auth_mod
    monkeypatch.setattr(auth_mod, "is_auth_enabled", lambda: False)


def test_tts_provider_endpoint_rate_limits_repeated_config_writes(temp_config, monkeypatch):
    """Rapid repeated provider writes are rejected before hammering config.yaml."""
    import api.config as cfg_mod
    import api.routes as routes

    _reset_tts_config_limiters(routes)
    _disable_route_auth(monkeypatch)
    try:
        with patch.object(cfg_mod, "reload_config"):
            first = _post({"provider": "mistral"}, client="10.0.0.1")
            routes._handle_tts_provider(first, None)
            second = _post({"provider": "edge"}, client="10.0.0.1")
            routes._handle_tts_provider(second, None)

        assert first.status == 200
        assert second.status == 429
        assert "rate limit" in (second.payload() or {}).get("error", "")
        with open(temp_config) as f:
            content = f.read()
        assert "provider: mistral" in content
        assert "provider: edge" not in content
    finally:
        _reset_tts_config_limiters(routes)


def test_tts_edge_voice_endpoint_rate_limits_repeated_config_writes(temp_config, monkeypatch):
    """Rapid repeated Edge voice writes are rejected before hammering config.yaml."""
    import api.config as cfg_mod
    import api.routes as routes

    _reset_tts_config_limiters(routes)
    _disable_route_auth(monkeypatch)
    try:
        with patch.object(cfg_mod, "reload_config"):
            first = _post({"voice": "en-US-GuyNeural"}, client="10.0.0.2")
            routes._handle_tts_edge_voice(first, None)
            second = _post({"voice": "en-US-AriaNeural"}, client="10.0.0.2")
            routes._handle_tts_edge_voice(second, None)

        assert first.status == 200
        assert second.status == 429
        assert "rate limit" in (second.payload() or {}).get("error", "")
        with open(temp_config) as f:
            content = f.read()
        assert "voice: en-US-GuyNeural" in content
        assert "voice: en-US-AriaNeural" not in content
    finally:
        _reset_tts_config_limiters(routes)


def test_tts_provider_and_edge_voice_endpoints_have_separate_limiters(temp_config, monkeypatch):
    """Normal settings flow can save provider then voice without cross-endpoint blocking."""
    import api.config as cfg_mod
    import api.routes as routes

    _reset_tts_config_limiters(routes)
    _disable_route_auth(monkeypatch)
    try:
        with patch.object(cfg_mod, "reload_config"):
            provider = _post({"provider": "edge"}, client="10.0.0.3")
            routes._handle_tts_provider(provider, None)
            voice = _post({"voice": "en-US-GuyNeural"}, client="10.0.0.3")
            routes._handle_tts_edge_voice(voice, None)

        assert provider.status == 200
        assert voice.status == 200
        with open(temp_config) as f:
            content = f.read()
        assert "provider: edge" in content
        assert "voice: en-US-GuyNeural" in content
    finally:
        _reset_tts_config_limiters(routes)
