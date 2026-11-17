"""Silence auto-stop is opt-in: manual stop is the default capture (PR #8564).

The contract test in `test_mic_dictation_feedback.py` pins the call site; this
runs the shipped predicate against real storage states, because "off by default"
is exactly the kind of claim a source grep cannot falsify (the previous default
was opt-OUT and grepped fine).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(src, name):
    """Return the full source of a top-level `function name(...)` in boot.js."""
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


_HARNESS = r"""
const _MIC_AUTOSTOP_KEY = 'hermes-mic-auto-stop';
const store = {};
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
__FUNCSIF__
const out = {};
out.absent = _micAutoStopEnabled();
store[_MIC_AUTOSTOP_KEY] = '0';
out.explicitZero = _micAutoStopEnabled();
store[_MIC_AUTOSTOP_KEY] = 'false';
out.explicitFalse = _micAutoStopEnabled();
store[_MIC_AUTOSTOP_KEY] = 'true';
out.explicitTrue = _micAutoStopEnabled();
delete store[_MIC_AUTOSTOP_KEY];
globalThis.localStorage.getItem = () => { throw new Error('storage blocked'); };
out.blockedStorage = _micAutoStopEnabled();
console.log(JSON.stringify(out));
"""


def _run():
    js = _HARNESS.replace("__FUNCSIF__", _extract_function(BOOT_JS, "_micAutoStopEnabled"))
    proc = subprocess.run(
        [NODE, "-e", js], capture_output=True, text=True, cwd=ROOT, timeout=60
    )
    assert proc.returncode == 0, "node harness failed:\n%s" % proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def runs():
    if NODE is None:
        pytest.skip("node not on PATH")
    return _run()


def test_default_is_off(runs):
    assert runs["absent"] is False, "auto-stop must be off when the preference was never set"
    assert runs["explicitZero"] is False
    assert runs["explicitFalse"] is False


def test_opt_in_arms_the_rule(runs):
    assert runs["explicitTrue"] is True


def test_blocked_storage_degrades_to_manual_stop(runs):
    assert runs["blockedStorage"] is False, (
        "a blocked localStorage must not silently turn auto-stop ON"
    )
