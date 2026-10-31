"""Dictation feedback inside the mic button (state, level, silence auto-stop).

The behaviour lives in `static/boot.js`, which this suite cannot execute, so
these are contract checks on the published surface: the two child elements the
button gains, the CSS that renders them, the lifecycle hooks that drive them,
and the one preference that changes user-visible behaviour. They fail if the
wiring is removed, renamed, or driven by polling instead of the capture's own
events.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_button_gets_state_and_level_children():
    assert "micStateEl.className='mic-state'" in BOOT_JS
    assert "micLevelEl.className='mic-level'" in BOOT_JS
    assert "btn.append(micStateEl,micLevelEl)" in BOOT_JS


def test_children_are_decorative_for_screen_readers():
    # The timer and the level bar duplicate the tooltip state; exposing them
    # would make every second of a dictation an announcement.
    assert BOOT_JS.count("micStateEl.setAttribute('aria-hidden','true')") == 1
    assert BOOT_JS.count("micLevelEl.setAttribute('aria-hidden','true')") == 1


def test_state_is_driven_by_the_capture_lifecycle():
    # rec starts with the capture, thinking with the request, ready/empty when
    # the transcript is committed. No polling: the DOM is written from one place.
    assert "_micPaint('rec')" in BOOT_JS
    assert "_micPaint('think','···')" in BOOT_JS
    assert "_micPaint('ready','✓ '+_micWordCount(clean)+'p',_MIC_READY_MS)" in BOOT_JS
    assert "_micPaint('empty','0p',_MIC_EMPTY_MS)" in BOOT_JS


def test_level_meter_uses_the_capture_stream_not_a_second_capture():
    # A second getUserMedia would prompt a second permission and open two input
    # streams for one dictation.
    assert "_micStartMeter(captureStream)" in BOOT_JS
    meter = BOOT_JS.split("function _micStartMeter(stream){", 1)[1].split("function _micWordCount", 1)[0]
    assert "getUserMedia" not in meter
    assert "createMediaStreamSource(stream)" in meter


def test_silence_auto_stop_is_opt_out_and_bounded():
    assert "_MIC_AUTOSTOP_KEY='mic_autostop'" in BOOT_JS
    assert "localStorage.getItem(_MIC_AUTOSTOP_KEY)!=='0'" in BOOT_JS
    # It may only fire after speech was actually heard, never on a quiet room.
    assert "_MIC_SILENCE_MS=1600" in BOOT_JS
    auto_stop = BOOT_JS.split("function _micStartMeter(stream){", 1)[1].split("function _micWordCount", 1)[0]
    assert "if(!_micSpokeAt) return;" in auto_stop
    assert "_micAutoStopEnabled()" in auto_stop


def test_css_renders_state_inside_the_button_without_new_i18n_keys():
    assert ".mic-btn .mic-state{display:none;" in STYLE_CSS
    assert ".mic-btn.mic-busy>svg{display:none;}" in STYLE_CSS
    assert ".mic-btn.mic-busy .mic-state{display:inline-block;" in STYLE_CSS
    assert ".mic-btn .mic-level{position:absolute;" in STYLE_CSS
    assert "@media(prefers-reduced-motion:reduce){.mic-btn .mic-level{transition:none;}}" in STYLE_CSS


def test_tooltip_anchor_is_untouched():
    # .has-tooltip is what positions the button; adding absolutely positioned
    # children must not introduce a competing position rule on .mic-btn.
    assert ".has-tooltip{position:relative;}" in STYLE_CSS
    assert ".mic-btn{position:" not in STYLE_CSS


# --- behavioural coverage ---------------------------------------------------
#
# Source-presence assertions above cannot see a state that is painted and never
# cleared (review 5674998963: a failed transcription left the button on 'think'
# forever). These run the shipped `_transcribeBlob` under node with a stubbed
# DOM/fetch, so every exit path is exercised as code rather than as text.

import json
import shutil
import subprocess

import pytest

NODE = shutil.which("node")


def _extract_function(src, name):
    """Return the full source of a top-level `function name(...)` in boot.js."""
    marker = "function %s(" % name
    start = src.index(marker)
    if src[max(0, start - 6):start] == "async ":
        start -= 6
    brace = src.index("{", start)
    depth = 0
    for pos in range(brace, len(src)):
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
            if depth == 0:
                return src[start:pos + 1]
    raise AssertionError("unbalanced braces around %s" % name)


_HARNESS = r"""
const _micForceMediaRecorderKey = 'mic_force_mediarecorder';
const _micForceMediaRecorderExpiryKey = 'mic_force_mediarecorder_expires';
const _MIC_FORCE_MR_TTL_MS = 24 * 60 * 60 * 1000;
let _micTranscribeSeq = 0;
const _MIC_EMPTY_MS = 2500;
const paints = [];
let statuses = [];
const toasts = [];
const commits = [];
const store = {};
const pending = [];
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
globalThis.window = {};
globalThis.document = {};
let _forceMediaRecorder = true;
let recognition = null;
const SpeechRecognition = function () {};

