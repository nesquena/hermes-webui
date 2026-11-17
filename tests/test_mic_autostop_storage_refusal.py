"""Silence auto-stop checkbox must not fork from the value in force (PR #8564).

`_micAutoStopEnabled()` reads `hermes-mic-auto-stop` from localStorage at capture
time, so that key IS the effective preference: a refused `setItem` (quota,
private mode) used to leave the checkbox on the new value while the meter kept
the old one, and aborted the handler before the autosave was scheduled.

These run the shipped `onchange` handler from `static/panels.js` under node,
together with the shipped `_micAutoStopEnabled()` predicate, against a
controlled localStorage that can start refusing writes.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(src, name):
    """Return the full source of a top-level `function name(...)`."""
    marker = "function %s(" % name
    start = src.index(marker)
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


def _extract_mic_autostop_block(src):
    """The `if(micAutoStopCb){...}` block that wires the checkbox, verbatim."""
    # The name also appears in `_preferencesPayloadFromUi`; the wiring block is the
    # last one, followed by the voice-silence setup.
    start = src.rindex("const micAutoStopCb=$('settingsMicAutoStop');")
    end = src.index("const voiceSilence=parseInt(", start)
    block = src[start:end].rstrip()
    if not block.endswith("}"):
        raise AssertionError("mic auto-stop block did not end with a closing brace")
    return block


_HARNESS = r"""
const _MIC_AUTOSTOP_KEY = 'hermes-mic-auto-stop';
const KEY = 'hermes-mic-auto-stop';
const store = {};
let failWrites = __FAIL_INIT__;
__SEED_CACHE__
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { if (failWrites) throw Object.assign(new Error('QuotaExceededError'), {name: 'QuotaExceededError'}); store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
const els = {};
function mk(id){ if(!els[id]) els[id] = {id: id, checked: false, hidden: true}; return els[id]; }
globalThis.$ = (id) => (id === 'settingsMicAutoStop' || id === 'settingsMicAutoStopNote'
  || id === 'settingsMicAutoStopNoteSavedOn' || id === 'settingsMicAutoStopNoteSavedOff') ? mk(id) : null;
let saves = 0;
const changed = [];
globalThis._speechBool = () => __SERVER_VALUE__;
globalThis._speechPreferenceIsOwned = () => true;
globalThis._syncSpeechPreferenceCache = (k, v) => { localStorage.setItem(KEY, String(v)); };
globalThis._markSpeechPreferenceChanged = (k) => { changed.push(k); };
globalThis._settingsSpeechUnappliedKeys = new Set();
globalThis._schedulePreferencesAutosave = () => { saves += 1; };

__MIC_PREDICATE__

__MIC_NOTE__

__MIC_BLOCK__

const cb = mk('settingsMicAutoStop');
const note = mk('settingsMicAutoStopNote');
function click(next){
  cb.checked = next;
  cb.onchange.call(cb);
}
function snap(){
  return {
    checkbox: cb.checked,
    effective: _micAutoStopEnabled(),
    note: note.hidden === false,
    // Which direction the note describes: exactly one wording at a time.
    noteSavedOn: mk('settingsMicAutoStopNoteSavedOn').hidden === false,
    noteSavedOff: mk('settingsMicAutoStopNoteSavedOff').hidden === false,
    saves: saves,
    stored: (KEY in store) ? store[KEY] : null,
    changed: changed.length,
  };
}
const out = {};
out.defaultOff = snap();
click(true);  out.onWritable = snap();
click(false); out.offWritable = snap();
click(true);  const savesBeforeRefusal = saves;
failWrites = true;
click(false); out.offRefused = snap();
out.savesBeforeRefusal = savesBeforeRefusal;
failWrites = false;
click(true);  out.onAgain = snap();
failWrites = true;
click(false); out.offRefusedTwice = snap();
// ...and the mirror: the switch turned ON is refused too.
failWrites = false;
click(false); out.offWritableAgain = snap();
const savesBeforeOnRefusal = saves;
failWrites = true;
click(true); out.onRefused = snap();
out.savesBeforeOnRefusal = savesBeforeOnRefusal;
console.log(JSON.stringify(out));
"""


def _run(fail_init=False, server_value=False, cache=None):
    seed = "" if cache is None else "store[KEY] = %s;" % json.dumps(cache)
    js = (
        _HARNESS
        .replace("__FAIL_INIT__", "true" if fail_init else "false")
        .replace("__SEED_CACHE__", seed)
        .replace("__SERVER_VALUE__", "true" if server_value else "false")
        .replace("__MIC_PREDICATE__", _extract_function(BOOT_JS, "_micAutoStopEnabled"))
        .replace("__MIC_NOTE__", _extract_function(PANELS_JS, "_setMicAutoStopStorageNote"))
        .replace("__MIC_BLOCK__", _extract_mic_autostop_block(PANELS_JS))
    )
    proc = subprocess.run(
        [NODE, "-e", js], capture_output=True, text=True, cwd=ROOT, timeout=60
    )
    assert proc.returncode == 0, "node harness failed:\n%s" % proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_init(fail_init=False, server_value=False, cache=None):
    """Snap immediately after hydration, before any user interaction."""
    if NODE is None:
        pytest.skip("node not on PATH")
    out = _run(fail_init=fail_init, server_value=server_value, cache=cache)
    return out["defaultOff"]


@pytest.fixture(scope="module")
def runs():
    if NODE is None:
        pytest.skip("node not on PATH")
    return _run()


def test_absent_preference_is_off_and_unsaved(runs):
    # Init seeds the runtime cache from the (absent) server value, so the key
    # exists as "false" and the predicate agrees: nothing enabled, nothing saved.
    assert runs["defaultOff"] == {
        "checkbox": False,
        "effective": False,
        "note": False,
        "noteSavedOn": False,
        "noteSavedOff": False,
        "saves": 0,
        "stored": "false",
        "changed": 0,
    }


def test_writable_toggle_moves_both_checkbox_and_effective_value(runs):
    on = runs["onWritable"]
    assert on["checkbox"] is True
    assert on["effective"] is True
    assert on["stored"] == "true"
    assert on["saves"] == 1
    off = runs["offWritable"]
    assert (off["checkbox"], off["effective"], off["stored"]) == (False, False, "false")
    assert off["saves"] == 2


def test_refused_write_keeps_displayed_and_effective_in_agreement(runs):
    refused = runs["offRefused"]
    # The box rolls back to the value that is actually in force instead of
    # showing a preference the meter will not honour.
    assert refused["checkbox"] is True
    assert refused["effective"] is True
    assert refused["stored"] == "true"


def test_refused_write_is_visible_and_does_not_schedule_a_save(runs):
    refused = runs["offRefused"]
    assert refused["note"] is True, "a refused write must be reported, not swallowed"
    assert refused["saves"] == runs["savesBeforeRefusal"], "nothing changed, nothing to persist"


def test_repeated_refusal_never_forks_the_control(runs):
    again = runs["onAgain"]
    assert (again["checkbox"], again["effective"]) == (True, True)
    assert again["note"] is False
    twice = runs["offRefusedTwice"]
    assert twice["checkbox"] is twice["effective"] is True
    assert twice["stored"] == "true"
    assert twice["note"] is True


def test_writable_hydration_applies_the_owned_server_value():
    on = _run_init(server_value=True, cache=None)
    assert (on["checkbox"], on["effective"], on["stored"]) == (True, True, "true")
    assert on["note"] is False
    off = _run_init(server_value=False, cache="true")
    assert (off["checkbox"], off["effective"], off["stored"]) == (False, False, "false")
    assert off["note"] is False


def test_refused_hydration_does_not_show_a_preference_the_meter_ignores():
    # Owned server value OFF, stale cache ON, seed write refused: the box must
    # follow the runtime predicate instead of claiming auto-stop is off while it
    # is still armed.
    off = _run_init(fail_init=True, server_value=False, cache="true")
    assert off["checkbox"] is True
    assert off["checkbox"] is off["effective"]
    assert off["stored"] == "true"
    # The opposite direction: server ON, no cached value, seed write refused.
    on = _run_init(fail_init=True, server_value=True, cache=None)
    assert on["checkbox"] is False
    assert on["checkbox"] is on["effective"]
    assert on["stored"] is None


def test_refused_hydration_is_visible_and_schedules_nothing():
    for snap in (
        _run_init(fail_init=True, server_value=False, cache="true"),
        _run_init(fail_init=True, server_value=True, cache=None),
    ):
        assert snap["note"] is True, "a refused seed write must be reported"
        assert snap["saves"] == 0
        assert snap["changed"] == 0


def test_each_storage_note_names_its_own_direction():
    # "You turned it on, but this browser can't store the setting, so auto-stop
    # stays off here" — the direction the user asked for AND the value in force,
    # so the sentence is true for the direction that was detected instead of a
    # generic "not changed". It must not claim a save: nothing is persisted.
    saved_on = _run_init(fail_init=True, server_value=True, cache=None)
    assert saved_on["note"] is True
    assert (saved_on["noteSavedOn"], saved_on["noteSavedOff"]) == (True, False)

    saved_off = _run_init(fail_init=True, server_value=False, cache="true")
    assert saved_off["note"] is True
    assert (saved_off["noteSavedOn"], saved_off["noteSavedOff"]) == (False, True)

    # A refused toggle is the mirror case: wanted off, stays on.
    refused = _run()["offRefused"]
    assert refused["note"] is True
    assert (refused["noteSavedOn"], refused["noteSavedOff"]) == (False, True)

    # ... and wanted on, stays off: the handler returns before the autosave, so
    # the note that used to read "Saved as on" described a write that never ran.
    refused_on_run = _run()
    refused_on = refused_on_run["onRefused"]
    assert refused_on["note"] is True
    assert (refused_on["noteSavedOn"], refused_on["noteSavedOff"]) == (True, False)
    assert refused_on["checkbox"] is refused_on["effective"] is False
    assert refused_on["saves"] == refused_on_run["savesBeforeOnRefusal"], (
        "the refused write must not schedule a save"
    )

    # With storage working there is nothing to say.
    writable = _run_init(server_value=True, cache=None)
    assert writable["note"] is False
    assert (writable["noteSavedOn"], writable["noteSavedOff"]) == (False, False)


# ── refused hydration must not leak into an unrelated settings save ──────────
#
# The same refused seed leaves the checkbox showing the value the METER reads
# rather than the durable one, and `mic_auto_stop` stays owned. Every settings
# write builds its mic field from that checkbox (`_speechPreferencesPayloadFromUi`,
# used by the autosave AND by the whole-settings save), so a save triggered by
# an unrelated preference used to overwrite the durable mic intent with the
# fallback on screen. These compose the shipped helpers with hydration, the real
# runtime predicate and the real payload builder.

_HARNESS_UNRELATED_SAVE = r"""
const _MIC_AUTOSTOP_KEY = 'hermes-mic-auto-stop';
const KEY = 'hermes-mic-auto-stop';
const store = {};
let failMicWrites = __FAIL_INIT__;
__SEED_CACHE__
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => {
    if (failMicWrites && k === KEY) throw Object.assign(new Error('QuotaExceededError'), {name: 'QuotaExceededError'});
    store[k] = String(v);
  },
  removeItem: (k) => { delete store[k]; },
};
const els = {};
function mk(id){ if(!els[id]) els[id] = {id: id, checked: false, hidden: true}; return els[id]; }
globalThis.$ = (id) => (id === 'settingsMicAutoStop' || id === 'settingsMicAutoStopNote'
  || id === 'settingsMicAutoStopNoteSavedOn' || id === 'settingsMicAutoStopNoteSavedOff') ? mk(id) : null;
