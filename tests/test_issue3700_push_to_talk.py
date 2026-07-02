import re
from pathlib import Path


def _boot_src() -> str:
    return Path("static/boot.js").read_text(encoding="utf-8")


def _commands_src() -> str:
    return Path("static/commands.js").read_text(encoding="utf-8")


def test_start_mic_capture_extracted():
    src = _boot_src()
    assert re.search(r"async function _startMicCapture\([^)]*\)\{", src)
    assert "_activeCaptureMode='speech'" in src
    assert "const captureMode=_rawAudioMode?'media-raw':'media-transcribe';" in src
    assert "_activeCaptureMode=captureMode;" in src


def test_toggle_mic_capture_exposed_on_window():
    src = _boot_src()
    assert "async function _toggleMicCapture()" in src
    assert "window._toggleMicCapture=_toggleMicCapture;" in src


def test_toggle_helper_respects_hidden_mic_setting():
    src = _boot_src()
    assert "function _micButtonAvailable()" in src
    assert "btn.classList.contains('composer-control-hidden')" in src
    assert "btn.getAttribute('aria-hidden')==='true'" in src


def test_voice_command_respects_hidden_mic_setting_before_clicking():
    src = _commands_src()
    assert "function cmdVoice()" in src
    assert "mic.classList.contains('composer-control-hidden')" in src
    assert "mic.getAttribute('aria-hidden')!=='true'" in src
    assert "showToast(t('cmd_voice_use_mic'));" in src


def test_old_onclick_handler_removed():
    src = _boot_src()
    assert "btn.onclick=async()=>{" not in src


def test_pointer_hold_wiring_present():
    src = _boot_src()
    assert "let _micHoldTimer=null;" in src
    assert "let _micHoldActive=false;" in src
    assert "let _micPointerDown=false;" in src
    assert "let _micStartSeq=0;" in src
    assert "const _micHoldThresholdMs=300;" in src
    assert "btn.addEventListener('pointerdown'" in src
    assert "btn.addEventListener('pointerup'" in src
    assert (
        "btn.addEventListener('pointerleave'" in src
        or "btn.addEventListener('pointercancel'" in src
    )
    assert "_micHoldTimer=setTimeout(async()=>{" in src
    assert "},_micHoldThresholdMs);" in src


def test_hold_release_routes_to_stop_mic():
    src = _boot_src()
    assert re.search(
        r"btn\.addEventListener\('pointerup',async e=>\{.*?_stopMic\(\);.*?\}\);",
        src,
        re.DOTALL,
    )
    assert re.search(
        r"btn\.addEventListener\('pointer(cancel|leave)',\(\)=>\{.*?_stopMic\(\);.*?\}\);",
        src,
        re.DOTALL,
    )
    assert "const startSeq=++_micStartSeq;" in src
    assert "if(startSeq!==_micStartSeq||!_micButtonAvailable()||(holdRequired&&!_micHoldActive)){" in src
    assert "_stopTracks(captureStream);" in src
    assert "if(startSeq!==_micStartSeq) return;" in src


def test_reserved_ctrl_shift_d_shortcut_is_not_bound():
    src = _boot_src()
    assert "(e.metaKey||e.ctrlKey)&&e.shiftKey&&!e.altKey&&(e.key==='d'||e.key==='D')" not in src
    assert "await window._toggleMicCapture();" not in src


def test_stop_mic_still_exposed_and_active_capture_mode_retained():
    src = _boot_src()
    assert "window._stopMic=_stopMic;" in src
    assert "_activeCaptureMode" in src
    assert "_stopMic();" in src
