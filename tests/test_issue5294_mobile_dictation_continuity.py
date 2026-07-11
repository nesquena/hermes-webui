"""Behavioural regression for #5294 (salvage of the composer-mic dictation fix).

The composer microphone (SpeechRecognition inside `_ensureSpeechRecognition`)
must keep a MOBILE dictation session alive across a natural pause, while DESKTOP
dictation stays one-shot (a single utterance that finalizes on the first pause).

Rather than regex the source, this drives the ACTUAL `_ensureSpeechRecognition`
(and its gate helpers `_micDictationContinuous` / `_micShouldRestartDictation`)
from static/boot.js via node with a fake SpeechRecognition, then simulates a
final result followed by a pause (`onend`) and observes whether the session
restarts. It pins both directions so the mobile-gate can't silently regress into
either "desktop keeps restarting" or "mobile dies on every pause".
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const input = JSON.parse(process.argv[3]);

// ── Extract a named function body from boot.js by brace-matching ────────────
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

// ── Fake browser + closure environment ──────────────────────────────────────
const _store = Object.assign({}, input.store || {});
global.localStorage = {
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
};
global.navigator = { wakeLock: null };
global.window = {
  _micActive: false,
  _micPendingSend: false,
  matchMedia: (q) => ({ matches: q.indexOf('pointer:coarse') >= 0 ? !!input.coarse : false }),
};
global.document = { addEventListener: () => {}, visibilityState: 'visible' };

// Closure state that the extracted functions read/mutate at top level.
let SpeechRecognition = function () {
  this.continuous = undefined;
  this.interimResults = undefined;
  this.lang = undefined;
  this.onstart = null;
  this.onresult = null;
  this.onend = null;
  this.onerror = null;
  this.startCalls = 0;
  this.stopCalls = 0;
  this.start = function () { this.startCalls++; };
  this.stop = function () { this.stopCalls++; };
};
let recognition = null;
let _finalText = '';
let _prefix = '';
let _isRecording = false;
let _speechStopRequested = false;
let _micWakeLock = null;
let _micWakeLockOp = null;
let _micRestartCount = 0;
const _micMaxRestarts = 20;
let _activeCaptureMode = null;
let _forceMediaRecorder = false;
const _micForceMediaRecorderKey = 'mic_force_mediarecorder';
const _locale = { _speech: 'en-US' };
let ta = { value: '' };
const toasts = [];

function autoResize() {}
function send() {}
function showToast(m) { toasts.push(m); }
function t(k) { return k; }
function _applyDeferredServerSttFlip() {}
function _micToastKeyForRecognitionError() { return null; }
function _setRecording(on) {
  window._micActive = on;
  if (!on) { _finalText = ''; _prefix = ''; }
}
function _releaseMicWakeLock() { _micWakeLock = null; return Promise.resolve(); }
function _acquireMicWakeLock() { return Promise.resolve(); }

// ── The real functions under test ───────────────────────────────────────────
eval(extractFunc('_micDictationContinuous'));
eval(extractFunc('_micShouldRestartDictation'));
eval(extractFunc('_ensureSpeechRecognition'));

// ── Drive a dictation session: start → final result → natural pause ─────────
recognition = _ensureSpeechRecognition();
const continuousAfterEnsure = recognition.continuous;

// Simulate _startMicCapture's speech branch entering an active session.
_activeCaptureMode = 'speech';
_speechStopRequested = !!input.stopRequested;
// #5294 regression: allow pre-seeding the restart budget so we can prove a real
// result resets it (a long productive dictation must not silently hit the cap).
_micRestartCount = (typeof input.preRestartCount === 'number') ? input.preRestartCount : 0;
window._micActive = true;
recognition.start();          // initial start (mimics _startMicCapture)
const startCallsAfterStart = recognition.startCalls;

// A final result lands, then the recognizer ends on a natural pause.
recognition.onstart();
recognition.onresult({
  resultIndex: 0,
  results: [Object.assign([{ transcript: 'hello world' }], { isFinal: true, length: 1 })],
});
recognition.onend();

const out = {
  continuousAfterEnsure: continuousAfterEnsure,
  restarted: recognition.startCalls > startCallsAfterStart,
  micActiveAfterPause: !!window._micActive,
  restartCountAfter: _micRestartCount,
  taValue: ta.value,
  toasts: toasts,
};
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("mic_dictation_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _run(driver_path, *, coarse=False, store=None, stop_requested=False, pre_restart_count=None):
    payload = {"coarse": coarse, "store": store or {}, "stopRequested": stop_requested}
    if pre_restart_count is not None:
        payload["preRestartCount"] = pre_restart_count
    result = subprocess.run(
        [NODE, driver_path, str(BOOT_JS_PATH), json.dumps(payload)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


# ─────────────────────────────────────────────────────────────────────────────
# DESKTOP must stay ONE-SHOT: continuous=false and no auto-restart on a pause.
# ─────────────────────────────────────────────────────────────────────────────
class TestDesktopStaysOneShot:
    def test_desktop_recognition_is_not_continuous(self, driver_path):
        out = _run(driver_path, coarse=False)
        assert out["continuousAfterEnsure"] is False

    def test_desktop_does_not_restart_on_natural_pause(self, driver_path):
        out = _run(driver_path, coarse=False)
        assert out["restarted"] is False, "desktop dictation must finalize on the first pause"
        assert out["micActiveAfterPause"] is False, "desktop session must end after the pause"

    def test_desktop_optout_flag_forces_one_shot_even_on_coarse_pointer(self, driver_path):
        # An explicit 'false' opt-out must win even on a touch/coarse device.
        out = _run(driver_path, coarse=True, store={"hermes-mic-continuous": "false"})
        assert out["continuousAfterEnsure"] is False
        assert out["restarted"] is False


# ─────────────────────────────────────────────────────────────────────────────
# MOBILE must keep the session ALIVE through a pause via restart-on-onend.
# ─────────────────────────────────────────────────────────────────────────────
class TestMobileKeepsSessionAlive:
    def test_mobile_recognition_is_continuous(self, driver_path):
        out = _run(driver_path, coarse=True)
        assert out["continuousAfterEnsure"] is True

    def test_mobile_restarts_on_natural_pause(self, driver_path):
        out = _run(driver_path, coarse=True)
        assert out["restarted"] is True, "mobile dictation must restart to survive a natural pause"
        assert out["micActiveAfterPause"] is True, "mobile session must remain active after the pause"

    def test_mobile_intentional_stop_does_not_restart(self, driver_path):
        # _speechStopRequested (set by _stopMic on send/toggle) must suppress the
        # restart so an intentional stop still finalizes on mobile.
        out = _run(driver_path, coarse=True, stop_requested=True)
        assert out["restarted"] is False
        assert out["micActiveAfterPause"] is False

    def test_desktop_optin_flag_enables_continuity(self, driver_path):
        # An explicit 'true' opt-in makes a non-coarse (desktop) device continuous.
        out = _run(driver_path, coarse=False, store={"hermes-mic-continuous": "true"})
        assert out["continuousAfterEnsure"] is True
        assert out["restarted"] is True

    def test_productive_result_resets_restart_budget(self, driver_path):
        # A real result means the continuity restarts are PRODUCTIVE, so the
        # restart budget must reset — otherwise a long mobile dictation with many
        # natural pauses silently hits _micMaxRestarts and dies mid-session. Seed
        # the count AT the cap: onresult must reset it to 0 so the ensuing pause
        # still restarts. Without the reset, count stays at the cap and the
        # session would NOT restart.
        out = _run(driver_path, coarse=True, pre_restart_count=20)
        assert out["restartCountAfter"] == 1, (
            "a productive onresult must reset _micRestartCount (to 0), leaving 1 "
            "after the subsequent natural-pause restart — not stay pinned at the cap"
        )
        assert out["restarted"] is True, (
            "mobile dictation must keep restarting through pauses once a real "
            "result has proven the session productive, even past the raw cap"
        )
