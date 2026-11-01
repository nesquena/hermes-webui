"""Dictation feedback: the composer strip (#micStatus) is the readout.

The behaviour lives in `static/boot.js`, which this suite cannot execute, so
these are contract checks on the published surface — the strip's children, the
aria wiring, the CSS that renders the three states, the lifecycle hooks that
drive them, and the strings in every locale — plus two node harnesses that run
the shipped `_transcribeBlob` / `_setRecording` against a stubbed DOM and
fetch. They fail if the wiring is removed, renamed, moved back into the mic
button, or driven by polling instead of the capture's own events.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _pill_markup():
    pos = INDEX_HTML.index('id="micStatus"')
    start = INDEX_HTML.rindex("<div", 0, pos)
    return INDEX_HTML[start:INDEX_HTML.index("</div>", pos) + 6]


def _readout_block():
    """The dictation readout section of boot.js, from its header to the meter."""
    start = BOOT_JS.index("// ── Dictation readout")
    return BOOT_JS[start:BOOT_JS.index("// Read at fire time", start)]


# --- the readout lives in the strip, not in the button ----------------------

def test_readout_is_rendered_by_the_strip_not_the_button():
    pill = _pill_markup()
    for child in ("mic-dot", "mic-spinner", "mic-status-text", "mic-timer",
                  "mic-level", "mic-level-fill"):
        assert child in pill, "the strip must own the %s element" % child
    # boot.js wires the shipped elements; it must not build its own widget.
    assert "status.querySelector('.mic-status-text')" in BOOT_JS
    assert "status.querySelector('.mic-timer')" in BOOT_JS
    assert "status.querySelector('.mic-level')" in BOOT_JS
    assert "btn.append(micStateEl" not in BOOT_JS, (
        "the readout must not be built inside the mic button: the footer "
        "squeezes that button to 25 px with the workspace pane open"
    )
    assert "mic-busy" not in BOOT_JS and "mic-busy" not in STYLE_CSS
    # The button keeps its icon and its red recording state as the stop control.
    assert "btn.classList.toggle('recording',on)" in BOOT_JS
    assert ".mic-btn.recording{" in STYLE_CSS


def test_only_the_label_is_announced():
    pill = _pill_markup()
    # The live region is the LABEL, not the strip: an aria-live region on the
    # pill re-fires atomically whenever any descendant mutates, and the
    # aria-hidden timer inside it changes every second — VoiceOver/Safari could
    # then announce "Listening…" once per second.
    strip_tag = pill[:pill.index(">") + 1]
    assert "aria-live" not in strip_tag, (
        "the live region belongs to the label; the strip holds the ticking timer"
    )
    assert "aria-atomic" not in strip_tag
    # Dot, spinner, timer and bar are decorative duplicates of the label; the
    # timer in particular ticks every second and must stay silent.
    assert pill.count('aria-hidden="true"') == 4
    label = pill[pill.index('class="mic-status-text"'):]
    label = label[:label.index(">")]
    assert 'aria-live="polite"' in label, (
        "the transcribing / outcome transitions must be announced"
    )
    assert 'aria-atomic="true"' in label
    assert "aria-hidden" not in label, "the label is the announced node"
    # Every painted state writes this node's text, so it must not carry a
    # [data-i18n] key: `applyLocaleToDOM()` re-stamps those nodes on every
    # Settings open and every language switch, which would overwrite a live
    # "Transcribing…" with the static "Listening…" and announce it.
    assert "data-i18n" not in label, "a painted label cannot be re-stamped by i18n"


def test_every_state_is_driven_by_the_capture_lifecycle():
    # rec starts with the capture, think with the request, and every settle —
    # text, silence, failure — paints a timed outcome. No polling: the strip is
    # written from these calls only.
    assert "_micPaint('rec')" in BOOT_JS
    assert "_micPaint('think')" in BOOT_JS
    assert "_micPaint('done',_micWordCount(clean),_MIC_READY_MS)" in BOOT_JS
    assert "_micPaint('empty',0,_MIC_EMPTY_MS)" in BOOT_JS
    assert "_micPaint('failed',0,_MIC_FAILED_MS)" in BOOT_JS


def test_outcome_is_a_translated_sentence_not_a_symbol():
    # "✓ 15p" was explained nowhere; the count is spelled out and pluralised,
    # and every label goes through the translator.
    labels = BOOT_JS.split("function _micStateLabel(state,count){", 1)[1].split(
        "function _micStartElapsed", 1)[0]
    for key in ("voice_listening", "voice_transcribing", "voice_transcribe_failed",
                "mic_no_speech"):
        assert "t('" + key in labels, "%s must come from the translator" % key
    assert "'voice_word_added':'voice_words_added'" in labels
    assert "t(count===1?'voice_word_added':'voice_words_added',count||0)" in labels
    # No symbol stand-ins left in the label path.
    for dead in ("✓", "···", "p'"):
        assert dead not in labels, "the untranslated %r readout must be gone" % dead


def test_a_failed_or_stuck_request_can_never_stay_transcribing():
    readout = _readout_block()
    # A request that never settles is retired by a bound, well past the local
    # Whisper time of a long clip; every other paint cancels it.
    assert "_MIC_THINK_MAX_MS=180000" in readout
    assert "_micPaint(null);},_MIC_THINK_MAX_MS)" in readout
    assert "if(_micStateTimer){clearTimeout(_micStateTimer);_micStateTimer=null;}" in readout
    # Failure exits paint an outcome instead of returning silently.
    transcribe = BOOT_JS.split("async function _transcribeBlob(", 1)[1].split(
        "function _stopTracks", 1)[0]
    assert transcribe.count("_micPaint('failed',0,_MIC_FAILED_MS)") == 2, (
        "both the browser-fallback and the hard-failure exits must settle the strip"
    )
    # A frozen page (bfcache) cannot keep a capture alive either: the readout
    # exposes a reset hook and the single boot-level pageshow handler calls it.
    assert "window._micResetOnRestore=" in readout
    assert "window._micResetOnRestore === 'function'" in BOOT_JS


# --- the CSS carries the state, with theme tokens ---------------------------

def test_css_renders_all_three_states_on_the_strip():
    assert (".mic-status.is-rec .mic-dot,.mic-status.is-rec .mic-timer,"
            ".mic-status.is-rec .mic-level{display:block;}") in STYLE_CSS
    assert ".mic-status.is-think .mic-spinner{display:block;}" in STYLE_CSS
    # Reuses the existing spin keyframe rather than three static dots.
    assert "animation:spin .8s linear infinite" in STYLE_CSS
    assert "@keyframes spin{to{transform:rotate(360deg);}}" in STYLE_CSS
    assert ".mic-status .mic-level-fill{" in STYLE_CSS
    assert "transform:scaleX(var(--mic-level,0))" in STYLE_CSS
    # The track is a ring, not a filled plate: an ink plate measured 1.00-1.89:1
    # against the state fill in 38 palette blocks, so in silence the bar read as
    # a full meter and the fill's edge vanished in greyscale. A 1 px border in
    # the verified ink token leaves a transparent interior, so silence is an
    # empty outline and speech a filled bar, readable without colour.
    assert ("border:1px solid color-mix(in srgb,var(--text) 70%,transparent)"
            in STYLE_CSS)
    track = STYLE_CSS.split(".mic-status .mic-level{", 1)[1].split("}", 1)[0]
    assert "background:transparent" in track, "the ring's interior must stay empty"
    assert "box-sizing:border-box" in track, (
        "the 1 px ring must stay inside the 46 px slot, not grow it"
    )
    assert "height:6px" in track, (
        "a 4 px box leaves a hairline interior once the 1 px ring is inset"
    )


def test_state_colours_come_from_theme_tokens():
    readout_css = STYLE_CSS[STYLE_CSS.index("── Dictation readout"):]
    readout_css = readout_css[:readout_css.index("── Turn-based voice mode")]
    for token in ("--error", "--warning", "--success", "--text"):
        assert "var(%s)" % token in readout_css, "%s must drive a state colour" % token
    # The hard-coded amber and green that measured 1.46:1 / 1.81:1 on light.
    for hexcode in ("#f5b301", "#34c759", "#9aa0a6"):
        assert hexcode not in readout_css
    # Warning and success are mixed toward the body colour: the raw tokens miss
    # the 3:1 graphical minimum on the light themes.
    assert "color-mix(in srgb,var(--warning) 60%,var(--text))" in readout_css
    assert "color-mix(in srgb,var(--success) 60%,var(--text))" in readout_css
    # The label itself stays on --text, which every palette keeps above 4.5:1.
    assert ".mic-status{--mic-state:var(--text);font-size:11px;color:var(--text);" in STYLE_CSS


def test_level_meter_uses_the_capture_stream_not_a_second_capture():
    # A second getUserMedia would prompt a second permission and open two input
    # streams for one dictation.
    assert "_micStartMeter(captureStream)" in BOOT_JS
    meter = BOOT_JS.split("function _micStartMeter(stream){", 1)[1].split(
        "function _micWordCount", 1)[0]
    assert "getUserMedia" not in meter
    assert "createMediaStreamSource(stream)" in meter


def test_silence_auto_stop_is_opt_in_and_bounded():
    # Maintainer decision: manual capture is the default, silence auto-stop is a
    # Settings preference that starts OFF.
    assert "_MIC_AUTOSTOP_KEY='hermes-mic-auto-stop'" in BOOT_JS
    assert "localStorage.getItem(_MIC_AUTOSTOP_KEY)==='true'" in BOOT_JS
    assert "_MIC_SILENCE_MS=1600" in BOOT_JS
    auto_stop = BOOT_JS.split("function _micStartMeter(stream){", 1)[1].split(
        "function _micWordCount", 1)[0]
    # It may only fire after speech was actually heard, never on a quiet room.
    assert "if(!_micSpokeAt) return;" in auto_stop
    assert "_micAutoStopEnabled()" in auto_stop


def test_auto_stop_preference_is_wired_through_settings():
    # One preference, four surfaces: server default OFF (durable), the Sound
    # panel checkbox, its runtime cache key, and the boot-time mirror that turns
    # the server value into localStorage.
    config_py = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
    assert '"mic_auto_stop": False' in config_py
    assert config_py.count('"mic_auto_stop",') >= 2  # speech key set + bool validation
    assert 'id="settingsMicAutoStop"' in INDEX_HTML
    assert "mic_auto_stop:'hermes-mic-auto-stop'" in PANELS_JS
    assert "['mic_auto_stop','hermes-mic-auto-stop']" in BOOT_JS
    assert "settings_label_mic_autostop" in I18N_JS


def test_settings_note_names_the_direction_and_the_description_is_short():
    # "this preference was not changed" described neither direction, and it
    # appeared before the user had done anything.
    assert 'id="settingsMicAutoStopNoteSavedOn"' in INDEX_HTML
    assert 'id="settingsMicAutoStopNoteSavedOff"' in INDEX_HTML
    assert "settings_mic_autostop_storage_blocked" not in INDEX_HTML
    assert "settings_mic_autostop_storage_blocked" not in I18N_JS
    assert "function _setMicAutoStopStorageNote(wantedOn,effectiveOn){" in PANELS_JS
    # Hydration and the refused toggle both report the value in force.
    assert "_setMicAutoStopStorageNote(micAutoStop,micAutoStopCb.checked)" in PANELS_JS
    assert "_setMicAutoStopStorageNote(desired,this.checked)" in PANELS_JS
    desc = INDEX_HTML.split('data-i18n="settings_desc_mic_autostop">', 1)[1].split("<", 1)[0]
    assert desc == "When on, dictation stops 1.6 s after you finish speaking."
    # A refused toggle returns before the autosave and the key stays unapplied,
    # so the note may not claim the setting was saved.
    assert "Saved as on" not in INDEX_HTML and "Saved as off" not in INDEX_HTML
    assert "You turned it on" in INDEX_HTML and "You turned it off" in INDEX_HTML


def test_tooltip_anchor_is_untouched():
    # .has-tooltip is what positions the button; the readout moving out of it
    # must not introduce a competing position rule on .mic-btn.
    assert ".has-tooltip{position:relative;}" in STYLE_CSS
    assert ".mic-btn{position:" not in STYLE_CSS


# --- the new strings exist in every locale, with working placeholders -------

NODE = shutil.which("node")

_LOCALE_PROBE = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8')
  + '\n;globalThis.__LOCALES = LOCALES; globalThis.__T = t; globalThis.__SET = setLocale;';
const sandbox = {
  console,
  document: { documentElement: {}, addEventListener() {}, querySelectorAll() { return []; } },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  navigator: { language: 'en', languages: ['en'] },
  setTimeout, clearTimeout,
};
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: 'i18n.js' });
const locales = sandbox.__LOCALES;
const keys = process.argv.slice(3);
const out = { codes: Object.keys(locales), strings: {}, rendered: {}, words: {} };
for (const code of out.codes) {
  const row = {};
  for (const k of keys) {
    const v = Object.prototype.hasOwnProperty.call(locales[code], k) ? locales[code][k] : null;
    // Slavic locales carry the word-count sentence as a function so it can pick
    // the paucal form; everything else is a plain {0} template.
    row[k] = typeof v === 'function' ? 'function' : v;
  }
  out.strings[code] = row;
}
// Render the word-count sentence the way `_micStateLabel` asks for it.
for (const code of out.codes) {
  sandbox.__SET(code);
  out.words[code] = [1, 2, 3, 5, 12, 22].map(
    (n) => sandbox.__T(n === 1 ? 'voice_word_added' : 'voice_words_added', n)
  );
}
sandbox.__SET('en');
out.rendered.plural = sandbox.__T('voice_words_added', 3);
out.rendered.singular = sandbox.__T('voice_word_added', 1);
out.rendered.transcribing = sandbox.__T('voice_transcribing');
console.log(JSON.stringify(out));
"""

