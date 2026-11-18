"""Regression checks for #8708 — a refused localStorage write during Settings
hydration must not leave the rest of the speech controls unwired.

The bug: `_syncSpeechPreferenceCache()` mirrored the server value into
localStorage with an unguarded `setItem`.  When the browser refuses that write
(quota exceeded, storage disabled in some private-mode browsers) the throw
escaped the speech block of `loadSettingsPanel()`, so every control hydrated
after it — the TTS checkboxes, the voice-mode button toggle (#1488), the engine
selector, the voice selector, the rate/pitch sliders — kept its default value and
had no `onchange` handler.  The whole function body sits in one try/catch, so the
user also got a `settings_load_failed` toast.

These tests execute the *real* source of the speech helper block and of the
contiguous hydration region of `panels.js` (same block-extraction harness that
`test_issue5435_tts_voice_preferences.py` uses) in node, against a `Storage`
whose `setItem` throws `DOMException('QuotaExceededError')` — the refusal model
the issue describes.  Not measured in a real browser with a full quota, and
Safari/iOS is not covered.

Note: `window._mirrorSpeechSettingsFromServer` (boot.js) is intentionally left
undefined here so the test measures the panels.js hydration path only.
"""

import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _slice(start_marker, end_marker, start_from=0):
    start = PANELS_JS.index(start_marker, start_from)
    end = PANELS_JS.index(end_marker, start)
    assert start < end, f"empty slice {start_marker!r} -> {end_marker!r}"
    return PANELS_JS[start:end]


# Slices are resolved lazily inside each test so a moved/renamed marker surfaces
# as one failing test instead of breaking collection for the whole file.
def _speech_helpers():
    """Speech cache helpers: storage-key map, ownership tracking, the mirror
    write guard added in #8708 and `_syncSpeechPreferenceCache` itself."""
    return _slice(
        "const _SETTINGS_SPEECH_STORAGE_KEYS=",
        "function _setOwnedSpeechPayload",
    )


def _speech_hydration():
    """The contiguous hydration region that used to abort: from the server mirror
    call through the rate/pitch sliders, continuing a little past it
    (notifications and sidebar density) to prove the rest of the panel still
    hydrates."""
    return _slice(
        "if(typeof window._mirrorSpeechSettingsFromServer==='function') window._mirrorSpeechSettingsFromServer(settings);",
        "const autoTitleRefreshSel=$('settingsAutoTitleRefresh');",
    )


def _font_size_hydration():
    """The first mirror write in `loadSettingsPanel()` — font size runs before the
    speech block, so a refusal there killed the entire panel on its own."""
    return _slice(
        "const fontSizeVal=settings.font_size",
        "const jumpButtonsCb=$('settingsSessionJumpButtons');",
    )


def _storage_guard():
    """Just the write guard added in #8708."""
    return _slice(
        "function _writeSettingsStorageMirror(",
        "function _syncSpeechPreferenceCache(",
    )


def _boot_voice_mode_consumer():
    """The real boot consumer from boot.js that reads localStorage directly."""
    start = BOOT_JS.index("function _voiceModePrefEnabled(){")
    end = BOOT_JS.index("let _voiceModeState='idle';", start)
    assert start < end
    return BOOT_JS[start:end]


def _boot_raw_audio_consumer():
    """The real boot consumer from boot.js that applies raw audio mode in memory."""
    start = BOOT_JS.index("function _applyRawAudioModePreference(enabled){")
    assignment = "window._applyRawAudioModePreference=_applyRawAudioModePreference;"
    end = BOOT_JS.index(assignment, start) + len(assignment)
    assert start < end
    return BOOT_JS[start:end]


def _boot_speech_mirror():
    """The real boot mirror that applies server speech values before hydration."""
    start = BOOT_JS.index("function _mirrorSpeechSettingsFromServer(s){")
    assignment = (
        "window._mirrorSpeechSettingsFromServer=_mirrorSpeechSettingsFromServer;"
    )
    end = BOOT_JS.index(assignment, start) + len(assignment)
    assert start < end
    return BOOT_JS[start:end]


def _ui_quota_chip_window_helper():
    """The real ui.js helper `_preferencesPayloadFromUi` calls for the quota-chip
    window setting (#8548). The page loads ui.js before panels.js, so the
    harness composes it the same way instead of stubbing it."""
    start = UI_JS.index("function _normalizeQuotaChipWindow(")
    end = UI_JS.index("function _quotaWindowSeconds(", start)
    assert start < end
    return UI_JS[start:end]


def _payload_helpers():
    """Preferences payload builders plus the real autosave POST path.

    The harness composes production functions rather than stubbing them, so the
    autosave regression exercises the same `_schedulePreferencesAutosave`,
    `_rememberPreferencesSaved` and `_autosavePreferencesSettings` the panel uses.
    """
    return _ui_quota_chip_window_helper() + _slice(
        "function _setOwnedSpeechPayload(",
        "function _retryPreferencesAutosave(",
    )


_HARNESS = r"""
const assert = require('assert');

// ── Browser surface ───────────────────────────────────────────────────────
// Models the release-gate stub from #8708: Storage#setItem throws a
// DOMException('QuotaExceededError') when the browser refuses the write.
// Reads keep working, as they do with a full quota.
const REFUSE = __REFUSE__;
const store = new Map();
class Storage {
  getItem(key){ return store.has(key) ? store.get(key) : null; }
  setItem(key, value){
    if(REFUSE) throw new DOMException('The quota has been exceeded.', 'QuotaExceededError');
    store.set(key, String(value));
  }
  removeItem(key){ store.delete(key); }
  clear(){ store.clear(); }
}
const localStorage = new Storage();

const warnings = [];
const errors = [];
console.warn = (...args) => warnings.push(args.map(String).join(' '));
console.error = (...args) => errors.push(args.map(String).join(' '));

class FakeEl {
  constructor(tag){
    this.tagName = tag || 'div';
    this.id = '';
    this.value = '';
    this.checked = false;
    this.textContent = '';
    this.innerHTML = '';
    this.children = [];
    this.listeners = [];
    this.hidden = false;
    this.disabled = false;
    this.style = {};
    this.dataset = {};
    this.classList = { add(){}, remove(){}, toggle(){}, contains(){ return false; } };
    this.onchange = null;
    this.oninput = null;
  }
  querySelector(){ return null; }
  querySelectorAll(){ return []; }
  appendChild(node){ this.children.push(node); return node; }
  addEventListener(type, fn, opts){ this.listeners.push({ type: type, fn: fn }); }
  closest(){ return null; }
  focus(){}
  setAttribute(){}
  remove(){}
}
const els = {};
function $(id){
  if(!(id in els)) els[id] = new FakeEl('div');
  els[id].id = id;
  return els[id];
}
const document = {
  createElement(tag){ return new FakeEl(tag); },
  getElementById(id){ return els[id] || null; },
  documentElement: { classList: { toggle(){} } },
};
const window = {
  speechSynthesis: {
    getVoices(){ return [{ name: 'Test Voice', lang: 'en-US' }]; },
    addEventListener(){},
  },
};
const speechSynthesis = window.speechSynthesis;
const t = (key) => key;

const applyTtsCalls = [];
const autosaveCalls = { n: 0 };
const toasts = [];
const fontSizeApps = [];
function _applyTtsEnabled(value){ applyTtsCalls.push(value); }
function _schedulePreferencesAutosave(){ autosaveCalls.n += 1; }
function showToast(message){ toasts.push(String(message)); }
function _applyFontSize(value){ fontSizeApps.push(value); }
function _syncFontSizePicker(){}

/*__HELPERS__*/

function __hydrate(settings){
/*__HYDRATION__*/
}

function __hydrateHead(settings){
/*__HEAD__*/
}

const payload = {
  tts_enabled: true,
  tts_auto_read: true,
  tts_engine: 'edge',
  tts_voice: 'en-US-AriaNeural',
  tts_rate: 1.4,
  tts_pitch: 0.8,
  voice_mode_button: true,
  voice_continuous: true,
  voice_silence_ms: 2400,
  raw_audio_mode: true,
  notifications_enabled: true,
  sidebar_density: 'detailed',
  font_size: 'large',
  persisted_speech_keys: [
    'tts_enabled', 'tts_auto_read', 'tts_engine', 'tts_voice', 'tts_rate',
    'tts_pitch', 'voice_mode_button', 'voice_continuous', 'voice_silence_ms',
    'raw_audio_mode',
  ],
};

__MODE_ASSERTS__
"""


