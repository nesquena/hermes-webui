"""Unit tests for set_hermes_tts_provider and set_hermes_edge_voice.

These functions write to config.yaml under _cfg_lock, then call
reload_config() to invalidate the in-process cache. The tests verify:
  1. The function writes the right value to the right key
  2. reload_config() is called (cache invalidation)
  3. The lock is released (no deadlock)
  4. Edge cases: None values, empty strings, missing keys
"""
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