STRIP_KEYS = (
    "voice_listening",
    "voice_transcribing",
    "voice_word_added",
    "voice_words_added",
    "voice_transcribe_failed",
    "mic_no_speech",
    "settings_desc_mic_autostop",
    "settings_mic_autostop_storage_saved_on",
    "settings_mic_autostop_storage_saved_off",
)


@pytest.fixture(scope="module")
def locale_strings(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not on PATH")
    probe = tmp_path_factory.mktemp("i18n") / "locale_probe.cjs"
    probe.write_text(_LOCALE_PROBE, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(probe), str(ROOT / "static" / "i18n.js")] + list(STRIP_KEYS),
        capture_output=True, text=True, cwd=ROOT, timeout=60,
    )
    assert proc.returncode == 0, "locale probe failed:\n%s" % proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_every_new_string_exists_in_every_locale(locale_strings):
    codes = locale_strings["codes"]
    assert len(codes) == 15, codes
    missing = [
        "%s:%s" % (code, key)
        for code in codes
        for key, value in locale_strings["strings"][code].items()
        if not value
    ]
    assert missing == [], "untranslated keys: %s" % missing


def test_word_count_renders_with_the_count_in_the_sentence(locale_strings):
    # The count moved into the sentence: "3 words added", never "15p".
    assert locale_strings["rendered"]["plural"] == "3 words added"
    assert locale_strings["rendered"]["singular"] == "1 word added"
    assert locale_strings["rendered"]["transcribing"] == "Transcribing…"
    for code, row in locale_strings["strings"].items():
        assert row["voice_word_added"], code
        assert "{0}" in row["voice_word_added"], code
        if row["voice_words_added"] == "function":
            continue  # paucal locales: rendered through t() and checked below
        assert "{0}" in row["voice_words_added"], code