_REFUSE_ASSERTS = r"""
// (a) A refused write must leave hydration finished and every later control
// both present and wired, showing the effective runtime value. With no cache
// entry, the boot consumers read their fallback, so that is what the control
// must show (#8749). Raw audio is applied in memory by boot.js, so it keeps the
// server value.
const hasNote = (el) => {
  if(!el) return false;
  if(el.title && el.title.includes('settings_unapplied_note')) return true;
  if(el.dataset && el.dataset.unapplied === 'true') return true;
  if(el.unappliedNote && (el.unappliedNote.textContent || '').includes('settings_unapplied_note')) return true;
  if(el.children && el.children.some(c => c && c.className === 'settings-unapplied-note')) return true;
  return false;
};
let threw = null;
try {
  __hydrateHead(payload);   // font size runs first in loadSettingsPanel()
  __hydrate(payload);
} catch (e) {
  // loadSettingsPanel() wraps its whole body in this catch -> user-visible toast.
  threw = e;
  showToast('settings_load_failed' + e.message);
}
assert.deepStrictEqual(errors, []);

// Wiring is asserted before the throw so the baseline red reports the symptom the
// issue describes (controls left unwired) rather than only the escaped exception.
const threwAt = threw
  ? ' (hydration threw at ' +
    (String(threw.stack).split('\n').find((line) => /at (_\w+|__hydrate)/.test(line)) || threw.message).trim().replace(/^at /, '') +
    ')'
  : '';
const wired = [
  ['settingsTtsEnabled', 'onchange', 'checked', false],
  ['settingsTtsAutoRead', 'onchange', 'checked', false],
  ['settingsVoiceModeEnabled', 'onchange', 'checked', false],
  ['settingsRawAudio', 'onchange', 'checked', true],
  ['settingsTtsRate', 'oninput', 'value', '1'],
  ['settingsTtsPitch', 'oninput', 'value', '1'],
];
wired.forEach(([id, handler, prop, want]) => {
  const el = els[id];
  assert.ok(el, id + ' was never hydrated' + threwAt);
  assert.strictEqual(typeof el[handler], 'function', id + ' has no ' + handler + ' handler');
  assert.strictEqual(el[prop], want, id + '.' + prop + ' must show the effective runtime value');
});
assert.strictEqual(els.settingsTtsEngine.value, 'browser', 'engine select must show the effective runtime value');
assert.strictEqual(typeof els.settingsTtsEngine.onchange, 'function', 'engine select has no onchange');
assert.strictEqual(typeof els.settingsTtsVoice.onchange, 'function', 'voice select has no onchange');
assert.strictEqual(els.settingsFontSize.value, 'large', 'font size select must still hydrate');
assert.deepStrictEqual(fontSizeApps, ['large'], '_applyFontSize must still run');
// The divergent speech controls carry the unapplied note; raw audio does not.
['settingsTtsEnabled', 'settingsTtsAutoRead', 'settingsVoiceModeEnabled',
 'settingsTtsEngine', 'settingsTtsRate', 'settingsTtsPitch'].forEach((id) => {
  assert.ok(hasNote(els[id]), id + ' must show the unapplied note');
});
assert.strictEqual(hasNote(els.settingsRawAudio), false,
  'raw audio is applied in memory, so it must not show the unapplied note');

// (c) the rest of the panel is not blocked by the refusal.
assert.strictEqual(els.settingsNotificationsEnabled.checked, true, 'hydration stopped before the notification toggle');
assert.strictEqual(els.settingsNotificationsEnabled.listeners.length, 1, 'notification toggle is unwired');
assert.strictEqual(els.settingsSidebarDensity.value, 'detailed', 'hydration stopped before sidebar density');
assert.strictEqual(els.settingsSidebarDensity.listeners.length, 1, 'sidebar density is unwired');

// No user-visible failure: the escaped exception is what used to raise the
// "settings load failed" toast in loadSettingsPanel().
assert.strictEqual(threw, null, 'settings hydration must not throw: ' + (threw && threw.stack));
assert.deepStrictEqual(toasts, [], 'a refused cache write must not surface a user-visible error');

assert.strictEqual(store.size, 0, 'a refused write must not leave a partial cache behind');
assert.strictEqual(warnings.length, 1, 'the refusal must be reported exactly once: ' + JSON.stringify(warnings));
assert.ok(/localStorage write refused/.test(warnings[0]), 'warning must name the refused mirror write: ' + warnings[0]);

// Changing a control under a refusing storage still applies the value and still
// schedules the server save, and the diagnostics stay deduplicated.
els.settingsTtsEnabled.checked = false;
els.settingsTtsEnabled.onchange.call(els.settingsTtsEnabled);
assert.deepStrictEqual(applyTtsCalls, [false], 'TTS toggle must still apply immediately');
els.settingsTtsRate.oninput.call(els.settingsTtsRate);
assert.strictEqual(autosaveCalls.n, 2, 'controls must still schedule their server save');
assert.strictEqual(warnings.length, 1, 'no per-keystroke catch noise: ' + JSON.stringify(warnings));
assert.deepStrictEqual(errors, []);

// Unchanged contract: only owned keys are mirrored, so an unowned key never even
// attempts a write (and therefore never warns).
const before = store.size;
_captureSpeechPreferenceOwnership({ persisted_speech_keys: [] });
_syncSpeechPreferenceCache('tts_engine', 'browser');
assert.strictEqual(store.size, before, 'unowned keys must not be mirrored');
assert.strictEqual(warnings.length, 1, 'unowned keys must not warn');
"""


