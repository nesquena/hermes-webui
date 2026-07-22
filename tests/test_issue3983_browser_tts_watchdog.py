from pathlib import Path
import re


REPO = Path(__file__).resolve().parents[1]


def _extract_function(src: str, name: str) -> str:
    anchor = f"function {name}("
    start = src.find(anchor)
    assert start != -1, f"{name}() must exist"
    body_start = src.find("{", start)
    assert body_start != -1, f"{name}() must have a body"
    depth = 1
    idx = body_start + 1
    while depth and idx < len(src):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
        idx += 1
    assert depth == 0, f"{name}() body must balance braces"
    return src[start:idx]


def test_boot_js_declares_browser_tts_recovery_helpers():
    src = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    assert "let _browserTtsKeepAlive=null;" in src
    assert "let _browserTtsWatchdog=null;" in src
    assert "let _browserTtsSuppressNextErrorRearm=false;" in src
    assert "function _clearBrowserTtsRecovery()" in src
    assert "function _armBrowserTtsRecovery(clean, rate)" in src


def test_browser_tts_watchdog_rearms_listening_if_onend_drops():
    src = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    arm_body = _extract_function(src, "_armBrowserTtsRecovery")
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
    speak_body = _extract_function(src, "_speakResponse")
    assert "const utter=new SpeechSynthesisUtterance(bChunks[idx]);" in speak_body
    assert "utter.onend=()=>{" in speak_body
    assert "utter.onerror=()=>{" in speak_body
    assert speak_body.count("_clearBrowserTtsRecovery();") >= 2, (
        "Both browser TTS completion callbacks must clear watchdog/keep-alive handles."
    )
    assert "_browserTtsSuppressNextErrorRearm=false;" in speak_body
    assert "_voiceModeActive&&_voiceModeState==='speaking'" in speak_body
    assert "if(_browserTtsSuppressNextErrorRearm){" in speak_body
    assert "_armBrowserTtsRecovery(bChunks[idx], utter.rate);" in speak_body

    deactivate_body = _extract_function(src, "_deactivate")
    assert "_clearBrowserTtsRecovery();" in deactivate_body, (
        "_deactivate() must clear browser TTS watchdog/keep-alive handles."
    )
    assert "_browserTtsSuppressNextErrorRearm=false;" in deactivate_body


def test_edge_audio_branch_stays_separate():
    src = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    ui = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    # Server audio engines (edge/openai/elevenlabs) route through the shared
    # chunked player; the browser speechSynthesis watchdog must stay out of
    # that branch and out of the shared player itself.
    m = re.search(
        r'if\(engine==="elevenlabs"\|\|engine==="openai"\|\|engine==="edge"\)\{(.*?)\n\s+return;\n\s+\}',
        src,
        re.DOTALL,
    )
    assert m, "Shared server-audio branch must exist"
    assert "_armBrowserTtsRecovery" not in m.group(1), (
        "The browser speechSynthesis workaround must not be injected into the server audio branch."
    )
    player = re.search(r'function _playServerTtsChunks\(.*?\n\}', ui, re.DOTALL)
    assert player, "_playServerTtsChunks must exist in ui.js"
    assert "new Audio(url)" in player.group(0)
    assert "audio.onended" in player.group(0)
    assert "_armBrowserTtsRecovery" not in player.group(0)
