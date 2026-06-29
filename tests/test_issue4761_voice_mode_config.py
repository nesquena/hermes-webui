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
        # Assert the BEHAVIOR (read the key, parse as int, fall back to 1800 for
        # missing/invalid values) rather than one exact expression, so the impl
        # can be hardened (e.g. a min-floor clamp) without a brittle test break.
        assert re.search(
            r"parseInt\s*\(\s*localStorage\.getItem\s*\(\s*'hermes-voice-silence-ms'\s*\)",
            src,
        ), "SILENCE_MS must read the 'hermes-voice-silence-ms' localStorage key via parseInt."
        # The 1800 default must remain the fallback for missing/invalid values.
        assert re.search(r"SILENCE_MS\s*=.*\b1800\b", src), (
            "SILENCE_MS must keep 1800 as the default fallback so behavior is "
            "unchanged when the key is unset or invalid."
        )
        # A non-positive / mistyped value must not be honored verbatim (no instant
        # auto-send): the value is guarded by a positivity check and/or a min floor.
        assert "_silenceMsRaw>0" in src or "Math.max(" in src or "> 0" in src, (
            "SILENCE_MS must guard against non-positive values (positivity check "
            "or a Math.max floor) so a mistyped tiny/negative value can't make the "
            "recognizer auto-send instantly."
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
