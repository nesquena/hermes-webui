import json
import base64
import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
BOOT = Path("static/boot.js").read_text(encoding="utf-8")


def _voice_runtime():
    pref = BOOT.index("  function _voiceModePrefEnabled(){")
    start = BOOT.index("  let _voiceModeActive=false;")
    end = BOOT.index("  function _speakResponse(", start)
    return BOOT[pref:BOOT.index("  let _voiceModeActive=false;", pref)] + BOOT[start:end]


HARNESS = r"""
const state = { sends: 0, starts: 0, aborts: 0, recognitions: [] };
let now = 0;
let nextTimer = 1;
const timers = new Map();
function setTimer(cb, delay) { const id = nextTimer++; timers.set(id, { at: now + (delay || 0), cb }); return id; }
function clearTimer(id) { timers.delete(id); }
function advance(ms) {
  const target = now + ms;
  for (;;) {
    const due = [...timers.entries()].filter(([, t]) => t.at <= target)
      .sort((a, b) => (a[1].at - b[1].at) || (a[0] - b[0]))[0];
    if (!due) break;
    timers.delete(due[0]); now = due[1].at; due[1].cb();
  }
  now = target;
}
function SpeechRecognition() {
  const instance = {
    onresult: null, onend: null, onerror: null,
    start() { state.starts += 1; if (state.throwStart) { state.throwStart = false; throw Error('start'); } },
    abort() { state.aborts += 1; },
  };
  state.recognitions.push(instance); return instance;
}
const localStorage = { getItem(key) { return ({
  'hermes-voice-mode-button': 'true', 'hermes-voice-silence-ms': '1000',
  'hermes-voice-continuous': 'false', 'hermes-tts-engine': 'browser'
})[key] || null; } };
const element = () => ({ style: {}, classList: { add(){}, remove(){} }, textContent: '', className: '', value: '' });
const modeBtn = element(), bar = element(), indicator = element(), label = element(), micBtn = element(), ta = element();
const windowObj = { SpeechRecognition, speechSynthesis: { cancel(){}, speaking:false, getVoices(){ return []; }, speak(){} } };
const document = { querySelectorAll(){ return []; } };
const S = { session: { session_id: 's1' }, busy: false, activeStreamId: null };
const t = key => key;
const autoResize = () => {};
const _micOriginNeedsSecureContext = () => false;
const _deactivate = () => {};
const showToast = () => {};
const _setButtonTooltip = () => {};
const stopTTS = () => {};
const _locale = { _speech: 'en-US' };
const send = () => { state.sends += 1; };
const _speakResponse = () => { state.speaks = (state.speaks || 0) + 1; };
const api = new Function('window','document','SpeechRecognition','localStorage','modeBtn','bar','indicator','label','micBtn','ta','S','t','autoResize','_micOriginNeedsSecureContext','_deactivate','showToast','_setButtonTooltip','stopTTS','_locale','send','setTimeout','clearTimeout','Date','_speakResponse', `${RUNTIME}
return { activate(){ _voiceModeActive=true; _voiceContextId+=1; _voiceLease={id:1,contextId:_voiceContextId,recognition:null,finalText:'',interimText:'',silenceTimer:null,restartTimer:null,deadlineAt:0,submitted:false,settled:false,owner:null}; }, start: () => _startListening(_voiceLease), first: () => _voiceLease && _voiceLease.recognition, prepare: () => window._voiceLeasePrepareSubmission(), bind: window._voiceLeaseBind, settle: window._voiceLeaseSettleOwner, state: () => _voiceModeState, lease: () => _voiceLease };`)(windowObj,document,SpeechRecognition,localStorage,modeBtn,bar,indicator,label,micBtn,ta,S,t,autoResize,_micOriginNeedsSecureContext,_deactivate,showToast,_setButtonTooltip,stopTTS,_locale,send,setTimer,clearTimer,{ now: () => now },_speakResponse);
api.activate(); api.start();
const first = api.first();
first.onresult({ resultIndex: 0, results: [{ 0: { transcript: 'hello' }, isFinal: true }] });
first.onend();
const second = api.first();
const before = { starts: state.starts, sends: state.sends, state: api.state() };
advance(500);
const afterRestart = { starts: state.starts, recognizerChanged: api.first() !== first };
advance(500);
const afterSend = { sends: state.sends, state: api.state() };
api.prepare(); api.bind('stream-1', 's1');
const settled = api.settle('s1', 'stream-1', { success: false });
console.log(JSON.stringify({ before, afterRestart, afterSend, settled, finalState: api.state(), aborts: state.aborts }));
"""


def _run_runtime():
    encoded = base64.b64encode(_voice_runtime().encode()).decode()
    script = HARNESS.replace(
        "${RUNTIME}", "${Buffer.from('" + encoded + "','base64').toString()}"
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_voice_lease_composes_endpoint_restart_and_exact_terminal_settlement():
    result = _run_runtime()
    assert result["before"] == {"starts": 1, "sends": 0, "state": "listening"}
    assert result["afterRestart"] == {"starts": 2, "recognizerChanged": True}
    assert result["afterSend"] == {"sends": 1, "state": "thinking"}
    assert result["settled"] is True
    assert result["finalState"] == "listening"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_voice_lease_ignores_duplicate_or_stale_owner_callbacks():
    result = _run_runtime()
    assert result["settled"] is True
    assert result["aborts"] >= 1
