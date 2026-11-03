"""Request lifecycle for server-backed TTS playback (#6541).

`stopTTS()` invalidated the playback generation (so a late synthesis response
could no longer start audio), but it never cancelled the request itself: the
in-flight `/api/tts` fetch kept running after the user pressed stop and after a
replacement playback had claimed the generation. A stale synthesis therefore
stayed on the wire — it still reached the provider, still counted against the
per-client rate window, and its response was decoded into nothing.

These scenarios drive the REAL ui.js functions under node (same harness style as
`test_openai_tts_chunk_generation_race.py`: extract the functions from
`static/ui.js`, run them against a controllable `fetch` that records the
`AbortSignal` it receives) and assert the observable request/playback outcome:

  1. stop with a request pending aborts it, and a response that still lands
     never reaches the AudioContext (no decode, no source, no toast).
  2. a double click on the Listen button issues no second request, aborts the
     first, and a later click yields exactly one playback.
  3. stop during audible playback still stops the source and clears state.
  4. the same cancellation contract on the ElevenLabs sender.
  5. a replacement playback aborts the prior request but never its own.
  6. an aborted request settles silently — cancellation is not a user error.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER = r'''
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunction(src, name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', src.indexOf(')', start));
  let depth = 1;
  i += 1;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth += 1;
    else if (src[i] === '}') depth -= 1;
    i += 1;
  }
  return src.slice(start, i);
}

// ---- module-level state the extracted ui.js functions touch ----
let _ttsSpeaking = false;
let _ttsGeneration = 0;
let _ttsRequestMinGapMs = 2000;
let _ttsLastRequestTs = 0;
let _ttsCurrentUtterance = null;
let _ttsChunkQueue = [];
let _ttsChunkIndex = 0;
let _ttsActiveBtn = null;
let _playingEdgeAudio = null;
let _ttsAudioCtx = null;
// Tracked pending-request registry (absent on the pre-fix head; the driver
// declares it so extracted helpers that assign to it resolve).
let _ttsPendingAborts = null;

function t(key){ return key; }
const S = { session: { session_id: 's6541' }, activeProfile: 'default' };

// ---- controllable fakes ----
const toasts = [];
function showToast(msg){ toasts.push(String(msg)); }

// `ignoreAbort` models a transport that keeps the request alive (server already
// synthesizing): the signal is aborted, but the response can still arrive.
let ignoreAbort = false;
let fetchCalls = [];
globalThis.fetch = function(url, opts){
  const entry = {
    url: url,
    opts: opts,
    signal: (opts && opts.signal) || null,
    aborted: false,
    ignore: ignoreAbort,
  };
  const promise = new Promise(function(resolve, reject){
    entry.resolve = resolve;
    entry.reject = reject;
    if (entry.signal && typeof entry.signal.addEventListener === 'function') {
      entry.signal.addEventListener('abort', function(){
        entry.aborted = true;
        if (entry.ignore) return;
        const err = new Error('The operation was aborted.');
        err.name = 'AbortError';
        reject(err);
      });
    }
  });
  entry.promise = promise;
  fetchCalls.push(entry);
  return promise;
};

const startedSources = [];
class FakeAudioContext {
  constructor(){
    this.state = 'running';
    this.destination = {};
    this._decode = null;
    this.decodeCalls = 0;
  }
  resume(){ this.state = 'running'; return Promise.resolve(); }
  decodeAudioData(buf, ok, err){ this.decodeCalls += 1; this._decode = { ok, err }; }
  createBufferSource(){
    const src = {
      buffer: null,
      connect(){},
      start(){ this.started = true; },
      stop(){ this.stopped = true; },
      disconnect(){ this.disconnected = true; },
      onended: null,
    };
    startedSources.push(src);
    return src;
  }
}

class FakeURL {
  constructor(path, base){ this.href = (base || '') + path; }
}
FakeURL.createObjectURL = () => 'blob:fake';
FakeURL.revokeObjectURL = () => {};
const URL = FakeURL;
class Blob { constructor(parts, opts){ this.parts = parts; this.type = (opts && opts.type) || ''; } }

const speechSynthesis = {
  cancelCalls: 0,
  cancel(){ this.cancelCalls += 1; },
  speak(){},
  getVoices(){ return []; },
};
class FakeUtterance {
  constructor(text){ this.text = text; this.rate = 1; this.pitch = 1; this.voice = null; }
}
const SpeechSynthesisUtterance = FakeUtterance;

const _ls = {};
const localStorage = {
  getItem(k){ return Object.prototype.hasOwnProperty.call(_ls, k) ? _ls[k] : null; },
  setItem(k, v){ _ls[k] = String(v); },
};

let domRows = [];
const allBtns = [];
const document = {
  baseURI: 'http://localhost/',
  querySelectorAll(sel){
    if (sel.indexOf('data-speaking') >= 0) {
      return allBtns.filter((b) => b.dataset && b.dataset.speaking === '1');
    }
    if (sel.indexOf('assistant') >= 0) return domRows;
    return [];
  },
};
const location = { href: 'http://localhost/' };
const window = {
  AudioContext: FakeAudioContext,
  speechSynthesis,
  _hermesTtsIsRegistered: () => false,
  _hermesTtsSynth: () => Promise.resolve(new ArrayBuffer(8)),
};

eval(['_splitForTTS', '_stripForTTS', '_beginTtsPlayback', '_ownsTtsPlayback',
  '_noteTtsRequestSent', '_ttsRequestWaitMs', '_acquireTtsRequestSlot', '_sendTtsRequest',
  '_stopActivePlaybackAudio', '_getTtsAudioCtx', '_playAudioBuf', '_playOpenaiTts',
  '_playElevenLabsTts', 'speakMessage', 'stopTTS']
  .map((n) => extractFunction(ui, n)).join('\n'));
// Request-registry helpers: absent on the pre-fix head, where nothing tracked
// the in-flight request at all. Extracted leniently so the same driver runs
// against both revisions and the failure is the missing cancellation, not a
// missing symbol.
eval(['_abortPendingTtsRequests', '_newTtsAbortController', '_trackTtsAbort', '_releaseTtsAbort']
  .map((n) => { try { return extractFunction(ui, n); } catch (e) { return ''; } })
  .join('\n'));

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
function fakeBtn(text){
  const btn = { dataset: {}, closest: () => ({ dataset: { rawText: text || 'Hello there.' } }) };
  allBtns.push(btn);
  return btn;
}
const okResp = () => Promise.resolve({
  ok: true, status: 200,
  headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'audio/mpeg' : null) },
  arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
});

function resetState(){
  _ttsSpeaking = false; _ttsGeneration = 0; _ttsRequestMinGapMs = 200; _ttsLastRequestTs = 0;
  _ttsCurrentUtterance = null; _ttsChunkQueue = []; _ttsChunkIndex = 0; _ttsActiveBtn = null;
  _playingEdgeAudio = null; _ttsAudioCtx = null;
  _ttsPendingAborts = null;
  ignoreAbort = false;
  toasts.length = 0; fetchCalls.length = 0; startedSources.length = 0;
  allBtns.length = 0; domRows = [];
  speechSynthesis.cancelCalls = 0;
  for (const k of Object.keys(_ls)) delete _ls[k];
}

function assert(cond, msg){ if (!cond) throw new Error(msg); }
function assertRequestAborted(entry, label){
  assert(!!entry, label + ': request was never issued');
  assert(!!entry.signal, label + ': request carried no AbortSignal (stop did not cancel it)');
  assert(entry.signal.aborted === true,
    label + ': stopTTS() left the pending TTS request running (signal not aborted)');
}
function noAudioStarted(label){
  assert(_ttsAudioCtx === null || _ttsAudioCtx.decodeCalls === 0,
    label + ': a cancelled request still reached the AudioContext');
  assert(startedSources.length === 0, label + ': a cancelled request still started audio');
}

// 1. Stop while the first synthesis request is pending: the request must be
//    aborted, and a response that lands anyway must never start audio.
function scenario1(){
  resetState();
  ignoreAbort = true;                       // transport keeps the request alive
  const btn = fakeBtn();
  _playOpenaiTts('AAAA', btn);
  return sleep(0).then(() => {
    assert(fetchCalls.length === 1, 's1: request not issued (calls=' + fetchCalls.length + ')');
    stopTTS();
    assertRequestAborted(fetchCalls[0], 's1');
    fetchCalls[0].resolve(okResp());        // response arrives after the stop
    return sleep(40);
  }).then(() => {
    noAudioStarted('s1');
    assert(_ttsSpeaking === false, 's1: speaking state left on after stop');
    assert(btn.dataset.speaking === '0', 's1: listen button left in the speaking state');
    assert(toasts.length === 0, 's1: cancellation surfaced a toast: ' + JSON.stringify(toasts));
    return 'PASS';
  });
}

// 2. Double click: the second click stops instead of issuing a second request;
//    a later click produces exactly one playback.
function scenario2(){
  resetState();
  _ls['hermes-tts-engine'] = 'openai';
  const btn = fakeBtn();
  speakMessage(btn);
  return sleep(0).then(() => {
    assert(fetchCalls.length === 1, 's2: first click issued no request');
    speakMessage(btn);                      // second click on a speaking button
    assertRequestAborted(fetchCalls[0], 's2');
    assert(fetchCalls.length === 1,
      's2: double click issued a second request (calls=' + fetchCalls.length + ')');
    fetchCalls[0].resolve(okResp());        // stale response arrives
    return sleep(40);
  }).then(() => {
    noAudioStarted('s2');
    ignoreAbort = false;
    return sleep(250);                      // let the shared pacing window lapse
  }).then(() => {
    speakMessage(btn);                      // fresh click
    return sleep(0);
  }).then(() => {
    assert(fetchCalls.length === 2, 's2: re-listen issued no request (calls=' + fetchCalls.length + ')');
    assert(fetchCalls[1].signal && !fetchCalls[1].signal.aborted,
      's2: the new playback request was aborted prematurely');
    fetchCalls[1].resolve(okResp());
    return sleep(40);
  }).then(() => {
    assert(_ttsAudioCtx && _ttsAudioCtx.decodeCalls === 1,
      's2: the new playback did not decode exactly once');
    _ttsAudioCtx._decode.ok({});
    assert(startedSources.length === 1,
      's2: double click produced overlapping playbacks (sources=' + startedSources.length + ')');
    assert(_playingEdgeAudio === startedSources[0], 's2: the new playback is not the active source');
    return 'PASS';
  });
}

// 3. Stop during audible playback still stops the source.
function scenario3(){
  resetState();
  const btn = fakeBtn();
  _playOpenaiTts('AAAA', btn);
  return sleep(0).then(() => {
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    _ttsAudioCtx._decode.ok({});
    assert(startedSources.length === 1, 's3: playback did not start');
    assert(_playingEdgeAudio === startedSources[0], 's3: active source not tracked');
    stopTTS();
    assert(startedSources[0].stopped === true, 's3: stopTTS() did not stop the audible source');
    assert(_playingEdgeAudio === null, 's3: active source handle survived stop');
    assert(_ttsSpeaking === false, 's3: speaking state survived stop');
    assert(btn.dataset.speaking === '0', 's3: listen button survived stop');
    return 'PASS';
  });
}

// 4. The ElevenLabs sender honours the same cancellation contract.
function scenario4(){
  resetState();
  ignoreAbort = true;
  const btn = fakeBtn();
  _playElevenLabsTts('hello there', btn);
  return sleep(0).then(() => {
    assert(fetchCalls.length === 1, 's4: elevenlabs request not issued');
    assert(JSON.parse(fetchCalls[0].opts.body).engine === 'elevenlabs', 's4: wrong engine payload');
    stopTTS();
    assertRequestAborted(fetchCalls[0], 's4');
    fetchCalls[0].resolve(okResp());
    return sleep(40);
  }).then(() => {
    noAudioStarted('s4');
    assert(toasts.length === 0, 's4: cancellation surfaced a toast: ' + JSON.stringify(toasts));
    assert(btn.dataset.speaking === '0', 's4: listen button survived stop');
    return 'PASS';
  });
}

// 5. A replacement playback aborts the prior request but never its own.
function scenario5(){
  resetState();
  _playOpenaiTts('AAAA', fakeBtn());
  return sleep(0).then(() => {
    assert(fetchCalls.length === 1, 's5: first request not issued');
    _playOpenaiTts('BB', fakeBtn());        // direct replacement (auto-read/direct caller)
    assertRequestAborted(fetchCalls[0], 's5');
    return sleep(300);                      // shared pacing window
  }).then(() => {
    assert(fetchCalls.length === 2, 's5: replacement request not issued');
    assert(fetchCalls[1].signal && fetchCalls[1].signal.aborted !== true,
      's5: the replacement aborted its own request');
    fetchCalls[1].resolve(okResp());
    return sleep(30);
  }).then(() => {
    assert(_ttsAudioCtx && _ttsAudioCtx.decodeCalls === 1, 's5: replacement did not decode once');
    _ttsAudioCtx._decode.ok({});
    assert(startedSources.length === 1, 's5: unexpected extra playback');
    assert(_playingEdgeAudio === startedSources[0], 's5: replacement is not the active source');
    return 'PASS';
  });
}

// 6. An aborted request settles silently: no toast, no error state, no
//    unhandled rejection.
function scenario6(){
  resetState();
  const unhandled = [];
  const onUnhandled = (e) => { unhandled.push(e); };
  process.on('unhandledRejection', onUnhandled);
  const btn = fakeBtn();
  _playOpenaiTts('AAAA', btn);
  return sleep(0).then(() => {
    stopTTS();                              // transport rejects with AbortError
    return sleep(20);
  }).then(() => {
    process.removeListener('unhandledRejection', onUnhandled);
    assert(unhandled.length === 0, 's6: unhandled rejection after cancel: ' + unhandled[0]);
    assert(toasts.length === 0, 's6: cancellation surfaced a toast: ' + JSON.stringify(toasts));
    assert(_ttsSpeaking === false, 's6: speaking state survived cancel');
    assert(btn.dataset.speaking === '0', 's6: listen button survived cancel');
    noAudioStarted('s6');
    return 'PASS';
  });
}

const runner = {
  scenario1: scenario1, scenario2: scenario2, scenario3: scenario3,
  scenario4: scenario4, scenario5: scenario5, scenario6: scenario6,
}[process.argv[3]];
if (!runner) throw new Error('unknown scenario: ' + process.argv[3]);
let outcome;
try {
  outcome = runner();
} catch (e) {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(1);
}
if (outcome && typeof outcome.then === 'function') {
  outcome.then(
    (verdict) => { process.stdout.write(JSON.stringify({ verdict: verdict })); process.exit(0); },
    (err) => { process.stderr.write(String((err && err.stack) || err)); process.exit(1); }
  );
} else {
  process.stdout.write(JSON.stringify({ verdict: outcome }));
  process.exit(0);
}
'''


def _run(tmp_path, scenario: str) -> str:
    driver = tmp_path / "driver_6541.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(REPO / "static" / "ui.js"), scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f"{scenario} failed under node:\n{result.stderr}")
    return json.loads(result.stdout)["verdict"]


@pytest.mark.parametrize(
    "scenario",
    ["scenario1", "scenario2", "scenario3", "scenario4", "scenario5", "scenario6"],
)
def test_tts_request_lifecycle(tmp_path, scenario):
    assert _run(tmp_path, scenario) == "PASS"
