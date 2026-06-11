from pathlib import Path
import re


REPO = Path(__file__).resolve().parents[1]


def test_boot_js_declares_browser_tts_recovery_helpers():
    src = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    assert "let _browserTtsKeepAlive=null;" in src
    assert "let _browserTtsWatchdog=null;" in src
    assert "let _browserTtsSuppressNextErrorRearm=false;" in src
    assert "function _clearBrowserTtsRecovery()" in src
    assert "function _armBrowserTtsRecovery(clean, rate)" in src


def test_browser_tts_watchdog_rearms_listening_if_onend_drops():
    src = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    arm_idx = src.find("function _armBrowserTtsRecovery(clean, rate){")
    assert arm_idx != -1
    arm_body = src[arm_idx : arm_idx + 1400]
    assert "_browserTtsWatchdog=setTimeout" in arm_body
    assert "_voiceModeState!=='speaking'" in arm_body
    assert "_browserTtsSuppressNextErrorRearm=true;" in arm_body
    assert "speechSynthesis.cancel()" in arm_body
    assert "_startListening();" in arm_body
    assert "_browserTtsKeepAlive=setInterval" in arm_body
    assert "speechSynthesis.pause();" in arm_body
    assert "speechSynthesis.resume();" in arm_body


def test_browser_tts_callbacks_and_deactivate_clear_recovery_handles():
    src = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    speak_idx = src.find("const utter=new SpeechSynthesisUtterance(clean);")
    assert speak_idx != -1
    speak_body = src[speak_idx : speak_idx + 1200]
    assert "utter.onend=()=>{" in speak_body
    assert "utter.onerror=()=>{" in speak_body
    assert speak_body.count("_clearBrowserTtsRecovery();") >= 2, (
        "Both browser TTS completion callbacks must clear watchdog/keep-alive handles."
    )
    assert "_browserTtsSuppressNextErrorRearm=false;" in speak_body
    assert "_voiceModeActive&&_voiceModeState==='speaking'" in speak_body
    assert "if(_browserTtsSuppressNextErrorRearm){" in speak_body
    assert "_armBrowserTtsRecovery(clean, utter.rate);" in speak_body

    deactivate_match = re.search(
        r"function _deactivate\(\)\{(.*?)\n  \}",
        src,
        re.DOTALL,
    )
    assert deactivate_match, "_deactivate() must exist"
    deactivate_body = deactivate_match.group(1)
    assert "_clearBrowserTtsRecovery();" in deactivate_body, (
        "_deactivate() must clear browser TTS watchdog/keep-alive handles."
    )
    assert "_browserTtsSuppressNextErrorRearm=false;" in deactivate_body


def test_edge_audio_branch_stays_separate():
    src = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    edge_match = re.search(
        r'if\(engine==="edge"\)\{(.*?)\n\s+return;\n\s+\}',
        src,
        re.DOTALL,
    )
    assert edge_match, "Edge audio branch must exist"
    edge_body = edge_match.group(1)
    assert "const audio = new Audio(url);" in edge_body
    assert "audio.onended = () => {" in edge_body
    assert "_armBrowserTtsRecovery" not in edge_body, (
        "The browser speechSynthesis workaround must not be injected into the Edge audio branch."
    )
