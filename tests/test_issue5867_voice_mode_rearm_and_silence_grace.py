import json
import base64
import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
BOOT = Path("static/boot.js").read_text(encoding="utf-8")
MESSAGES = Path("static/messages.js").read_text(encoding="utf-8")


def _voice_runtime():
    pref = BOOT.index("  function _voiceModePrefEnabled(){")
    start = BOOT.index("  let _voiceModeActive=false;")
    end = BOOT.index("  function _speakResponse(", start)
    return BOOT[pref:BOOT.index("  let _voiceModeActive=false;", pref)] + BOOT[start:end]


def _function_source(source: str, name: str) -> str:
    anchor = f"function {name}("
    start = source.index(anchor)
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    paren_start = source.index("(", start)
    paren_depth = 1
    paren_end = paren_start + 1
    while paren_depth:
        if source[paren_end] == "(":
            paren_depth += 1
        elif source[paren_end] == ")":
            paren_depth -= 1
        paren_end += 1
    body_start = source.index("{", paren_end)
    depth = 1
    idx = body_start + 1
    while depth:
        if source[idx] == "{":
            depth += 1
        elif source[idx] == "}":
            depth -= 1
        idx += 1
    return source[start:idx]


OWNER_SOURCE = _function_source(MESSAGES, "_setActivePaneIdleIfOwner")
SEND_SOURCE = _function_source(MESSAGES, "send")
ATTACH_SOURCE = _function_source(MESSAGES, "attachLiveStream")
LOAD_SOURCE = _function_source(Path("static/sessions.js").read_text(encoding="utf-8"), "loadSession")
NEW_SESSION_SOURCE = _function_source(Path("static/sessions.js").read_text(encoding="utf-8"), "newSession")
PROFILE_SOURCE = _function_source(Path("static/panels.js").read_text(encoding="utf-8"), "switchToProfile")
_model_handler_start = BOOT.index("$('modelSelect').onchange=async()=>{")
MODEL_HANDLER_SOURCE = BOOT[_model_handler_start:BOOT.index("$('msg').addEventListener", _model_handler_start)]
MODEL_HANDLER_SOURCE = MODEL_HANDLER_SOURCE.rstrip().removesuffix(';')
_completion_start = BOOT.index("  window._voiceModeOnResponseComplete=function")
VOICE_COMPLETE_SOURCE = BOOT[_completion_start:BOOT.index("  // ordinary autoReadLastAssistant", _completion_start)]
SPEAK_SOURCE = _function_source(BOOT, "_speakResponse")
ACTIVATE_SOURCE = _function_source(BOOT, "_activate")


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
return { activate(){ _voiceModeActive=true; _voiceContextId+=1; _voiceLease={id:1,contextId:_voiceContextId,recognition:null,finalText:'',interimText:'',silenceTimer:null,restartTimer:null,deadlineAt:0,submitted:false,settled:false,owner:null}; }, start: () => _startListening(_voiceLease), first: () => _voiceLease && _voiceLease.recognition, prepare: () => window._voiceLeasePrepareSubmission(), bind: window._voiceLeaseBind, settle: window._voiceLeaseSettleOwner, complete: (...args) => window._voiceModeOnResponseComplete(...args), state: () => _voiceModeState, lease: () => _voiceLease };`)(windowObj,document,SpeechRecognition,localStorage,modeBtn,bar,indicator,label,micBtn,ta,S,t,autoResize,_micOriginNeedsSecureContext,_deactivate,showToast,_setButtonTooltip,stopTTS,_locale,send,setTimer,clearTimer,{ now: () => now },_speakResponse);
api.activate(); api.start();
const first = api.first();
api.start();
const duplicateStart = { starts: state.starts, recognizerChanged: api.first() !== first, aborts: state.aborts };
first.onresult({ resultIndex: 0, results: [{ 0: { transcript: 'hello' }, isFinal: true }] });
first.onend();
const second = api.first();
const before = { starts: state.starts, sends: state.sends, state: api.state() };
advance(500);
const afterRestart = { starts: state.starts, recognizerChanged: api.first() !== first };
advance(500);
const afterSend = { sends: state.sends, state: api.state() };
api.prepare(); api.bind('stream-1', 's1');
const wrongOwner = api.settle('old-session', 'old-stream', { success: false });
const stale = api.settle('s1', 'stream-1', { success: false }, {}, 0);
const beforeValidOwnerState = api.state();
const settled = api.settle('s1', 'stream-1', { success: false });
const duplicate = api.settle('s1', 'stream-1', { success: false });
const ownerEvents = [];
const bootCompletion = windowObj._voiceModeOnResponseComplete;
windowObj._voiceModeOnResponseComplete = (...args) => { ownerEvents.push(args); return bootCompletion(...args); };
const source = {};
const LIVE_STREAMS = { s1: { streamId: 'stream-1', source } };
const ownerFactory = new Function('activeSid','streamId','source','window','_isActiveSession','S','INFLIGHT','setBusy','setComposerStatus','setStatus',
  "const _transportGeneration=1; return (" + Buffer.from('${OWNER_B64}', 'base64').toString() + ");");
const owner = ownerFactory('s1', 'stream-1', source, windowObj, () => true, S, { s1: {} }, () => { S.busy = false; }, () => {}, () => {});
S.busy = true;
owner({ success: false }, source, 1);
const terminalOwnerEvents = ownerEvents.slice();
ownerEvents.length = 0;
const oldSource = {}, replacementSource = {};
windowObj._liveStreamTransportAuthority = { s1: { streamId: 'stream-1', source: replacementSource, generation: 2 } };
windowObj._liveStreamTransportSourceGeneration = new WeakMap();
windowObj._liveStreamTransportSourceGeneration.set(replacementSource, 2);
api.activate(); api.prepare(); api.bind('stream-1', 's1');
api.complete('s1', 'stream-1', oldSource, 1, { success: false });
const staleSourceState = api.state();
api.complete('s1', 'stream-1', replacementSource, 2, { success: false });
console.log(JSON.stringify({ before, afterRestart, duplicateStart, afterSend, settled, duplicate, stale, wrongOwner, beforeValidOwnerState, finalState: api.state(), staleSourceState, aborts: state.aborts, ownerEvents: terminalOwnerEvents, transportEvents: ownerEvents }));
"""


PRODUCTION_HARNESS = r"""
const source = Buffer.from('${SEND_B64}', 'base64').toString();
const ownerSource = Buffer.from('${OWNER_B64}', 'base64').toString();
const runtimeSource = Buffer.from('${RUNTIME_B64}', 'base64').toString();
const completionSource = Buffer.from('${COMPLETE_B64}', 'base64').toString();
const speakSource = Buffer.from('${SPEAK_B64}', 'base64').toString();
const activateSource = Buffer.from('${ACTIVATE_B64}', 'base64').toString();
const msg = { value: 'production voice text' };
const elements = new Map([['msg', msg]]);
const S = {
  session: { session_id: 's1', workspace: 'D:/workspace', title: 'Untitled', model: 'model-1', profile: 'default' },
  pendingFiles: [], messages: [], toolCalls: [], busy: false, activeStreamId: null,
};
const INFLIGHT = {};
const bound = [];
const completionEvents = [];
const state = { starts: 0, aborts: 0, recognitions: [], localSettlements: [] };
const windowObj = {
  _defaultMessageMode: 'steer',
};
const documentObj = { querySelector(){ return null; }, querySelectorAll(){ return []; } };
const localStorage = { getItem(key) { return ({
  'hermes-voice-mode-button': 'true', 'hermes-voice-silence-ms': '1000',
  'hermes-voice-continuous': 'false', 'hermes-tts-engine': 'browser'
})[key] || null; }, setItem(){} };
function SpeechRecognition() {
  const instance = { onresult: null, onend: null, onerror: null,
    start(){ state.starts += 1; }, abort(){ state.aborts += 1; } };
  state.recognitions.push(instance); return instance;
}
const elementVoice = () => ({ style: {}, classList: { add(){}, remove(){} }, textContent: '', className: '', value: '' });
const modeBtn = elementVoice(), bar = elementVoice(), indicator = elementVoice(), label = elementVoice(), micBtn = elementVoice();
const t = key => key;
const autoResize = () => {};
const _micOriginNeedsSecureContext = () => false;
const _deactivate = () => {};
const showToast = () => {};
const _setButtonTooltip = () => {};
const stopTTS = () => {};
const _locale = { _speech: 'en-US' };
const element = () => ({ value: '', style: {}, options: [], classList: { add(){}, remove(){} }, querySelectorAll(){ return []; } });
const $ = id => elements.get(id) || element();
const noOp = () => {};
const streamSource = {};
windowObj._liveStreamTransportAuthority = { s1: { streamId: 'stream-1', generation: 1 } };
windowObj._liveStreamTransportSourceGeneration = new WeakMap();
windowObj._liveStreamTransportSourceGeneration.set(streamSource, 1);
windowObj._liveStreamTransportRelease = noOp;
const ownerFactory = new Function('activeSid','streamId','source','window','_isActiveSession','S','INFLIGHT','setBusy','setComposerStatus','setStatus',
  'const _transportGeneration=1; return (' + ownerSource + ');');
const owner = ownerFactory('s1', 'stream-1', streamSource, windowObj, () => true, S, INFLIGHT, value => { S.busy = value; }, noOp, noOp);
const scope = {
  $, S, INFLIGHT, window: windowObj, document: documentObj,
  COMMANDS: [], parseCommand: () => null, _pendingSelections: [], _sendInProgress: false, _sendInProgressSid: null,
  _composerTextWithPendingSelections: () => msg.value, _flushSelectionBlocksToComposer: noOp,
  shouldInterceptCompressionRecoveryContinuation: () => false, isCompressionUiRunning: () => false,
  _clearStaleBusyStateBeforeSend: noOp, _forcedSkillDirectivePending: null,
  _clearComposerDraft: () => Promise.resolve(), uploadPendingFiles: async () => [],
  setComposerStatus: noOp, autoResize: noOp, renderTray: noOp, clearLiveToolCards: noOp,
  appendThinking: noOp, ensureLiveWorklogShell: noOp, setBusy: value => { S.busy = value; },
  updateSendBtn: noOp, _runOptionalPreStartUiStep: (_name, fn) => fn(), _runOptionalPostStartUiStep: (_name, fn) => fn(),
  saveInflightState: noOp, markInflight: noOp, renderSessionListFromCache: noOp, renderSessionList: noOp,
  startApprovalPolling: noOp, startClarifyPolling: noOp, _fetchYoloState: noOp,
  applySessionTitleUpdate: noOp, upsertActiveSessionForLocalTurn: noOp, _chatPayloadModelState: () => ({ model: 'model-1', model_provider: 'provider-1' }),
  _readPendingSessionModel: () => null, _clearPendingSessionModel: noOp, _activeProvider: 'provider-1',
  updateQueueBadge: noOp, _queueDrainSid: null, localStorage,
  api: async () => ({ stream_id: 'stream-1' }), attachLiveStream: (sid, streamId) => {
    bound.push({ type: 'attach', sid, streamId });
    S.activeStreamId=null; S.session.active_stream_id=null; delete INFLIGHT[sid];
    owner({ success: true }, streamSource, 1);
  },
  clearInflightState: noOp, clearInflight: noOp, stopApprovalPolling: noOp, stopClarifyPolling: noOp,
  hideApprovalCard: noOp, hideClarifyCard: noOp, removeThinking: noOp, clearOptimisticSessionStreaming: noOp,
  showToast: noOp, setStatus: noOp, renderMessages: noOp, _appRootPath: () => '/', history: { replaceState: noOp },
  _approvalSessionId: null, _clarifySessionId: null, _AGENT_COMMANDS_RUN_ON_WEBUI: new Set(),
};
const builtins = new Set(['Array','Boolean','Buffer','Date','Error','JSON','Map','Math','Number','Object','Promise','RegExp','Set','String','Symbol','URL','undefined','NaN','Infinity','isNaN','parseInt','encodeURIComponent','decodeURIComponent','setTimeout','clearTimeout','console']);
for (const match of source.matchAll(/\b[A-Za-z_$][\w$]*\b/g)) {
  const name = match[0];
  if (!builtins.has(name) && !(name in scope)) scope[name] = noOp;
}
const send = new Function('scope', 'with(scope){ return (' + source + '); }')(scope);
const speechSynthesis = { cancel(){}, speaking:false, getVoices(){ return []; }, speak(){} };
const voiceFactory = new Function('window','document','SpeechRecognition','localStorage','modeBtn','bar','indicator','label','micBtn','ta','S','t','autoResize','_micOriginNeedsSecureContext','_deactivate','showToast','_setButtonTooltip','stopTTS','_locale','send','setTimeout','clearTimeout','Date','_voiceLeaseSettleOwner','speechSynthesis', runtimeSource + '\n' + completionSource + '\n' + speakSource + '\n' + activateSource + '\nreturn { activate:()=>_activate(), start:()=>_startListening(_voiceLease), first:()=>_voiceLease&&_voiceLease.recognition, state:()=>_voiceModeState, lease:()=>_voiceLease, settleLocal:window._voiceLeaseSettleLocal };');
const voiceApi = voiceFactory(windowObj, documentObj, SpeechRecognition, localStorage, modeBtn, bar, indicator, label, micBtn, msg, S, t, autoResize, _micOriginNeedsSecureContext, _deactivate, showToast, _setButtonTooltip, stopTTS, _locale, send, setTimeout, clearTimeout, Date, (...args)=>windowObj._voiceLeaseSettleOwner(...args), speechSynthesis);
const originalPrepare=windowObj._voiceLeasePrepareSubmission;
windowObj._voiceLeasePrepareSubmission=(...args)=>{ const lease=originalPrepare(...args)||voiceApi.lease(); bound.push({ type: 'prepare', leaseId: lease && lease.id }); return lease; };
const originalBind=windowObj._voiceLeaseBind;
windowObj._voiceLeaseBind=(streamId,sid)=>{ bound.push({ type: 'bind', streamId, sid }); return originalBind(streamId,sid); };
const originalCompletion=windowObj._voiceModeOnResponseComplete;
windowObj._voiceModeOnResponseComplete=(...args)=>{ completionEvents.push(args); return originalCompletion(...args); };
const originalSettleLocal=windowObj._voiceLeaseSettleLocal;
windowObj._voiceLeaseSettleLocal=(lease)=>{ state.localSettlements.push(lease && lease.id); return originalSettleLocal(lease); };
voiceApi.activate();
const recognition=voiceApi.first();
recognition.onresult({ resultIndex: 0, results: [{ 0: { transcript: msg.value }, isFinal: true }] });
recognition.onend();
(async () => {
  await new Promise(resolve => setTimeout(resolve, 1150));
  console.log(JSON.stringify({ bound, completionEvents, streamId: S.activeStreamId, busy: S.busy, inProgress: scope._sendInProgress, starts: state.starts, aborts: state.aborts, localSettlements: state.localSettlements, replacementId: state.replacementId || null, leaseId: voiceApi.lease().id, leaseSettled: voiceApi.lease().settled, finalState: voiceApi.state(), petCalls: state.petCalls || 0 }));
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""


def _run_runtime():
    encoded = base64.b64encode((_voice_runtime() + "\n" + VOICE_COMPLETE_SOURCE).encode()).decode()
    owner_encoded = base64.b64encode(OWNER_SOURCE.encode()).decode()
    script = HARNESS.replace(
        "${RUNTIME}", "${Buffer.from('" + encoded + "','base64').toString()}"
    )
    script = script.replace("${OWNER_B64}", owner_encoded)
    result = subprocess.run([NODE], input=script, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def _run_production_send():
    script = PRODUCTION_HARNESS.replace(
        "${SEND_B64}", base64.b64encode(SEND_SOURCE.encode()).decode()
    ).replace("${OWNER_B64}", base64.b64encode(OWNER_SOURCE.encode()).decode())
    script = script.replace("${RUNTIME_B64}", base64.b64encode(_voice_runtime().encode()).decode())
    script = script.replace("${COMPLETE_B64}", base64.b64encode(VOICE_COMPLETE_SOURCE.encode()).decode())
    script = script.replace("${SPEAK_B64}", base64.b64encode(SPEAK_SOURCE.encode()).decode())
    script = script.replace("${ACTIVATE_B64}", base64.b64encode(ACTIVATE_SOURCE.encode()).decode())
    result = subprocess.run([NODE], input=script, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def _run_production_local_pet(replace_owner=False):
    script = PRODUCTION_HARNESS.replace(
        "const msg = { value: 'production voice text' };", "const msg = { value: '/pet' };"
    ).replace(
        "parseCommand: () => null,", "parseCommand: () => ({ name: 'pet', args: '' }),"
    ).replace(
        "_pendingSelections: [],", "_pendingSelections: [], handlePetSlashCommand: async () => { state.petCalls = (state.petCalls || 0) + 1; return { handled: true, message: 'pet response' }; },"
    )
    if replace_owner:
        script = script.replace(
            "handlePetSlashCommand: async () => { state.petCalls = (state.petCalls || 0) + 1; return { handled: true, message: 'pet response' }; },",
            "handlePetSlashCommand: async () => { state.petCalls = (state.petCalls || 0) + 1; await Promise.resolve(); const replacement=windowObj._voiceLeasePrepareSubmission({ replaceOwner: true }); state.replacementId=replacement && replacement.id; return { handled: true, message: 'pet response' }; },",
        )
    script = script.replace(
        "${SEND_B64}", base64.b64encode(SEND_SOURCE.encode()).decode()
    ).replace("${OWNER_B64}", base64.b64encode(OWNER_SOURCE.encode()).decode())
    script = script.replace("${RUNTIME_B64}", base64.b64encode(_voice_runtime().encode()).decode())
    script = script.replace("${COMPLETE_B64}", base64.b64encode(VOICE_COMPLETE_SOURCE.encode()).decode())
    script = script.replace("${SPEAK_B64}", base64.b64encode(SPEAK_SOURCE.encode()).decode())
    script = script.replace("${ACTIVATE_B64}", base64.b64encode(ACTIVATE_SOURCE.encode()).decode())
    result = subprocess.run([NODE], input=script, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


NO_SPEECH_HARNESS = r"""
const state = { starts: 0, sends: 0, aborts: 0, recognitions: [] };
let now = 0;
let nextTimer = 1;
const timers = new Map();
function setTimer(cb, delay) { const id = nextTimer++; timers.set(id, { at: now + (delay || 0), cb }); return id; }
function clearTimer(id) { timers.delete(id); }
function advance(ms) {
  const target = now + ms;
  for (;;) {
    const due = [...timers.entries()].filter(([, t]) => t.at <= target).sort((a, b) => (a[1].at - b[1].at) || (a[0] - b[0]))[0];
    if (!due) break;
    timers.delete(due[0]); now = due[1].at; due[1].cb();
  }
  now = target;
}
function SpeechRecognition() {
  const instance = { onresult: null, onend: null, onerror: null, start() { state.starts += 1; }, abort() { state.aborts += 1; } };
  state.recognitions.push(instance); return instance;
}
const localStorage = { getItem(key) { return ({ 'hermes-voice-mode-button': 'true', 'hermes-voice-silence-ms': '1000', 'hermes-voice-continuous': 'false', 'hermes-tts-engine': 'browser' })[key] || null; } };
const element = () => ({ style: {}, classList: { add() {}, remove() {} }, textContent: '', className: '', value: '' });
const modeBtn = element(), bar = element(), indicator = element(), label = element(), micBtn = element(), ta = element();
const windowObj = { SpeechRecognition, speechSynthesis: { cancel() {}, speaking: false, getVoices() { return []; }, speak() {} } };
const document = { querySelectorAll() { return []; } };
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
const _speakResponse = () => {};
const runtimeSource = Buffer.from('${RUNTIME_B64}', 'base64').toString();
const api = new Function('window','document','SpeechRecognition','localStorage','modeBtn','bar','indicator','label','micBtn','ta','S','t','autoResize','_micOriginNeedsSecureContext','_deactivate','showToast','_setButtonTooltip','stopTTS','_locale','send','setTimeout','clearTimeout','Date','_speakResponse', runtimeSource + `
return { activate() { _voiceModeActive = true; _voiceContextId += 1; _voiceLease = _newVoiceLease(); }, start: () => _startListening(_voiceLease), first: () => _voiceLease && _voiceLease.recognition, state: () => _voiceModeState };`)(windowObj, document, SpeechRecognition, localStorage, modeBtn, bar, indicator, label, micBtn, ta, S, t, autoResize, _micOriginNeedsSecureContext, _deactivate, showToast, _setButtonTooltip, stopTTS, _locale, send, setTimer, clearTimer, { now: () => now }, _speakResponse);
api.activate(); api.start();
const first = api.first();
first.onerror({ error: 'no-speech' });
first.onend();
advance(800);
const second = api.first();
second.onresult({ resultIndex: 0, results: [{ 0: { transcript: 'hello' }, isFinal: true }] });
const abortsBeforeStaleEnd = state.aborts;
first.onend();
const liveAfterStaleEnd = api.first() === second;
const staleEndAborts = state.aborts - abortsBeforeStaleEnd;
advance(1000);
console.log(JSON.stringify({ starts: state.starts, sends: state.sends, aborts: state.aborts, liveAfterStaleEnd, staleEndAborts, state: api.state() }));
"""


def _run_no_speech_restart():
    script = NO_SPEECH_HARNESS.replace(
        "${RUNTIME_B64}", base64.b64encode(_voice_runtime().encode()).decode()
    )
    result = subprocess.run([NODE], input=script, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


ATTACH_RACE_HARNESS = r"""
const attachSource = Buffer.from('${ATTACH_B64}', 'base64').toString();
const runtimeSource = Buffer.from('${RUNTIME_B64}', 'base64').toString();
const state = { sources: [], completionEvents: [], starts: 0, aborts: 0, ownerSettlements: [], unhandled: [] };
function SpeechRecognition() {
  const instance = { onresult: null, onend: null, onerror: null, start() { state.starts += 1; }, abort() { state.aborts += 1; } };
  state.recognitions = state.recognitions || []; state.recognitions.push(instance); return instance;
}
class FakeEventSource {
  static OPEN = 1;
  constructor(url) { this.url = url; this.readyState = 0; this.handlers = {}; this.closed = false; state.sources.push(this); }
  addEventListener(name, handler) { (this.handlers[name] ||= []).push(handler); }
  close() { this.closed = true; this.readyState = 2; }
  emit(name, event) { for (const handler of this.handlers[name] || []) handler(event); }
}
const noOp = () => {};
const windowObj = {
  _voiceModeOnResponseComplete(...args) { state.completionEvents.push(args); },
  SpeechRecognition,
};
const documentObj = {
  hidden: false, visibilityState: 'visible', baseURI: 'http://localhost/',
  addEventListener() {}, hasFocus() { return true; }, querySelector() { return null; },
  querySelectorAll() { return []; }, getElementById() { return null; },
  createElement() { return { style: {}, dataset: {}, classList: { add() {}, remove() {} } }; },
};
const S = {
  session: { session_id: 's1', active_stream_id: null, pending_started_at: 1 },
  messages: [], toolCalls: [], activeStreamId: null, busy: false,
};
const INFLIGHT = { s1: { streamId: 'stream-1', messages: [], uploaded: [], toolCalls: [] } };
const localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
const voiceElement = () => ({ style: {}, classList: { add() {}, remove() {} }, textContent: '', className: '', value: '' });
const modeBtn = voiceElement(), bar = voiceElement(), indicator = voiceElement(), label = voiceElement(), micBtn = voiceElement(), ta = voiceElement();
const voiceApi = new Function('window','document','SpeechRecognition','localStorage','modeBtn','bar','indicator','label','micBtn','ta','S','t','autoResize','_micOriginNeedsSecureContext','_deactivate','showToast','_setButtonTooltip','stopTTS','_locale','send','setTimeout','clearTimeout','Date','_speakResponse', runtimeSource + `
return { activate() { _voiceModeActive = true; _voiceContextId += 1; _voiceLease = _newVoiceLease(); }, start: () => _startListening(_voiceLease), adopt: window._voiceLeaseAdoptStream, complete: window._voiceModeOnResponseComplete, settle: window._voiceLeaseSettleOwner, first: () => _voiceLease && _voiceLease.recognition, state: () => _voiceModeState, lease: () => _voiceLease };`)(windowObj, documentObj, SpeechRecognition, localStorage, modeBtn, bar, indicator, label, micBtn, { value: '' }, S, key => key, () => {}, () => false, () => {}, () => {}, () => {}, { _speech: 'en-US' }, () => {}, setTimeout, clearTimeout, Date, () => {});
windowObj._voiceLeaseAdoptStream = (...args) => voiceApi.adopt(...args);
windowObj._voiceModeOnResponseComplete = (...args) => voiceApi.complete(...args);
const originalOwnerSettlement = windowObj._voiceLeaseSettleOwner;
windowObj._voiceLeaseSettleOwner = (...args) => { state.ownerSettlements.push(args); return originalOwnerSettlement(...args); };
process.on('unhandledRejection', error => state.unhandled.push(String(error && error.stack || error)));
const scope = {
  window: windowObj, document: documentObj, location: { href: 'http://localhost/' },
  S, INFLIGHT, EventSource: FakeEventSource, localStorage,
  api: async () => ({ active: false, replay_available: false }), setTimeout, clearTimeout,
  requestAnimationFrame: callback => setTimeout(callback, 0),
  cancelAnimationFrame: clearTimeout,
  URL, encodeURIComponent, console,
  _desktopBackgroundedForNotifications: false,
  _sendInProgress: false, _sendInProgressSid: null,
  setBusy: value => { S.busy = value; }, setComposerStatus: noOp, setStatus: noOp,
  _isActiveSession: () => true,
  showLiveRunStatus: noOp, hideLiveRunStatus: noOp, _clearLiveRunStatusTimer: noOp,
  snapshotLiveTurnHtmlForSession: noOp, _resumeSessionStreamAfterLiveChat: noOp,
  _suspendSessionStreamForLiveChat: noOp, saveInflightState: noOp,
  renderSessionList: noOp, renderMessages: noOp, clearInflight: noOp,
  clearInflightState: noOp, resetTurnWorkspaceMutations: noOp, _resetStreamScrollFollow: noOp,
  appendThinking: noOp, ensureLiveWorklogShell: noOp, updateSendBtn: noOp,
  setComposerStatus: noOp, startApprovalPolling: noOp, startClarifyPolling: noOp,
  _fetchYoloState: noOp, _clearLiveRunStatusTimer: noOp,
  _voiceLeaseRetargetOwner: noOp,
};
const builtins = new Set(['Array','Boolean','Buffer','Date','Error','EventSource','JSON','Map','Math','Number','Object','Promise','RegExp','Set','String','Symbol','URL','WeakMap','undefined','NaN','Infinity','isNaN','parseInt','encodeURIComponent','decodeURIComponent','setTimeout','clearTimeout','console']);
for (const match of attachSource.matchAll(/\b[A-Za-z_$][\w$]*\b/g)) {
  const name = match[0];
  if (!builtins.has(name) && !(name in scope)) scope[name] = noOp;
}
const attachFactory = new Function('scope', `with(scope){
  const LIVE_STREAMS = {};
  const LIVE_STREAM_TRANSPORT_AUTHORITY = Object.create(null);
  const LIVE_STREAM_TRANSPORT_SOURCE_GENERATION = new WeakMap();
  let LIVE_STREAM_TRANSPORT_GENERATION = 0;
  function _releaseLiveStreamTransportAuthority(sid, generation) {
    const authority = LIVE_STREAM_TRANSPORT_AUTHORITY[sid];
    if (authority && authority.generation === generation) delete LIVE_STREAM_TRANSPORT_AUTHORITY[sid];
  }
  window._liveStreamTransportAuthority = LIVE_STREAM_TRANSPORT_AUTHORITY;
  window._liveStreamTransportSourceGeneration = LIVE_STREAM_TRANSPORT_SOURCE_GENERATION;
  return (${attachSource});
}`);
const attach = attachFactory(scope);
(async () => {
  voiceApi.activate(); voiceApi.start();
  S.activeStreamId = 'stream-1'; S.session.active_stream_id = 'stream-1'; S.busy = true;
  voiceApi.adopt('s1', 'stream-1');
  attach('s1', 'stream-1', [], { reconnecting: true });
  await new Promise(resolve => setTimeout(resolve, 0));
  await new Promise(resolve => setTimeout(resolve, 1500));
  console.log(JSON.stringify({ sources: state.sources.length, completionEvents: state.completionEvents.length, ownerSettlements: state.ownerSettlements.length, starts: state.starts, aborts: state.aborts, state: voiceApi.state(), owner: voiceApi.lease().owner, activeStreamId: S.activeStreamId, sessionActiveStreamId: S.session.active_stream_id, busy: S.busy, unhandled: state.unhandled }));
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""


def _run_attach_transport_race() -> dict:
    script = ATTACH_RACE_HARNESS.replace(
        "${ATTACH_B64}", base64.b64encode(ATTACH_SOURCE.encode()).decode()
    ).replace("${RUNTIME_B64}", base64.b64encode((_voice_runtime() + "\n" + VOICE_COMPLETE_SOURCE).encode()).decode())
    result = subprocess.run([NODE], input=script, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def _run_session_transition(source: str, setup: str, call: str) -> dict:
    encoded = base64.b64encode(source.encode()).decode()
    script = f"""
const source=Buffer.from('{encoded}','base64').toString();
const mode={json.dumps(setup)};
const scope={{mode, currentSid:'s1', window:{{}}, document:{{hidden:false,visibilityState:'visible'}}, location:{{href:'http://localhost/'}},
  history:{{replaceState(){{}}}}, localStorage:{{getItem(){{return null;}},setItem(){{}},removeItem(){{}}}},
  S:{{session:mode==='cold'?null:{{session_id:'s1',workspace:'w',model:'m',profile:'default'}},messages:[],pendingFiles:[],toolCalls:[],busy:false,activeStreamId:null,activeProfile:'default'}},
  _loadingSessionId:null,_loadSessionGeneration:0,_sendInProgress:mode==='send',_newSessionInFlight:null,
  _voiceResumeCount:0,_voiceInvalidateCount:0,_metadataWrites:0,
  $:()=>({{value:'',style:{{}},options:[],classList:{{add(){{}},remove(){{}}}},querySelectorAll(){{return [];}}}}),
  api:async()=>{{if(mode==='stale') scope._loadingSessionId='other'; const e=Error('network');e.status=mode==='self'?404:500;throw e;}},
  window:{{_voiceLeaseInvalidate(){{scope._voiceInvalidateCount++;}},_voiceLeaseCaptureContext(){{return {{sid:'s1',contextId:scope._voiceInvalidateCount}};}},_voiceLeaseContextCurrent(){{return mode!=='superseded';}},_voiceLeaseResume(){{scope._voiceResumeCount++;}},_clearPendingSelections(){{}}}},
  setComposerStatus(){{}},showToast(){{}},renderSessionList(){{}},startSessionStream(){{}},stopSessionStream(){{}},
  updateQueueBadge(){{}},clearLiveToolCards(){{}},renderMessages(){{}},syncTopbar(){{}},updateSendBtn(){{}},
  setStatus(){{}},loadDir(){{return Promise.resolve();}},refreshSessionList(){{}},
  _voiceLeaseSettleLocal(){{}},_applySessionContextMetadataUpdate(){{scope._metadataWrites++;}},
}};
const builtins=new Set(['Array','Boolean','Buffer','Date','Error','JSON','Map','Math','Number','Object','Promise','RegExp','Set','String','Symbol','URL','undefined','NaN','Infinity','isNaN','parseInt','encodeURIComponent','decodeURIComponent','setTimeout','clearTimeout','console']);
for(const match of source.matchAll(/\\b[A-Za-z_$][\\w$]*\\b/g)){{const name=match[0];if(!builtins.has(name)&&!(name in scope))scope[name]=()=>{{}};}}
const fn=new Function('scope','with(scope){{return ('+source+');}}')(scope);
(async()=>{{try{{await ({call});}}catch(_){{}} console.log(JSON.stringify({{resume:scope._voiceResumeCount,invalidate:scope._voiceInvalidateCount,metadata:scope._metadataWrites,loading:scope._loadingSessionId}}));}})();
"""
    result = subprocess.run([NODE], input=script, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def _run_profile_model_race() -> dict:
    encoded = base64.b64encode(PROFILE_SOURCE.encode()).decode()
    script = f"""
const source=Buffer.from('{encoded}','base64').toString();
const makeEl=()=>({{style:{{}},classList:{{add(){{}},remove(){{}}}},disabled:false,value:'',options:[],querySelectorAll(){{return [];}}}});
const scope={{
  window:{{_activeProvider:'old-provider',_defaultModel:'old-model',_voiceLeaseInvalidate(){{}},_voiceLeaseCaptureContext(){{return {{sid:'s1',contextId:1}};}},_voiceLeaseContextCurrent(){{return true;}},_voiceLeaseResume(){{}}}},
  document:{{hidden:false,visibilityState:'visible',querySelector(){{return null;}},querySelectorAll(){{return []; }},createElement:makeEl}},
  location:{{href:'http://localhost/'}},history:{{replaceState(){{}}}},localStorage:{{getItem(){{return null;}},setItem(){{}},removeItem(){{}}}},
  S:{{session:{{session_id:'s1',workspace:'w',model:'old-model',model_provider:'old-provider',profile:'default'}},messages:[],pendingFiles:[],toolCalls:[],busy:false,activeStreamId:null,activeProfile:'default'}},
  _profileSwitchGeneration:0,_profileSwitchOpeningExistingSession:false,_workspacePanelMode:'closed',_sendInProgress:false,
  _profileMatchesActiveProfile:()=>true,
  $:()=>makeEl(),
  api:async()=>{{scope.S.session.model='new-model';scope.S.session.model_provider='new-provider';return {{active:'other',is_default:false,default_model:'old-model',default_model_provider:'old-provider'}};}},
  renderSessionList:async()=>{{}},renderSessionListFromCache(){{}},loadDir:async()=>{{}},
  setTimeout,clearTimeout,
}};
const builtins=new Set(['Array','Boolean','Buffer','Date','Error','JSON','Map','Math','Number','Object','Promise','RegExp','Set','String','Symbol','URL','undefined','NaN','Infinity','isNaN','parseInt','encodeURIComponent','decodeURIComponent','setTimeout','clearTimeout','console']);
for(const match of source.matchAll(/\\b[A-Za-z_$][\\w$]*\\b/g)){{const name=match[0];if(!builtins.has(name)&&!(name in scope))scope[name]=()=>{{}};}}
const fn=new Function('scope','with(scope){{return ('+source+');}}')(scope);
(async()=>{{await fn('other');console.log(JSON.stringify({{model:scope.S.session.model,provider:scope.S.session.model_provider,profile:scope.S.session.profile}}));}})().catch(error=>{{console.error(error.stack||error);process.exitCode=1;}});
"""
    result = subprocess.run([NODE], input=script, capture_output=True, text=True)
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
    assert result["duplicateStart"] == {"starts": 1, "recognizerChanged": False, "aborts": 0}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_voice_lease_ignores_duplicate_or_stale_owner_callbacks():
    result = _run_runtime()
    assert result["settled"] is True
    assert result["duplicate"] is False
    assert result["stale"] is False
    assert result["wrongOwner"] is False
    assert result["beforeValidOwnerState"] == "thinking"
    assert result["aborts"] >= 1
    assert result["staleSourceState"] == "thinking"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_actual_messages_owner_seam_settles_voice_outcome_once():
    result = _run_runtime()
    assert result["ownerEvents"] == [["s1", "stream-1", {}, 1, {"success": False}]]
    assert result["transportEvents"][0][3] == 1
    assert result["transportEvents"][1][3] == 2


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_production_send_binds_and_settles_through_owner_seam():
    result = _run_production_send()
    assert [entry["type"] for entry in result["bound"]] == ["prepare", "bind", "attach"]
    assert result["bound"][1] == {"type": "bind", "streamId": "stream-1", "sid": "s1"}
    assert result["completionEvents"] == [["s1", "stream-1", {}, 1, {"success": True}]]
    assert result["streamId"] is None
    assert result["busy"] is False
    assert result["inProgress"] is False
    assert result["starts"] >= 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_production_local_non_command_slash_uses_voice_transition():
    result = _run_production_local_pet()
    assert [entry["type"] for entry in result["bound"]] == ["prepare"]
    assert result["petCalls"] == 1
    assert result["completionEvents"] == []
    assert result["aborts"] == 1
    assert len(result["localSettlements"]) == 1
    assert result["starts"] == 2
    assert result["finalState"] == "listening"
    assert result["streamId"] is None
    assert result["busy"] is False
    assert result["inProgress"] is False


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_local_slash_finally_cannot_settle_a_replacement_voice_lease():
    result = _run_production_local_pet(replace_owner=True)
    assert result["petCalls"] == 1
    assert len(result["localSettlements"]) == 1
    assert result["localSettlements"][0] != result["replacementId"]
    assert result["finalState"] == "thinking", result


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_no_speech_restart_cannot_replace_newer_live_recognizer():
    result = _run_no_speech_restart()
    assert result == {"starts": 2, "sends": 1, "aborts": 1, "liveAfterStaleEnd": True, "staleEndAborts": 0, "state": "thinking"}


def test_production_send_and_stream_paths_share_the_lease_seams():
    assert "_voiceLeasePrepareSubmission" not in SEND_SOURCE.split("if(!S.session)", 1)[0]
    assert "if(typeof window._voiceLeasePrepareSubmission==='function') window._voiceLeasePrepareSubmission();" in SEND_SOURCE
    assert "if(streamId&&typeof window._voiceLeaseBind==='function') window._voiceLeaseBind(streamId,activeSid);" in SEND_SOURCE
    assert "if(!streamId&&!_voiceLocalDispatchSettled&&typeof window._voiceLeaseSettleLocal==='function') window._voiceLeaseSettleLocal();" in SEND_SOURCE
    assert ATTACH_SOURCE.count("_setActivePaneIdleIfOwner({success:true},source,_transportGeneration);") == 1
    assert ATTACH_SOURCE.count("_setActivePaneIdleIfOwner({success:false},source,_transportGeneration);") >= 5
    assert "window._voiceModeOnResponseComplete(ownerSid,ownerStreamId,terminalSource,terminalGeneration,voiceOutcome);" in OWNER_SOURCE


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reconnecting_dead_stream_settles_exact_voice_owner_without_event_source():
    result = _run_attach_transport_race()
    assert result == {
        "sources": 0,
        "completionEvents": 0,
        "ownerSettlements": 1,
        "starts": 1,
        "aborts": 1,
        "state": "listening",
        "owner": {"sid": "s1", "streamId": "stream-1"},
        "activeStreamId": None,
        "sessionActiveStreamId": None,
        "busy": False,
        "unhandled": [],
    }


def test_change4_exact_terminal_and_parsed_slash_boundaries_are_behavioral_contracts():
    assert "window._voiceModeOnResponseComplete(ownerSid,ownerStreamId,terminalSource,terminalGeneration,voiceOutcome);" in OWNER_SOURCE
    parsed_boundary = SEND_SOURCE.index("const _parsedCmd=parseCommand(text);")
    prepare_boundary = SEND_SOURCE.index("_voiceLeasePrepareSubmission", parsed_boundary)
    command_dispatch = SEND_SOURCE.index("const _cmd=", parsed_boundary)
    assert parsed_boundary < prepare_boundary < command_dispatch


def test_change4_shared_stream_adoption_and_duplicate_listener_guard_exist():
    assert "_voiceLeaseAdoptStream(activeSid,streamId)" in ATTACH_SOURCE
    start_source = _function_source(BOOT, "_startListening")
    assert "lease.recognition" in start_source.split("_clearBrowserTtsRecovery", 1)[0]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("mode,expected_resume", [("current", 1), ("stale", 0), ("self", 0), ("send", 0)])
def test_load_session_failure_recovers_only_the_surviving_voice_owner(mode, expected_resume):
    result = _run_session_transition(LOAD_SOURCE, mode, "fn('s1',{force:true})")
    assert result["resume"] == expected_resume


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_failed_switch_resumes_the_surviving_current_session_voice_owner():
    result = _run_session_transition(LOAD_SOURCE, "current", "fn('s2',{force:true})")
    assert result["resume"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_failure_recovers_prior_owner_and_clears_cold_start_lease():
    prior = _run_session_transition(NEW_SESSION_SOURCE, "prior", "fn(false)")
    cold = _run_session_transition(NEW_SESSION_SOURCE, "cold", "fn(false)")
    assert prior["resume"] == 1
    assert cold["resume"] == 0
    assert cold["invalidate"] == 2


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_same_session_model_transition_stays_inert_when_lease_context_is_superseded():
    result = _run_session_transition(MODEL_HANDLER_SOURCE, "superseded", "fn()")
    assert result["metadata"] == 0
    assert result["resume"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_same_session_profile_transition_stays_inert_when_lease_context_is_superseded():
    result = _run_session_transition(PROFILE_SOURCE, "superseded", "fn('other')")
    assert result["resume"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_profile_transition_does_not_overwrite_newer_same_session_model():
    result = _run_profile_model_race()
    assert result == {"model": "new-model", "provider": "new-provider", "profile": "other"}
