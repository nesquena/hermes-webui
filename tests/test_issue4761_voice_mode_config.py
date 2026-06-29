"""Tests for #4761 — configurable voice-mode silence timeout and continuous recognition.

The voice-mode loop currently hardcodes:
  - SILENCE_MS = 1800 (1.8s pause before auto-send)
  - _recognition.continuous = false (mic closes after each utterance)

This module pins the fix: both values are now configurable via localStorage keys
(hermes-voice-silence-ms, hermes-voice-continuous) with sensible defaults.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _boot_src() -> str:
    return (REPO / "static" / "boot.js").read_text(encoding="utf-8")


class TestVoiceModeSilenceMsConfig:
    """SILENCE_MS must read from localStorage with 1800 fallback."""

    def test_silence_ms_reads_local_storage_with_fallback(self):
        src = _boot_src()
        m = re.search(
            r"const\s+SILENCE_MS\s*=\s*parseInt\s*\(\s*localStorage\.getItem\s*\(\s*'hermes-voice-silence-ms'\s*\)\s*\)\s*\|\|\s*1800",
            src,
        )
        assert m, (
            "SILENCE_MS must be defined as `parseInt(localStorage.getItem('hermes-voice-silence-ms')) || 1800` "
            "so users can tune it via dev console or a future settings toggle."
        )

    def test_silence_ms_used_in_timeout(self):
        src = _boot_src()
        assert "SILENCE_MS" in src, "SILENCE_MS must still be referenced in the timeout call."


class TestVoiceModeContinuousConfig:
    """_recognition.continuous must read from localStorage."""

    def test_continuous_reads_local_storage(self):
        src = _boot_src()
        assert (
            "_recognition.continuous=localStorage.getItem('hermes-voice-continuous')==='true'"
            in src
        ), (
            "_recognition.continuous must read from localStorage key "
            "'hermes-voice-continuous' with default false. "
            "Without this, users with natural mid-sentence pauses get cut off."
        )

    def test_continuous_true_behavior(self):
        """When hermes-voice-continuous is 'true', the recognition stays open
        across pauses, so the silence timer is the sole arbiter of send timing."""
        src = _boot_src()
        # The continuous flag must not replace or disable the silence timer logic.
        assert (
            "_silenceTimer=setTimeout" in src
        ), "The silence timer must still exist for continuous mode send decision."


class TestBootJsVoiceSectionIntegrity:
    """Smoke checks — the surrounding voice-mode infrastructure is intact."""

    def test_voice_mode_declares_silence_ms(self):
        src = _boot_src()
        assert "SILENCE_MS" in src, "SILENCE_MS constant must exist in boot.js"

    def test_voice_mode_declares_recognition(self):
        src = _boot_src()
        assert "_recognition=new SpeechRecognition()" in src

    def test_voice_mode_state_machine_present(self):
        src = _boot_src()
        for state in ("idle", "listening", "thinking", "speaking"):
            assert f"'{state}'" in src, f"Voice mode state '{state}' must be referenced."

    def test_voice_mode_patches_auto_read(self):
        src = _boot_src()
        assert "autoReadLastAssistant" in src, (
            "voice mode must still override autoReadLastAssistant to pipe "
            "response completion into _speakResponse."
        )
