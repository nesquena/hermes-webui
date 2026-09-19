"""
Executed decision-matrix tests for the phone-sized touch device Enter/newline
behavior (companion to the source-contract tests in test_mobile_layout.py).

These run the REAL predicates and keydown branch extracted from static/boot.js
inside Node, with matchMedia/screen stubs, across a device matrix — so CI
proves the executed behavior at the phone/tablet boundary, not only that the
source contains certain strings. Requested in review of PR #7376.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
BOOT_JS = REPO / "static" / "boot.js"
NODE = shutil.which("node")


_DRIVER = r"""
const fs = require('fs');
const [bootPath, argsJson] = process.argv.slice(-2);
const args = JSON.parse(argsJson);
const src = fs.readFileSync(bootPath, 'utf8');

function extractFunction(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(name + ' not found');
  const brace = source.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < source.length; i++) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('function body not closed for ' + name);
}

// Extract the real production predicates plus the named phone-size constant.
const fns = ['_hasFinePointerCoexisting', '_isPhoneSizedTouchDevice', '_isTouchKeyboardViewport']
  .map((n) => extractFunction(src, n))
  .join('\n');
const constMatch = src.match(/^const _PHONE_MAX_MIN_SIDE_PX\s*=\s*[^;]+;/m);
if (!constMatch) throw new Error('_PHONE_MAX_MIN_SIDE_PX constant not found');
const declarations = constMatch[0];

// Extract the keydown handler's Enter decision block: from the
// "if(e.key==='Enter'){" guard (the send-key one, after the autocomplete
// dropdown block) to its closing brace.
const anchor = src.indexOf('// Send key: respect user preference.');
if (anchor < 0) throw new Error('send-key comment anchor not found');
const guard = src.indexOf("if(e.key==='Enter'){", anchor);
if (guard < 0) throw new Error('Enter guard not found after send-key anchor');
let depth = 0, end = -1;
for (let i = src.indexOf('{', guard); i < src.length; i++) {
  if (src[i] === '{') depth += 1;
  else if (src[i] === '}') {
    depth -= 1;
    if (depth === 0) { end = i + 1; break; }
  }
}
if (end < 0) throw new Error('Enter decision block not closed');
// Wrap in a handler so `return` statements inside the block are legal.
const enterBlock = 'function __enterHandler(e){ ' + src.slice(guard, end) + ' }';

// Browser environment stubs.
function makeEnv(profile) {
  const media = profile.media || {};
  globalThis.window = globalThis;
  globalThis.matchMedia = (q) => {
    const key = String(q).replace(/\s+/g, '');
    const matches = Object.prototype.hasOwnProperty.call(media, key) ? !!media[key] : false;
    return { matches, media: q, onchange: null, addListener() {}, removeListener() {},
             addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; } };
  };
  if (profile.screenThrows) {
    globalThis.screen = undefined;
  } else {
    globalThis.screen = { width: profile.screen[0], height: profile.screen[1] };
  }
  globalThis.window._sendKey = profile.sendKey || 'enter';
}

let sentCount = 0;
globalThis.send = () => { sentCount += 1; };

