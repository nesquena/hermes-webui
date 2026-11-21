"""Regression checks for #9113: an accepted TTS voice that leaves the catalog
must survive an unrelated settings autosave.

The bug: `_populateTtsVoices` marked an option `selected` only when the stored
voice was still in the rebuilt catalog. Once it was not, the native select fell
back to its `value=""` Default option and the next unrelated autosave (for
example the Notifications toggle) posted `tts_voice: ""`, replacing the accepted
preference.

The checks below run the real `_populateTtsVoices`, `_speechSetting` and
`_speechPreferencesPayloadFromUi` blocks extracted from `static/panels.js`
against a node model of a native `<select>` (an assigned value with no matching
option leaves the control on its first option, exactly like the browser's
selectedness algorithm). No browser, no network.
"""

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _slice(start_marker, end_marker, start_from=0):
    start = PANELS_JS.index(start_marker, start_from)
    end = PANELS_JS.index(end_marker, start)
    assert start < end, f"empty slice {start_marker!r} -> {end_marker!r}"
    return PANELS_JS[start:end]


def _blocks():
    populate_start = PANELS_JS.index("window._populateTtsVoices=function(){")
    handler_start = PANELS_JS.index("ttsVoiceSel.onchange=function(){", populate_start)
    handler_start += len("ttsVoiceSel.onchange=function(){")
    handler_end = PANELS_JS.index("_schedulePreferencesAutosave();", handler_start)
    handler_end += len("_schedulePreferencesAutosave();")
    return {
        # storage map, ownership tracking, mirror guard, cache sync, note helpers
        "helpers": _slice(
            "const _SETTINGS_SPEECH_STORAGE_KEYS=",
            "function _setOwnedSpeechPayload",
        ),
        # _setOwnedSpeechPayload + both payload builders
        "payload": _slice(
            "function _setOwnedSpeechPayload(",
            "function _retryPreferencesAutosave(",
        ),
        # persistedSpeechKeys + ownership capture + the real _speechSetting
        "speech_setting": _slice(
            "const persistedSpeechKeys = new Set(",
            "const _speechBool=function",
        ),
        "populate": _slice(
            "window._populateTtsVoices=function(){",
            "if(ttsVoiceSel&&'speechSynthesis' in window){",
        ),
        # engine hydration: shows the saved engine and records which engine the
        # persisted voice belongs to (before the onchange wiring)
        "engine_hydration": _slice(
            "const saved=String(_speechSetting('tts_engine'",
            "ttsEngineSel.onchange=function(){",
        ),
        # the production voice <select> onchange body
        "voice_handler": PANELS_JS[handler_start:handler_end],
    }