_NORMAL_ASSERTS = r"""
// (b) With a working storage the behaviour is byte-for-byte what it was:
// hydration wires everything and the cache mirrors the server values.
let threw = null;
try {
  __hydrateHead(payload);
  __hydrate(payload);
} catch (e) {
  threw = e;
  showToast('settings_load_failed' + e.message);
}
assert.strictEqual(threw, null, 'settings hydration must not throw: ' + (threw && threw.stack));
assert.deepStrictEqual(toasts, []);
assert.deepStrictEqual(errors, []);
assert.strictEqual(warnings.length, 0, 'a working storage must not warn: ' + JSON.stringify(warnings));

const wired = [
  ['settingsTtsEnabled', 'onchange', 'checked', true],
  ['settingsTtsAutoRead', 'onchange', 'checked', true],
  ['settingsVoiceModeEnabled', 'onchange', 'checked', true],
  ['settingsRawAudio', 'onchange', 'checked', true],
  ['settingsTtsRate', 'oninput', 'value', '1.4'],
  ['settingsTtsPitch', 'oninput', 'value', '0.8'],
];
wired.forEach(([id, handler, prop, want]) => {
  const el = els[id];
  assert.ok(el, id + ' was never hydrated');
  assert.strictEqual(typeof el[handler], 'function', id + ' has no ' + handler + ' handler');
  assert.strictEqual(el[prop], want, id + '.' + prop + ' must show the server value');
});
assert.strictEqual(els.settingsTtsEngine.value, 'edge');
assert.strictEqual(typeof els.settingsTtsEngine.onchange, 'function');
assert.strictEqual(typeof els.settingsTtsVoice.onchange, 'function');
assert.strictEqual(els.settingsFontSize.value, 'large');

const mirrored = {
  'hermes-voice-continuous': 'true',
  'hermes-voice-silence-ms': '2400',
  'hermes-tts-engine': 'edge',
  'hermes-tts-voice': 'en-US-AriaNeural',
  'hermes-tts-rate': '1.4',
  'hermes-tts-pitch': '0.8',
  'hermes-font-size': 'large',
};
Object.keys(mirrored).forEach((key) => {
  assert.strictEqual(store.get(key), mirrored[key], 'cache mirror missing ' + key);
});

// Unowned keys are still never mirrored.
const before = store.size;
_captureSpeechPreferenceOwnership({ persisted_speech_keys: [] });
_syncSpeechPreferenceCache('tts_engine', 'browser');
assert.strictEqual(store.size, before, 'unowned keys must not be mirrored');
assert.strictEqual(warnings.length, 0);
"""


def _run_harness(refuse):
    script = (
        _HARNESS.replace("__REFUSE__", "true" if refuse else "false")
        .replace("/*__HELPERS__*/", _speech_helpers())
        .replace("/*__HYDRATION__*/", _speech_hydration())
        .replace("/*__HEAD__*/", _font_size_hydration())
        .replace("__MODE_ASSERTS__", _REFUSE_ASSERTS if refuse else _NORMAL_ASSERTS)
    )
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            "node harness failed (refuse=%s)\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (refuse, result.stdout, result.stderr)
        )
    return result


def test_refused_storage_write_during_hydration_keeps_speech_controls_wired():
    """(a)+(c): refusal is non-fatal, controls wired, effective value shown."""
    _run_harness(refuse=True)


def test_working_storage_hydration_is_unchanged():
    """(b): no regression on the normal (writable storage) path."""
    _run_harness(refuse=False)


def test_hydration_path_has_no_unguarded_localstorage_write():
    """The speech hydration region must not call setItem directly.

    Guards the exact regression: a new mirror write added inline would throw
    again before the next control is wired.
    """
    hydration = _speech_hydration()
    for line in hydration.splitlines():
        assert "localStorage.setItem" not in line, (
            "hydration must go through _writeSettingsStorageMirror: " + line.strip()
        )
    assert "_writeSettingsStorageMirror('hermes-font-size'" in _font_size_hydration(), (
        "the font-size mirror write in loadSettingsPanel must be guarded too"
    )
    # The six #5435 cache sync points are untouched.
    for setting_key in [
        "voice_continuous",
        "voice_silence_ms",
        "tts_engine",
        "tts_voice",
        "tts_rate",
        "tts_pitch",
    ]:
        assert (
            f"_syncSpeechPreferenceCache('{setting_key}'," in hydration
        ), f"missing cache sync for {setting_key}"


def test_refusal_guard_warns_once_and_is_not_a_silent_catch():
    """The guard catches, reports once through console.warn, and never rethrows."""
    guard = _storage_guard()
    assert "try{" in guard
    assert "catch(e){" in guard
    assert "catch(e){}" not in guard, "the catch must not be empty"
    assert "console.warn(" in guard, "the refusal must stay observable"
    assert "_settingsStorageWriteRefusedWarned" in guard, "the warning must be deduplicated"
    assert "throw" not in guard, "the guard must not rethrow"
    assert "localStorage.setItem(storageKey,String(value))" in guard

    # `_syncSpeechPreferenceCache` routes every mirror write through the guard and
    # still ignores keys the client does not own.
    sync = _slice(
        "function _syncSpeechPreferenceCache(",
        "function _setOwnedSpeechPayload",
    )
    assert "if(!_speechPreferenceIsOwned(settingKey)) return;" in sync
    assert "_writeSettingsStorageMirror(storageKey,value)" in sync
    assert "localStorage.setItem" not in sync


def test_speech_handler_writes_go_through_the_guard():
    """Post-hydration onchange mirrors are guarded too, so a refused write cannot
    skip the immediate apply / autosave that follows it."""
    for storage_key in [
        "hermes-tts-enabled",
        "hermes-tts-auto-read",
        "hermes-voice-mode-button",
        "hermes-tts-engine",
        "hermes-tts-voice",
        "hermes-tts-rate",
        "hermes-tts-pitch",
        "hermes-raw-audio-mode",
    ]:
        assert (
            f"_writeSettingsStorageMirror('{storage_key}'" in PANELS_JS
        ), f"{storage_key} must be written through _writeSettingsStorageMirror"
        assert (
            f"localStorage.setItem('{storage_key}'" not in PANELS_JS
        ), f"{storage_key} still has an unguarded localStorage.setItem"