# 1 / few (2-4) / many (5+). Russian and Polish keep the paucal for compounds
# ending in 2-4 (22 слова / 22 słowa); Czech only for bare 2-4 (22 slov).
PAUCAL_WORD_COUNTS = {
    "ru": ["Добавлено 1 слово", "Добавлено 2 слова", "Добавлено 3 слова",
           "Добавлено 5 слов", "Добавлено 12 слов", "Добавлено 22 слова"],
    "pl": ["Dodano 1 słowo", "Dodano 2 słowa", "Dodano 3 słowa",
           "Dodano 5 słów", "Dodano 12 słów", "Dodano 22 słowa"],
    "cs": ["Přidáno 1 slovo", "Přidáno 2 slova", "Přidáno 3 slova",
           "Přidáno 5 slov", "Přidáno 12 slov", "Přidáno 22 slov"],
}


def test_paucal_locales_agree_with_their_own_count(locale_strings):
    # A count-only two-form split rendered "2 words" with the 5+ form in these
    # three locales ("Dodano 2 słów" instead of "Dodano 2 słowa").
    for code, expected in PAUCAL_WORD_COUNTS.items():
        assert locale_strings["words"][code] == expected, code


# --- the locale re-stamp must not rewrite the painted label -----------------
#
# `applyLocaleToDOM()` re-stamps every `[data-i18n]` node, and
# `loadSettingsPanel()` calls it on every Settings open and on every language
# switch. While the strip's label carried `data-i18n="voice_listening"`, an
# in-flight "Transcribing…" — or any outcome — was silently rewritten to
# "Listening…" the moment Settings was opened, and the aria-live region then
# announced a state the runtime was not in. This runs the shipped
# `_micPaint` and the shipped `applyLocaleToDOM()` against the label markup as
# it is published in index.html.