_HARNESS = r"""
const assert = require('assert');

// ── Browser surface ──────────────────────────────────────────────────────
const store = new Map();
let REFUSE = false;
const localStorage = {
  getItem(key){ return store.has(key) ? store.get(key) : null; },
  setItem(key, value){
    if(REFUSE) throw new Error('QuotaExceededError');
    store.set(key, String(value));
  },
  removeItem(key){ store.delete(key); },
};

class FakeEl {
  constructor(tag){
    this.tagName = tag || 'div';
    this.value = '';
    this.checked = false;
    this.textContent = '';
    this.className = '';
    this.children = [];
    this.dataset = {};
    this.style = {};
  }
  appendChild(node){ if(node) node.parentElement = this; this.children.push(node); return node; }
  addEventListener(){}
  closest(){ return null; }
  querySelector(){ return null; }
  remove(){}
}

// Native <select> selectedness: assigning a value no option carries leaves the
// control on its first option (the labelled Default), and innerHTML rebuilds
// reset selectedness to the first option.
class Select extends FakeEl {
  constructor(){ super('select'); this._selected = null; }
  get options(){
    const out = [];
    (this.children || []).forEach((child) => {
      if(!child) return;
      if(child.tagName === 'option') out.push(child);
      else if(child.tagName === 'optgroup') (child.children || []).forEach((o) => {
        if(o && o.tagName === 'option') out.push(o);
      });
    });
    return out;
  }
  get selectedIndex(){ return this._selected ? this.options.indexOf(this._selected) : -1; }
  get value(){ return this._selected ? this._selected.value : ''; }
  set value(v){ this._selected = this.options.find((o) => o.value === String(v)) || null; }
  get innerHTML(){ return ''; }
  set innerHTML(html){
    this.children = [];
    this._selected = null;
    const re = /<option value="([^"]*)"[^>]*>([^<]*)<\/option>/g;
    let m;
    while((m = re.exec(html))){
      const opt = new FakeEl('option');
      opt.value = m[1]; opt.textContent = m[2];
      this.children.push(opt);
    }
    this._selected = this.options[0] || null;
  }
  appendChild(node){
    if(node) node.parentElement = this;
    this.children.push(node);
    if(!this._selected) this._selected = this.options[0] || null;
    return node;
  }
}

const document = { createElement(tag){ return new FakeEl(tag); } };
const controls = {};
function $(id){ return controls[id] || null; }
const t = (key) => key;
function userPicksVoice(value){
  ttsVoiceSel.value = value;
  (function(){
__VOICE_HANDLER_BODY__
  }).call(ttsVoiceSel);
}

const ALL_VOICES = [
  { name: 'First Voice', lang: 'en-US' },
  { name: 'Second Voice', lang: 'en-US' },
  { name: 'Third Voice', lang: 'en-US' },
];
let _voices = ALL_VOICES.slice();
const window = {};
window.speechSynthesis = { getVoices(){ return _voices; }, addEventListener(){} };
const speechSynthesis = window.speechSynthesis;

__HELPERS__

__PAYLOAD__

// The real autosave scheduler is part of the extracted payload block; replace it
// with a counter so this test never arms a network write.
let autosaveCalls = 0;
_schedulePreferencesAutosave = function(){ autosaveCalls += 1; };

const settings = {
  tts_engine: 'browser',
  tts_voice: 'Third Voice',
  persisted_speech_keys: ['tts_engine', 'tts_voice'],
};
const ttsVoiceSel = new Select();
controls.settingsTtsVoice = ttsVoiceSel;
store.set('hermes-tts-engine', 'browser');

{
  __SPEECH_SETTING__

  const ttsEngineSel = new Select();
  controls.settingsTtsEngine = ttsEngineSel;

  __ENGINE_HYDRATION__

  __POPULATE__

  // The engine <select> onchange path: the chosen option exists, the mirror is
  // written and the voices are rebuilt for that engine.
  const selectEngine = (id) => {
    if(!ttsEngineSel.options.some((o) => o.value === id)){
      const opt = new FakeEl('option');
      opt.value = id;
      ttsEngineSel.appendChild(opt);
    }
    ttsEngineSel.value = id;
    store.set('hermes-tts-engine', id);
  };

  const labels = () => ttsVoiceSel.options.map((o) => String(o.textContent || ''));
  const unavailable = () => labels().filter((label) => label.indexOf('(unavailable)') !== -1);
  const payloadFor = () => __PAYLOAD_FN__;

  // 1. The catalog still offers the accepted voice: it is selected normally and
  // no temporary option is invented.
  window._populateTtsVoices();
  assert.strictEqual(ttsVoiceSel.value, 'Third Voice', 'offered voice must be selected');
  assert.ok(ttsVoiceSel.selectedIndex >= 0, 'select must have a selected option');
  assert.deepStrictEqual(unavailable(), [], 'no unavailable option while the voice is offered');

  // 2. A catalog rebuild drops the accepted voice. The select must keep it as a
  // selected "(unavailable)" option and an unrelated save must still carry it.
  _voices = ALL_VOICES.filter((v) => v.name !== 'Third Voice');
  window._populateTtsVoices();
  assert.strictEqual(ttsVoiceSel.value, 'Third Voice', 'accepted voice must stay selected after rebuild');
  assert.ok(ttsVoiceSel.selectedIndex >= 0, 'select must not collapse to no selection');
  assert.deepStrictEqual(unavailable(), ['Third Voice (unavailable)'], 'missing voice must be shown once');
  assert.strictEqual(
    ttsVoiceSel.options[ttsVoiceSel.selectedIndex].value,
    'Third Voice',
    'selected option must carry the accepted value'
  );
  const lostPayload = payloadFor();
  assert.strictEqual(lostPayload.tts_voice, 'Third Voice', 'unrelated save must keep the accepted voice');
  assert.notStrictEqual(lostPayload.tts_voice, '', 'unrelated save must not write the Default voice');
  assert.strictEqual(store.get('hermes-tts-voice'), 'Third Voice', 'runtime cache keeps the accepted voice');

  // 3. The voice returns to the catalog: the real option is selected again, the
  // temporary one disappears and the value is unchanged.
  _voices = ALL_VOICES.slice();
  window._populateTtsVoices();
  assert.strictEqual(ttsVoiceSel.value, 'Third Voice', 'recovered voice must be selected again');
  assert.deepStrictEqual(unavailable(), [], 'unavailable option must disappear');
  assert.strictEqual(
    ttsVoiceSel.options.filter((o) => o.value === 'Third Voice').length,
    1,
    'exactly one option may carry the voice'
  );
  assert.strictEqual(payloadFor().tts_voice, 'Third Voice', 'recovered voice is still the payload value');

  // 4. A panel that could not populate the control (no speechSynthesis, so the
  // select only has its static Default option) must not report Default either:
  // the accepted voice still belongs to the selected engine.
  const bareSel = new Select();
  bareSel.innerHTML = '<option value="">Default (system)</option>';
  controls.settingsTtsVoice = bareSel;
  assert.strictEqual(bareSel.value, '', 'unpopulated select sits on its Default option');
  assert.strictEqual(payloadFor().tts_voice, 'Third Voice', 'unpopulated select must not wipe the accepted voice');
  controls.settingsTtsVoice = ttsVoiceSel;

  // 5. Engine switch: a browser voice is not carried into Edge and the new
  // engine starts from its own default, which is what gets saved.
  selectEngine('edge');
  window._populateTtsVoices();
  assert.strictEqual(ttsVoiceSel.value, '', 'edge select cannot represent the browser voice');
  assert.deepStrictEqual(unavailable(), [], 'engine switch must not fake a browser voice in edge');
  const edgePayload = payloadFor();
  assert.strictEqual(edgePayload.tts_engine, 'edge');
  assert.strictEqual(edgePayload.tts_voice, '', 'the edge default voice is what gets saved');

  // 6. Switching back recovers the accepted voice without a spurious change.
  selectEngine('browser');
  window._populateTtsVoices();
  assert.strictEqual(ttsVoiceSel.value, 'Third Voice', 'browser voice must come back after engine switch');
  assert.strictEqual(payloadFor().tts_voice, 'Third Voice', 'intent survived the engine switch');

  // 7. An intentional edit to Default must still persist Default and must not
  // resurrect the previous voice on the next rebuild.
  userPicksVoice('');
  assert.strictEqual(payloadFor().tts_voice, '', 'explicit Default must be persisted as Default');
  window._populateTtsVoices();
  assert.strictEqual(ttsVoiceSel.value, '', 'Default stays selected after a rebuild');
  assert.deepStrictEqual(unavailable(), [], 'explicit Default clears the unavailable option');
  assert.strictEqual(payloadFor().tts_voice, '', 'explicit Default must not be resurrected');

  // 8. Refused storage mirror: the payload omits the key so the durable server
  // value survives, and the select still shows the accepted voice.
  store.set('hermes-tts-voice', 'Third Voice');
  REFUSE = true;
  __REHYDRATE__
  window._populateTtsVoices();
  assert.strictEqual(ttsVoiceSel.value, 'Third Voice', 'refused mirror still shows the accepted voice');
  assert.ok(
    !Object.prototype.hasOwnProperty.call(payloadFor(), 'tts_voice'),
    'refused mirror must omit tts_voice instead of writing the stale cache'
  );
}

process.stdout.write('ok');
"""