_REGRESSION_HARNESS = r"""
const assert = require('assert');

let REFUSE = true;
const store = new Map();
class Storage {
  getItem(key){ return store.has(key) ? store.get(key) : null; }
  setItem(key, value){
    if(REFUSE) throw new DOMException('The quota has been exceeded.', 'QuotaExceededError');
    store.set(key, String(value));
  }
  removeItem(key){ store.delete(key); }
  clear(){ store.clear(); }
}
const localStorage = new Storage();

const warnings = [];
const errors = [];
console.warn = (...args) => warnings.push(args.map(String).join(' '));
console.error = (...args) => errors.push(args.map(String).join(' '));

class FakeEl {
  constructor(tag){
    this.tagName = tag || 'div';
    this.id = '';
    this.value = '';
    this.checked = false;
    this.textContent = '';
    this._innerHTML = '';
    this.children = [];
    this.listeners = [];
    this.hidden = false;
    this.disabled = false;
    this.style = {};
    this.dataset = {};
    this.classList = { add(){}, remove(){}, toggle(){}, contains(){ return false; } };
    this.onchange = null;
    this.oninput = null;
    this.title = '';
    this.parentElement = null;
  }
  set innerHTML(val){
    this._innerHTML = val;
    this.children = [];
  }
  get innerHTML(){ return this._innerHTML || ''; }
  querySelector(sel){
    if(sel === '.settings-unapplied-note'){
      return this.children.find(c => c && c.className === 'settings-unapplied-note') || null;
    }
    return null;
  }
  querySelectorAll(){ return []; }
  appendChild(node){
    if(node) node.parentElement = this;
    this.children.push(node);
    if(node && node.selected) this.value = node.value;
    return node;
  }
  addEventListener(type, fn, opts){ this.listeners.push({ type: type, fn: fn }); }
  closest(sel){
    if(sel === '.settings-field') return this.parentElement || this;
    return null;
  }
  focus(){}
  setAttribute(k, v){ this[k] = v; }
  remove(){
    if(this.parentElement && Array.isArray(this.parentElement.children)){
      const idx = this.parentElement.children.indexOf(this);
      if(idx !== -1) this.parentElement.children.splice(idx, 1);
    }
  }
}
Object.defineProperty(FakeEl.prototype, 'selected', {
  set(val){
    this._selected = val;
    if(val && this.parentElement && this.parentElement.tagName === 'select'){
      this.parentElement.value = this.value;
    }
  },
  get(){ return this._selected; },
  configurable: true,
});
const els = {};
function $(id){
  if(!(id in els)) els[id] = new FakeEl('div');
  els[id].id = id;
  return els[id];
}
const document = {
  createElement(tag){ return new FakeEl(tag); },
  getElementById(id){ return els[id] || null; },
  querySelector(){ return null; },
  documentElement: { classList: { toggle(){} }, dataset: {} },
};
const voiceschangedListeners = [];
const window = {
  speechSynthesis: {
    getVoices(){ return window._voices || [{ name: 'Test Voice', lang: 'en-US' }]; },
    addEventListener(type, fn){ if(type === 'voiceschanged') voiceschangedListeners.push(fn); },
  },
};
const speechSynthesis = window.speechSynthesis;
const t = (key) => key;
const esc = (value) => String(value);

const applyTtsCalls = [];
const toasts = [];
const fontSizeApps = [];
function _applyTtsEnabled(value){ applyTtsCalls.push(value); }
function showToast(message){ toasts.push(String(message)); }
function _applyFontSize(value){ fontSizeApps.push(value); }
function _syncFontSizePicker(){}

// Real preferences autosave surface. Capture the debounce timer so the test can
// flush it deterministically, and record the POST body api() receives.
const posts = [];
const pendingTimers = [];
global.setTimeout = (fn) => { pendingTimers.push(fn); return pendingTimers.length; };
global.clearTimeout = () => {};
let _settingsPreferencesAutosaveTimer = null;
let _settingsPreferencesAutosaveRetryPayload = null;
let _settingsDirty = false;
let _settingsHermesDefaultModelOnOpen = '';
let _settingsHermesDefaultModelProviderOnOpen = null;
function api(path, options){ posts.push(options); return Promise.resolve(JSON.parse(options.body)); }
function setScreenReaderMode(){}
function applyEmptyStateSuggestionPref(){}
function applyEmptyStatePanelPref(){}
function applyConversationOutlinePreference(){}
function _persistDefaultMessageMode(value){ return value || 'steer'; }
function _applyBusyComposerPlaceholder(){}
function clearMessageRenderCache(){}
function renderMessages(){}

// State used by the real boot raw-audio consumer injected below.
let _rawAudioMode = localStorage.getItem('hermes-raw-audio-mode') === 'true';
function _updateMicTooltip(){}

// Setup fake DOM for real boot consumer
const hasSTT = true;
const hasTTS = true;
const modeBtn = $('btnVoiceMode');
const bar = $('voiceModeBar');
const indicator = $('voiceModeIndicator');
const label = $('voiceModeLabel');
const micBtn = $('btnMic');
const ta = $('msg');
function _deactivate(){}

/*__HELPERS__*/
/*__PAYLOAD_HELPERS__*/

// Inject the REAL boot consumers from boot.js (not stubs)
/*__BOOT_RAW_AUDIO__*/
/*__BOOT_SPEECH_MIRROR__*/
/*__BOOT_VOICE_MODE__*/

function __hydrate(settings){
/*__HYDRATION__*/
}

function __hydrateHead(settings){
/*__HEAD__*/
}

// 1. Conflicting obsolete cache in store
store.set('hermes-voice-mode-button', 'false');
store.set('hermes-raw-audio-mode', 'false');
store.set('hermes-tts-enabled', 'false');
store.set('hermes-tts-auto-read', 'false');
store.set('hermes-tts-engine', 'browser');
store.set('hermes-tts-rate', '1');
store.set('hermes-tts-pitch', '1');

// Real boot consumer runs at boot:
_applyVoiceModePref();
assert.strictEqual(modeBtn.style.display, 'none', 'boot consumer must hide voice mode button when cached as false');
assert.strictEqual(_voiceModePrefEnabled(), false, 'boot consumer must evaluate to false');

// Server payload with conflicting authoritative values
const payload = {
  tts_enabled: true,
  tts_auto_read: true,
  tts_engine: 'edge',
  tts_voice: 'en-US-AriaNeural',
  tts_rate: 1.4,
  tts_pitch: 0.8,
  voice_mode_button: true,
  voice_continuous: true,
  voice_silence_ms: 2400,
  raw_audio_mode: true,
  notifications_enabled: true,
  sidebar_density: 'detailed',
  font_size: 'large',
  persisted_speech_keys: [
    'tts_enabled', 'tts_auto_read', 'tts_engine', 'tts_voice', 'tts_rate',
    'tts_pitch', 'voice_mode_button', 'voice_continuous', 'voice_silence_ms',
    'raw_audio_mode',
  ],
};

// Real boot sequence: the server mirror applies raw audio in memory, then the
// voice-mode consumer reads the refused cache.
_mirrorSpeechSettingsFromServer(payload);
_applyVoiceModePref();
assert.strictEqual(modeBtn.style.display, 'none', 'voice mode still reads the refused cache after the mirror');

let threw = null;
try {
  __hydrateHead(payload);
  __hydrate(payload);
} catch(e) {
  threw = e;
}
assert.strictEqual(threw, null, 'hydration must not throw: ' + (threw && threw.stack));

// 1. Coherence: checkbox reflects runtime value (false), agreeing with _voiceModePrefEnabled() and modeBtn
assert.strictEqual(els.settingsVoiceModeEnabled.checked, false, 'checkbox must reflect effective runtime value (false), not unapplied server value');
assert.strictEqual(_voiceModePrefEnabled(), false, 'boot consumer must agree with effective runtime value');
assert.strictEqual(modeBtn.style.display, 'none', 'button display must agree with runtime value');

// 2. Note communicating unapplied server value must be present
const hasNote = (el) => {
  if(!el) return false;
  if(el.title && el.title.includes('settings_unapplied_note')) return true;
  if(el.dataset && el.dataset.unapplied === 'true') return true;
  if(el.unappliedNote && (el.unappliedNote.textContent || '').includes('settings_unapplied_note')) return true;
  if(el.children && el.children.some(c => c && c.className === 'settings-unapplied-note')) return true;
  return false;
};
assert.ok(hasNote(els.settingsVoiceModeEnabled), 'unapplied note ("settings_unapplied_note") must be present on voiceModeCb');

// 3. Exactly 1 warning logged for refused mirror write
assert.strictEqual(warnings.length, 1, 'refusal must be reported exactly once: ' + JSON.stringify(warnings));
assert.ok(/localStorage write refused/.test(warnings[0]), 'warning must name refused write: ' + warnings[0]);

// 3b. The refusal is observable to the caller, not swallowed: hydration uses
// this return value to choose between the server value and the runtime cache.
// #8564's mic seed applies the same pattern to `_syncSpeechPreferenceCache`.
assert.strictEqual(_writeSettingsStorageMirror('hermes-probe-refused', 'x'), false, 'refused mirror write must report failure');
assert.strictEqual(_syncSpeechPreferenceCache('tts_engine', 'browser'), false, 'refused cache sync must report failure');

// 4. Other speech consumers. Raw audio is applied directly from the server by
// boot.js (_applyRawAudioModePreference), so its runtime is the server value
// even though the cache write was refused; no "unapplied" note is needed.
assert.strictEqual(_rawAudioMode, true, 'raw audio boot consumer must apply the server value in memory');
assert.strictEqual(els.settingsRawAudio.checked, true, 'raw_audio_mode must agree with its runtime (server value)');
assert.strictEqual(store.get('hermes-raw-audio-mode'), 'false', 'raw audio cache write must still be refused');
assert.strictEqual(hasNote(els.settingsRawAudio), false, 'raw_audio_mode is applied, so it must not show the unapplied note');

assert.strictEqual(els.settingsTtsEnabled.checked, false, 'tts_enabled must reflect effective runtime value');
assert.ok(hasNote(els.settingsTtsEnabled), 'tts_enabled must show unapplied note');

assert.strictEqual(els.settingsTtsAutoRead.checked, false, 'tts_auto_read must reflect effective runtime value');
assert.ok(hasNote(els.settingsTtsAutoRead), 'tts_auto_read must show unapplied note');

assert.strictEqual(els.settingsTtsEngine.value, 'browser', 'tts_engine must reflect effective runtime value');
assert.ok(hasNote(els.settingsTtsEngine), 'tts_engine must show unapplied note');

assert.strictEqual(els.settingsTtsRate.value, '1', 'tts_rate must reflect effective runtime value');
assert.ok(hasNote(els.settingsTtsRate), 'tts_rate must show unapplied note');

assert.strictEqual(els.settingsTtsPitch.value, '1', 'tts_pitch must reflect effective runtime value');
assert.ok(hasNote(els.settingsTtsPitch), 'tts_pitch must show unapplied note');

// 5. Unrelated saves must omit every speech key whose mirror write was refused,
// so the durable server value is never replaced by the stale cache. Raw audio
// was applied in memory by boot.js, so it may still be sent.
const unappliedKeys = [
  'voice_mode_button', 'tts_enabled', 'tts_auto_read', 'tts_engine',
  'tts_voice', 'tts_rate', 'tts_pitch', 'voice_continuous', 'voice_silence_ms',
];
const unrelatedSavePayload = _preferencesPayloadFromUi();
unappliedKeys.forEach((key) => {
  assert.ok(
    !Object.prototype.hasOwnProperty.call(unrelatedSavePayload, key),
    'unrelated save must omit unapplied key ' + key + ': ' + JSON.stringify(unrelatedSavePayload)
  );
});
assert.strictEqual(unrelatedSavePayload.raw_audio_mode, true, 'raw_audio_mode is applied, so it may be sent');

// 6. A real unrelated edit still reaches the server: the autosave timer fires,
// the POST is sent, and its body omits the unapplied speech keys.
const postsBefore = posts.length;
els.settingsNotificationsEnabled.checked = true;
const notifListener = els.settingsNotificationsEnabled.listeners.find((l) => l.type === 'change');
assert.ok(notifListener, 'notification toggle must be wired to the autosave');
notifListener.fn();
assert.strictEqual(posts.length, postsBefore, 'autosave must stay debounced');
assert.strictEqual(pendingTimers.length, 1, 'autosave timer must be armed');
assert.strictEqual(warnings.length, 1, 'remembering the saved prefs must not add catch noise');

pendingTimers.shift()().then(() => {
  assert.strictEqual(posts.length, postsBefore + 1, 'autosave POST must actually be sent');
  const body = JSON.parse(posts[postsBefore].body);
  assert.strictEqual(body.notifications_enabled, true, 'POST must carry the unrelated edit');
  unappliedKeys.forEach((key) => {
    assert.ok(
      !Object.prototype.hasOwnProperty.call(body, key),
      'autosave POST must omit unapplied key ' + key + ': ' + JSON.stringify(body)
    );
  });
  assert.strictEqual(body.raw_audio_mode, true, 'autosave POST may carry the applied raw-audio value');
  assert.deepStrictEqual(errors, []);
  assert.deepStrictEqual(toasts, []);
}).catch((e) => {
  process.stderr.write(String(e.stack || e));
  process.exit(1);
});

// 7. Explicit user change: with refusing storage, note stays visible and boot consumer reads old cache;
// with writable storage, note clears and cache holds the choice.
els.settingsVoiceModeEnabled.checked = true;
els.settingsVoiceModeEnabled.onchange.call(els.settingsVoiceModeEnabled);
assert.strictEqual(hasNote(els.settingsVoiceModeEnabled), true, 'refused edit must keep unapplied note visible');
assert.strictEqual(_voiceModePrefEnabled(), false, 'boot consumer must still read old cache after refused edit');
assert.strictEqual(modeBtn.style.display, 'none', 'button display must agree with runtime value (none)');
assert.strictEqual(store.get('hermes-voice-mode-button'), 'false', 'refused edit must not mutate cache');
const userSavePayload = _preferencesPayloadFromUi();
assert.strictEqual(userSavePayload.voice_mode_button, true, 'explicit user change must persist chosen value even when refused');

// Writable storage: note must clear and cache must hold the choice
REFUSE = false;
els.settingsVoiceModeEnabled.checked = true;
els.settingsVoiceModeEnabled.onchange.call(els.settingsVoiceModeEnabled);
assert.strictEqual(hasNote(els.settingsVoiceModeEnabled), false, 'writable edit must clear unapplied note');
assert.strictEqual(store.get('hermes-voice-mode-button'), 'true', 'cache must hold choice when writable');
assert.strictEqual(_voiceModePrefEnabled(), true, 'boot consumer must agree with writable choice');
assert.strictEqual(modeBtn.style.display, '', 'button display must agree with runtime value (visible)');
const writableSavePayload = _preferencesPayloadFromUi();
assert.strictEqual(writableSavePayload.voice_mode_button, true, 'persisted payload carries choice');
"""


