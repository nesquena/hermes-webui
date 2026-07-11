from pathlib import Path


BOOT_JS = Path("static/boot.js").read_text(encoding="utf-8")
MESSAGES_JS = Path("static/messages.js").read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def test_apperror_reuses_voice_mode_completion_hook():
    block = _between(
        MESSAGES_JS,
        "source.addEventListener('apperror',e=>{",
        "source.addEventListener('warning',e=>{",
    )
    assert "window._voiceModeOnResponseComplete" in block, (
        "apperror finalization must call the existing voice-mode completion hook "
        "so failed hands-free turns leave 'thinking'."
    )


def test_terminal_stream_error_reuses_voice_mode_completion_hook():
    block = _between(
        MESSAGES_JS,
        "function _handleStreamError(source){",
        "\n\n  (async()=>{",
    )
    assert "window._voiceModeOnResponseComplete" in block, (
        "_handleStreamError must call the existing voice-mode completion hook "
        "so unrecoverable SSE drops re-arm hands-free voice mode."
    )


def test_onend_waits_for_remaining_silence_grace():
    block = _between(
        BOOT_JS,
        "_recognition.onend=()=>{",
        "\n\n    _recognition.onerror=(event)=>{",
    )
    assert "const _remainingSilenceMs=_silenceDeadlineAt-Date.now();" in block, (
        "voice mode must compare recognition onend against the pending silence "
        "deadline instead of treating onend as an unconditional send signal."
    )
    assert "_armVoiceModeSilenceTimer(_remainingSilenceMs);" in block, (
        "when onend fires before the configured silence grace expires, voice "
        "mode must re-arm the remaining delay."
    )


def test_voice_silence_timer_remains_send_authority():
    assert "_silenceTimer=setTimeout(()=>{" in BOOT_JS
    assert "},_voiceSilenceMs());" in BOOT_JS, (
        "final recognition results must still arm the configurable silence timer."
    )
    assert "if(typeof autoReadLastAssistant==='function') setTimeout(()=>autoReadLastAssistant(), 300);" in MESSAGES_JS, (
        "the successful done path must keep routing through autoReadLastAssistant."
    )
