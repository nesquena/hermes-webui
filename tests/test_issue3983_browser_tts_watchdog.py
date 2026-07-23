"""Browser speech watchdog and unified voice-mode lifecycle regressions."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TTS_JS = (ROOT / "static" / "tts.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _function_region(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_shared_controller_owns_browser_watchdog_and_keepalive():
    browser = _function_region(TTS_JS, "async function _browserChunk", "async function _resolveEffectiveEngine")
    assert "state.watchdog=setTimeout" in browser
    assert "window.speechSynthesis.cancel()" in browser
    assert "state.keepAlive=setInterval" in browser
    assert "window.speechSynthesis.pause()" in browser
    assert "window.speechSynthesis.resume()" in browser
    assert ".finally(()=>_clearBrowser(state))" in browser


def test_stop_clears_every_browser_recovery_resource():
    stop = _function_region(TTS_JS, "function stop(", "async function getCapability")
    clear = _function_region(TTS_JS, "function _clearBrowser", "function stop(")
    assert "_clearBrowser(state,true)" in stop
    assert "if(state.chunkReject)" in stop
    assert "clearTimeout(state.watchdog)" in clear
    assert "clearInterval(state.keepAlive)" in clear
    assert "window.speechSynthesis.cancel()" in clear


def test_voice_mode_routes_through_controller_and_rearms_at_most_once():
    speak = _function_region(BOOT_JS, "function _speakResponse", "// Hook into response completion")
    assert "window.HermesTTS.speak(clean" in speak
    assert "const voiceGeneration=++_voiceTtsGeneration" in speak
    assert "if(_voiceTtsRearmTimer)clearTimeout" in speak
    assert "voiceGeneration===_voiceTtsGeneration" in speak
    assert "onEnd:()=>rearm(500)" in speak
    assert "onError:error=>" in speak
    assert "SpeechSynthesisUtterance" not in speak
    assert "fetch(" not in speak


def test_voice_mode_does_not_require_browser_tts_globally():
    voice_setup = _function_region(BOOT_JS, "// ── Turn-based voice mode", "const modeBtn")
    assert "if(!hasSTT) return;" in voice_setup
    assert "hasTTS" not in voice_setup
    assert "speechSynthesis" not in voice_setup


def test_session_and_profile_switch_deactivate_voice_mode_before_stopping_tts():
    session_load = _function_region(SESSIONS_JS, "async function loadSession", "// Resolve canonical lineage SID")
    profile_switch = _function_region(PANELS_JS, "async function switchToProfile", "// ── #4671 profile-switch")
    assert "window._voiceModeDeactivate()" in session_load
    assert "window._voiceModeDeactivate()" in profile_switch
