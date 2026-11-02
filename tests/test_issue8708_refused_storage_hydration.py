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

import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


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
// both present and wired, showing the server-authoritative value.
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
  ['settingsTtsEnabled', 'onchange', 'checked', true],
  ['settingsTtsAutoRead', 'onchange', 'checked', true],
  ['settingsVoiceModeEnabled', 'onchange', 'checked', true],
  ['settingsRawAudio', 'onchange', 'checked', true],
  ['settingsTtsRate', 'oninput', 'value', '1.4'],
  ['settingsTtsPitch', 'oninput', 'value', '0.8'],
];
wired.forEach(([id, handler, prop, want]) => {
  const el = els[id];
  assert.ok(el, id + ' was never hydrated' + threwAt);
  assert.strictEqual(typeof el[handler], 'function', id + ' has no ' + handler + ' handler');
  assert.strictEqual(el[prop], want, id + '.' + prop + ' must show the server value');
});
assert.strictEqual(els.settingsTtsEngine.value, 'edge', 'engine select must show the server value');
assert.strictEqual(typeof els.settingsTtsEngine.onchange, 'function', 'engine select has no onchange');
assert.strictEqual(typeof els.settingsTtsVoice.onchange, 'function', 'voice select has no onchange');
assert.strictEqual(els.settingsFontSize.value, 'large', 'font size select must still hydrate');
assert.deepStrictEqual(fontSizeApps, ['large'], '_applyFontSize must still run');

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
    """(a)+(c): refusal is non-fatal, controls wired, server value authoritative."""
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
