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
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
INDEX_HTML_PATH = REPO_ROOT / "static" / "index.html"

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
// Render scenarios drive the analyser with a fixed byte level so the tick's
// bar-height / fill-width / peak / clip maths are exercised against known input.
const RENDER_LEVEL = {
  render_bars_loud: 255, render_bars_quiet: 40,
  render_fill_loud: 255, render_fill_quiet: 120,
}[scenario];
FakeCtx.prototype.createAnalyser = function () {
  return {
    fftSize: 0, smoothingTimeConstant: 0, frequencyBinCount: 8,
    getByteFrequencyData(buf) {
      if (RENDER_LEVEL === undefined) return; // lifecycle scenarios: silence
      for (let k = 0; k < buf.length; k++) buf[k] = RENDER_LEVEL;
    },
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
// Lifecycle scenarios: arm an id, never invoke the callback (we test teardown,
// not rendering). Render scenarios: fire the rAF callback a bounded number of
// times so the ACTUAL `_vuTick` render logic runs, then stop (the tick
// re-arms rAF at its tail, so an unbounded fake would loop forever).
let tickBudget = RENDER_LEVEL === undefined ? 0 : 1;
const requestAnimationFrame = (cb) => {
  obs.rafRequested++;
  if (tickBudget > 0) { tickBudget--; cb(); }
  return 4242;
};
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

// The meter node is aria-hidden; the accessible "Listening…" state lives on the
// separate #micStatus element. Capture whether it stays in the a11y tree
// (display != none) and is visually hidden (sr-only) while the meter shows.
const statusState = () => ({
  display: status.style.display,
  srOnly: status.classList.contains('sr-only'),
});
// Coerce untouched style props (undefined) to null so JSON.stringify keeps the
// key — a dropped key would read as "not rendered" for the wrong reason.
const barsSnapshot = () => els['micVuBars'].children.map((b) => ({
  height: b.style.height ?? null, peak: b.classList.contains('peak'),
}));
const fillSnapshot = () => ({
  width: els['micVuFillInner'].style.width ?? null,
  clip: els['micVuFillInner'].classList.contains('clip'),
});

if (scenario === 'start_stop') {
  api.vuStart({ id: 'fake-stream' });
  out.snapshots.afterStart = api.state();
  out.snapshots.hostDisplayAfterStart = host.style.display;
  out.snapshots.statusAfterStart = statusState();
  api.vuStop();
  out.snapshots.afterStop = api.state();
  out.snapshots.hostDisplayAfterStop = host.style.display;
  out.snapshots.statusAfterStop = statusState();
} else if (scenario.indexOf('render_bars') === 0) {
  els['settingsVuStyle'].value = 'bars';
  api.vuStart({ id: 'fake-stream' });
  out.snapshots.bars = barsSnapshot();
  out.snapshots.fill = fillSnapshot();
  out.snapshots.activeStyle = api.state().activeStyle;
} else if (scenario.indexOf('render_fill') === 0) {
  els['settingsVuStyle'].value = 'fill';
  api.vuStart({ id: 'fake-stream' });
  out.snapshots.fill = fillSnapshot();
  out.snapshots.bars = barsSnapshot();
  out.snapshots.activeStyle = api.state().activeStyle;
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


def test_meter_keeps_listening_state_accessible_to_screen_readers():
    # Accessibility regression: the meter node is aria-hidden, so when it takes
    # over the #micStatus slot the "Listening…" state must NOT be removed from
    # the accessibility tree (display:none). It should be visually hidden
    # (.sr-only) while remaining available to screen readers, then fully hidden
    # again once the session ends.
    r = _run("start_stop")
    after_start = r["snapshots"]["statusAfterStart"]
    assert after_start["display"] != "none", (
        "showing the meter removed the Listening state from the a11y tree"
    )
    assert after_start["srOnly"] is True, (
        "Listening text must be visually hidden but kept for screen readers"
    )
    after_stop = r["snapshots"]["statusAfterStop"]
    assert after_stop["srOnly"] is False
    assert after_stop["display"] == "none"


def test_render_bars_draws_levels_and_flags_peaks():
    # Loud input (255) must render full-height bars and flag them as peaks; the
    # tick actually runs (rAF callback fired once) so removing the render body
    # fails this. Trailing bars with no spectrum data collapse to the 2px floor.
    r = _run("render_bars_loud")
    bars = r["snapshots"]["bars"]
    assert len(bars) == 16
    assert bars[0]["height"] == "16px", bars
    assert bars[0]["peak"] is True, bars
    # frequencyBinCount is 8, so bars 8..15 receive no data → floor height, no peak.
    assert bars[15]["height"] == "2px", bars
    assert bars[15]["peak"] is False, bars
    # 'bars' style must not touch the fill track.
    assert not r["snapshots"]["fill"]["width"]


def test_render_bars_quiet_stays_below_peak_threshold():
    # Quiet input (40) renders a short-but-above-floor bar and never trips the
    # >200 peak threshold — pins the height maths and the peak boundary.
    r = _run("render_bars_quiet")
    bars = r["snapshots"]["bars"]
    assert bars[0]["height"] == "3px", bars
    assert bars[0]["peak"] is False, bars


def test_render_fill_tracks_level_and_flags_clip():
    # Loud input fills the level bar to 100% and trips the >230 clip flag.
    r = _run("render_fill_loud")
    fill = r["snapshots"]["fill"]
    assert fill["width"] == "100%", fill
    assert fill["clip"] is True, fill
    # 'fill' style must not touch the bar track.
    assert all(not b["height"] for b in r["snapshots"]["bars"]), r["snapshots"]["bars"]
    assert r["snapshots"]["activeStyle"] == "fill"


def test_render_fill_quiet_below_clip_threshold():
    # Mid-level input (120) → ~47% width, below the clip threshold.
    r = _run("render_fill_quiet")
    fill = r["snapshots"]["fill"]
    assert fill["width"] == "47%", fill
    assert fill["clip"] is False, fill


def test_index_html_meter_is_aria_hidden_and_status_is_a_live_region():
    # The visual meter is decorative and must be aria-hidden; the accessible
    # recording state lives on #micStatus, which must be a live region so it is
    # announced when it takes over from the meter's slot.
    html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    meter = re.search(r'<div class="mic-vu" id="micVuMeter"[^>]*>', html)
    assert meter and 'aria-hidden="true"' in meter.group(0), (
        "decorative VU meter must be aria-hidden"
    )
    status = re.search(r'<div class="mic-status" id="micStatus"[^>]*>', html)
    assert status and 'role="status"' in status.group(0), (
        "#micStatus must be a role=status live region for screen readers"
    )