_LABEL_CLOBBER_PROBE = r"""
const fs = require('fs');
const vm = require('vm');
const labelAttrs = __LABEL_ATTRS__;
const registry = [];
function mkEl(attrs) {
  const a = Object.assign({}, attrs);
  const node = {
    textContent: '',
    style: { display: '', setProperty() {} },
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(a, k) ? a[k] : null; },
    setAttribute(k, v) { a[k] = String(v); },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(a, k); },
    removeAttribute(k) { delete a[k]; },
  };
  registry.push(node);
  return node;
}
const sandbox = {
  console,
  document: {
    documentElement: {},
    addEventListener() {},
    querySelector(sel) { return null; },
    getElementById() { return null; },
    querySelectorAll(sel) {
      const m = /^\[([a-zA-Z0-9-]+)\]$/.exec(String(sel).trim());
      if (!m) return [];
      return registry.filter((n) => n.hasAttribute(m[1]));
    },
  },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  navigator: { language: 'en', languages: ['en'] },
  setTimeout, clearTimeout, setInterval, clearInterval,
};
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
// Exactly the elements the shipped markup publishes for the strip.
sandbox.status = mkEl({ id: 'micStatus' });
sandbox.micTextEl = mkEl(labelAttrs);
sandbox.micTimerEl = mkEl({ 'aria-hidden': 'true' });
sandbox.micLevelEl = mkEl({ 'aria-hidden': 'true' });
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox, { filename: 'i18n.js' });
vm.runInContext(__FUNCS__, sandbox, { filename: 'boot-readout.js' });
vm.runInContext(`
  const out = {};
  setLocale('it');
  out.listening = t('voice_listening');
  out.transcribing = t('voice_transcribing');
  _micPaint('think');
  out.inFlightBefore = micTextEl.textContent;
  applyLocaleToDOM();
  out.inFlightAfter = micTextEl.textContent;
  _micStateClear();
  _micPaint('done', 2);
  out.outcomeBefore = micTextEl.textContent;
  applyLocaleToDOM();
  out.outcomeAfter = micTextEl.textContent;
  _micStateClear();
  globalThis.__out = out;
`, sandbox, { filename: 'driver.js' });
console.log(JSON.stringify(sandbox.__out));
"""


