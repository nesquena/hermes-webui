import json
from pathlib import Path
import shutil
import subprocess
import pytest


BOOT_JS = Path("static/boot.js").read_text(encoding="utf-8")
MESSAGES_JS = Path("static/messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
_VOICE_BLOCK_START = "function _voiceModePrefEnabled(){"
_VOICE_BLOCK_END = "\n\n  function _speakResponse(){"


def _between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def _compact(src: str) -> str:
    return "".join(src.split())


def _extract_voice_mode_runtime(src: str) -> str:
    start = src.index(_VOICE_BLOCK_START)
    end = src.index(_VOICE_BLOCK_END, start)
    return src[start:end]


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
        "_ownedRecognition.onresult=(event)=>{",
        "\n\n    _ownedRecognition.onend=()=>{",
    )
    assert "_armVoiceModeSilenceTimer(_voiceSilenceMs());" in _compact(block), (
        "final recognition results must route their send timing through the "
        "shared silence helper instead of open-coding a second timer path."
    )


def test_start_listening_keeps_armed_send_authority_intact():
    block = _between(
        BOOT_JS,
        "function _startListening(generation=_voiceGeneration){",
        "\n\n  function _voiceModeSend(){",
    )
    compact = _compact(block)
    assert "generation!==_voiceGeneration" in compact
    assert "S.busy||S.activeStreamId" in compact
    assert "_recognition!==_ownedRecognition" in compact
    assert "_scheduleVoiceModeRestart" in compact
    assert "if(_voiceModeState==='listening'&&_recognition)return;" in compact


def test_shared_silence_helper_clears_state_before_sending():
    block = _between(
        BOOT_JS,
        "function _armVoiceModeSilenceTimer(delayMs){",
        "\n\n  function _clearBrowserTtsRecovery(){",
    )
    compact = _compact(block)
    assert "_clearVoiceModeSilenceTimer();" in compact
    assert "_silenceDeadlineAt=Date.now()+_safeDelay;" in compact
    assert "_silenceTimer=setTimeout(()=>{_silenceTimer=null;_silenceDeadlineAt=0;if(!_voiceModeActive||_generation!==_voiceGeneration)return;_voiceModeSend();},_safeDelay);" in compact, (
        "the shared silence helper must clear its pending state before the "
        "send callback fires, so the timer stays the single send authority."
    )


def test_onend_rearms_remaining_grace_before_any_immediate_send():
    block = _between(
        BOOT_JS,
        "_ownedRecognition.onend=()=>{",
        "\n\n    _ownedRecognition.onerror=(event)=>{",
    )
    compact = _compact(block)
    assert "const_remainingSilenceMs=_silenceDeadlineAt-Date.now();" in compact
    rearm = compact.index("_scheduleVoiceModeRestart(0,generation);")
    branch_return = compact.index("return;", rearm)
    immediate_send = compact.index("_voiceModeSend();")
    assert branch_return < immediate_send, (
        "when onend fires before the configured silence grace expires, voice "
        "mode must re-arm the remaining delay and return before any immediate "
        "send path can run."
    )


def test_manual_send_and_cancel_share_voice_ownership_hooks():
    assert "window._voiceModePrepareManualSend" in MESSAGES_JS
    assert "voiceModeInternal:true" in BOOT_JS
    assert "_invalidateVoiceGeneration()" in BOOT_JS
    assert "_clearOwnerInflightState();" in MESSAGES_JS
    assert "_voiceModeOnResponseComplete({errorOnly:true})" in MESSAGES_JS


