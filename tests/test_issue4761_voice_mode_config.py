"""Tests for #4761, configurable voice-mode silence timeout and continuous recognition.

The voice-mode loop used to hardcode:
  - a startup silence timeout constant (1.8s pause before auto-send)
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
    """The silence timeout must read from localStorage with 1800 fallback."""

    def test_silence_ms_reads_local_storage_with_fallback(self):
        src = _boot_src()
        # Assert the BEHAVIOR (read the key, parse as int, fall back to 1800 for
        # missing/invalid values) rather than one exact expression, so the impl
        # can be hardened (e.g. a min-floor clamp) without a brittle test break.
        assert re.search(
            r"parseInt\s*\(\s*localStorage\.getItem\s*\(\s*'hermes-voice-silence-ms'\s*\)",
            src,
        ), "Voice mode must read the 'hermes-voice-silence-ms' localStorage key via parseInt."
        # The 1800 default must remain the fallback for missing/invalid values.
        assert re.search(r"return\s*\(.*\)\?Math\.max\(200,_silenceMsRaw\):1800", src), (
            "Voice mode must keep 1800 as the default fallback so behavior is "
            "unchanged when the key is unset or invalid."
        )
        # A non-positive or mistyped value must not be honored verbatim.
        assert "_silenceMsRaw>0" in src or "Math.max(" in src or "> 0" in src, (
            "Voice mode must guard against non-positive values (positivity check "
            "or a Math.max floor) so a mistyped tiny/negative value can't make the "
            "recognizer auto-send instantly."
        )

    def test_silence_ms_read_at_timeout_use_time(self):
        src = _boot_src()
        assert "_voiceSilenceMs()" in src, "The silence timeout must be read when scheduling auto-send."
        assert re.search(r"function _armVoiceSilence\(lease\)[\s\S]*const delay=_voiceSilenceMs\(\)", src), (
            "Voice mode must call _voiceSilenceMs() inside the lease-owned timer so settings apply without reload."
        )


class TestVoiceModeContinuousConfig:
    """_recognition.continuous must read from localStorage."""

    def test_continuous_reads_local_storage(self):
        src = _boot_src()
        assert (
            "recognition.continuous=localStorage.getItem('hermes-voice-continuous')==='true'"
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
            "lease.silenceTimer=setTimeout" in src
        ), "The silence timer must still exist for continuous mode send decision."


class TestBootJsVoiceSectionIntegrity:
    """Smoke checks, the surrounding voice-mode infrastructure is intact."""

    def test_voice_mode_declares_silence_helper(self):
        src = _boot_src()
        assert "function _voiceSilenceMs()" in src, "The voice silence timeout helper must exist in boot.js"

    def test_voice_mode_declares_recognition(self):
        src = _boot_src()
        assert "const recognition=new SpeechRecognition();" in src

    def test_voice_mode_state_machine_present(self):
        src = _boot_src()
        for state in ("idle", "listening", "thinking", "speaking"):
            assert f"'{state}'" in src, f"Voice mode state '{state}' must be referenced."

    def test_voice_mode_uses_owned_completion(self):
        src = _boot_src()
        assert "window._voiceModeOnResponseComplete=function(activeSid,streamId,source,generation,outcome)" in src, (
            "voice mode must expose its exact-owner completion transition."
        )
