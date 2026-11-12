"""Execute the shipped viewport consumers, not a Python copy of their math.

These browserless cases reproduce the measured fold-transition geometry. They
prove style/bounds decisions, not WebKit event delivery or actual PWA painting.
"""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function(source, name):
    match = re.search(r"^function " + re.escape(name) + r"\([^\n]*\)\s*\{.*?^\}", source, re.M | re.S)
    assert match, f"Missing actual function {name}"
    return match.group(0)


def _run(body):
    assert NODE is not None
    boot = (ROOT / "static/boot.js").read_text(encoding="utf-8")
    sessions = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
    # The baseline predates this helper. Do not supply a replacement there:
    # execute its original consumer unchanged for the failing-before proof.
    helper = (_function(boot, "_plausibleVisualViewport")
              if "function _plausibleVisualViewport(" in boot else "")
    source = r"""
const window = {innerHeight: 678, innerWidth: 390};
const properties = new Map([['--keyboard-bottom-inset', '328px']]);
const document = {documentElement: {style: {
  setProperty: (key, value) => properties.set(key, value),
  removeProperty: key => properties.delete(key),
}}};
const matchMedia = () => ({matches: true});
const _hasFinePointerCoexisting = () => false;
function sample(geometry) {
  window.visualViewport = geometry;
  _syncKeyboardBottomInset();
  return {inset: properties.get('--keyboard-bottom-inset') || '',
          bounds: _projectPickerVisibleBounds()};
}
""" + "\n".join([
        helper,
        _function(boot, "_isTouchKeyboardViewport"),
        _function(boot, "_syncKeyboardBottomInset"),
        _function(sessions, "_projectPickerVisibleBounds"),
        body,
    ])
    result = subprocess.run([NODE, "-e", source], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


FALLBACK = {"top": 0, "left": 0, "bottom": 678, "right": 390}


def test_measured_negative_height_clears_keyboard_inset():
    result = _run("console.log(JSON.stringify(sample({height:-33554432,width:390,offsetTop:9,offsetLeft:0,scale:1}))); ")
    assert result["inset"] == "", result
    assert result["bounds"] == FALLBACK


@pytest.mark.parametrize("field,value", [
    ("height", "0"), ("height", "NaN"), ("height", "Infinity"),
    ("height", "-Infinity"), ("height", "1200"),
    ("width", "0"), ("width", "-1"), ("width", "NaN"),
    ("width", "Infinity"), ("width", "-Infinity"), ("width", "900"),
])
def test_invalid_geometry_clears_existing_inset_and_picker_falls_back(field, value):
    result = _run(f"console.log(JSON.stringify(sample({{height:350,width:390,offsetTop:9,offsetLeft:5,scale:1,{field}:{value}}}))); ")
    assert result == {"inset": "", "bounds": FALLBACK}


def test_keyboard_and_fold_recovery_use_fresh_geometry():
    result = _run("""
const vv = {height:350,width:390,offsetTop:0,offsetLeft:0,scale:1};
const results = [sample(vv)];
vv.height = -33554432; vv.offsetTop = 9; results.push(sample(vv));
vv.height = 350; vv.offsetTop = 0; results.push(sample(vv));
vv.height = 678; results.push(sample(vv));
console.log(JSON.stringify(results));
""")
    assert [item["inset"] for item in result] == ["328px", "", "328px", ""]
    assert [item["bounds"] for item in result] == [
        {"top": 0, "left": 0, "bottom": 350, "right": 390}, FALLBACK,
        {"top": 0, "left": 0, "bottom": 350, "right": 390}, FALLBACK,
    ]


def test_normal_offset_keyboard_geometry_is_not_discarded():
    result = _run("console.log(JSON.stringify(sample({height:350,width:380,offsetTop:9,offsetLeft:5,scale:1}))); ")
    assert result == {"inset": "319px", "bounds": {"top": 9, "left": 5, "bottom": 359, "right": 385}}


@pytest.mark.parametrize("scale", [0.8, 1.2, 2])
def test_pinch_zoom_suppresses_keyboard_inset_but_preserves_picker_bounds(scale):
    result = _run(f"console.log(JSON.stringify(sample({{height:300,width:195,offsetTop:40,offsetLeft:20,scale:{scale}}}))); ")
    assert result == {"inset": "", "bounds": {"top": 40, "left": 20, "bottom": 340, "right": 215}}


def test_missing_visual_viewport_clears_inset_and_uses_window():
    assert _run("console.log(JSON.stringify(sample(null)));") == {"inset": "", "bounds": FALLBACK}