_VOICE_LIFECYCLE_SCENARIO = r"""
(async () => {
  // Setup voices in synthesis
  window._voices = [
    { name: 'Old Voice', lang: 'en-US' },
    { name: 'New Voice', lang: 'en-US' },
    { name: 'Third Voice', lang: 'en-US' },
  ];

  // 1. Hydrate Old Voice with writable storage
  REFUSE = false;
  store.set('hermes-tts-engine', 'browser');
  store.set('hermes-tts-voice', 'Old Voice');

  const voicePayload = {
    tts_enabled: true,
    tts_auto_read: true,
    tts_engine: 'browser',
    tts_voice: 'Old Voice',
    tts_rate: 1,
    tts_pitch: 1,
    voice_mode_button: false,
    voice_continuous: false,
    voice_silence_ms: 1800,
    raw_audio_mode: false,
    notifications_enabled: false,
    sidebar_density: 'compact',
    font_size: 'normal',
    persisted_speech_keys: [
      'tts_enabled', 'tts_auto_read', 'tts_engine', 'tts_voice', 'tts_rate',
      'tts_pitch', 'voice_mode_button', 'voice_continuous', 'voice_silence_ms',
      'raw_audio_mode',
    ],
  };

  __hydrateHead(voicePayload);
  __hydrate(voicePayload);

  const hasNote = (el) => {
    if(!el) return false;
    if(el.title && el.title.includes('settings_unapplied_note')) return true;
    if(el.dataset && el.dataset.unapplied === 'true') return true;
    if(el.unappliedNote && (el.unappliedNote.textContent || '').includes('settings_unapplied_note')) return true;
    if(el.children && el.children.some(c => c && c.className === 'settings-unapplied-note')) return true;
    return false;
  };

  assert.strictEqual(els.settingsTtsVoice.value, 'Old Voice', 'voice selector must hydrate with Old Voice');
  assert.strictEqual(hasNote(els.settingsTtsVoice), false, 'writable hydration must have no unapplied note');

  // 2. Choose New Voice and complete the save
  els.settingsTtsVoice.value = 'New Voice';
  els.settingsTtsVoice.onchange.call(els.settingsTtsVoice);
  assert.strictEqual(store.get('hermes-tts-voice'), 'New Voice', 'writable save must mirror to localStorage');
  assert.strictEqual(hasNote(els.settingsTtsVoice), false, 'writable edit must have no unapplied note');
  assert.strictEqual(pendingTimers.length, 1, 'autosave timer armed');
  await pendingTimers.shift()();
  assert.strictEqual(JSON.parse(posts[posts.length - 1].body).tts_voice, 'New Voice', 'autosave body carries New Voice');

  // 3. Start refusing writes
  REFUSE = true;

  // 4. Choose Third Voice and complete that save
  els.settingsTtsVoice.value = 'Third Voice';
  els.settingsTtsVoice.onchange.call(els.settingsTtsVoice);
  assert.strictEqual(store.get('hermes-tts-voice'), 'New Voice', 'refused write must leave old cache in place');
  assert.strictEqual(pendingTimers.length, 1, 'autosave timer armed');
  await pendingTimers.shift()();
  assert.strictEqual(JSON.parse(posts[posts.length - 1].body).tts_voice, 'Third Voice', 'autosave body carries Third Voice');

  // 5. Fire the registered voiceschanged callback
  assert.ok(voiceschangedListeners.length > 0, 'voiceschanged listener must have been registered');
  voiceschangedListeners.forEach((fn) => fn());

  // 6. Assert the selector still shows Third Voice and keeps the unapplied note
  assert.strictEqual(els.settingsTtsVoice.value, 'Third Voice', 'selector must still show Third Voice after voiceschanged rebuild');
  assert.strictEqual(hasNote(els.settingsTtsVoice), true, 'unapplied note must stay visible after voiceschanged rebuild');

  // 7. Change an unrelated control
  els.settingsNotificationsEnabled.checked = true;
  const notifListener = els.settingsNotificationsEnabled.listeners.find((l) => l.type === 'change');
  assert.ok(notifListener, 'notification toggle listener exists');
  notifListener.fn();
  assert.strictEqual(pendingTimers.length, 1, 'unrelated autosave armed');
  await pendingTimers.shift()();

  // 8. Assert the autosave body carries Third Voice, never New Voice
  const lastPost = JSON.parse(posts[posts.length - 1].body);
  assert.strictEqual(lastPost.notifications_enabled, true, 'unrelated save carries notification change');
  assert.strictEqual(lastPost.tts_voice, 'Third Voice', 'unrelated save must carry Third Voice, never New Voice');
  assert.notStrictEqual(lastPost.tts_voice, 'New Voice', 'unrelated save must never revert to New Voice');

  // 9. The refused voice edit must also keep its unapplied indication visible,
  // because the runtime still reads the stale cache for this key.
  assert.strictEqual(hasNote(els.settingsTtsVoice), true, 'refused voice edit must show unapplied note');

  assert.deepStrictEqual(errors, []);
})().catch((e) => {
  process.stderr.write(String(e.stack || e));
  process.exit(1);
});
"""


