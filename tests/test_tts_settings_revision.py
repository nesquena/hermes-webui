"""Revisioned exact-sparse speech settings persistence."""

from __future__ import annotations

import json

import pytest

import api.config as config


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    monkeypatch.setattr(config, "_SETTINGS_WRITE_VERSION", 0)
    return path


def test_revisioned_engine_update_preserves_absent_voice_and_unrelated_values(
    isolated_settings,
):
    isolated_settings.write_text(
        json.dumps({"tts_engine": "edge", "theme": "dark"}), encoding="utf-8"
    )

    before = config.speech_settings_snapshot()
    result = config.set_tts_engine_revisioned(before["revision"], "agent")
    stored = json.loads(isolated_settings.read_text(encoding="utf-8"))

    assert before == {
        "revision": 0,
        "values": {"tts_engine": "edge"},
        "present_keys": ["tts_engine"],
    }
    assert stored == {"tts_engine": "agent", "theme": "dark"}
    assert result["revision"] == 1
    assert result["values"] == {"tts_engine": "agent"}
    assert "tts_voice" not in result["values"]


def test_revisioned_engine_update_preserves_present_voice_exactly(isolated_settings):
    isolated_settings.write_text(
        json.dumps(
            {
                "tts_engine": "openai",
                "tts_voice": "legacy-browser-voice",
                "tts_rate": 1.25,
            }
        ),
        encoding="utf-8",
    )

    result = config.set_tts_engine_revisioned(0, "agent")

    assert result["values"] == {
        "tts_engine": "agent",
        "tts_voice": "legacy-browser-voice",
        "tts_rate": 1.25,
    }


def test_revision_conflict_does_not_write(isolated_settings):
    initial = '{"tts_engine":"edge"}'
    isolated_settings.write_text(initial, encoding="utf-8")

    with pytest.raises(RuntimeError, match="settings_conflict"):
        config.set_tts_engine_revisioned(99, "agent")

    assert isolated_settings.read_text(encoding="utf-8") == initial


def test_normal_settings_save_advances_shared_revision(isolated_settings):
    assert config.speech_settings_snapshot()["revision"] == 0

    config.save_settings({"tts_engine": "browser"})

    snapshot = config.speech_settings_snapshot()
    assert snapshot["revision"] == 1
    assert snapshot["values"]["tts_engine"] == "browser"


def test_revisioned_settings_merge_rejects_stale_autosave(isolated_settings):
    config.save_settings_revisioned(0, {"tts_engine": "agent", "theme": "dark"})

    with pytest.raises(RuntimeError, match="settings_conflict"):
        config.save_settings_revisioned(0, {"tts_engine": "browser"})

    stored = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert stored["tts_engine"] == "agent"
    assert stored["theme"] == "dark"