def _run_script():
    blocks = _blocks()
    assert "_markSpeechPreferenceChanged('tts_voice')" in blocks["voice_handler"]
    assert "_writeSettingsStorageMirror('hermes-tts-voice',this.value)" in blocks["voice_handler"]
    script = (
        _HARNESS.replace("__HELPERS__", blocks["helpers"])
        .replace("__PAYLOAD__", blocks["payload"])
        .replace("__SPEECH_SETTING__", blocks["speech_setting"])
        .replace("__POPULATE__", blocks["populate"])
        .replace("__VOICE_HANDLER_BODY__", blocks["voice_handler"])
        # `payloadFor` calls the real preferences payload builder, i.e. the same
        # path an unrelated control's autosave uses.
        .replace("__PAYLOAD_FN__", "_preferencesPayloadFromUi()")
        # A settings load re-captures ownership and re-hydrates the engine before
        # repopulating the voices.
        .replace(
            "__REHYDRATE__",
            "_captureSpeechPreferenceOwnership(settings);\n  __ENGINE_HYDRATION__",
        )
        .replace(
            "__ENGINE_HYDRATION__",
            "(function(){\n%s\n})();" % blocks["engine_hydration"],
        )
    )
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            "node scenario failed\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (result.stdout, result.stderr)
        )
    assert result.stdout.strip() == "ok", result.stdout


def test_unavailable_voice_survives_unrelated_autosave():
    _run_script()