def test_conflicting_cache_refused_write_reflects_runtime_and_shows_unapplied_note():
    """#8749: Conflicting obsolete cache + refused write must reflect effective runtime
    value, show the unapplied note, and preserve durable server value on unrelated saves."""
    script = (
        _REGRESSION_HARNESS.replace("/*__HELPERS__*/", _speech_helpers())
        .replace("/*__PAYLOAD_HELPERS__*/", _payload_helpers())
        .replace("/*__BOOT_RAW_AUDIO__*/", _boot_raw_audio_consumer())
        .replace("/*__BOOT_SPEECH_MIRROR__*/", _boot_speech_mirror())
        .replace("/*__BOOT_VOICE_MODE__*/", _boot_voice_mode_consumer())
        .replace("/*__HYDRATION__*/", _speech_hydration())
        .replace("/*__HEAD__*/", _font_size_hydration())
    )
    result = subprocess.run(
        ["node", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            "node harness failed\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (result.stdout, result.stderr)
        )


def test_voice_lifecycle_refused_rebuild_preserves_desired_voice():
    """#8749: Maintainer's voice lifecycle regression:
    hydrate 'Old Voice' with writable storage ->
    choose 'New Voice' and complete save ->
    start refusing writes ->
    choose 'Third Voice' and complete that save ->
    fire the registered voiceschanged callback ->
    assert the selector still shows 'Third Voice' ->
    change an unrelated control ->
    assert the autosave body carries 'Third Voice', never 'New Voice'.
    """
    marker = "// 1. Conflicting obsolete cache in store"
    harness_base = _REGRESSION_HARNESS[: _REGRESSION_HARNESS.index(marker)]
    full_script = harness_base + _VOICE_LIFECYCLE_SCENARIO
    script = (
        full_script.replace("/*__HELPERS__*/", _speech_helpers())
        .replace("/*__PAYLOAD_HELPERS__*/", _payload_helpers())
        .replace("/*__BOOT_RAW_AUDIO__*/", _boot_raw_audio_consumer())
        .replace("/*__BOOT_SPEECH_MIRROR__*/", _boot_speech_mirror())
        .replace("/*__BOOT_VOICE_MODE__*/", _boot_voice_mode_consumer())
        .replace("/*__HYDRATION__*/", _speech_hydration())
        .replace("/*__HEAD__*/", _font_size_hydration())
    )
    result = subprocess.run(
        ["node", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            "voice lifecycle harness failed\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (result.stdout, result.stderr)
        )


def test_settings_unapplied_note_locale_parity():
    """#8749: settings_unapplied_note must be present in all 14 locale bundles in static/i18n.js."""
    script = """
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync('static/i18n.js', 'utf8');
const sandbox = {
  document: { documentElement: {} },
  localStorage: { getItem: () => null, setItem: () => {} },
  window: {},
};
vm.runInNewContext(code + '; globalThis.LOCALES = LOCALES;', sandbox);
const locales = sandbox.LOCALES;
const targetLocales = ['en', 'it', 'ja', 'ru', 'es', 'de', 'zh', 'pt', 'ko', 'fr', 'cs', 'tr', 'pl', 'vi'];
const missing = [];
for (const loc of targetLocales) {
  if (!locales[loc] || typeof locales[loc].settings_unapplied_note !== 'string' || !locales[loc].settings_unapplied_note.trim()) {
    missing.push(loc);
  }
}
if (missing.length > 0) {
  console.error('Missing settings_unapplied_note in locales:', missing);
  process.exit(1);
}
console.log('OK');
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(
            "locale parity check failed\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (result.stdout, result.stderr)
        )


# ── VOICE-SELECT-F1: native <select> option membership (#8749 fold) ─────────
# FakeEl keeps whatever `.value` it is given.  A real <select> does not:
# assigning a value no option carries leaves selectedIndex -1 and value '',
# i.e. a blank control instead of its labelled default option.

_NATIVE_SELECT_MODEL = r"""
class NativeSelect extends FakeEl {
  constructor(){ super('select'); }
  get options(){
    const out = [];
    (this.children || []).forEach((c) => {
      if(c && c.tagName === 'option') out.push(c);
      else if(c && c.tagName === 'optgroup') {
        (c.children || []).forEach((o) => { if(o && o.tagName === 'option') out.push(o); });
      }
    });
    return out;
  }
  get selectedIndex(){ return this._selectedOpt ? this.options.indexOf(this._selectedOpt) : -1; }
  set selectedIndex(i){ this._selectedOpt = this.options[i] || null; }
  get value(){ return this._selectedOpt ? this._selectedOpt.value : ''; }
  set value(v){ this._selectedOpt = this.options.find((o) => o.value === String(v)) || null; }
  get innerHTML(){ return this._innerHTML || ''; }
  set innerHTML(html){
    this._innerHTML = html;
    this.children = [];
    this._selectedOpt = null;
    const re = /<option value="([^"]*)"[^>]*>([^<]*)<\/option>/g;
    let m;
    while((m = re.exec(html))){
      const opt = new FakeEl('option');
      opt.value = m[1]; opt.textContent = m[2]; opt.parentElement = this;
      this.children.push(opt);
    }
    this._selectedOpt = this.options[0] || null;  // display-size-1 select: first option
  }
  appendChild(node){
    if(node) node.parentElement = this;
    this.children.push(node);
    // Selectedness algorithm: an inserted selected option wins; otherwise the
    // first option is selected when nothing is.
    const inserted = node && node.tagName === 'optgroup' ? (node.children || []) : [node];
    const picked = inserted.filter((o) => o && o.tagName === 'option' && o._selected);
    if(picked.length) this._selectedOpt = picked[picked.length - 1];
    else if(!this._selectedOpt) this._selectedOpt = this.options[0] || null;
    return node;
  }
}
const voiceRow = new FakeEl('div');
els.settingsTtsVoice = new NativeSelect();
els.settingsTtsVoice.parentElement = voiceRow;
const voiceSel = els.settingsTtsVoice;
const selectedLabel = (sel) => (sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex].textContent : null);
const rowNote = () => voiceRow.children.find((c) => c && c.className === 'settings-unapplied-note') || null;
window._voices = [
  { name: 'Old Voice', lang: 'en-US' },
  { name: 'New Voice', lang: 'en-US' },
  { name: 'Third Voice', lang: 'en-US' },
];
function nativePayload(engine, voice){
  return {
    tts_enabled: true, tts_auto_read: false, tts_engine: engine, tts_voice: voice,
    tts_rate: 1, tts_pitch: 1, voice_mode_button: false, voice_continuous: false,
    voice_silence_ms: 1800, raw_audio_mode: false, notifications_enabled: false,
    sidebar_density: 'compact', font_size: 'normal',
    persisted_speech_keys: [
      'tts_enabled', 'tts_auto_read', 'tts_engine', 'tts_voice', 'tts_rate',
      'tts_pitch', 'voice_mode_button', 'voice_continuous', 'voice_silence_ms',
      'raw_audio_mode',
    ],
  };
}
function hydrateNative(engine, voice){
  store.set('hermes-tts-engine', engine);
  store.set('hermes-tts-voice', voice);
  const p = nativePayload(engine, voice);
  __hydrateHead(p);
  __hydrate(p);
}
"""


def _run_native_select_scenario(body):
    marker = "// 1. Conflicting obsolete cache in store"
    harness_base = _REGRESSION_HARNESS[: _REGRESSION_HARNESS.index(marker)]
    script = (
        (harness_base + _NATIVE_SELECT_MODEL + "(async () => {\n" + body + "\n"
         "  assert.deepStrictEqual(errors, []);\n"
         "})().catch((e) => { process.stderr.write(String(e.stack || e)); process.exit(1); });\n")
        .replace("/*__HELPERS__*/", _speech_helpers())
        .replace("/*__PAYLOAD_HELPERS__*/", _payload_helpers())
        .replace("/*__BOOT_RAW_AUDIO__*/", _boot_raw_audio_consumer())
        .replace("/*__BOOT_SPEECH_MIRROR__*/", _boot_speech_mirror())
        .replace("/*__BOOT_VOICE_MODE__*/", _boot_voice_mode_consumer())
        .replace("/*__HYDRATION__*/", _speech_hydration())
        .replace("/*__HEAD__*/", _font_size_hydration())
    )
    result = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError(
            "native select harness failed\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (result.stdout, result.stderr)
        )


@pytest.mark.parametrize(
    "engine,default_label",
    [
        ("openai", "OpenAI voice (server-configured)"),
        ("elevenlabs", "Hermy — ElevenLabs (server-configured)"),
        ("edge", "Default (Xiaoxiao)"),
    ],
)
def test_engine_switch_from_browser_voice_keeps_labelled_default(engine, default_label):
    """VOICE-SELECT-F1: writable storage, browser engine + 'Old Voice', switch the
    engine: the rebuilt voice select has no 'Old Voice' option, so it must keep
    its labelled default (index 0), not go blank (selectedIndex -1)."""
    _run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('browser', 'Old Voice');
  assert.strictEqual(voiceSel.value, 'Old Voice');
  assert.strictEqual(selectedLabel(voiceSel), 'Old Voice (en-US)');
  els.settingsTtsEngine.value = '__ENGINE__';
  els.settingsTtsEngine.onchange.call(els.settingsTtsEngine);
  assert.strictEqual(store.get('hermes-tts-engine'), '__ENGINE__');
  assert.strictEqual(voiceSel.selectedIndex, 0, 'voice select must keep its labelled default, not go blank');
  assert.strictEqual(selectedLabel(voiceSel), '__LABEL__');
  assert.strictEqual(voiceSel.value, '');
  assert.strictEqual(rowNote(), null, 'writable storage must not show the unapplied note');
  assert.strictEqual(pendingTimers.length, 1, 'engine change arms the autosave');
  await pendingTimers.shift()();
  const body = JSON.parse(posts[posts.length - 1].body);
  assert.strictEqual(body.tts_engine, '__ENGINE__');
  assert.strictEqual(body.tts_voice, '', 'the engine default voice is what gets saved');
""".replace("__ENGINE__", engine).replace("__LABEL__", default_label))