def _label_tag_attrs():
    """The attributes of the shipped `.mic-status-text` label, verbatim."""
    pill = _pill_markup()
    pos = pill.index('class="mic-status-text"')
    start = pill.rindex("<span", 0, pos)
    tag = pill[start:pill.index(">", pos) + 1]
    return dict(re.findall(r'([a-zA-Z0-9-]+)="([^"]*)"', tag))


def _run_label_clobber_probe():
    funcs = "\n".join([
        "let _micElapsedTimer = null;",
        "let _micStateTimer = null;",
        "const _MIC_THINK_MAX_MS = 180000;",
        _extract_function(BOOT_JS, "_micStateClear"),
        _extract_function(BOOT_JS, "_micPaint"),
        _extract_function(BOOT_JS, "_micStateLabel"),
        _extract_function(BOOT_JS, "_micStopElapsed"),
    ])
    js = (_LABEL_CLOBBER_PROBE
          .replace("__LABEL_ATTRS__", json.dumps(_label_tag_attrs()))
          .replace("__FUNCS__", json.dumps(funcs)))
    proc = subprocess.run(
        [NODE, "-e", js, str(ROOT / "static" / "i18n.js")],
        capture_output=True, text=True, cwd=ROOT, timeout=60,
    )
    assert proc.returncode == 0, "node harness failed:\n%s" % proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_locale_restamp_cannot_rewrite_the_painted_label():
    if NODE is None:
        pytest.skip("node not on PATH")
    out = _run_label_clobber_probe()
    # The strip really was painted for the in-flight state before the re-stamp.
    assert out["inFlightBefore"] == out["transcribing"], out
    assert out["inFlightBefore"] != out["listening"], out
    assert out["inFlightAfter"] == out["inFlightBefore"], (
        "applyLocaleToDOM() turned the in-flight %r into %r: opening Settings "
        "or switching language re-stamps [data-i18n] nodes and announces a "
        "state the capture is not in" % (out["inFlightBefore"], out["inFlightAfter"])
    )
    # Same for a settled outcome, which carries the word count.
    assert out["outcomeBefore"] not in (out["listening"], out["transcribing"]), out
    assert out["outcomeAfter"] == out["outcomeBefore"], (
        "applyLocaleToDOM() rewrote the outcome %r into %r"
        % (out["outcomeBefore"], out["outcomeAfter"])
    )


# --- behavioural coverage: the shipped code, under node ---------------------
#
# Source-presence assertions cannot see a state that is painted and never
# cleared (a failed transcription left the button on 'think'; a successful
# no-speech answer did the same). These run the shipped `_transcribeBlob` under
# node with a stubbed DOM/fetch, so every exit path is exercised as code.


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
const _MIC_READY_MS = 3000;
const _MIC_EMPTY_MS = 2500;
const _MIC_FAILED_MS = 4000;
let _micStateTimer = null;
const paints = [];
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

function _micStateClear() { if (_micStateTimer) { clearTimeout(_micStateTimer); _micStateTimer = null; } }
function _micPaint(state, count) { paints.push(state === undefined ? null : state); }
function showToast(m) { toasts.push(m); }
function t(k, ...a) { return a.length ? k + '(' + a.join(',') + ')' : k; }
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
  return { ok: false, status: status, json: async () => body };
}

