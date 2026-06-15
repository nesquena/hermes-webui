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
    assert "const utter=new SpeechSynthesisUtterance(clean);" in speak_body
    assert "utter.onend=()=>{" in speak_body
    assert "utter.onerror=()=>{" in speak_body
    assert speak_body.count("_clearBrowserTtsRecovery();") >= 2, (
        "Both browser TTS completion callbacks must clear watchdog/keep-alive handles."
    )
    assert "_browserTtsSuppressNextErrorRearm=false;" in speak_body
    assert "_voiceModeActive&&_voiceModeState==='speaking'" in speak_body
    assert "if(_browserTtsSuppressNextErrorRearm){" in speak_body
    assert "_armBrowserTtsRecovery(clean, utter.rate);" in speak_body

    deactivate_body = _extract_function(src, "_deactivate")
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
    # After refactor: boot.js delegates to _playEdgeTts() instead of inline Audio()
    assert "_playEdgeTts(" in edge_body, (
        "Edge branch must delegate to _playEdgeTts()"
    )
    assert "_armBrowserTtsRecovery" not in edge_body, (
        "Browser speechSynthesis workaround must not leak into Edge branch."
    )

    # Verify _playEdgeTts() itself is clean (the actual audio code moved to ui.js)
    ui_src = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    play_edge_body = _extract_function(ui_src, "_playEdgeTts")
    assert "_playServerTts(" in play_edge_body, (
        "_playEdgeTts() must delegate to _playServerTts()"
    )
    assert "_armBrowserTtsRecovery" not in play_edge_body, (
        "Browser speechSynthesis workaround must not leak into _playEdgeTts()."
    )


def test_voicevox_audio_branch_stays_separate():
    """VOICEVOX server-side audio must not trigger browser SpeechSynthesis workarounds."""
    src = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    vv_match = re.search(
        r'if\(engine==="voicevox"\)\{(.*?)\n\s+return;\n\s+\}',
        src, re.DOTALL,
    )
    assert vv_match, "VOICEVOX audio branch must exist"
    vv_body = vv_match.group(1)
    assert "_playVoicevoxTts(" in vv_body, (
        "VOICEVOX branch must delegate to _playVoicevoxTts()"
    )
    assert "_armBrowserTtsRecovery" not in vv_body, (
        "Browser speechSynthesis workaround must not leak into VOICEVOX branch"
    )

    # Verify _playVoicevoxTts() itself is clean
    ui_src = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    play_vv_body = _extract_function(ui_src, "_playVoicevoxTts")
    assert "_armBrowserTtsRecovery" not in play_vv_body, (
        "Browser speechSynthesis workaround must not leak into _playVoicevoxTts()"
    )
    assert "_playServerTts(" in play_vv_body, (
        "_playVoicevoxTts() must delegate to _playServerTts()"
    )