def test_error_only_completion_runs_after_idle_settlement():
    assert "let isRecoveryControlMessage=false;\n      if(S.session&&eventMatchesCurrent){" in MESSAGES_JS
    apperror_hook = "_setActivePaneIdleIfOwner();\n      if(eventMatchesCurrent&&!isRecoveryControlMessage&&typeof window._voiceModeOnResponseComplete==='function') window._voiceModeOnResponseComplete({errorOnly:true});"
    assert apperror_hook in MESSAGES_JS
    cancel_idx = MESSAGES_JS.index("source.addEventListener('cancel',e=>{")
    cancel_render_idx = MESSAGES_JS.index("renderSessionList();", cancel_idx)
    cancel_hook_idx = MESSAGES_JS.index("_setActivePaneIdleIfOwner();\n      if(typeof window._voiceModeOnResponseComplete==='function') window._voiceModeOnResponseComplete({errorOnly:true});", cancel_idx)
    assert cancel_hook_idx > cancel_render_idx
    error_idx = MESSAGES_JS.index("function _handleStreamError(source){")
    error_hook_idx = MESSAGES_JS.index("_setActivePaneIdleIfOwner();\n    if(S.session&&S.session.session_id===activeSid&&typeof window._voiceModeOnResponseComplete==='function') window._voiceModeOnResponseComplete({errorOnly:true});", error_idx)
    error_render_idx = MESSAGES_JS.index("renderMessages({preserveScroll:true});", error_idx)
    assert error_hook_idx > error_render_idx


def test_grace_restart_preserves_accumulated_voice_text():
    block = _between(
        BOOT_JS,
        "function _startListening(generation=_voiceGeneration){",
        "\n\n  function _voiceModeSend(){",
    )
    compact = _compact(block)
    assert "let_finalText=_voiceModeAccumulatedText;" in compact
    assert "_finalText=_voiceModeAccumulatedText;" in compact
    assert "_voiceModeAccumulatedText=final||interim;" in compact
    assert "_scheduleVoiceModeRestart(0,generation);" in compact


def test_busy_activation_captures_completion_owner():
    block = _between(
        BOOT_JS,
        "function _activate(){",
        "\n\n  function _deactivate(){",
    )
    compact = _compact(block)
    assert "if(typeofS!=='undefined'&&S.busy){" in compact
    assert "_voiceThinkingGeneration=_voiceGeneration;" in compact
    assert "_voiceCompletionGeneration=_voiceGeneration;" in compact
    assert "_voiceModeThinkingSid=(S.session&&S.session.session_id)||null;" in compact
    assert "_setState('thinking');" in compact


def test_voice_silence_timer_remains_send_authority():
    assert "_voiceSilenceMs()" in BOOT_JS
    assert "_armVoiceModeSilenceTimer(_voiceSilenceMs());" in BOOT_JS, (
        "final recognition results must still derive auto-send timing from the "
        "configurable silence timeout."
    )
    assert "if(typeof autoReadLastAssistant==='function') setTimeout(()=>autoReadLastAssistant(), 300);" in MESSAGES_JS, (
        "the successful done path must keep routing through autoReadLastAssistant."
    )