(async () => {
  const begin = () => { paints.length = 0; toasts.length = 0; };

  // 1. success: 'think' while in flight, then the transcript is committed.
  begin();
  let p = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  out.successPaints = paints.slice();
  pending.shift().resolve(okResponse({ transcript: 'hi there' }));
  await p;
  out.successCommits = commits.slice();

  // 2. a 200 with nothing heard settles to 'empty' (never stays on 'think').
  begin();
  p = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  pending.shift().resolve(okResponse({ transcript: '' }));
  await p;
  out.emptyCommits = commits.slice();
  out.emptyToasts = toasts.slice();
  out.emptyPaints = paints.slice();

  // 3. HTTP failure must settle to 'failed' and report the reason.
  begin();
  p = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  pending.shift().resolve(errResponse(400, { error: 'bad audio' }));
  await p;
  out.http400Paints = paints.slice();
  out.http400Toasts = toasts.slice();

  // 4. rejected fetch (no status) must settle too.
  begin();
  p = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  pending.shift().reject(new Error('network down'));
  await p;
  out.rejectedPaints = paints.slice();
  out.rejectedToasts = toasts.slice();

  // 5. browser fallback path settles too, and arms recognition.
  begin();
  p = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  pending.shift().resolve(errResponse(503, { error: 'unavailable' }));
  await p;
  out.fallbackPaints = paints.slice();
  out.fallbackRecognitionArmed = recognition !== null;
  out.fallbackStore = Object.assign({}, store);

  // 6. a stale request must not settle a newer request's strip.
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
  pending.shift().resolve(okResponse({ transcript: 'newer' }));
  await second;
  out.staleFinalCommits = commits.slice(-1);

  // 7. a superseded request whose no-speech response lands late must not paint
  // over the newer capture's strip either.
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
    assert transcribe_runs["successPaints"] == ["think"]
    assert transcribe_runs["successCommits"] == ["hi there"]
    assert transcribe_runs["emptyCommits"] == ["hi there"], (
        "an empty transcript must not append anything to the composer"
    )
    # The strip says "No speech detected. Try again." on its own; a toast on
    # top of it repeated the sentence and covered the header on a phone.
    assert transcribe_runs["emptyToasts"] == [], (
        "the empty path must not toast: the strip already carries the outcome"
    )
    assert transcribe_runs["http400Toasts"] == ["bad audio"], (
        "a failure keeps its toast: it carries the server message"
    )
    assert transcribe_runs["rejectedToasts"] == ["network down"], (
        "a rejected request keeps its toast too"
    )
    assert transcribe_runs["emptyPaints"] == ["think", "empty"], (
        "a successful no-speech response must retire 'think' to the timed "
        "empty outcome, not leave the strip in flight forever"
    )


def test_stale_no_speech_does_not_paint_a_newer_capture(transcribe_runs):
    assert transcribe_runs["staleNoSpeechPaints"] == [], (
        "a superseded request's empty response must not touch a strip its "
        "successor owns"
    )
    assert transcribe_runs["staleNoSpeechFinalCommits"] == ["newer still"]
    assert transcribe_runs["staleNoSpeechToasts"] == ["mic_no_speech"], (
        "a superseded 200-with-no-speech has no strip to speak through, so it "
        "must still toast: silence is the one thing it cannot say"
    )


def test_http_failure_settles_to_an_outcome(transcribe_runs):
    assert transcribe_runs["http400Paints"] == ["think", "failed"], (
        "a 400 must show the failure outcome, not spin on 'think'"
    )
    assert transcribe_runs["http400Toasts"] == ["bad audio"]


def test_rejected_fetch_settles_to_an_outcome(transcribe_runs):
    assert transcribe_runs["rejectedPaints"] == ["think", "failed"]
    assert transcribe_runs["rejectedToasts"] == ["network down"]


def test_browser_fallback_settles_the_strip(transcribe_runs):
    assert transcribe_runs["fallbackPaints"] == ["think", "failed"]
    assert transcribe_runs["fallbackRecognitionArmed"] is True
    assert transcribe_runs["fallbackStore"]["mic_force_mediarecorder"] == "0"


def test_a_stale_request_does_not_settle_a_newer_capture(transcribe_runs):
    assert transcribe_runs["stalePaints"] == [], (
        "the superseded request must not touch a strip its successor owns"
    )
    assert transcribe_runs["staleFinalCommits"] == ["newer"]


# --- capture takes ownership, not just a newer request ----------------------
#
# The schedule above starts two transcriptions, which only proves that a newer
# request outranks an older one. It cannot see the real lifecycle: a recording
# that starts while a transcription is in flight owns the strip even though no
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
const _MIC_FAILED_MS = 4000;
const _MIC_THINK_MAX_MS = 180000;
const _MIC_AUTOSTOP_KEY = 'hermes-mic-auto-stop';
const paints = [];
const toasts = [];
const commits = [];
const store = {};
const pending = [];
const SpeechRecognition = function () {};
const ta = { value: '' };

function _stubEl() {
  const set = new Set();
  const attrs = {};
  return {
    textContent: '',
    hidden: true,
    style: { display: '', setProperty() {} },
    classList: {
      toggle(cls, on) { on ? set.add(cls) : set.delete(cls); },
      contains(cls) { return set.has(cls); },
    },
    setAttribute(k, v) { attrs[k] = v; },
    removeAttribute(k) { delete attrs[k]; },
    getAttribute(k) { return attrs[k]; },
  };
}
const btn = _stubEl();
const status = _stubEl();
const micTextEl = _stubEl();
const micTimerEl = _stubEl();
const micLevelEl = _stubEl();

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
  value: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) } },
});

