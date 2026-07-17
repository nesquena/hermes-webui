"""Behavioural regression for the dictation VU meter (#5894).

The live mic-level meter opens a Web Audio ``AudioContext`` + ``AnalyserNode``
and a ``requestAnimationFrame`` render loop while dictating. Every recorder
termination path (``onstop`` / ``onerror`` / the ``getUserMedia`` reject, and
the ``_vuStart`` try/catch itself) funnels through ``_vuStop()``. If ``_vuStop``
ever stops fully tearing that down, a dictation session leaks an AudioContext
and a rAF loop that keeps polling a dead stream — silent, and invisible until a
user racks up several starts.

Rather than regex the source, this drives the ACTUAL ``_vuStart`` / ``_vuStop``
(and the helpers they close over) extracted from static/boot.js under node with
fake Web Audio + DOM, and asserts the teardown contract directly:

  * start acquires exactly one context, wires the analyser, arms a rAF;
  * stop closes the context, disconnects the analyser, cancels the rAF, and
    nulls the closure handles (so a re-start can't double-close);
  * the error path inside ``_vuStart`` still releases the context it created;
  * ``_vuStop`` is idempotent and safe with nothing running;
  * a null stream is a no-op (browser SpeechRecognition exposes no MediaStream).
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

// ── Extract the contiguous VU-meter block from boot.js ──────────────────────
// From the section banner through the end of `_vuStop`, brace-matched so the
// slice carries every const/let/function the meter closes over.
const startMarker = '// ── VU meter (Web Audio AnalyserNode)';
const start = src.indexOf(startMarker);
if (start < 0) throw new Error('VU meter block marker not found');
const stopAt = src.indexOf('function _vuStop(', start);
if (stopAt < 0) throw new Error('_vuStop not found');
let i = src.indexOf('{', stopAt);
let depth = 1; i++;
while (depth > 0 && i < src.length) {
  if (src[i] === '{') depth++;
  else if (src[i] === '}') depth--;
  i++;
}
const block = src.slice(start, i);

// ── Observations shared with the fakes ──────────────────────────────────────
const obs = {
  ctxCreated: 0, srcCreated: 0, srcConnected: 0,
  analyserDisconnected: 0, ctxClosed: 0,
  rafRequested: 0, rafCancelled: 0, rafCancelledId: null,
};

function makeEl() {
  const cls = new Set();
  const el = {
    style: {}, dataset: {}, value: 'bars', children: [],
    classList: {
      add: (c) => cls.add(c),
      remove: (c) => cls.delete(c),
      contains: (c) => cls.has(c),
    },
    appendChild(c) { el.children.push(c); return c; },
    removeAttribute(k) { delete el.dataset[k.replace(/^data-/, '')]; },
    querySelector() { return null; },
    set innerHTML(_v) { el.children.length = 0; },
    get innerHTML() { return ''; },
  };
  return el;
}

const els = {};
const $ = (id) => (els[id] || (els[id] = makeEl()));
const status = makeEl();

function FakeCtx() { obs.ctxCreated++; }
FakeCtx.prototype.createMediaStreamSource = function (_stream) {
  obs.srcCreated++;
  if (scenario === 'start_error_cleanup') throw new Error('boom');
  return { connect() { obs.srcConnected++; } };
};
FakeCtx.prototype.createAnalyser = function () {
  return {
    fftSize: 0, smoothingTimeConstant: 0, frequencyBinCount: 8,
    getByteFrequencyData() {},
    disconnect() { obs.analyserDisconnected++; },
  };
};
FakeCtx.prototype.close = function () { obs.ctxClosed++; };

const window = { _micActive: false, AudioContext: FakeCtx };
const documentFake = { createElement: () => makeEl() };
const location = { hash: '' };
const _store = {};
const localStorage = {
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
};
// Non-looping rAF: arm an id, never invoke the callback (we test lifecycle,
// not the render tick).
const requestAnimationFrame = () => { obs.rafRequested++; return 4242; };
const cancelAnimationFrame = (id) => { obs.rafCancelled++; obs.rafCancelledId = id; };

const factory = new Function(
  '$', 'status', 'window', 'document', 'location', 'localStorage',
  'requestAnimationFrame', 'cancelAnimationFrame',
  block + `
;return {
  vuStart: _vuStart,
  vuStop: _vuStop,
  stylePref: _vuStylePref,
  state: () => ({
    hasCtx: !!_vuAudioCtx, hasAnalyser: !!_vuAnalyser,
    rafId: _vuRafId, activeStyle: _vuActiveStyle,
  }),
};`
);
const api = factory($, status, window, documentFake, location, localStorage,
  requestAnimationFrame, cancelAnimationFrame);

const host = $('micVuMeter');
const out = { obs, snapshots: {} };

if (scenario === 'start_stop') {
  api.vuStart({ id: 'fake-stream' });
  out.snapshots.afterStart = api.state();
  out.snapshots.hostDisplayAfterStart = host.style.display;
  api.vuStop();
  out.snapshots.afterStop = api.state();
  out.snapshots.hostDisplayAfterStop = host.style.display;
} else if (scenario === 'stop_idempotent') {
  api.vuStop();
  api.vuStop();
  out.snapshots.afterStop = api.state();
  out.snapshots.hostDisplayAfterStop = host.style.display;
} else if (scenario === 'no_stream') {
  api.vuStart(null);
  out.snapshots.afterStart = api.state();
} else if (scenario === 'start_error_cleanup') {
  api.vuStart({ id: 'fake-stream' });
  out.snapshots.afterStart = api.state();
} else {
  throw new Error('unknown scenario: ' + scenario);
}

process.stdout.write(JSON.stringify(out));
"""