// Shipped source spliced in verbatim below, no eval.
__FUNCSIF__

function _micPaint(state) { paints.push(state === undefined ? null : state); }
function setComposerStatus(s) { statuses.push(s); }
function showToast(m) { toasts.push(m); }
function t(k) { return k; }
function _commitTranscript(text) { commits.push(text); }
function _composerHasContent() { return false; }
function _ensureSpeechRecognition() {
  return { started: false, start() { this.started = true; } };
}
function fetch(url, opts) {
  let resolve, reject;
  const p = new Promise((res, rej) => { resolve = res; reject = rej; });
  pending.push({ resolve, reject });
  return p;
}

const blob = new Blob(['x'], { type: 'audio/webm' });
const out = {};

function okResponse(body) {
  return { ok: true, status: 200, json: async () => body };
}
function errResponse(status, body) {
  return { ok: false, status, json: async () => body };
}

(async () => {
  const begin = () => { paints.length = 0; statuses = []; toasts.length = 0; };

  // 1. success: transcript committed, 'think' was painted while in flight.
  begin();
  let p = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  out.successPainted = paints.slice();
  pending.shift().resolve(okResponse({ transcript: 'hi there' }));
  await p;
  out.successCommits = commits.slice();
  out.successStatuses = statuses.slice();

  // 2. empty transcript is "nothing heard", not a failure: upstream answers 200
  // with an empty transcript and no_speech. Nothing is committed; the user gets
  // the "no speech" toast instead of a silently unchanged composer.
  begin();
  p = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  pending.shift().resolve(okResponse({ transcript: '' }));
  await p;
  out.emptyCommits = commits.slice();
  out.emptyToasts = toasts.slice();
  out.emptyPaints = paints.slice();

  // 3. HTTP failure must retire 'think' and clear the status line.
  begin();
  p = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  pending.shift().resolve(errResponse(400, { error: 'bad audio' }));
  await p;
  out.http400Paints = paints.slice();
  out.http400Statuses = statuses.slice();
  out.http400Toasts = toasts.slice();

  // 4. rejected fetch (no status) must also settle the button.
  begin();
  p = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  pending.shift().reject(new Error('network down'));
  await p;
  out.rejectedPaints = paints.slice();
  out.rejectedStatuses = statuses.slice();
  out.rejectedToasts = toasts.slice();

  // 5. browser fallback path settles too, and arms recognition.
  begin();
  p = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  pending.shift().resolve(errResponse(503, { error: 'unavailable' }));
  await p;
  out.fallbackPaints = paints.slice();
  out.fallbackStatuses = statuses.slice();
  out.fallbackRecognitionArmed = recognition !== null;
  out.fallbackStore = Object.assign({}, store);

  // 6. a stale request must not clear a newer capture's button.
  begin();
  const first = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  const second = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  const firstReq = pending.shift();
  begin();
  firstReq.resolve(errResponse(400, { error: 'stale' }));
  await first;
  out.stalePaints = paints.slice();
  out.staleStatuses = statuses.slice();
  pending.shift().resolve(okResponse({ transcript: 'newer' }));
  await second;
  out.staleFinalCommits = commits.slice(-1);

  // 7. a superseded request whose no-speech response lands late must not paint
  // over the newer capture's button either.
  begin();
  const staleEmpty = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  const newerReq = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  const staleEmptyReq = pending.shift();
  begin();
  staleEmptyReq.resolve(okResponse({ transcript: '', no_speech: true }));
  await staleEmpty;
  out.staleNoSpeechPaints = paints.slice();
  out.staleNoSpeechToasts = toasts.slice();
  pending.shift().resolve(okResponse({ transcript: 'newer still' }));
  await newerReq;
  out.staleNoSpeechFinalCommits = commits.slice(-1);

  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def _run_transcribe_harness():
    funcs = "\n".join([
        _extract_function(BOOT_JS, "_getMicForceMediaRecorderStored"),
        _extract_function(BOOT_JS, "_setMicForceMediaRecorder"),
        _extract_function(BOOT_JS, "_isServerSttUnavailable"),
        _extract_function(BOOT_JS, "_allowBrowserSttFallback"),
        _extract_function(BOOT_JS, "_transcribeBlob"),
    ])
    js = _HARNESS.replace("__FUNCSIF__", funcs)
    proc = subprocess.run(
        [NODE, "-e", js], capture_output=True, text=True, cwd=ROOT, timeout=60
    )
    assert proc.returncode == 0, "node harness failed:\n%s" % proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def transcribe_runs():
    if NODE is None:
        pytest.skip("node not on PATH")
    return _run_transcribe_harness()


def test_success_commits_and_empty_reports_no_speech(transcribe_runs):
    assert transcribe_runs["successPainted"][0] == "think"
    assert transcribe_runs["successCommits"] == ["hi there"]
    assert transcribe_runs["successStatuses"] == ["Transcribing…", ""]
    assert transcribe_runs["emptyCommits"] == ["hi there"], (
        "an empty transcript must not append anything to the composer"
    )
    assert transcribe_runs["emptyToasts"] == ["mic_no_speech"]
    assert transcribe_runs["emptyPaints"] == ["think", "empty"], (
        "a successful no-speech response must retire 'think' to the timed "
        "empty tick, not leave the button painting in flight forever"
    )


def test_stale_no_speech_does_not_paint_a_newer_capture(transcribe_runs):
    assert transcribe_runs["staleNoSpeechPaints"] == [], (
        "a superseded request's empty response must not touch a button its "
        "successor owns"
    )
    assert transcribe_runs["staleNoSpeechFinalCommits"] == ["newer still"]


def test_http_failure_retires_the_thinking_state(transcribe_runs):
    assert transcribe_runs["http400Paints"] == ["think", None], (
        "a 400 must leave the button idle, not spinning on 'think'"
    )
    assert transcribe_runs["http400Statuses"] == ["Transcribing…", ""]
    assert transcribe_runs["http400Toasts"] == ["bad audio"]


def test_rejected_fetch_retires_the_thinking_state(transcribe_runs):
    assert transcribe_runs["rejectedPaints"] == ["think", None]
    assert transcribe_runs["rejectedStatuses"] == ["Transcribing…", ""]
    assert transcribe_runs["rejectedToasts"] == ["network down"]


def test_browser_fallback_settles_the_button(transcribe_runs):
    assert transcribe_runs["fallbackPaints"] == ["think", None]
    assert transcribe_runs["fallbackStatuses"] == ["Transcribing…", ""]
    assert transcribe_runs["fallbackRecognitionArmed"] is True
    assert transcribe_runs["fallbackStore"]["mic_force_mediarecorder"] == "0"


def test_a_stale_request_does_not_clear_a_newer_capture(transcribe_runs):
    assert transcribe_runs["stalePaints"] == [], (
        "the superseded request must not touch a button its successor owns"
    )
    assert transcribe_runs["staleStatuses"] == []
    assert transcribe_runs["staleFinalCommits"] == ["newer"]


# --- capture takes ownership, not just a newer request ----------------------
#
# The schedule above starts two transcriptions, which only proves that a newer
# request outranks an older one. It cannot see the real lifecycle: a recording
# that starts while a transcription is in flight owns the button even though no
# second request exists yet. This runs the shipped `_setRecording`,
# `_micPaint`/timer and `_startMicCapture` (media path) under node so the
# schedule pending A -> capture B starts -> A fails is exercised as code.

_CAPTURE_HARNESS = r"""
const _micForceMediaRecorderKey = 'mic_force_mediarecorder';
const _micForceMediaRecorderExpiryKey = 'mic_force_mediarecorder_expires';
const _MIC_FORCE_MR_TTL_MS = 24 * 60 * 60 * 1000;
let _micTranscribeSeq = 0;
let _micStartSeq = 0;
let _isRecording = false;
let _finalText = '';
let _prefix = '';
let _rawAudioMode = false;
let _micHoldActive = false;
let _canRecordAudio = true;
let _activeCaptureMode = null;
let _speechStopRequested = false;
let _micRestartCount = 0;
let _forceMediaRecorder = true;
let recognition = null;
let mediaStream = null;
let mediaRecorder = null;
let audioChunks = [];
let _micElapsedTimer = null;
let _micStateTimer = null;
let _micMeterCtx = null;
let _micMeterRaf = 0;
let _micSpokeAt = 0;
const _MIC_READY_MS = 3000;
const _MIC_EMPTY_MS = 2500;
const _MIC_AUTOSTOP_KEY = 'mic_autostop';
const paints = [];
let statuses = [];
const toasts = [];
const commits = [];
const store = {};
const pending = [];
const SpeechRecognition = function () {};
const ta = { value: '' };

function _stubBtn() {
  const set = new Set();
  return {
    set,
    append() {},
    addEventListener() {},
    classList: {
      toggle(cls, on) { on ? set.add(cls) : set.delete(cls); },
      contains(cls) { return set.has(cls); },
    },
  };
}
const btn = _stubBtn();
const micStateEl = { textContent: '', setAttribute() {} };
const micLevelEl = { textContent: '', style: { setProperty() {} }, setAttribute() {} };
const status = { style: {} };
const statusText = { textContent: '' };

globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
globalThis.window = { _micPendingSend: false };
globalThis.document = {};
globalThis.cancelAnimationFrame = () => {};
globalThis.requestAnimationFrame = () => 0;

class MediaRecorder {
  static isTypeSupported() { return true; }
  constructor(_stream, opts) {
    this.mimeType = (opts && opts.mimeType) || '';
    this.state = 'inactive';
    this.onerror = null;
    this.ondataavailable = null;
  }
  start() { this.state = 'recording'; }
  stop() { this.state = 'inactive'; }
}
globalThis.MediaRecorder = MediaRecorder;
window.MediaRecorder = MediaRecorder;
// Node >= 21 exposes a read-only `navigator` global; replace it explicitly.
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  writable: true,
  value: {
    mediaDevices: {
      getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }),
    },
  },
});

// Shipped source spliced in verbatim below, no eval.
__FUNCSIF__

function setComposerStatus(s) { statuses.push(s); }
function showToast(m) { toasts.push(m); }
function t(k) { return k; }
// The real `_commitTranscript` is spliced in below (its paint is the finding);
// only its surroundings are stubbed here.
let _dictationAppend = false;
const sends = [];
function autoResize() {}
function send() { sends.push(ta.value); }
function _micWordCount(text) {
  return String(text || '').trim().split(/\s+/).filter(Boolean).length;
}
function _ensureSpeechRecognition() { return { start() {} }; }
function _setButtonTooltipAndKey() {}
function _micButtonAvailable() { return true; }
function _micOriginNeedsSecureContext() { return false; }
function _acquireMicWakeLock() {}
function _stopMic() {}
function _stopTracks() {}
function _micStartMeter() {}
function _applyDeferredServerSttFlip() {}
function _micToastKeyForRecognitionError() { return 'mic_denied'; }
async function _sendRawAudio() {}
function fetch(url, opts) {
  let resolve, reject;
  const p = new Promise((res, rej) => { resolve = res; reject = rej; });
  pending.push({ resolve, reject });
  return p;
}

const blob = new Blob(['x'], { type: 'audio/webm' });
const out = {};

function okResponse(body) { return { ok: true, status: 200, json: async () => body }; }
function errResponse(s, body) { return { ok: false, status: s, json: async () => body }; }
const isTimer = (s) => /^\d+:\d{2}$/.test(s || '');

(async () => {
  const snap = () => ({
    recording: btn.classList.contains('state-rec'),
    ready: btn.classList.contains('state-ready'),
    busy: btn.classList.contains('mic-busy'),
    label: micStateEl.textContent,
    labelIsTimer: isTimer(micStateEl.textContent),
    elapsedRunning: _micElapsedTimer !== null,
    statuses: statuses.slice(),
    toasts: toasts.slice(),
  });
  const reset = () => { paints.length = 0; statuses = []; toasts.length = 0; };

  // Capture A opens and stops; its transcription is in flight on the button.
  _setRecording(true);
  _setRecording(false);
  reset();
  const a = _transcribeBlob(blob, 'draft');
  await new Promise((r) => setImmediate(r));
  out.aInFlight = snap();

  // Capture B takes the button through the real start path while A is pending.
  await _startMicCapture();
  out.bStarted = snap();

  // A now fails late. Its settlement must not reach B's button.
  reset();
  pending.shift().resolve(errResponse(400, { error: 'late' }));
  await a;
  out.afterAFails = snap();

  // The recording clock must still be running for B.
  await new Promise((r) => setTimeout(r, 700));
  out.afterAFailsLater = snap();

  // Success arm: pending A, real capture B takes over, then A returns 200.
  // The transcript is still committed; the button stays B's.
  _setRecording(false);
  _isRecording = false; // the harness never ran the stop path for the last capture
  ta.value = 'draft';
  reset();
  const a2 = _transcribeBlob(blob, 'draft');
  await new Promise((r) => setImmediate(r));
  await _startMicCapture();
  reset();
  pending.shift().resolve(okResponse({ transcript: 'older words' }));
  await a2;
  out.afterASucceeds = snap();
  out.afterASucceedsComposer = ta.value;

  // Empty arm: same schedule, A returns an empty transcript.
  _setRecording(false);
  _isRecording = false;
  ta.value = 'draft';
  reset();
  const a3 = _transcribeBlob(blob, 'draft');
  await new Promise((r) => setImmediate(r));
  await _startMicCapture();
  reset();
  pending.shift().resolve(okResponse({ transcript: '   ' }));
  await a3;
  out.afterAEmpties = snap();
  out.afterAEmptiesComposer = ta.value;

  // Control: an owning success still paints its tick and stops its own clock.
  _setRecording(false);
  _isRecording = false;
  ta.value = '';
  const a4 = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  pending.shift().resolve(okResponse({ transcript: 'two words' }));
  await a4;
  out.ownerSuccess = snap();
  out.ownerSuccessComposer = ta.value;

  _micStopElapsed();
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""


def _run_capture_harness():
    funcs = "\n".join([
        _extract_function(BOOT_JS, "_isServerSttUnavailable"),
        _extract_function(BOOT_JS, "_allowBrowserSttFallback"),
        _extract_function(BOOT_JS, "_micStateClear"),
        _extract_function(BOOT_JS, "_micPaint"),
        _extract_function(BOOT_JS, "_micStartElapsed"),
        _extract_function(BOOT_JS, "_micStopElapsed"),
        _extract_function(BOOT_JS, "_micStopMeter"),
        _extract_function(BOOT_JS, "_setRecording"),
        _extract_function(BOOT_JS, "_commitTranscript"),
        _extract_function(BOOT_JS, "_transcribeBlob"),
        _extract_function(BOOT_JS, "_startMicCapture"),
    ])
    js = _CAPTURE_HARNESS.replace("__FUNCSIF__", funcs)
    proc = subprocess.run(
        [NODE, "-e", js], capture_output=True, text=True, cwd=ROOT, timeout=60
    )
    assert proc.returncode == 0, "node harness failed:\n%s" % proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def capture_runs():
    if NODE is None:
        pytest.skip("node not on PATH")
    return _run_capture_harness()


def test_pending_transcription_paints_thinking_while_in_flight(capture_runs):
    assert capture_runs["aInFlight"]["statuses"] == ["Transcribing…"]
    assert capture_runs["aInFlight"]["recording"] is False


def test_a_new_capture_takes_the_button_from_a_pending_request(capture_runs):
    started = capture_runs["bStarted"]
    assert started["recording"] is True, "the new capture must own state-rec"
    assert started["busy"] is True
    assert started["labelIsTimer"] is True, "the elapsed clock must be the label"


def test_a_late_failure_cannot_settle_a_recorder_that_took_over(capture_runs):
    after = capture_runs["afterAFails"]
    assert after["recording"] is True, (
        "a failed transcription of the previous capture must not clear the "
        "successor recording's state"
    )
    assert after["busy"] is True
    assert after["labelIsTimer"] is True, "the successor's timer label must survive"
    assert after["elapsedRunning"] is True, "the successor's clock must keep running"
    assert after["statuses"] == [], (
        "the stale request must not clear the composer status of a live capture"
    )
    # The failure itself is still surfaced to the user; only the button is off-limits.
    assert after["toasts"] == ["late"]


def test_the_recording_clock_keeps_ticking_after_the_late_failure(capture_runs):
    later = capture_runs["afterAFailsLater"]
    assert later["recording"] is True
    assert later["labelIsTimer"] is True
    assert later["elapsedRunning"] is True


def test_a_late_success_cannot_paint_over_a_recorder_that_took_over(capture_runs):
    after = capture_runs["afterASucceeds"]
    assert after["ready"] is False, (
        "an older request's ✓ Np must not replace the live capture's label"
    )
    assert after["recording"] is True
    assert after["labelIsTimer"] is True
    assert after["elapsedRunning"] is True, (
        "committing an older transcript must not stop the newer capture's clock"
    )


def test_a_late_success_still_commits_its_transcript(capture_runs):
    assert capture_runs["afterASucceedsComposer"] == "older words", (
        "the valid transcript must land in the composer even when the button "
        "belongs to a newer capture"
    )


def test_a_late_empty_response_cannot_paint_over_a_recorder(capture_runs):
    after = capture_runs["afterAEmpties"]
    assert after["ready"] is False
    assert after["recording"] is True
    assert after["busy"] is True
    assert after["labelIsTimer"] is True
    assert after["elapsedRunning"] is True
    assert capture_runs["afterAEmptiesComposer"] == "draft", (
        "an empty transcript leaves the composer untouched"
    )


def test_an_owning_success_still_paints_its_tick(capture_runs):
    owner = capture_runs["ownerSuccess"]
    assert owner["ready"] is True, "the owning request must still show its tick"
    assert owner["recording"] is False
    assert owner["label"] == "✓ 2p"
    assert owner["elapsedRunning"] is False, "the owner's own clock stops on settle"
    assert capture_runs["ownerSuccessComposer"] == "two words"