_VOICE_RESTART_HARNESS = r"""
const block = %s;
const state = { sends: 0, starts: 0, aborts: 0, recognitions: [] };
let now = 0;
const timers = [];
let nextTimerId = 1;
function schedule(cb, delay){
  const id = nextTimerId++;
  timers.push({ id, at: now + (delay || 0), cb });
  return id;
}
function cancel(id){
  const idx = timers.findIndex((timer) => timer.id === id);
  if (idx >= 0) timers.splice(idx, 1);
}
function advance(ms){
  const target = now + ms;
  for(;;){
    const due = timers.filter((timer) => timer.at <= target).sort((a, b) => (a.at - b.at) || (a.id - b.id))[0];
    if(!due) break;
    timers.splice(timers.indexOf(due), 1);
    now = due.at;
    due.cb();
  }
  now = target;
}
function SpeechRecognition(){
  const inst = {
    continuous: false,
    interimResults: false,
    lang: null,
    onstart: null,
    onresult: null,
    onend: null,
    onerror: null,
    start(){
      state.starts += 1;
      if (typeof inst.onstart === 'function') inst.onstart();
    },
    abort(){
      state.aborts += 1;
    }
  };
  state.recognitions.push(inst);
  return inst;
}
const FakeDate = { now: () => now };
const localStorage = {
  getItem(key){
    const store = {
      'hermes-voice-mode-button': 'true',
      'hermes-voice-silence-ms': '1000',
      'hermes-voice-continuous': 'false',
    };
    return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
  }
};
const modeBtn = { style: { display: '' }, classList: { add(){}, remove(){} }, title: '' };
const bar = { style: { display: '' } };
const indicator = { className: '' };
const label = { textContent: '' };
const micBtn = { classList: { add(){}, remove(){} }, title: '' };
const ta = { value: '' };
const autoResize = () => {};
const speechSynthesis = { cancel(){}, pause(){}, resume(){}, speaking: false };
const S = { session: { session_id: 'sess-1' } };
const send = () => { state.sends += 1; };
const _micOriginNeedsSecureContext = () => false;
const _deactivate = () => {};
const t = (key) => key;
const showToast = () => {};
const windowObj = {};
const _locale = { _speech: 'en-US' };
const api = new Function(
  'SpeechRecognition', 'setTimeout', 'clearTimeout', 'Date', 'localStorage',
  'modeBtn', 'bar', 'indicator', 'label', 'micBtn', 'ta', 'autoResize', 'speechSynthesis',
  'S', 'send', '_micOriginNeedsSecureContext', '_deactivate', 't', 'showToast', 'window', '_locale', 'state',
  `${block}
  return {
    activate(){ _voiceModeActive = true; },
    startListening: _startListening,
    currentRecognition(){ return _recognition; },
    sendCount(){ return state.sends; },
    recognitionCount(){ return state.recognitions.length; },
    stateName(){ return _voiceModeState; },
    hasSilenceTimer(){ return !!_silenceTimer; }
  };`
)(
  SpeechRecognition, schedule, cancel, FakeDate, localStorage,
  modeBtn, bar, indicator, label, micBtn, ta, autoResize, speechSynthesis,
  S, send, _micOriginNeedsSecureContext, _deactivate, t, showToast, windowObj, _locale, state
);
api.activate();
api.startListening();
const first = api.currentRecognition();
first.onerror({ error: 'no-speech' });
first.onend();
advance(500);
const second = api.currentRecognition();
second.onresult({
  resultIndex: 0,
  results: [{ 0: { transcript: 'hello' }, isFinal: true }],
});
const armedBeforeStale = api.hasSilenceTimer();
advance(300);
const stillSecond = api.currentRecognition() === second;
const countAfterStale = api.recognitionCount();
const armedAfterStale = api.hasSilenceTimer();
advance(700);
console.log(JSON.stringify({
  armedBeforeStale,
  stillSecond,
  countAfterStale,
  armedAfterStale,
  sendCount: api.sendCount(),
  recognitionCount: api.recognitionCount(),
  stateName: api.stateName(),
}));
"""


def _run_voice_restart_harness() -> dict:
    script = _VOICE_RESTART_HARNESS % json.dumps(_extract_voice_mode_runtime(BOOT_JS))
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    return json.loads(result.stdout.strip())


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_no_speech_recovery_stale_restart_cannot_cancel_pending_send():
    out = _run_voice_restart_harness()
    assert out["armedBeforeStale"] is True, (
        "the harness must arm the silence timer before the stale restart fires, "
        f"got {out}"
    )
    assert out["stillSecond"] is True, (
        "the stale restart must not replace the recognizer that captured the "
        f"new utterance, got {out}"
    )
    assert out["countAfterStale"] == 2, (
        "no-speech recovery must keep exactly two recognition instances alive "
        "across the stale-restart sequence, got "
        f"{out}"
    )
    assert out["armedAfterStale"] is True, (
        "the stale restart must leave the pending send timer armed, got "
        f"{out}"
    )
    assert out["sendCount"] == 1, (
        "the re-armed utterance must still auto-send exactly once after the "
        f"stale restart sequence, got {out}"
    )
    assert out["recognitionCount"] == 2, (
        "the stale restart path must not create a third recognizer instance, "
        f"got {out}"
    )
    assert out["stateName"] == "thinking", (
        "after the silence timer fires, voice mode should proceed into the "
        f"normal send path, got {out}"
    )