@pytest.mark.parametrize(
    "engine,voice,label",
    [
        ("openai", "", "OpenAI voice (server-configured)"),
        ("browser", "Third Voice", "Third Voice (en-US)"),
        ("edge", "en-US-AriaNeural", "Aria (English, Female)"),
    ],
)
def test_valid_saved_voice_is_selected_in_native_select(engine, voice, label):
    """A saved voice the rebuilt list offers stays selected (edge options sit in optgroups)."""
    _run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('__ENGINE__', '__VOICE__');
  assert.ok(voiceSel.selectedIndex >= 0, 'select must not be blank');
  assert.strictEqual(voiceSel.value, '__VOICE__');
  assert.strictEqual(selectedLabel(voiceSel), '__LABEL__');
""".replace("__ENGINE__", engine).replace("__VOICE__", voice).replace("__LABEL__", label))


def test_refused_voice_edit_keeps_note_and_selection_in_native_select():
    """A refused mirror write keeps the row note and the desired voice across a
    voiceschanged rebuild, and the desired voice is what gets saved."""
    _run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('browser', 'Old Voice');
  assert.strictEqual(rowNote(), null);
  REFUSE = true;
  voiceSel.value = 'Third Voice';
  voiceSel.onchange.call(voiceSel);
  assert.strictEqual(store.get('hermes-tts-voice'), 'Old Voice', 'refused write leaves the cache');
  assert.ok(rowNote() && rowNote().textContent.includes('settings_unapplied_note'), 'refused edit shows the row note');
  voiceschangedListeners.forEach((fn) => fn());
  assert.strictEqual(voiceSel.value, 'Third Voice', 'rebuild keeps the desired voice');
  assert.strictEqual(selectedLabel(voiceSel), 'Third Voice (en-US)');
  assert.ok(rowNote() && rowNote().style.display !== 'none', 'note stays visible after the rebuild');
  await pendingTimers.shift()();
  assert.strictEqual(JSON.parse(posts[posts.length - 1].body).tts_voice, 'Third Voice');
""")


