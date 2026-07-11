from pathlib import Path


BOOT_JS = Path("static/boot.js").read_text(encoding="utf-8")
MESSAGES_JS = Path("static/messages.js").read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def _compact(src: str) -> str:
    return "".join(src.split())


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


def test_final_results_route_send_timing_through_shared_silence_helper():
    block = _between(
        BOOT_JS,
        "_recognition.onresult=(event)=>{",
        "\n\n    _recognition.onend=()=>{",
    )
    assert "_armVoiceModeSilenceTimer(_voiceSilenceMs());" in _compact(block), (
        "final recognition results must route their send timing through the "
        "shared silence helper instead of open-coding a second timer path."
    )


def test_shared_silence_helper_clears_state_before_sending():
    block = _between(
        BOOT_JS,
        "function _armVoiceModeSilenceTimer(delayMs){",
        "\n\n  function _clearBrowserTtsRecovery(){",
    )
    compact = _compact(block)
    assert "_clearVoiceModeSilenceTimer();" in compact
    assert "_silenceDeadlineAt=Date.now()+_safeDelay;" in compact
    assert "_silenceTimer=setTimeout(()=>{_silenceTimer=null;_silenceDeadlineAt=0;_voiceModeSend();},_safeDelay);" in compact, (
        "the shared silence helper must clear its pending state before the "
        "send callback fires, so the timer stays the single send authority."
    )


def test_onend_rearms_remaining_grace_before_any_immediate_send():
    block = _between(
        BOOT_JS,
        "_recognition.onend=()=>{",
        "\n\n    _recognition.onerror=(event)=>{",
    )
    compact = _compact(block)
    assert "const_remainingSilenceMs=_silenceDeadlineAt-Date.now();" in compact, (
        "voice mode must compare recognition onend against the pending silence "
        "deadline instead of treating onend as an unconditional send signal."
    )
    rearm = compact.index("_armVoiceModeSilenceTimer(_remainingSilenceMs);")
    branch_return = compact.index("return;", rearm)
    immediate_send = compact.index("_voiceModeSend();")
    assert branch_return < immediate_send, (
        "when onend fires before the configured silence grace expires, voice "
        "mode must re-arm the remaining delay and return before any immediate "
        "send path can run."
    )
    assert compact.count("_voiceModeSend();") == 1, (
        "onend should expose only one immediate send site, the post-deadline "
        "branch after the remaining-grace early return."
    )


def test_voice_silence_timer_remains_send_authority():
    assert "_voiceSilenceMs()" in BOOT_JS
    assert "_armVoiceModeSilenceTimer(_voiceSilenceMs());" in BOOT_JS, (
        "final recognition results must still derive auto-send timing from the "
        "configurable silence timeout."
    )
    assert "if(typeof autoReadLastAssistant==='function') setTimeout(()=>autoReadLastAssistant(), 300);" in MESSAGES_JS, (
        "the successful done path must keep routing through autoReadLastAssistant."
    )
