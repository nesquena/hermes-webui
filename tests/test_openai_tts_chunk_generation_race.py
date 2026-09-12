"""Behavioral race tests for the OpenAI-compatible TTS chunk chain.

Reviewer feedback (nesquena-hermes on #7529): `_ttsSpeaking` is a single
global boolean and cannot distinguish playback A from playback B.  A
stop→start gap lets a late fetch or `onended` callback from A resume the
cancelled chain under B.  These tests drive the real extracted functions
under node with controllable fetch promises and fake audio sources and
assert the generation-token guards at every asynchronous boundary:

  1. Start A, leave its first fetch pending, stop A, start B, then resolve
     A; assert no A source starts.
  2. Start A, prefetch A2, stop A, start B, fire A1 `onended`; assert A2
     never starts and B remains the active source.
  3. Reject an abandoned prefetch and assert there is no
     `unhandledrejection` (prefetches settle into {ok}/{err} immediately).
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

function extractFunction(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('{', ui.indexOf(')', start));
  let depth = 1;
  i += 1;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth += 1;
    else if (ui[i] === '}') depth -= 1;
    i += 1;
  }
  return ui.slice(start, i);
}

// ---- module-level state the extracted functions touch ----
let _ttsSpeaking = false;
let _ttsGeneration = 0;
let _ttsCurrentUtterance = null;
let _ttsChunkQueue = [];
let _ttsChunkIndex = 0;
let _ttsActiveBtn = null;
let _playingEdgeAudio = null;
let _ttsAudioCtx = null;

// ---- controllable fakes ----
const toasts = [];
function showToast(msg) { toasts.push(msg); }

let fetchCalls = [];
function resetFetch() { fetchCalls = []; }
globalThis.fetch = function (url, opts) {
  return new Promise((resolve, reject) => {
    fetchCalls.push({ resolve, reject, url, opts });
  });
};

const startedSources = [];
class FakeAudioContext {
  constructor() { this.state = 'running'; this.destination = {}; this._decode = null; this.decodeCalls = 0; }
  resume() {}
  decodeAudioData(buf, ok, err) { this.decodeCalls += 1; this._decode = { ok, err }; }
  createBufferSource() {
    const src = {
      buffer: null,
      connect() {},
      start() { startedSources.push(this); this.started = true; },
      stop() { this.stopped = true; },
      disconnect() {},
      onended: null,
    };
    return src;
  }
}
const window = { AudioContext: FakeAudioContext };
const document = { baseURI: 'http://localhost/', querySelectorAll() { return []; } };
const location = { href: 'http://localhost/' };

eval(['_splitForTTS', '_playOpenaiTts', '_getTtsAudioCtx', '_playAudioBuf', 'stopTTS']
  .map(extractFunction).join('\n'));

function resetState() {
  _ttsSpeaking = false; _ttsGeneration = 0; _ttsCurrentUtterance = null;
  _ttsChunkQueue = []; _ttsChunkIndex = 0; _ttsActiveBtn = null;
  _playingEdgeAudio = null; _ttsAudioCtx = null;
  toasts.length = 0; fetchCalls.length = 0; startedSources.length = 0;
}

const longText = Array(60).fill('这是一段足够长的用于测试分块播放的中文文本段落。').join('');
const okResp = () => Promise.resolve({ ok: true, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) });

// 1. Start A, leave its first fetch pending, stop A, start B, resolve A.
function scenario1() {
  resetState();
  _playOpenaiTts('AAAA', { dataset: {} });
  if (fetchCalls.length !== 1) throw new Error('s1: A fetch not issued');
  stopTTS();
  _playOpenaiTts('BB', { dataset: {} });
  if (fetchCalls.length !== 2) throw new Error('s1: B fetch not issued');
  fetchCalls[0].resolve(okResp());
  return new Promise((res, rej) => setTimeout(() => {
    try {
      // The generation guard must stop A's chain before it even reaches the
      // AudioContext: resolving A's fetch must NOT create a context or
      // request a decode. Without the guard, _getTtsAudioCtx() runs and
      // _ttsAudioCtx becomes non-null (and a source would start).
      if (_ttsAudioCtx !== null) {
        throw new Error('s1: A resumed the chain after stop->start');
      }
      if (startedSources.length !== 0) {
        throw new Error('s1: A source started after stop->start');
      }
      if (_ttsSpeaking !== true) throw new Error('s1: B should still be speaking');
      res('PASS');
    } catch (e) { rej(e); }
  }, 25));
}

// 2. Start A, prefetch A2, stop A, start B, fire A1 onended.
function scenario2() {
  resetState();
  _playOpenaiTts(longText, { dataset: {} });           // fetch A1 (call 0)
  fetchCalls[0].resolve(okResp());
  return new Promise((res, rej) => setTimeout(() => {
    try {
      if (fetchCalls.length !== 2) throw new Error('s2: A2 prefetch not issued');
      const ctxA = _ttsAudioCtx;
      if (!ctxA || !ctxA._decode) throw new Error('s2: A decode not requested');
      ctxA._decode.ok({});                              // A1 plays; src0 started
      if (startedSources.length !== 1) throw new Error('s2: A1 source missing');
      stopTTS();
      _playOpenaiTts('B', { dataset: {} });             // fetch B (call 2)
      startedSources[0].onended();                      // late A1 onended
      fetchCalls[2].resolve(okResp());                  // B plays: then → decodeAudioData
      fetchCalls[1].resolve(okResp());                  // resolve abandoned A2 prefetch
      setTimeout(() => {
        try {
          // decodeAudioData calls: A1 (1) + B (2). If the abandoned A2
          // prefetch were consumed (no generation guard), it would add a
          // third decode and start an extra source.
          if (_ttsAudioCtx.decodeCalls !== 2) {
            throw new Error('s2: abandoned A2 prefetch was decoded after stop->start (calls=' + _ttsAudioCtx.decodeCalls + ')');
          }
          const ctxB = _ttsAudioCtx;
          if (!ctxB || !ctxB._decode) throw new Error('s2: B decode not requested');
          ctxB._decode.ok({});                          // B plays; src1 started
          if (startedSources.length !== 2) {
            throw new Error('s2: A2 started after stop->start: ' + startedSources.length);
          }
          if (_playingEdgeAudio !== startedSources[1]) {
            throw new Error('s2: B is not the active source');
          }
          res('PASS');
        } catch (e) { rej(e); }
      }, 25);
    } catch (e) { rej(e); }
  }, 25));
}

// 3. Reject an abandoned prefetch; no unhandled rejection, no toast.
function scenario3() {
  resetState();
  const unhandled = [];
  const onUnhandled = (e) => { unhandled.push(e); };
  process.on('unhandledRejection', onUnhandled);
  _playOpenaiTts(longText, { dataset: {} });
  fetchCalls[0].resolve(okResp());
  return new Promise((res, rej) => setTimeout(() => {
    try {
      _ttsAudioCtx._decode.ok({});                      // A1 plays; prefetch A2 (call 1)
      if (fetchCalls.length !== 2) throw new Error('s3: A2 prefetch missing');
      stopTTS();
      fetchCalls[1].reject(new Error('network down'));  // abandon the prefetch
      setTimeout(() => {
        process.removeListener('unhandledRejection', onUnhandled);
        try {
          if (unhandled.length !== 0) throw new Error('s3: unhandled rejection: ' + unhandled[0]);
          if (toasts.length !== 0) throw new Error('s3: error surfaced for abandoned prefetch: ' + toasts[0]);
          res('PASS');
        } catch (e) { rej(e); }
      }, 25);
    } catch (e) { rej(e); }
  }, 25));
}

const scenario = process.argv[3];
const runner = { scenario1: scenario1, scenario2: scenario2, scenario3: scenario3 }[scenario];
if (!runner) throw new Error('unknown scenario: ' + scenario);
runner().then(
  (verdict) => { process.stdout.write(JSON.stringify({ verdict: verdict })); process.exit(0); },
  (err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); }
);
'''


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("scenario", ["scenario1", "scenario2", "scenario3"])
def test_openai_tts_chunk_chain_race(tmp_path, scenario):
    """Behavioral race coverage: late callbacks from a stopped playback must
    never resume the chunk chain under a new playback."""
    driver = tmp_path / "tts_race_driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(REPO / "static" / "ui.js"), scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node driver failed: {result.stderr}"
    assert json.loads(result.stdout) == {"verdict": "PASS"}