// Execute the real code under each profile.
const results = [];
for (const profile of args.profiles) {
  makeEnv(profile);
  eval(declarations + '\n' + fns);
  const phone = _isPhoneSizedTouchDevice();
  const vk = _isTouchKeyboardViewport();

  sentCount = 0;
  const prevented = [];
  // Simulate the plain-Enter keydown path through the real branch.
  const e = {
    key: 'Enter', shiftKey: false, ctrlKey: false, metaKey: false,
    isComposing: false, keyCode: 13, code: 'Enter', location: 0,
    preventDefault() { prevented.push(1); },
  };
  // `_isImeEnter` and `_isNumpadEnter` are called by the block; define them
  // as the production ones would behave for this event shape (plain Enter,
  // not composing, not numpad).
  globalThis._isImeEnter = () => false;
  globalThis._isNumpadEnter = () => false;
  eval(enterBlock);
  __enterHandler(e);

  results.push({
    name: profile.name,
    phone,
    touchKeyboardViewport: vk,
    sent: sentCount,
    prevented: prevented.length,
  });
}
console.log(JSON.stringify(results));
"""


def _run_matrix(profiles):
    assert NODE is not None, "node not on PATH"
    result = subprocess.run(
        [NODE, "-e", _DRIVER, str(BOOT_JS), json.dumps({"profiles": profiles})],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
        )
    return {
        row["name"]: row
        for row in json.loads(result.stdout.strip().splitlines()[-1])
    }


# Device matrix: media keys match the exact strings boot.js queries
# ('(pointer:coarse)', '(any-pointer:fine)', '(hover:none)', and the combined
# '(hover:none) and (pointer:coarse)' used by _isTouchKeyboardViewport).
_PROFILES = [
    # Phone + S-Pen (the reported bug): coarse touch, fine stylus, phone screen.
    {"name": "phone_stylus", "media": {"(pointer:coarse)": True, "(any-pointer:fine)": True, "(hover:none)": True, "(hover:none)and(pointer:coarse)": True}, "screen": [384, 800]},
    # Phone + BT mouse: same as above.
    {"name": "phone_bt_mouse", "media": {"(pointer:coarse)": True, "(any-pointer:fine)": True, "(hover:none)": True, "(hover:none)and(pointer:coarse)": True}, "screen": [412, 915]},
    # Plain phone, no fine pointer.
    {"name": "phone_plain", "media": {"(pointer:coarse)": True, "(any-pointer:fine)": False, "(hover:none)": True, "(hover:none)and(pointer:coarse)": True}, "screen": [390, 844]},
    # Tablet-class touch device + hardware keyboard: desktop Enter semantics.
    {"name": "tablet_keyboard", "media": {"(pointer:coarse)": True, "(any-pointer:fine)": True, "(hover:none)": False, "(hover:none)and(pointer:coarse)": False}, "screen": [1024, 1366]},
    # Desktop: fine pointer only.
    {"name": "desktop", "media": {"(pointer:coarse)": False, "(any-pointer:fine)": True, "(hover:none)": False, "(hover:none)and(pointer:coarse)": False}, "screen": [1920, 1080]},
    # Degraded environments must fail closed (not phone, no virtual-keyboard inset).
    {"name": "screen_zero", "media": {"(pointer:coarse)": True, "(any-pointer:fine)": True, "(hover:none)": True, "(hover:none)and(pointer:coarse)": True}, "screen": [0, 0]},
    {"name": "screen_throws", "media": {"(pointer:coarse)": True, "(any-pointer:fine)": True, "(hover:none)": True, "(hover:none)and(pointer:coarse)": True}, "screenThrows": True},
]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_decision_matrix_phone_with_fine_pointer_keeps_newline():
    """Phone-sized touch device + fine pointer (S-Pen / BT mouse): plain Enter
    must NOT send — the newline default survives the co-existing fine pointer."""
    rows = _run_matrix(_PROFILES)
    for name in ("phone_stylus", "phone_bt_mouse"):
        row = rows[name]
        assert row["phone"] is True, f"{name}: should classify as phone-sized"
        assert row["sent"] == 0, f"{name}: plain Enter must not send"
        assert row["prevented"] == 0, f"{name}: Enter must fall through to the textarea (newline)"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_decision_matrix_plain_phone_keeps_newline():
    rows = _run_matrix(_PROFILES)
    row = rows["phone_plain"]
    assert row["phone"] is True
    assert row["sent"] == 0 and row["prevented"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_decision_matrix_tablet_with_keyboard_still_sends():
    """Tablet-class touch device + fine pointer (hardware keyboard) keeps
    desktop semantics: plain Enter sends."""
    rows = _run_matrix(_PROFILES)
    row = rows["tablet_keyboard"]
    assert row["phone"] is False
    assert row["sent"] == 1, "tablet + hardware keyboard must send on plain Enter"
    assert row["prevented"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_decision_matrix_desktop_still_sends():
    rows = _run_matrix(_PROFILES)
    row = rows["desktop"]
    assert row["sent"] == 1 and row["prevented"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_decision_matrix_degraded_screen_fails_closed():
    """Zero-size or unavailable screen dimensions must fail closed: no phone
    classification, no virtual-keyboard inset, desktop Enter semantics."""
    rows = _run_matrix(_PROFILES)
    for name in ("screen_zero", "screen_throws"):
        row = rows[name]
        assert row["phone"] is False, f"{name}: must not classify as phone"
        assert row["touchKeyboardViewport"] is False, f"{name}: must not claim a touch keyboard viewport"
        assert row["sent"] == 1, f"{name}: must keep desktop Enter semantics (fail closed)"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_decision_matrix_touch_keyboard_viewport_matches_enter_gate():
    """The virtual-keyboard inset predicate must agree with the Enter gate:
    phones (with or without fine pointer) keep the inset; tablet-class and
    desktop do not get it."""
    rows = _run_matrix(_PROFILES)
    assert rows["phone_stylus"]["touchKeyboardViewport"] is True
    assert rows["phone_bt_mouse"]["touchKeyboardViewport"] is True
    assert rows["phone_plain"]["touchKeyboardViewport"] is True
    assert rows["tablet_keyboard"]["touchKeyboardViewport"] is False
    assert rows["desktop"]["touchKeyboardViewport"] is False
