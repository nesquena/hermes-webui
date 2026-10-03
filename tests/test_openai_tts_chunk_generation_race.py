"""Behavioral race tests for the OpenAI-compatible TTS playback paths.

Reviewer feedback (nesquena-hermes on #7529):

  * Round 2: `_ttsSpeaking` is a single global boolean and cannot distinguish
    playback A from playback B — a stop→start gap lets a late fetch or
    `onended` callback from A resume the cancelled chain under B.
  * Round 3: prefetch must be paced to the server's per-client 2 s TTS rate
    limit (api/routes.py _TtsRateLimiter); auto replacement must stop the
    prior source; synchronous Web Audio failures must not dangle state.
  * Round 4: pacing must be shared ACROSS generations and engines; automatic
    replacement must go through the canonical stop boundary; terminal failure
    must invalidate scheduled work.
  * Round 5: every server-backed TTS request must go through one scheduler
    that reserves/rechecks the shared slot at send time with a bounded,
    owner-aware 429 retry; every playback engine and continuation needs one
    generation/owner token; voice-mode `_speakResponse()` must enter the same
    stop/start boundary; tests must drive the real base + voice-mode entry
    points, all cross-engine pacing directions, and stale Edge/ElevenLabs/
    extension completions after stop→start.
  * Round 6: delayed voice-mode mic rearm must be owner-aware — a stale
    terminal callback's timer must re-check active/speaking/generation at
    execution time so it cannot reopen the mic underneath a replacement;
    the shared `_playAudioBuf` path needs the same owner-aware resume/start/
    error settlement and partial-source cleanup as the OpenAI chunk path.

The driver extracts the REAL functions from static/ui.js and static/boot.js
(`_playOpenaiTts`, `_playEdgeTtsChunked`, `_playElevenLabsTts`, `speakMessage`,
`autoReadLastAssistant`, `_speakResponse`, the shared scheduler helpers, …)
and runs them under node with controllable fetch promises, fake audio sources
and a fake Audio element:

  1-3.  generation guards across stop→start / prefetch pacing / no unhandled
        rejections for abandoned prefetches.
  4-5.  direct replacement stops the prior source and keeps ownership;
        synchronous Web Audio failures are terminal, zero follow-on requests.
  6.    cross-engine pacing both directions through the real engine paths
        (Edge→OpenAI, OpenAI→Edge).
  7-10. decode failure, resume rejection, bounded 429 retry, start() throw.
  11.   real base entry point: autoReadLastAssistant() replaces an active
        Edge playback — canonical boundary, paced OpenAI request, stale Edge
        completion cannot start audio or clobber the new owner.
  12.   real voice-mode entry point: _speakResponse() replaces an active
        OpenAI playback — stop boundary, paced request, stale completion
        cannot clear the new playback's handle.
  13-14. remaining cross-engine pacing directions (OpenAI↔ElevenLabs,
        ElevenLabs↔Edge) plus voice-mode after a live Edge request, with a
        stale voice fetch that must not start audio.
  15.   stale ElevenLabs completion after stop→start must not decode/start,
        and a stale started source's late onended must not clear the newer
        playback's handle/state (_playAudioBuf cleanup ownership guard).
  16.   stale extension-engine synth completion after stop→start.
  17.   bounded 429 retry on the Edge and ElevenLabs senders.
  18.   voice-mode browser playback: a replaced utterance's late onend must
        not reopen the mic, and a stale watchdog must not fire after a
        replacement claimed the turn.
  19.   voice-mode delayed mic rearm ownership: A's retained rearm timer is
        drained after a real speakMessage() replacement — the mic must stay
        closed and B's handle/generation/button state must survive; the
        no-replacement control rearms exactly once.
  20.   shared _playAudioBuf (ElevenLabs): rejected AudioContext.resume() is
        terminal — state/button settled, no source starts, and the returned
        promise resolves (direct settle probe).
  21.   shared _playAudioBuf (registered engine): a synchronous
        createBufferSource throw is terminal and settles.
  22.   shared _playAudioBuf (ElevenLabs): a synchronous start() throw stops/
        disconnects the partially constructed source and settles.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

_DRIVER = r'''
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
const boot = fs.readFileSync(process.argv[3], 'utf8');

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

// ---- stand-ins for the boot.js voice-mode closure ----
let _voiceModeActive = false;
let _voiceModeState = 'idle';
let _voiceModeThinkingSid = null;
let _voiceTtsGenStart = 0;
let _voiceMicRearmTimer = null;
let _browserTtsKeepAlive = null;
let _browserTtsWatchdog = null;
let _browserTtsSuppressNextErrorRearm = false;
let _startListeningCalls = 0;
function _startListening(){ _startListeningCalls++; }
function _setState(state){ _voiceModeState = state; }
const S = { session: { session_id: 's1' } };
function t(key){ return key; }

// ---- controllable fakes ----
const toasts = [];
function showToast(msg){ toasts.push(String(msg)); }

let fetchCalls = [];
globalThis.fetch = function(url, opts){
  return new Promise((resolve, reject) => { fetchCalls.push({ resolve, reject, url, opts }); });
};

const startedSources = [];
class FakeAudioContext {
  constructor(){
    this.state = 'running';
    this.destination = {};
    this._decode = null;
    this.decodeCalls = 0;
    this.throwOnCreateSource = false;
    this.throwOnStart = false;
    this.rejectResume = false;
  }
  resume(){
    if (this.rejectResume) return Promise.reject(new Error('resume denied'));
    this.state = 'running';
    return Promise.resolve();
  }
  decodeAudioData(buf, ok, err){ this.decodeCalls += 1; this._decode = { ok, err }; }
  createBufferSource(){
    if (this.throwOnCreateSource) throw new Error('boom: createBufferSource');
    const ctx = this;
    const src = {
      buffer: null,
      connect(){},
      start(){ if (ctx.throwOnStart) throw new Error('boom: start'); this.started = true; },
      stop(){ this.stopped = true; },
      disconnect(){ this.disconnected = true; },
      onended: null,
    };
    startedSources.push(src);
    return src;
  }
}

const audioEls = [];
class FakeAudio {
  constructor(url){
    this.url = url; this.played = false; this.paused = false;
    this.onended = null; this.onerror = null; this.currentTime = 0;
    audioEls.push(this);
  }
  play(){ this.played = true; return Promise.resolve(); }
  pause(){ this.paused = true; }
}
const Audio = FakeAudio;

let _urlCounter = 0;
class FakeURL {
  constructor(path, base){ this.href = (base || '') + path; }
}
FakeURL.createObjectURL = () => 'blob:fake-' + (++_urlCounter);
FakeURL.revokeObjectURL = () => {};
const URL = FakeURL;
class Blob { constructor(parts){ this.parts = parts; } }

const speechSynthesis = {
  cancelCalls: 0, speakCalls: [], speaking: false,
  cancel(){ this.cancelCalls += 1; },
  speak(u){ this.speakCalls.push(u); this.speaking = true; },
  getVoices(){ return []; },
  pause(){}, resume(){},
};
class FakeUtterance {
  constructor(text){
    this.text = text; this.onend = null; this.onerror = null;
    this.rate = 1; this.pitch = 1; this.voice = null;
  }
}
const SpeechSynthesisUtterance = FakeUtterance;

const _ls = {};
const localStorage = {
  getItem(k){ return Object.prototype.hasOwnProperty.call(_ls, k) ? _ls[k] : null; },
  setItem(k, v){ _ls[k] = String(v); },
};

let domRows = [];
let speakingBtns = [];
const document = {
  baseURI: 'http://localhost/',
  querySelectorAll(sel){
    if (sel.indexOf('data-speaking') >= 0) return speakingBtns;
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

eval(['_splitForTTS', '_stripForTTS', '_beginTtsPlayback', '_ownsTtsPlayback', '_noteTtsRequestSent',
  '_ttsRequestWaitMs', '_acquireTtsRequestSlot', '_sendTtsRequest', '_stopActivePlaybackAudio',
  '_getTtsAudioCtx', '_playAudioBuf', '_playOpenaiTts', '_playEdgeTtsChunked', '_playElevenLabsTts',
  'speakMessage', 'autoReadLastAssistant', 'stopTTS']
  .map((n) => extractFunction(ui, n)).join('\n'));
eval(['_speakResponse', '_armBrowserTtsRecovery', '_clearBrowserTtsRecovery',
  '_scheduleVoiceMicRearm', '_clearVoiceMicRearm']
  .map((n) => extractFunction(boot, n)).join('\n'));

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
function fakeBtn(){ return { dataset: {} }; }
function fakeMsgBtn(text){ const row = { dataset: { rawText: text } }; return { dataset: {}, closest: () => row }; }
const longText = Array(60).fill('这是一段足够长的用于测试分块播放的中文文本段落。').join('');
const okResp = () => Promise.resolve({ ok: true, status: 200, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) });
const rateLimitedResp = () => Promise.resolve({ ok: false, status: 429, json: () => Promise.resolve({}) });

function resetState(){
  _ttsSpeaking = false; _ttsGeneration = 0; _ttsRequestMinGapMs = 200; _ttsLastRequestTs = 0;
  _ttsCurrentUtterance = null; _ttsChunkQueue = []; _ttsChunkIndex = 0; _ttsActiveBtn = null;
  _playingEdgeAudio = null; _ttsAudioCtx = null;
  _voiceModeActive = false; _voiceModeState = 'idle'; _voiceModeThinkingSid = null;
  _voiceTtsGenStart = 0;
  if (_voiceMicRearmTimer) { clearTimeout(_voiceMicRearmTimer); _voiceMicRearmTimer = null; }
  if (_browserTtsWatchdog) { clearTimeout(_browserTtsWatchdog); _browserTtsWatchdog = null; }
  if (_browserTtsKeepAlive) { clearInterval(_browserTtsKeepAlive); _browserTtsKeepAlive = null; }
  _browserTtsSuppressNextErrorRearm = false; _startListeningCalls = 0;
  toasts.length = 0; fetchCalls.length = 0; startedSources.length = 0; audioEls.length = 0;
  domRows = []; speakingBtns = [];
  speechSynthesis.cancelCalls = 0; speechSynthesis.speakCalls.length = 0; speechSynthesis.speaking = false;
  for (const k of Object.keys(_ls)) delete _ls[k];
  window._hermesTtsIsRegistered = () => false;
  window._hermesTtsSynth = () => Promise.resolve(new ArrayBuffer(8));
}

// 1. Start A, leave its first fetch pending, stop A, start B, resolve A.
//    The generation guard must stop A's chain before it reaches the
//    AudioContext; resolving A's fetch must NOT create a context or issue
//    a decode. Only B may decode — and B's first request must wait for
//    the shared pacing window, not issue immediately inside A's cooldown.
function scenario1() {
  resetState();
  _playOpenaiTts('AAAA', fakeBtn());
  return sleep(0).then(() => {
    if (fetchCalls.length !== 1) throw new Error('s1: A fetch not issued');
    stopTTS();
    _playOpenaiTts('BB', fakeBtn());
    return sleep(0);
  }).then(() => {
    if (fetchCalls.length !== 1) {
      throw new Error('s1: B issued during A cooldown (calls=' + fetchCalls.length + ')');
    }
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (_ttsAudioCtx !== null) throw new Error('s1: A resumed the chain after stop->start');
    if (startedSources.length !== 0) throw new Error('s1: A source started after stop->start');
    return sleep(250);
  }).then(() => {
    if (fetchCalls.length !== 2) {
      throw new Error('s1: B fetch not issued after cooldown (calls=' + fetchCalls.length + ')');
    }
    fetchCalls[1].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (_ttsAudioCtx === null || _ttsAudioCtx.decodeCalls !== 1) {
      throw new Error('s1: B did not decode exactly once (calls=' +
        (_ttsAudioCtx && _ttsAudioCtx.decodeCalls) + ')');
    }
    if (startedSources.length !== 0) {
      throw new Error('s1: source started before decode ok');
    }
    return 'PASS';
  });
}

// 2. Start A, resolve A1, prefetch A2 (request must be paced to the shared
//    rate window), stop A, start B, fire A1 onended, then let the stale A2
//    prefetch lapse: assert A2 is never requested, never decoded, and B
//    remains the active source. B's own first request is paced too.
function scenario2() {
  resetState();
  _playOpenaiTts(longText, fakeBtn());           // fetch A1 (call 0)
  return sleep(0).then(() => {
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (fetchCalls.length !== 1) {
      throw new Error('s2: A2 prefetch was not paced (calls=' + fetchCalls.length + ')');
    }
    const ctxA = _ttsAudioCtx;
    if (!ctxA || !ctxA._decode) throw new Error('s2: A decode not requested');
    ctxA._decode.ok({});                           // A1 plays; src0 started
    if (startedSources.length !== 1) throw new Error('s2: A1 source missing');
    stopTTS();
    _playOpenaiTts('B', fakeBtn());
    return sleep(0).then(() => {
      if (fetchCalls.length !== 1) {
        throw new Error('s2: B issued during A cooldown (calls=' + fetchCalls.length + ')');
      }
      startedSources[0].onended();                 // late A1 onended
      return sleep(250);
    });
  }).then(() => {
    if (fetchCalls.length !== 2) {
      throw new Error('s2: unexpected fetch after stop->start (calls=' + fetchCalls.length + ')');
    }
    fetchCalls[1].resolve(okResp());               // B plays: then -> decodeAudioData
    return sleep(30);
  }).then(() => {
    if (_ttsAudioCtx.decodeCalls !== 2) {
      throw new Error('s2: abandoned A2 was decoded after stop->start (calls=' + _ttsAudioCtx.decodeCalls + ')');
    }
    _ttsAudioCtx._decode.ok({});                   // B plays; src1 started
    if (startedSources.length !== 2) {
      throw new Error('s2: unexpected extra source started: ' + startedSources.length);
    }
    if (_playingEdgeAudio !== startedSources[1]) {
      throw new Error('s2: B is not the active source');
    }
    return 'PASS';
  });
}

// 3. Reject an abandoned prefetch; no unhandled rejection, no toast.
function scenario3() {
  resetState();
  const unhandled = [];
  const onUnhandled = (e) => { unhandled.push(e); };
  process.on('unhandledRejection', onUnhandled);
  _playOpenaiTts(longText, fakeBtn());
  return sleep(0).then(() => {
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    _ttsAudioCtx._decode.ok({});                   // A1 plays; A2 prefetch paced
    if (fetchCalls.length !== 1) throw new Error('s3: A2 prefetch not paced');
    return sleep(250);                             // A2 request goes out
  }).then(() => {
    if (fetchCalls.length !== 2) throw new Error('s3: A2 prefetch missing');
    stopTTS();
    fetchCalls[1].reject(new Error('network down')); // abandon the prefetch
    return sleep(30);
  }).then(() => {
    process.removeListener('unhandledRejection', onUnhandled);
    if (unhandled.length !== 0) throw new Error('s3: unhandled rejection: ' + unhandled[0]);
    if (toasts.length !== 0) throw new Error('s3: error surfaced for abandoned prefetch: ' + toasts[0]);
    return 'PASS';
  });
}

// 4. Direct replacement without an intervening stop: A is playing,
//    _playOpenaiTts() starts B directly (the auto-read shape). A must be
//    stopped/released, B's first request paced, B owns the active handle, a
//    stale A1 onended must not erase B, and a later stopTTS() stops B.
function scenario4() {
  resetState();
  _playOpenaiTts(longText, fakeBtn());          // A: fetch A1 (call 0)
  return sleep(0).then(() => {
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (fetchCalls.length !== 1) throw new Error('s4: A2 not paced yet');
    _ttsAudioCtx._decode.ok({});                // A1 plays; src0 started
    if (startedSources.length !== 1) throw new Error('s4: A1 source missing');
    _playOpenaiTts('BBBB', fakeBtn());          // direct replacement, no stop
    if (!startedSources[0].stopped) throw new Error('s4: A source not stopped on replacement');
    if (!startedSources[0].disconnected) throw new Error('s4: A source not disconnected on replacement');
    return sleep(0).then(() => {
      if (fetchCalls.length !== 1) {
        throw new Error('s4: B issued during A cooldown (calls=' + fetchCalls.length + ')');
      }
      startedSources[0].onended();              // stale A1 onended before B owns handle
      return sleep(250);                        // pacing window passes
    });
  }).then(() => {
    if (fetchCalls.length !== 2) {
      throw new Error('s4: B fetch not issued after cooldown (calls=' + fetchCalls.length + ')');
    }
    fetchCalls[1].resolve(okResp());            // B decodes
    return sleep(30);
  }).then(() => {
    if (_ttsAudioCtx.decodeCalls !== 2) {
      throw new Error('s4: decode calls != A1+B1: ' + _ttsAudioCtx.decodeCalls);
    }
    _ttsAudioCtx._decode.ok({});                // B plays; src1 started
    startedSources[0].onended();                // stale A1 onended after B owns handle
    if (_playingEdgeAudio !== startedSources[1]) {
      throw new Error('s4: B does not own the active handle');
    }
    if (startedSources.length !== 2) {
      throw new Error('s4: extra source started: ' + startedSources.length);
    }
    stopTTS();                                  // must reach B
    if (!startedSources[1].stopped) throw new Error('s4: stopTTS did not stop B');
    return 'PASS';
  });
}

// 5. Synchronous Web Audio failure: createBufferSource throws inside the
//    decode success callback. The terminal handler must clear speaking
//    state, toast the error, never leave an unhandled rejection, and no
//    follow-on request may fire past the pacing window (failure-matrix).
function scenario5() {
  resetState();
  const unhandled = [];
  const onUnhandled = (e) => { unhandled.push(e); };
  process.on('unhandledRejection', onUnhandled);
  _playOpenaiTts(longText, fakeBtn());
  return sleep(0).then(() => {
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (!_ttsAudioCtx || !_ttsAudioCtx._decode) throw new Error('s5: decode not requested');
    _ttsAudioCtx.throwOnCreateSource = true;
    _ttsAudioCtx._decode.ok({});                // synchronous throw inside callback
    return sleep(30);
  }).then(() => {
    if (_ttsSpeaking !== false) throw new Error('s5: speaking state left dangling');
    if (_playingEdgeAudio !== null) throw new Error('s5: active handle not cleared');
    if (toasts.length === 0) throw new Error('s5: no error toast');
    if (startedSources.length !== 0) throw new Error('s5: a source started despite failure');
    return sleep(300);                          // advance past the pacing window
  }).then(() => {
    process.removeListener('unhandledRejection', onUnhandled);
    if (unhandled.length !== 0) throw new Error('s5: unhandled rejection: ' + unhandled[0]);
    if (fetchCalls.length !== 1) {
      throw new Error('s5: follow-on request after terminal failure (calls=' + fetchCalls.length + ')');
    }
    return 'PASS';
  });
}

// 6. Cross-engine pacing through the REAL engine paths, both directions:
//    (a) a real Edge playback issues its request, then an OpenAI playback
//        starts inside the window and must wait;
//    (b) the reverse — an OpenAI request occupies the slot and the next real
//        Edge playback must wait for it. (The round-4 review flagged that
//        the previous scenario manually assigned the pacing timestamp; this
//        one drives the actual Edge/OpenAI request paths.)
function scenario6() {
  resetState();
  _playEdgeTtsChunked('AAAA', fakeBtn());      // Edge fetch0: window free -> immediate
  return sleep(0).then(() => {
    if (fetchCalls.length !== 1) throw new Error('s6: edge request not issued');
    fetchCalls[0].resolve(okResp());           // edge chunk plays
    return sleep(30);
  }).then(() => {
    if (audioEls.length !== 1 || !audioEls[0].played) throw new Error('s6: edge audio not playing');
    _playOpenaiTts('BB', fakeBtn());           // OpenAI start inside edge cooldown
    return sleep(0);
  }).then(() => {
    if (fetchCalls.length !== 1) {
      throw new Error('s6: openai issued during edge cooldown (calls=' + fetchCalls.length + ')');
    }
    return sleep(250);                         // window passes
  }).then(() => {
    if (fetchCalls.length !== 2) {
      throw new Error('s6: openai request not issued after cooldown (calls=' + fetchCalls.length + ')');
    }
    // (b) reverse direction: a new Edge playback inside the openai cooldown
    _playEdgeTtsChunked('CC', fakeBtn());
    return sleep(0).then(() => {
      if (fetchCalls.length !== 2) {
        throw new Error('s6: edge#2 issued during openai cooldown (calls=' + fetchCalls.length + ')');
      }
      return sleep(250);
    });
  }).then(() => {
    if (fetchCalls.length !== 3) {
      throw new Error('s6: edge#2 request not issued after cooldown (calls=' + fetchCalls.length + ')');
    }
    return 'PASS';
  });
}

// 7. decodeAudioData error: the terminal handler clears speaking state and
//    toasts, and zero follow-on requests fire past the pacing window.
function scenario7() {
  resetState();
  const unhandled = [];
  const onUnhandled = (e) => { unhandled.push(e); };
  process.on('unhandledRejection', onUnhandled);
  _playOpenaiTts(longText, fakeBtn());
  return sleep(0).then(() => {
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (!_ttsAudioCtx || !_ttsAudioCtx._decode) throw new Error('s7: decode not requested');
    _ttsAudioCtx._decode.err(new Error('decode boom')); // decode failure path
    return sleep(30);
  }).then(() => {
    if (_ttsSpeaking !== false) throw new Error('s7: speaking state left dangling');
    if (_playingEdgeAudio !== null) throw new Error('s7: active handle not cleared');
    if (toasts.length === 0) throw new Error('s7: no error toast');
    return sleep(300);                          // advance past the pacing window
  }).then(() => {
    process.removeListener('unhandledRejection', onUnhandled);
    if (unhandled.length !== 0) throw new Error('s7: unhandled rejection: ' + unhandled[0]);
    if (fetchCalls.length !== 1) {
      throw new Error('s7: follow-on request after decode failure (calls=' + fetchCalls.length + ')');
    }
    return 'PASS';
  });
}

// 8. AudioContext.resume() rejection (autoplay policy) is observed and
//    routed to the terminal handler; zero follow-on requests.
function scenario8() {
  resetState();
  const unhandled = [];
  const onUnhandled = (e) => { unhandled.push(e); };
  process.on('unhandledRejection', onUnhandled);
  _playOpenaiTts(longText, fakeBtn());
  return sleep(0).then(() => {
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (!_ttsAudioCtx || !_ttsAudioCtx._decode) throw new Error('s8: decode not requested');
    _ttsAudioCtx.state = 'suspended';
    _ttsAudioCtx.rejectResume = true;
    _ttsAudioCtx._decode.ok({});                // doStart waits on resume()
    return sleep(30);
  }).then(() => {
    if (_ttsSpeaking !== false) throw new Error('s8: speaking state left dangling');
    if (_playingEdgeAudio !== null) throw new Error('s8: active handle not cleared');
    if (toasts.length === 0) throw new Error('s8: no error toast');
    if (startedSources.length !== 0) throw new Error('s8: source started despite resume rejection');
    return sleep(300);                          // advance past the pacing window
  }).then(() => {
    process.removeListener('unhandledRejection', onUnhandled);
    if (unhandled.length !== 0) throw new Error('s8: unhandled rejection: ' + unhandled[0]);
    if (fetchCalls.length !== 1) {
      throw new Error('s8: follow-on request after resume rejection (calls=' + fetchCalls.length + ')');
    }
    return 'PASS';
  });
}

// 9. HTTP 429 is retried (bounded) after the pacing window; playback
//    succeeds on the retry and no error toast is shown.
function scenario9() {
  resetState();
  _playOpenaiTts(longText, fakeBtn());
  return sleep(0).then(() => {
    if (fetchCalls.length !== 1) throw new Error('s9: first fetch not issued');
    fetchCalls[0].resolve(rateLimitedResp());   // server window still open
    return sleep(300);                          // retry after pacing window
  }).then(() => {
    if (fetchCalls.length !== 2) {
      throw new Error('s9: no retry after 429 (calls=' + fetchCalls.length + ')');
    }
    if (toasts.length !== 0) throw new Error('s9: error toast on retriable 429: ' + toasts[0]);
    fetchCalls[1].resolve(okResp());            // retry succeeds
    return sleep(30);
  }).then(() => {
    if (!_ttsAudioCtx || _ttsAudioCtx.decodeCalls !== 1) {
      throw new Error('s9: retried chunk not decoded (calls=' +
        (_ttsAudioCtx && _ttsAudioCtx.decodeCalls) + ')');
    }
    _ttsAudioCtx._decode.ok({});                // plays; src0 started
    if (startedSources.length !== 1) {
      throw new Error('s9: source not started after retry: ' + startedSources.length);
    }
    if (_ttsSpeaking !== true) throw new Error('s9: speaking state not active after retry');
    return 'PASS';
  });
}

// 10. start() throws synchronously after the source was created: the
//     terminal handler must stop/disconnect the partially constructed
//     source, clear speaking state, toast, and leave zero follow-on
//     requests past the pacing window.
function scenario10() {
  resetState();
  const unhandled = [];
  const onUnhandled = (e) => { unhandled.push(e); };
  process.on('unhandledRejection', onUnhandled);
  _playOpenaiTts(longText, fakeBtn());
  return sleep(0).then(() => {
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (!_ttsAudioCtx || !_ttsAudioCtx._decode) throw new Error('s10: decode not requested');
    _ttsAudioCtx.throwOnStart = true;
    _ttsAudioCtx._decode.ok({});                // src created; start() throws
    return sleep(30);
  }).then(() => {
    if (startedSources.length !== 1) throw new Error('s10: source was not created');
    if (!startedSources[0].stopped) throw new Error('s10: partially created source not stopped');
    if (!startedSources[0].disconnected) throw new Error('s10: partially created source not disconnected');
    if (_ttsSpeaking !== false) throw new Error('s10: speaking state left dangling');
    if (_playingEdgeAudio !== null) throw new Error('s10: active handle not cleared');
    if (toasts.length === 0) throw new Error('s10: no error toast');
    return sleep(300);                          // advance past the pacing window
  }).then(() => {
    process.removeListener('unhandledRejection', onUnhandled);
    if (unhandled.length !== 0) throw new Error('s10: unhandled rejection: ' + unhandled[0]);
    if (fetchCalls.length !== 1) {
      throw new Error('s10: follow-on request after start failure (calls=' + fetchCalls.length + ')');
    }
    return 'PASS';
  });
}

// 11. Production-composed base entry point: autoReadLastAssistant() (the real
//     ui.js function) replaces an ACTIVE Edge playback. Asserts the canonical
//     stop boundary (edge audio released, speaking buttons reset), that the
//     OpenAI request is paced behind the edge request, and that the stale
//     Edge continuation can neither start audio, issue a follow-on request,
//     nor clobber the new playback.
function scenario11() {
  resetState();
  let autoReadFetchIdx = -1;
  const btn = fakeBtn();
  _playEdgeTtsChunked(longText, btn);          // Edge fetch0: immediate
  return sleep(0).then(() => {
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (audioEls.length !== 1) throw new Error('s11: edge audio missing');
    if (!audioEls[0].played) throw new Error('s11: edge audio not playing');
    btn.dataset.speaking = '1';
    speakingBtns = [btn];
    _ls['hermes-tts-engine'] = 'openai';
    _ls['hermes-tts-auto-read'] = 'true';
    domRows = [{ dataset: { rawText: 'a short auto-read reply' } }];
    autoReadFetchIdx = fetchCalls.length;
    autoReadLastAssistant();                   // the real base entry point
    if (!audioEls[0].paused) throw new Error('s11: edge audio not released by the stop boundary');
    if (btn.dataset.speaking !== '0') throw new Error('s11: speaking button not reset by the stop boundary');
    if (_ttsSpeaking !== true) throw new Error('s11: new playback must own speaking state');
    return sleep(0).then(() => {
      if (fetchCalls.length !== autoReadFetchIdx) {
        throw new Error('s11: openai request issued during edge cooldown (calls=' + fetchCalls.length + ')');
      }
      return sleep(250);
    });
  }).then(() => {
    if (fetchCalls.length !== autoReadFetchIdx + 1) {
      throw new Error('s11: openai request not issued after cooldown (calls=' + fetchCalls.length + ')');
    }
    fetchCalls[autoReadFetchIdx].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (!_ttsAudioCtx || _ttsAudioCtx.decodeCalls !== 1) throw new Error('s11: openai chunk not decoded');
    _ttsAudioCtx._decode.ok({});
    if (startedSources.length !== 1) throw new Error('s11: openai source missing');
    audioEls[0].onended();                     // stale edge completion fires late
    if (_playingEdgeAudio !== startedSources[0]) {
      throw new Error('s11: stale edge completion clobbered the openai handle');
    }
    if (_ttsSpeaking !== true) throw new Error('s11: stale edge completion cleared speaking state');
    return sleep(250);                         // would catch a leaked edge chunk request
  }).then(() => {
    if (fetchCalls.length !== autoReadFetchIdx + 1) {
      throw new Error('s11: stale edge completion issued a follow-on request (calls=' + fetchCalls.length + ')');
    }
    return 'PASS';
  });
}

// 12. Production-composed voice-mode entry point: the real boot.js
//     _speakResponse() replaces an active OpenAI playback. Asserts it enters
//     the canonical stop/start boundary (prior chain invalidated, prior
//     source stopped, manual button reset), that its own request goes
//     through the shared scheduler (paced), and that the stale prior
//     continuation can neither start audio, issue requests, nor clear the
//     new playback's handle/state.
function scenario12() {
  resetState();
  let voiceFetchIdx = -1;
  const btn = fakeBtn();
  btn.dataset.speaking = '1';
  speakingBtns = [btn];
  _playOpenaiTts(longText, btn);               // manual playback A; fetch0
  return sleep(0).then(() => {
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    _ttsAudioCtx._decode.ok({});               // A1 plays; src0 active
    if (startedSources.length !== 1) throw new Error('s12: A1 source missing');
    _voiceModeActive = true;
    _voiceModeState = 'thinking';
    _voiceModeThinkingSid = null;
    _ls['hermes-tts-engine'] = 'openai';
    domRows = [{ dataset: { rawText: longText } }];
    voiceFetchIdx = fetchCalls.length;         // 1
    _speakResponse();                          // the real voice-mode entry
    if (!startedSources[0].stopped) throw new Error('s12: prior source not stopped by the stop boundary');
    if (btn.dataset.speaking !== '0') throw new Error('s12: manual button not reset by the stop boundary');
    if (_voiceModeState !== 'speaking') throw new Error('s12: voice state not speaking');
    return sleep(0).then(() => {
      if (fetchCalls.length !== voiceFetchIdx) {
        throw new Error('s12: voice request issued during cooldown (calls=' + fetchCalls.length + ')');
      }
      return sleep(250);
    });
  }).then(() => {
    if (fetchCalls.length !== voiceFetchIdx + 1) {
      throw new Error('s12: voice request not issued after cooldown (calls=' + fetchCalls.length + ')');
    }
    fetchCalls[voiceFetchIdx].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (audioEls.length !== 1) throw new Error('s12: voice audio not created');
    if (!audioEls[0].played) throw new Error('s12: voice audio not played');
    if (_playingEdgeAudio !== audioEls[0]) throw new Error('s12: voice audio not the active handle');
    startedSources[0].onended();               // stale prior completion fires late
    if (_playingEdgeAudio !== audioEls[0]) throw new Error('s12: stale completion clobbered the handle');
    if (_ttsSpeaking !== true) throw new Error('s12: stale completion cleared speaking state');
    return sleep(250);                         // would catch a leaked follow-on request
  }).then(() => {
    if (fetchCalls.length !== voiceFetchIdx + 1) {
      throw new Error('s12: stale completion issued a follow-on request (calls=' + fetchCalls.length + ')');
    }
    return 'PASS';
  });
}

// 13. Cross-engine pacing through the real engine paths: OpenAI→ElevenLabs
//     and ElevenLabs→OpenAI — each direction must wait for the shared slot,
//     and an abandoned (replaced) completion must not start audio.
function scenario13() {
  resetState();
  _playOpenaiTts('AAAA', fakeBtn());           // fetch0 (immediate)
  _playElevenLabsTts('BBBB', fakeBtn());       // must wait (openai cooldown)
  return sleep(0).then(() => {
    if (fetchCalls.length !== 1) {
      throw new Error('s13: elevenlabs issued during openai cooldown (calls=' + fetchCalls.length + ')');
    }
    fetchCalls[0].resolve(okResp());           // abandoned openai completion
    return sleep(250);
  }).then(() => {
    if (fetchCalls.length !== 2) {
      throw new Error('s13: elevenlabs request not issued after cooldown (calls=' + fetchCalls.length + ')');
    }
    _playOpenaiTts('CCCC', fakeBtn());         // reverse: waits for elevenlabs window
    return sleep(0).then(() => {
      if (fetchCalls.length !== 2) {
        throw new Error('s13: openai issued during elevenlabs cooldown (calls=' + fetchCalls.length + ')');
      }
      fetchCalls[1].resolve(okResp());         // abandoned elevenlabs completion
      return sleep(250);
    });
  }).then(() => {
    if (fetchCalls.length !== 3) {
      throw new Error('s13: openai request not issued after cooldown (calls=' + fetchCalls.length + ')');
    }
    if (audioEls.length !== 0) throw new Error('s13: abandoned completion started audio');
    if (_ttsAudioCtx !== null && _ttsAudioCtx.decodeCalls !== 0) {
      throw new Error('s13: abandoned completion decoded audio');
    }
    return 'PASS';
  });
}

// 14. Cross-engine pacing: ElevenLabs→Edge and Edge→ElevenLabs, then the
//     voice-mode path behind a live Edge request: _speakResponse() must wait
//     for the shared slot, and its fetch resolved after a stop must not
//     start audio.
function scenario14() {
  resetState();
  _playElevenLabsTts('AAAA', fakeBtn());       // fetch0 (immediate)
  _playEdgeTtsChunked('BBBB', fakeBtn());      // must wait (elevenlabs cooldown)
  return sleep(0).then(() => {
    if (fetchCalls.length !== 1) {
      throw new Error('s14: edge issued during elevenlabs cooldown (calls=' + fetchCalls.length + ')');
    }
    fetchCalls[0].resolve(okResp());           // abandoned elevenlabs completion
    return sleep(250);
  }).then(() => {
    if (fetchCalls.length !== 2) {
      throw new Error('s14: edge request not issued after cooldown (calls=' + fetchCalls.length + ')');
    }
    _playElevenLabsTts('CCCC', fakeBtn());     // reverse: waits for edge window
    return sleep(0).then(() => {
      if (fetchCalls.length !== 2) {
        throw new Error('s14: elevenlabs#2 issued during edge cooldown (calls=' + fetchCalls.length + ')');
      }
      return sleep(250);
    });
  }).then(() => {
    if (fetchCalls.length !== 3) {
      throw new Error('s14: elevenlabs#2 request not issued after cooldown (calls=' + fetchCalls.length + ')');
    }
    fetchCalls[2].resolve(okResp());           // elevenlabs#2 is live but idle (no decode)
    _voiceModeActive = true;
    _voiceModeState = 'thinking';
    _voiceModeThinkingSid = null;
    _ls['hermes-tts-engine'] = 'edge';
    domRows = [{ dataset: { rawText: longText } }];
    const before = fetchCalls.length;          // 3
    _speakResponse();                          // voice-mode after a live request
    return sleep(0).then(() => {
      if (fetchCalls.length !== before) {
        throw new Error('s14: voice edge request issued inside cooldown (calls=' + fetchCalls.length + ')');
      }
      return sleep(250);
    }).then(() => {
      if (fetchCalls.length !== before + 1) {
        throw new Error('s14: voice edge request not issued after cooldown (calls=' + fetchCalls.length + ')');
      }
      const voiceFetch = fetchCalls[before];
      stopTTS();                               // voice playback stopped...
      voiceFetch.resolve(okResp());            // ...then the late fetch resolves
      return sleep(30);
    });
  }).then(() => {
    if (audioEls.length !== 0) throw new Error('s14: stale voice fetch started audio');
    if (startedSources.length !== 0) throw new Error('s14: unexpected source started');
    return 'PASS';
  });
}

// 15. Stale ElevenLabs completions after stop→start:
//     (a) an in-flight elevenlabs fetch resolved after a replacement must
//         not decode or start audio;
//     (b) a stale started source's late onended must not clear the newer
//         playback's handle/state (_playAudioBuf cleanup ownership guard).
function scenario15() {
  resetState();
  _playElevenLabsTts(longText, fakeBtn());     // A: fetch0 in flight
  stopTTS();
  _playOpenaiTts('BBBB', fakeBtn());           // B owns; waits for cooldown
  return sleep(0).then(() => {
    if (fetchCalls.length !== 1) throw new Error('s15: B issued during cooldown');
    fetchCalls[0].resolve(okResp());           // late A completion
    return sleep(30);
  }).then(() => {
    if (_ttsAudioCtx !== null) throw new Error('s15: stale elevenlabs completion created a context');
    return sleep(250);
  }).then(() => {
    if (fetchCalls.length !== 2) throw new Error('s15: B fetch not issued (calls=' + fetchCalls.length + ')');
    fetchCalls[1].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (!_ttsAudioCtx || _ttsAudioCtx.decodeCalls !== 1) throw new Error('s15: B decode missing');
    _ttsAudioCtx._decode.ok({});               // B plays; src0
    if (_playingEdgeAudio !== startedSources[0]) throw new Error('s15: B not the owner');
    // (b) play a real elevenlabs clip C, replace it with D, then fire C's
    // stale onended — D's handle/state must survive
    _playElevenLabsTts('CCCC', fakeBtn());     // C: claims, waits for window
    return sleep(250);
  }).then(() => {
    if (fetchCalls.length !== 3) throw new Error('s15: C fetch not issued (calls=' + fetchCalls.length + ')');
    fetchCalls[2].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (_ttsAudioCtx.decodeCalls !== 2) throw new Error('s15: C decode missing');
    _ttsAudioCtx._decode.ok({});               // C plays; src1
    if (startedSources.length !== 2) throw new Error('s15: C source missing');
    if (_playingEdgeAudio !== startedSources[1]) throw new Error('s15: C not the owner');
    _playOpenaiTts('DDDD', fakeBtn());         // D replaces C (releases src1); waits
    return sleep(250);
  }).then(() => {
    if (fetchCalls.length !== 4) throw new Error('s15: D fetch not issued (calls=' + fetchCalls.length + ')');
    fetchCalls[3].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (_ttsAudioCtx.decodeCalls !== 3) throw new Error('s15: D decode missing');
    _ttsAudioCtx._decode.ok({});               // D plays; src2
    if (startedSources.length !== 3) throw new Error('s15: D source missing');
    if (_playingEdgeAudio !== startedSources[2]) throw new Error('s15: D not the owner');
    startedSources[1].onended();               // stale C cleanup fires late
    if (_playingEdgeAudio !== startedSources[2]) throw new Error('s15: stale cleanup clobbered D handle');
    if (_ttsSpeaking !== true) throw new Error('s15: stale cleanup cleared speaking under D');
    return 'PASS';
  });
}

// 16. Stale extension-engine completion after stop→start: a synth promise
//     resolved late must not start audio, surface an error, or clear the
//     newer playback's state.
function scenario16() {
  resetState();
  let resolveSynth = null;
  window._hermesTtsIsRegistered = (id) => id === 'voicevox';
  window._hermesTtsSynth = () => new Promise((res) => { resolveSynth = res; });
  _ls['hermes-tts-engine'] = 'voicevox';
  const btn = fakeMsgBtn(longText);
  speakMessage(btn);                           // extension branch: synth pending
  if (typeof resolveSynth !== 'function') throw new Error('s16: synth not invoked');
  if (btn.dataset.speaking !== '1') throw new Error('s16: listen button not marked');
  stopTTS();
  _playOpenaiTts('BBBB', fakeBtn());           // B owns (free window -> fetch0)
  return sleep(0).then(() => {
    if (fetchCalls.length !== 1) throw new Error('s16: B fetch not issued');
    resolveSynth(new ArrayBuffer(8));          // late extension completion
    return sleep(30);
  }).then(() => {
    if (_ttsAudioCtx !== null) throw new Error('s16: stale synth completion decoded audio');
    if (startedSources.length !== 0) throw new Error('s16: stale synth completion started a source');
    if (_ttsSpeaking !== true) throw new Error('s16: stale synth completion cleared speaking');
    if (toasts.length !== 0) throw new Error('s16: stale synth completion surfaced an error: ' + toasts[0]);
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (!_ttsAudioCtx || _ttsAudioCtx.decodeCalls !== 1) throw new Error('s16: B decode missing');
    _ttsAudioCtx._decode.ok({});
    if (_playingEdgeAudio !== startedSources[0]) throw new Error('s16: B not the owner');
    return 'PASS';
  });
}

// 17. Bounded owner-aware 429 retry applies to every sender: a real Edge
//     request and a real ElevenLabs request are each retried after a 429 and
//     play once the retry succeeds, with no error toast on the retriable 429.
function scenario17() {
  resetState();
  _playEdgeTtsChunked('AAAA', fakeBtn());
  return sleep(0).then(() => {
    fetchCalls[0].resolve(rateLimitedResp());  // 429 -> bounded retry after window
    return sleep(300);
  }).then(() => {
    if (fetchCalls.length !== 2) throw new Error('s17: edge not retried after 429 (calls=' + fetchCalls.length + ')');
    if (toasts.length !== 0) throw new Error('s17: error toast on retriable edge 429: ' + toasts[0]);
    fetchCalls[1].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (audioEls.length !== 1 || !audioEls[0].played) throw new Error('s17: edge audio not played after retry');
    _playElevenLabsTts('BBBB', fakeBtn());     // waits for the edge window
    return sleep(0).then(() => {
      if (fetchCalls.length !== 2) throw new Error('s17: elevenlabs issued inside edge window');
      return sleep(250);
    });
  }).then(() => {
    if (fetchCalls.length !== 3) throw new Error('s17: elevenlabs fetch not issued (calls=' + fetchCalls.length + ')');
    fetchCalls[2].resolve(rateLimitedResp());  // 429 again
    return sleep(300);
  }).then(() => {
    if (fetchCalls.length !== 4) throw new Error('s17: elevenlabs not retried after 429 (calls=' + fetchCalls.length + ')');
    if (toasts.length !== 0) throw new Error('s17: error toast on retriable elevenlabs 429: ' + toasts[0]);
    fetchCalls[3].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (!_ttsAudioCtx || _ttsAudioCtx.decodeCalls !== 1) throw new Error('s17: elevenlabs retry not decoded');
    _ttsAudioCtx._decode.ok({});
    if (startedSources.length !== 1) throw new Error('s17: elevenlabs source missing');
    return 'PASS';
  });
}

// 18. Voice-mode browser playback: a replaced utterance's late onend must
//     not reopen the mic, and a watchdog armed under the replaced turn must
//     not fire after a replacement claimed the generation. Control: on a
//     turn with no replacement the watchdog still fires. Timeouts are capped
//     so the 4 s+ watchdog fires quickly; the guard semantics are unchanged.
function scenario18() {
  resetState();
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms, ...rest) => realSetTimeout(fn, Math.min(ms == null ? 0 : ms, 20), ...rest);
  _voiceModeActive = true;
  _voiceModeState = 'thinking';
  _voiceModeThinkingSid = null;
  _ls['hermes-tts-engine'] = 'browser';
  domRows = [{ dataset: { rawText: 'hello world reply text' } }];
  _speakResponse();                            // browser branch: utterance posted
  const utt = speechSynthesis.speakCalls[0];
  if (!utt) throw new Error('s18: utterance not spoken');
  if (_voiceModeState !== 'speaking') throw new Error('s18: state not speaking');
  _playOpenaiTts('BBBB', fakeBtn());           // replacement claims the turn
  if (typeof utt.onend === 'function') utt.onend();  // stale utterance completes late
  return sleep(40).then(() => {
    if (_startListeningCalls !== 0) throw new Error('s18: stale utterance reopened the mic');
    return sleep(40);
  }).then(() => {
    if (_startListeningCalls !== 0) throw new Error('s18: stale watchdog reopened the mic');
    // control: fresh turn, no replacement -> the real watchdog must fire
    _voiceModeState = 'thinking';
    _speakResponse();
    const utt2 = speechSynthesis.speakCalls[1];
    if (!utt2) throw new Error('s18: second utterance not spoken');
    return sleep(60).then(() => {
      if (_startListeningCalls !== 1) {
        throw new Error('s18: watchdog did not fire on the current turn (calls=' + _startListeningCalls + ')');
      }
      globalThis.setTimeout = realSetTimeout;
      return 'PASS';
    });
  });
}


// 19. Voice-mode delayed mic rearm ownership. A's real terminal callback
//     (browser branch) queues the owner-aware rearm timer; the real manual
//     entry point speakMessage() then replaces playback through the
//     canonical stopTTS boundary and advances the generation. Draining A's
//     retained timer must NOT restart listening, and B's handle/generation/
//     button state must remain intact. Control: on a turn with no
//     replacement the owner-aware rearm still fires — exactly once.
function scenario19() {
  resetState();
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms, ...rest) => realSetTimeout(fn, Math.min(ms == null ? 0 : ms, 20), ...rest);
  _voiceModeActive = true;
  _voiceModeState = 'thinking';
  _voiceModeThinkingSid = null;
  _ls['hermes-tts-engine'] = 'browser';
  domRows = [{ dataset: { rawText: 'first reply text' } }];
  _speakResponse();                            // A: browser branch, utterance posted
  const uttA = speechSynthesis.speakCalls[0];
  if (!uttA) throw new Error('s19: A utterance not spoken');
  uttA.onend();                                // A ends -> queues owner-aware rearm
  if (_voiceMicRearmTimer === null) throw new Error('s19: rearm timer not queued');
  _ls['hermes-tts-engine'] = 'openai';
  const btn = fakeMsgBtn('replacement text');
  speakMessage(btn);                           // B replaces via the stopTTS boundary
  const bGen = _ttsGeneration;
  if (btn.dataset.speaking !== '1') throw new Error('s19: B listen button not marked');
  return sleep(60).then(() => {                // drain A's retained timer window
    if (_startListeningCalls !== 0) throw new Error('s19: stale rearm reopened the mic under B');
    if (_voiceMicRearmTimer !== null) throw new Error('s19: stale rearm timer still pending');
    if (_ttsSpeaking !== true) throw new Error('s19: B speaking state lost');
    if (_ttsGeneration !== bGen) throw new Error('s19: B generation changed');
    if (btn.dataset.speaking !== '1') throw new Error('s19: B button state lost');
    if (fetchCalls.length !== 1) throw new Error('s19: B fetch not issued (calls=' + fetchCalls.length + ')');
    fetchCalls[0].resolve(okResp());           // B is still live and playable
    return sleep(30);
  }).then(() => {
    if (!_ttsAudioCtx || _ttsAudioCtx.decodeCalls !== 1) throw new Error('s19: B decode missing');
    _ttsAudioCtx._decode.ok({});
    if (_playingEdgeAudio !== startedSources[0]) throw new Error('s19: B handle lost after stale rearm drain');
    if (_startListeningCalls !== 0) throw new Error('s19: stale rearm reopened the mic after B start');
    // Control: fresh turn, no replacement -> the owner-aware rearm fires once.
    _voiceModeState = 'thinking';
    _ls['hermes-tts-engine'] = 'browser';
    domRows = [{ dataset: { rawText: 'second reply text' } }];
    _speakResponse();
    const uttC = speechSynthesis.speakCalls[speechSynthesis.speakCalls.length - 1];
    if (!uttC || uttC === uttA) throw new Error('s19: control utterance not spoken');
    uttC.onend();
    return sleep(60);
  }).then(() => {
    if (_startListeningCalls !== 1) {
      throw new Error('s19: control rearm did not fire exactly once (calls=' + _startListeningCalls + ')');
    }
    // Defense layers below the generation check: a retained timer must also
    // re-check activity (3a) and the speaking state (3b) when it fires.
    _voiceModeState = 'thinking';
    _speakResponse();
    const uttD = speechSynthesis.speakCalls[speechSynthesis.speakCalls.length - 1];
    if (!uttD) throw new Error('s19: phase3a utterance not spoken');
    uttD.onend();
    _voiceModeActive = false;                  // 3a: inactive at timer time
    return sleep(60).then(() => {
      if (_startListeningCalls !== 1) {
        throw new Error('s19: rearm fired while voice mode inactive (calls=' + _startListeningCalls + ')');
      }
      _voiceModeActive = true;
      _voiceModeState = 'thinking';
      _speakResponse();
      const uttE = speechSynthesis.speakCalls[speechSynthesis.speakCalls.length - 1];
      if (!uttE) throw new Error('s19: phase3b utterance not spoken');
      uttE.onend();
      _voiceModeState = 'listening';           // 3b: not speaking at timer time
      return sleep(60);
    });
  }).then(() => {
    if (_startListeningCalls !== 1) {
      throw new Error('s19: rearm fired while voice state not speaking (calls=' + _startListeningCalls + ')');
    }
    globalThis.setTimeout = realSetTimeout;
    return 'PASS';
  });
}

// 20. Shared _playAudioBuf path (ElevenLabs): a suspended AudioContext whose
//     resume() rejects is a terminal failure — speaking state and the Listen
//     button must be cleared, no source may start, the error must be
//     surfaced, and the returned promise must settle (direct probe).
function scenario20() {
  resetState();
  const unhandled = [];
  const onUnhandled = (e) => { unhandled.push(e); };
  process.on('unhandledRejection', onUnhandled);
  const btn = fakeBtn();
  _playElevenLabsTts('AAAA', btn);
  return sleep(0).then(() => {
    if (fetchCalls.length !== 1) throw new Error('s20: fetch not issued');
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (!_ttsAudioCtx || !_ttsAudioCtx._decode) throw new Error('s20: decode not requested');
    _ttsAudioCtx.state = 'suspended';
    _ttsAudioCtx.rejectResume = true;
    _ttsAudioCtx._decode.ok({});               // start must wait on resume()
    return sleep(30);
  }).then(() => {
    if (_ttsSpeaking !== false) throw new Error('s20: speaking state left dangling');
    if (btn.dataset.speaking !== '0') throw new Error('s20: listen button left speaking');
    if (startedSources.length !== 0) throw new Error('s20: source started despite resume rejection');
    if (_playingEdgeAudio !== null) throw new Error('s20: active handle not cleared');
    if (toasts.length === 0) throw new Error('s20: no error toast');
    // Direct settle probe: the returned promise itself must resolve on the
    // rejected resume (no dangling chain anywhere on this path).
    let directSettled = false;
    const directGen = _beginTtsPlayback();
    _playAudioBuf(new ArrayBuffer(8), null, 'TTS', directGen).then(function(){ directSettled = true; });
    _ttsAudioCtx._decode.ok({});
    return sleep(30).then(() => {
      if (!directSettled) throw new Error('s20: returned promise not settled after resume rejection');
      return sleep(20);
    });
  }).then(() => {
    process.removeListener('unhandledRejection', onUnhandled);
    if (unhandled.length !== 0) throw new Error('s20: unhandled rejection: ' + unhandled[0]);
    return 'PASS';
  });
}

// 21. Shared _playAudioBuf path (registered extension engine): a synchronous
//     createBufferSource throw is a terminal failure — state/button settled,
//     error surfaced, no source started, and the promise settles.
function scenario21() {
  resetState();
  const unhandled = [];
  const onUnhandled = (e) => { unhandled.push(e); };
  process.on('unhandledRejection', onUnhandled);
  window._hermesTtsIsRegistered = (id) => id === 'voicevox';
  window._hermesTtsSynth = () => Promise.resolve(new ArrayBuffer(8));
  _ls['hermes-tts-engine'] = 'voicevox';
  const btn = fakeMsgBtn('registered engine text');
  speakMessage(btn);                           // extension branch: synth -> _playAudioBuf
  return sleep(0).then(() => {
    if (!_ttsAudioCtx || !_ttsAudioCtx._decode) throw new Error('s21: decode not requested');
    _ttsAudioCtx.throwOnCreateSource = true;
    _ttsAudioCtx._decode.ok({});               // createBufferSource throws sync
    return sleep(30);
  }).then(() => {
    if (_ttsSpeaking !== false) throw new Error('s21: speaking state left dangling');
    if (btn.dataset.speaking !== '0') throw new Error('s21: listen button left speaking');
    if (startedSources.length !== 0) throw new Error('s21: source created despite failure');
    if (_playingEdgeAudio !== null) throw new Error('s21: active handle not cleared');
    if (toasts.length === 0) throw new Error('s21: no error toast');
    process.removeListener('unhandledRejection', onUnhandled);
    if (unhandled.length !== 0) throw new Error('s21: unhandled rejection: ' + unhandled[0]);
    return 'PASS';
  });
}

// 22. Shared _playAudioBuf path (ElevenLabs): a synchronous start() throw
//     after the source was created must stop/disconnect the partially
//     constructed source, clear the handle/state, surface the error, and
//     settle the promise.
function scenario22() {
  resetState();
  const unhandled = [];
  const onUnhandled = (e) => { unhandled.push(e); };
  process.on('unhandledRejection', onUnhandled);
  const btn = fakeBtn();
  _playElevenLabsTts('AAAA', btn);
  return sleep(0).then(() => {
    if (fetchCalls.length !== 1) throw new Error('s22: fetch not issued');
    fetchCalls[0].resolve(okResp());
    return sleep(30);
  }).then(() => {
    if (!_ttsAudioCtx || !_ttsAudioCtx._decode) throw new Error('s22: decode not requested');
    _ttsAudioCtx.throwOnStart = true;
    _ttsAudioCtx._decode.ok({});               // src created; start() throws sync
    return sleep(30);
  }).then(() => {
    if (startedSources.length !== 1) throw new Error('s22: source was not created');
    if (!startedSources[0].stopped) throw new Error('s22: partial source not stopped');
    if (!startedSources[0].disconnected) throw new Error('s22: partial source not disconnected');
    if (_ttsSpeaking !== false) throw new Error('s22: speaking state left dangling');
    if (btn.dataset.speaking !== '0') throw new Error('s22: listen button left speaking');
    if (_playingEdgeAudio !== null) throw new Error('s22: active handle not cleared');
    if (toasts.length === 0) throw new Error('s22: no error toast');
    process.removeListener('unhandledRejection', onUnhandled);
    if (unhandled.length !== 0) throw new Error('s22: unhandled rejection: ' + unhandled[0]);
    return 'PASS';
  });
}


const scenario = process.argv[4];
const runner = {
  scenario1: scenario1, scenario2: scenario2, scenario3: scenario3, scenario4: scenario4,
  scenario5: scenario5, scenario6: scenario6, scenario7: scenario7, scenario8: scenario8,
  scenario9: scenario9, scenario10: scenario10, scenario11: scenario11, scenario12: scenario12,
  scenario13: scenario13, scenario14: scenario14, scenario15: scenario15, scenario16: scenario16,
  scenario17: scenario17, scenario18: scenario18,
  scenario19: scenario19, scenario20: scenario20, scenario21: scenario21, scenario22: scenario22,
}[scenario];
if (!runner) throw new Error('unknown scenario: ' + scenario);
let outcome;
try {
  outcome = runner();
} catch (e) {
  process.stderr.write(String(e && e.stack || e));
  process.exit(1);
}
if (outcome && typeof outcome.then === 'function') {
  outcome.then(
    (verdict) => { process.stdout.write(JSON.stringify({ verdict: verdict })); process.exit(0); },
    (err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); }
  );
} else {
  process.stdout.write(JSON.stringify({ verdict: outcome }));
  process.exit(0);
}
'''


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("scenario", [
    "scenario1", "scenario2", "scenario3", "scenario4", "scenario5",
    "scenario6", "scenario7", "scenario8", "scenario9", "scenario10",
    "scenario11", "scenario12", "scenario13", "scenario14", "scenario15",
    "scenario16", "scenario17", "scenario18", "scenario19", "scenario20",
    "scenario21", "scenario22",
])
def test_openai_tts_chunk_chain_race(tmp_path, scenario):
    """Behavioral coverage for the unified TTS request scheduler and the
    generation/owner tokens: late callbacks from a stopped/replaced playback
    must never resume a chain, start audio, or clear a newer playback's
    state; every server-backed request (any engine, any entry point — Listen
    button, auto-read, voice mode) is paced through the shared slot with a
    bounded owner-aware 429 retry."""
    driver = tmp_path / "tts_race_driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(REPO / "static" / "ui.js"), str(REPO / "static" / "boot.js"), scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node driver failed: {result.stderr}"
    assert json.loads(result.stdout) == {"verdict": "PASS"}