# ── The unapplied note is its own line under the control's row ────────────
# Appended to the control's flex parent, the note squeezed the phone slider
# track (292 -> 129 px) and wrapped checkbox labels beside it. It now lives on
# the row's `.settings-field`, as a block with no left margin.

_NOTE_DOM = r"""
const assert = require('assert');
class El {
  constructor(tag, cls){ this.tagName = tag; this.className = cls || ''; this.children = [];
    this.parentElement = null; this.style = {}; this.dataset = {}; this.title = ''; this.textContent = ''; }
  appendChild(node){ if(node.parentElement) node.remove(); node.parentElement = this; this.children.push(node); return node; }
  remove(){ const p = this.parentElement; if(p){ p.children.splice(p.children.indexOf(this), 1); this.parentElement = null; } }
  _has(sel){ return (' ' + this.className + ' ').includes(' ' + sel.replace(/^\./, '') + ' '); }
  closest(sel){ let e = this; while(e){ if(e._has(sel)) return e; e = e.parentElement; } return null; }
  querySelector(sel){
    for(const k of this.children){ if(k._has(sel)) return k; const r = k.querySelector(sel); if(r) return r; }
    return null;
  }
  querySelectorAll(sel){
    const out = [];
    for(const k of this.children){ if(k._has(sel)) out.push(k); out.push(...k.querySelectorAll(sel)); }
    return out;
  }
}
const document = { createElement(tag){ return new El(tag); } };
const t = (key) => key;
/*__NOTE_FUNCS__*/
function checkboxRow(){
  const field = new El('div', 'settings-field'), label = new El('label'), input = new El('input');
  label.appendChild(input); label.appendChild(new El('span')); field.appendChild(label);
  field.appendChild(new El('div'));  // description
  return { field, control: input };
}
function selectRow(){
  const field = new El('div', 'settings-field'), select = new El('select');
  field.appendChild(new El('label')); field.appendChild(select); field.appendChild(new El('div'));
  return { field, control: select };
}
function sliderRow(){
  const field = new El('div', 'settings-field'), track = new El('div'), range = new El('input');
  field.appendChild(new El('label')); track.appendChild(range); track.appendChild(new El('span'));
  field.appendChild(track);
  return { field, control: range };
}
const rows = { settingsTtsAutoRead: checkboxRow(), settingsVoiceModeEnabled: checkboxRow(),
  settingsTtsVoice: selectRow(), settingsTtsRate: sliderRow(), settingsTtsPitch: sliderRow() };
for(const [id, { field, control }] of Object.entries(rows).filter(([key]) => key === '__ONLY__')){
  _setControlUnappliedNote(control, true);
  _setControlUnappliedNote(control, true);
  const notes = field.querySelectorAll('.settings-unapplied-note');
  assert.strictEqual(notes.length, 1, id + ': exactly one note');
  const note = notes[0];
  assert.strictEqual(note.parentElement, field, id + ': the note must sit on the .settings-field row, not in '
    + (note.parentElement && note.parentElement.tagName));
  assert.strictEqual(field.children[field.children.length - 1], note, id + ': the note is the row\'s last line');
  assert.strictEqual(note.style.display, 'block', id + ': the note is its own line');
  assert.ok(!note.style.marginLeft, id + ': no left margin, flush with the description');
  assert.strictEqual(note.style.marginTop, '4px', id);
  assert.ok(note.textContent.includes('settings_unapplied_note') && control.title === 'settings_unapplied_note', id);
  _setControlUnappliedNote(control, false);
  assert.strictEqual(field.querySelectorAll('.settings-unapplied-note').length, 0, id + ': clearing removes the note');
  assert.strictEqual(control.title, '', id + ': clearing removes the title');
  assert.strictEqual(control.dataset.unapplied, undefined, id);
  _setControlUnappliedNote(control, true);
  assert.strictEqual(field.querySelectorAll('.settings-unapplied-note').length, 1, id + ': re-shown once');
  process.stdout.write('CHECKED ' + id);
}
"""


def _note_functions():
    start_marker = (
        "function _unappliedNoteHost" if "function _unappliedNoteHost" in PANELS_JS
        else "function _findUnappliedSpeechNote"
    )
    return _slice(start_marker, "function _speechValuesEqual")


@pytest.mark.parametrize(
    "control",
    ["settingsTtsAutoRead", "settingsVoiceModeEnabled", "settingsTtsVoice", "settingsTtsRate", "settingsTtsPitch"],
)
def test_unapplied_note_is_its_own_line_on_the_settings_field_row(control):
    script = _NOTE_DOM.replace("/*__NOTE_FUNCS__*/", _note_functions()).replace("__ONLY__", control)
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "CHECKED " + control, result.stdout