// Shipped source spliced in verbatim below, no eval; `_micPaint` is renamed so
// the wrapper after it can record every state the shipped code paints.
__FUNCSIF__

function _micPaint(state, count, holdMs) {
  paints.push(state === undefined ? null : state);
  return _micPaintReal(state, count, holdMs);
}
function showToast(m) { toasts.push(m); }
function t(k, ...a) { return a.length ? k + '(' + a.join(',') + ')' : k; }
// The real `_commitTranscript` is spliced in below (its paint is the point);
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
const isClock = (s) => /^\d+:\d{2}$/.test(s || '');

(async () => {
  const snap = () => ({
    state: status.getAttribute('data-state') || null,
    label: micTextEl.textContent,
    timer: micTimerEl.textContent,
    timerIsClock: isClock(micTimerEl.textContent),
    elapsedRunning: _micElapsedTimer !== null,
    toasts: toasts.slice(),
  });
  const reset = () => { paints.length = 0; toasts.length = 0; };

  // Capture A opens and stops; its transcription is in flight in the strip.
  _setRecording(true);
  _setRecording(false);
  reset();
  const a = _transcribeBlob(blob, 'draft');
  await new Promise((r) => setImmediate(r));
  out.aInFlight = snap();
  out.aInFlightPaints = paints.slice();

  // Capture B takes the strip through the real start path while A is pending.
  await _startMicCapture();
  out.bStarted = snap();

  // A now fails late. Its settlement must not reach B's strip.
  reset();
  pending.shift().resolve(errResponse(400, { error: 'late' }));
  await a;
  out.afterAFails = snap();
  out.afterAFailsPaints = paints.slice();

  // The recording clock must still be running for B.
  await new Promise((r) => setTimeout(r, 700));
  out.afterAFailsLater = snap();

  // Success arm: pending A, real capture B takes over, then A returns 200.
  // The transcript is still committed; the strip stays B's.
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

  // Control: an owning success still paints its outcome and stops its clock.
  _setRecording(false);
  _isRecording = false;
  ta.value = '';
  const a4 = _transcribeBlob(blob, '');
  await new Promise((r) => setImmediate(r));
  pending.shift().resolve(okResponse({ transcript: 'two words' }));
  await a4;
  out.ownerSuccess = snap();
  out.ownerSuccessPaints = paints.slice();
  out.ownerSuccessComposer = ta.value;

  // Finding 1: a newer capture that never transcribes (raw audio, zero-byte
  // clip, recorder error) takes the strip without ever settling it. The
  // request that wrote 'Transcribing…' must retire it, or the user stares at
  // it forever.
  _setRecording(false);
  _isRecording = false;
  ta.value = 'draft';
  reset();
  const a5 = _transcribeBlob(blob, 'draft');
  await new Promise((r) => setImmediate(r));
  _rawAudioMode = true;
  await _startMicCapture();
  _rawAudioMode = false;
  reset();
  pending.shift().resolve(okResponse({ transcript: 'settled words' }));
  await a5;
  out.afterRawSupersede = snap();
  out.afterRawSupersedePaints = paints.slice();

  // Finding 2: a superseded recorder's late `onstop` still settles its
  // transcript, but it must not paint 'think' and must not take the newer
  // capture's label or clock.
  _setRecording(false);
  _isRecording = false;
  ta.value = 'draft';
  await _startMicCapture();
  reset();
  const late = _transcribeBlob(blob, 'draft', false);
  await new Promise((r) => setImmediate(r));
  out.supersededInFlight = snap();
  out.supersededPaints = paints.slice();
  reset();
  pending.shift().resolve(okResponse({ transcript: 'late words' }));
  await late;
  out.afterSuperseded = snap();
  out.afterSupersededPaints = paints.slice();
  out.afterSupersededComposer = ta.value;

  _micStopElapsed();
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""


def _run_capture_harness():
    paint = _extract_function(BOOT_JS, "_micPaint").replace(
        "function _micPaint(", "function _micPaintReal(", 1
    )
    funcs = "\n".join([
        _extract_function(BOOT_JS, "_isServerSttUnavailable"),
        _extract_function(BOOT_JS, "_allowBrowserSttFallback"),
        _extract_function(BOOT_JS, "_micStateClear"),
        paint,
        _extract_function(BOOT_JS, "_micStateLabel"),
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


def test_pending_transcription_paints_transcribing_in_the_strip(capture_runs):
    in_flight = capture_runs["aInFlight"]
    assert in_flight["state"] == "think"
    assert in_flight["label"] == "voice_transcribing"
    assert capture_runs["aInFlightPaints"] == ["think"]


def test_a_new_capture_takes_the_readout_from_a_pending_request(capture_runs):
    started = capture_runs["bStarted"]
    assert started["state"] == "rec", "the new capture must own the recording state"
    assert started["label"] == "voice_listening"
    assert started["timerIsClock"], "the elapsed m:ss clock must be showing"


def test_a_late_failure_cannot_settle_a_recorder_that_took_over(capture_runs):
    after = capture_runs["afterAFails"]
    assert after["state"] == "rec", (
        "a failed transcription of the previous capture must not clear the "
        "successor recording's state"
    )
    assert after["timerIsClock"], "the successor's clock must survive"
    assert after["elapsedRunning"] is True, "the successor's clock must keep running"
    assert capture_runs["afterAFailsPaints"] == [], (
        "the old request must not paint over the strip the new capture owns"
    )
    # The failure itself is still surfaced to the user; only the strip is off-limits.
    assert after["toasts"] == ["late"]


def test_the_recording_clock_keeps_ticking_after_the_late_failure(capture_runs):
    later = capture_runs["afterAFailsLater"]
    assert later["state"] == "rec"
    assert later["timerIsClock"]
    assert later["elapsedRunning"] is True


def test_a_late_success_cannot_paint_over_a_recorder_that_took_over(capture_runs):
    after = capture_runs["afterASucceeds"]
    assert after["state"] == "rec", (
        "an older request's word count must not replace the live capture's readout"
    )
    assert after["timerIsClock"]
    assert after["elapsedRunning"] is True, (
        "committing an older transcript must not stop the newer capture's clock"
    )


def test_a_late_success_still_commits_its_transcript(capture_runs):
    assert capture_runs["afterASucceedsComposer"] == "older words", (
        "the valid transcript must land in the composer even when the strip "
        "belongs to a newer capture"
    )


def test_a_late_empty_response_cannot_paint_over_a_recorder(capture_runs):
    after = capture_runs["afterAEmpties"]
    assert after["state"] == "rec"
    assert after["timerIsClock"]
    assert after["elapsedRunning"] is True
    assert capture_runs["afterAEmptiesComposer"] == "draft", (
        "an empty transcript leaves the composer untouched"
    )


def test_an_owning_success_paints_the_word_count_sentence(capture_runs):
    owner = capture_runs["ownerSuccess"]
    assert owner["state"] == "done", "the owning request must show its outcome"
    assert owner["label"] == "voice_words_added(2)", (
        "the count is spelled out and translated, not the old '✓ 2p'"
    )
    assert owner["elapsedRunning"] is False, "the owner's own clock stops on settle"
    assert capture_runs["ownerSuccessComposer"] == "two words"


def test_a_capture_that_never_transcribes_ends_the_transcribing_state(capture_runs):
    after = capture_runs["afterRawSupersede"]
    assert "think" not in capture_runs["afterRawSupersedePaints"], (
        "a raw-audio capture took the strip while a transcription was pending; "
        "the in-flight state must not survive it"
    )
    assert after["state"] == "rec", "the raw capture keeps the strip"
    assert after["timerIsClock"]
    assert after["elapsedRunning"] is True


def test_a_superseded_recorder_never_takes_the_strip(capture_runs):
    in_flight = capture_runs["supersededInFlight"]
    assert capture_runs["supersededPaints"] == [], (
        "a superseded recorder must not paint 'think' over the live capture"
    )
    assert in_flight["state"] == "rec"
    assert in_flight["timerIsClock"]

    after = capture_runs["afterSuperseded"]
    assert capture_runs["afterSupersededPaints"] == [], (
        "its settlement must not paint over the live capture either"
    )
    assert after["state"] == "rec"
    assert after["timerIsClock"]
    assert after["elapsedRunning"] is True


def test_a_superseded_recorder_still_commits_its_transcript(capture_runs):
    assert capture_runs["afterSupersededComposer"] == "late words", (
        "the user's words are committed even though the strip belongs elsewhere"
    )