let saves = 0;
globalThis._schedulePreferencesAutosave = () => { saves += 1; };
globalThis._speechBool = () => __SERVER_VALUE__;

__MIC_PREDICATE__

__MIC_NOTE__

__SPEECH_RUNTIME__

_captureSpeechPreferenceOwnership({persisted_speech_keys: ['mic_auto_stop'], mic_auto_stop: __SERVER_VALUE__});

__MIC_BLOCK__

const cb = mk('settingsMicAutoStop');
const note = mk('settingsMicAutoStopNote');
function has(o, k){ return Object.prototype.hasOwnProperty.call(o, k); }
const out = {};
out.hydrated = _speechPreferencesPayloadFromUi();
out.checkbox = cb.checked;
out.effective = _micAutoStopEnabled();
out.note = note.hidden === false;
out.stored = (KEY in store) ? store[KEY] : null;
// An unrelated preference change (send key, theme, ...) serializes exactly this
// payload: `Object.assign(payload, _speechPreferencesPayloadFromUi())`.
failMicWrites = false;
cb.checked = __TOGGLE__;
cb.onchange.call(cb);
out.afterEdit = _speechPreferencesPayloadFromUi();
out.editCheckbox = cb.checked;
out.editEffective = _micAutoStopEnabled();
out.saves = saves;
console.log(JSON.stringify(out));
"""


def _extract_speech_payload_runtime(src):
    """Ownership + cache helpers, then the payload builder, from panels.js."""
    start = src.index("const _SETTINGS_SPEECH_STORAGE_KEYS={")
    end = src.index("function _preferencesPayloadFromUi(){", start)
    return (src[start:end].rstrip()
            + "\n\n"
            + _extract_function(src, "_speechPreferencesPayloadFromUi"))


def _run_unrelated_save(fail_init=False, server_value=False, cache=None, toggle=False):
    if NODE is None:
        pytest.skip("node not on PATH")
    seed = "" if cache is None else "store[KEY] = %s;" % json.dumps(cache)
    js = (
        _HARNESS_UNRELATED_SAVE
        .replace("__FAIL_INIT__", "true" if fail_init else "false")
        .replace("__SEED_CACHE__", seed)
        .replace("__SERVER_VALUE__", "true" if server_value else "false")
        .replace("__TOGGLE__", "true" if toggle else "false")
        .replace("__MIC_PREDICATE__", _extract_function(BOOT_JS, "_micAutoStopEnabled"))
        .replace("__SPEECH_RUNTIME__", _extract_speech_payload_runtime(PANELS_JS))
        .replace("__MIC_NOTE__", _extract_function(PANELS_JS, "_setMicAutoStopStorageNote"))
        .replace("__MIC_BLOCK__", _extract_mic_autostop_block(PANELS_JS))
    )
    proc = subprocess.run(
        [NODE, "-e", js], capture_output=True, text=True, cwd=ROOT, timeout=60
    )
    assert proc.returncode == 0, "node harness failed:\n%s" % proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "server_value,cache",
    [(False, "true"), (True, None)],
    ids=["durable-off-stale-cache-on", "durable-on-no-cache"],
)
def test_refused_hydration_omits_the_mic_field_from_an_unrelated_save(server_value, cache):
    out = _run_unrelated_save(fail_init=True, server_value=server_value, cache=cache)
    # The control still tells the truth about the meter ...
    assert out["checkbox"] is out["effective"]
    assert out["note"] is True
    # ... and an unrelated save leaves the durable mic preference alone instead
    # of promoting the displayed fallback into it.
    assert "mic_auto_stop" not in out["hydrated"], out["hydrated"]


@pytest.mark.parametrize(
    "server_value,cache,toggle",
    [(False, "true", False), (True, None, False), (True, None, True)],
    ids=["off-stale-on", "on-no-cache", "on-no-cache-toggle-on"],
)
def test_deliberate_edit_after_a_refused_hydration_serializes_again(server_value, cache, toggle):
    out = _run_unrelated_save(
        fail_init=True, server_value=server_value, cache=cache, toggle=toggle
    )
    assert out["afterEdit"].get("mic_auto_stop") is toggle
    assert out["editCheckbox"] is out["editEffective"] is toggle
    assert out["saves"] == 1


def test_writable_hydration_keeps_serializing_the_mic_field():
    # Control: when the seed lands, the checkbox IS the durable intent and the
    # field must stay in the payload (sparse ownership untouched).
    out = _run_unrelated_save(fail_init=False, server_value=False, cache="true")
    assert out["hydrated"].get("mic_auto_stop") is False
    assert out["checkbox"] is out["effective"] is False
    assert out["note"] is False