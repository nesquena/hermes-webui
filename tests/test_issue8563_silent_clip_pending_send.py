"""Behavioural regression for #8563: a silent clip must not drop a queued send.

The server-STT path answers HTTP 200 with an empty transcript when the recorded
audio carried no speech. That must show the "no speech" toast ONLY when there is
nothing to send: if the user clicked Send while recording (an unambiguous send
intent) and the composer still holds a draft, the draft must go out.

Rather than regex the source, this drives the ACTUAL `_transcribeBlob` and
`_commitTranscript` from static/boot.js via node with a fake fetch/composer, and
observes whether `send()` runs and which toasts are shown.
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
  const slice = src.slice(start, i);
  // Keep an `async` modifier that sits just before `function` — without it the
  // extracted body would be re-parsed as a sync function and any `await` inside
  // it would be a syntax error.
  return src.slice(Math.max(0, start - 6), start).trim() === 'async' ? 'async ' + slice : slice;
}

// ── Fake browser + closure environment ──────────────────────────────────────
const _store = Object.assign({}, input.store || {});
global.localStorage = {
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
};
global.window = { _micActive: false, _micPendingSend: !!input.pendingSend };
global.document = { addEventListener: () => {}, visibilityState: 'visible' };

let SpeechRecognition = function () {};
let recognition = null;
let _forceMediaRecorder = false;
const _micForceMediaRecorderKey = 'mic_force_mediarecorder';
let ta = { value: input.composerText || '' };
const toasts = [];
let sendCalls = 0;
const statuses = [];

function autoResize() {}
function send() { sendCalls++; }
function showToast(m) { toasts.push(m); }
function t(k) { return k; }
function setComposerStatus(s) { statuses.push(s); }
function _ensureSpeechRecognition() { return null; }
// Mirrors static/ui.js _composerHasContent(): text, staged files or selected context.
function _composerHasContent() {
  return !!(ta.value.trim().length > 0 || (input.pendingFiles || 0) > 0 || !!input.hasSelection);
}

// The real transcription endpoint answer: 200 + empty transcript.
global.FormData = class { append() {} };
global.File = class { constructor(parts, name, opts) { this.name = name; } };
global.fetch = async () => ({
  ok: true,
  status: 200,
  json: async () => ({ ok: true, transcript: '', no_speech: true }),
});

// ── The real functions under test ───────────────────────────────────────────
eval(extractFunc('_commitTranscript'));
eval(extractFunc('_isServerSttUnavailable'));
eval(extractFunc('_allowBrowserSttFallback'));
eval(extractFunc('_transcribeBlob'));

const blob = { type: 'audio/webm', size: 1024 };

(async () => {
  await _transcribeBlob(blob, input.prefixSnapshot === undefined ? '' : input.prefixSnapshot);
  const out = {
    sendCalls: sendCalls,
    toasts: toasts,
    taValue: ta.value,
    pendingSendAfter: !!window._micPendingSend,
    statuses: statuses,
  };
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("silent_clip_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _run(driver_path, *, composer_text="", pending_send=False, prefix_snapshot="",
         pending_files=0, has_selection=False):
    payload = {
        "composerText": composer_text,
        "pendingSend": pending_send,
        "prefixSnapshot": prefix_snapshot,
        "pendingFiles": pending_files,
        "hasSelection": has_selection,
    }
    result = subprocess.run(
        [NODE, driver_path, str(BOOT_JS_PATH), json.dumps(payload)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


class TestSilentClipWithQueuedSend:
    def test_draft_is_sent_and_no_speech_toast_is_suppressed(self, driver_path):
        # The core regression: draft typed, recording started, Send clicked,
        # the clip turns out to be silent.
        out = _run(driver_path, composer_text="Deploy it", pending_send=True)
        assert out["sendCalls"] == 1, (
            "a Send clicked while recording must still send the composer draft "
            "when the transcript comes back empty"
        )
        assert "mic_no_speech" not in out["toasts"], (
            "the no-speech toast must be suppressed when there is something to send"
        )
        assert out["taValue"] == "Deploy it", "the draft must survive to be sent"
        assert out["pendingSendAfter"] is False, "_micPendingSend must be consumed"


class TestSilentClipWithoutAnythingToSend:
    def test_toast_is_shown_and_nothing_is_sent(self, driver_path):
        out = _run(driver_path, composer_text="", pending_send=False)
        assert out["sendCalls"] == 0
        assert out["toasts"] == ["mic_no_speech"]

    def test_pending_send_with_empty_composer_does_not_send(self, driver_path):
        # Send clicked while recording but nothing to send: no empty turn.
        out = _run(driver_path, composer_text="   ", pending_send=True)
        assert out["sendCalls"] == 0
        assert out["toasts"] == ["mic_no_speech"]
        assert out["pendingSendAfter"] is False


class TestSilentClipWithQueuedSendAndNoText:
    """A staged file or selected context is a turn even with an empty textarea."""

    def test_attachment_only_queued_send_is_sent(self, driver_path):
        out = _run(driver_path, composer_text="", pending_send=True, pending_files=1)
        assert out["sendCalls"] == 1, "a queued Send with a staged file must still send"
        assert "mic_no_speech" not in out["toasts"]
        assert out["pendingSendAfter"] is False

    def test_selection_only_queued_send_is_sent(self, driver_path):
        out = _run(driver_path, composer_text="", pending_send=True, has_selection=True)
        assert out["sendCalls"] == 1, "a queued Send with selected context must still send"
        assert "mic_no_speech" not in out["toasts"]
        assert out["pendingSendAfter"] is False

    def test_staged_file_without_queued_send_only_toasts(self, driver_path):
        out = _run(driver_path, composer_text="", pending_send=False, pending_files=1)
        assert out["sendCalls"] == 0
        assert out["toasts"] == ["mic_no_speech"]