def _run(scenario):
    with tempfile.TemporaryDirectory() as tmp:
        driver = Path(tmp) / "vu_driver.js"
        driver.write_text(_DRIVER_SRC, encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(driver), str(BOOT_JS_PATH), scenario],
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert proc.returncode == 0, f"driver failed ({scenario}):\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_start_arms_context_analyser_and_raf():
    r = _run("start_stop")
    after_start = r["snapshots"]["afterStart"]
    assert r["obs"]["ctxCreated"] == 1
    assert r["obs"]["srcConnected"] == 1
    assert after_start["hasCtx"] is True
    assert after_start["hasAnalyser"] is True
    assert after_start["rafId"] == 4242
    assert after_start["activeStyle"] == "bars"
    # Meter host is revealed while the static "Listening…" text is suppressed.
    assert r["snapshots"]["hostDisplayAfterStart"] == ""


def test_stop_tears_down_everything():
    r = _run("start_stop")
    after_stop = r["snapshots"]["afterStop"]
    # The teardown contract: context closed, analyser disconnected, rAF cancelled.
    assert r["obs"]["ctxClosed"] == 1, r["obs"]
    assert r["obs"]["analyserDisconnected"] == 1, r["obs"]
    assert r["obs"]["rafCancelled"] == 1, r["obs"]
    assert r["obs"]["rafCancelledId"] == 4242, r["obs"]
    # Closure handles nulled so a subsequent start can't double-close a stale ctx.
    assert after_stop["hasCtx"] is False
    assert after_stop["hasAnalyser"] is False
    assert after_stop["rafId"] == 0
    assert after_stop["activeStyle"] is None
    assert r["snapshots"]["hostDisplayAfterStop"] == "none"


def test_start_error_path_releases_the_context_it_created():
    # createMediaStreamSource throws after the context is constructed; _vuStart's
    # own try/catch must call _vuStop so the just-created context is not leaked.
    r = _run("start_error_cleanup")
    assert r["obs"]["ctxCreated"] == 1
    assert r["obs"]["ctxClosed"] == 1, "error path leaked the AudioContext"
    assert r["snapshots"]["afterStart"]["hasCtx"] is False
    assert r["snapshots"]["afterStart"]["rafId"] == 0


def test_stop_is_idempotent_with_nothing_running():
    r = _run("stop_idempotent")
    assert r["obs"]["ctxClosed"] == 0
    assert r["obs"]["rafCancelled"] == 0
    assert r["snapshots"]["afterStop"]["hasCtx"] is False
    assert r["snapshots"]["hostDisplayAfterStop"] == "none"


def test_null_stream_is_a_noop():
    # Browser SpeechRecognition exposes no MediaStream, so the meter must stay
    # dormant on that path rather than spin up a context against `null`.
    r = _run("no_stream")
    assert r["obs"]["ctxCreated"] == 0
    assert r["snapshots"]["afterStart"]["hasCtx"] is False
    assert r["snapshots"]["afterStart"]["rafId"] == 0
