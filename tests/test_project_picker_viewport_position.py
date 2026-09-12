"""Browserless behavioral regression for the single-session project picker.

The real ``_showProjectPicker`` function is executed in Node with a minimal DOM
fixture.  The fixture gives the picker a measured rendered height, so the test
observes the final fixed-position styles rather than checking for implementation
strings.
"""

from pathlib import Path
import json
import shutil
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _show_project_picker_source() -> str:
    start = SESSIONS_JS.find("function _showProjectPicker(")
    assert start >= 0, "_showProjectPicker not found in static/sessions.js"
    end = SESSIONS_JS.find("function _resizeProjectInput(", start)
    assert end > start, "_resizeProjectInput sentinel not found after picker"
    return SESSIONS_JS[start:end]


_DRIVER_PREFIX = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.style = {};
    this.className = '';
    this.textContent = '';
    this.scrollWidth = 180;
    this.offsetHeight = 0;
  }
  appendChild(child) { this.children.push(child); return child; }
  remove() { this.removed = true; }
  contains(target) { return target === this || this.children.includes(target); }
}

let mountedPicker = null;
let nextPickerHeight = 0;
const document = {
  querySelectorAll() { return []; },
  createElement(tag) { return new FakeElement(tag); },
  addEventListener() {},
  removeEventListener() {},
  body: {
    appendChild(el) {
      if (el.className === 'project-picker') {
        el.offsetHeight = nextPickerHeight;
        mountedPicker = el;
      }
      return el;
    },
  },
};
const window = {innerWidth: 1440, innerHeight: 900};
const _allProjects = Array.from({length: 12}, (_, i) => ({
  project_id: `p${i}`,
  name: `Project ${i}`,
  profile: 'default',
  color: '#7cb9ff',
}));
const setTimeout = fn => { fn(); return 0; };
"""

_DRIVER_SUFFIX = r"""
function runCase(rect, pickerHeight) {
  mountedPicker = null;
  nextPickerHeight = pickerHeight;
  const anchor = {getBoundingClientRect: () => rect};
  _showProjectPicker(
    {session_id: 'session-a', project_id: null, profile: 'default'},
    anchor,
  );
  if (!mountedPicker) throw new Error('project picker was not mounted');
  const style = mountedPicker.style;
  const maxHeight = style.maxHeight && style.maxHeight !== 'none'
    ? Number.parseFloat(style.maxHeight)
    : null;
  const renderedHeight = maxHeight === null ? pickerHeight : Math.min(pickerHeight, maxHeight);
  const topStyle = style.top || '';
  const bottomStyle = style.bottom || '';
  const top = topStyle && topStyle !== 'auto'
    ? Number.parseFloat(topStyle)
    : window.innerHeight - Number.parseFloat(bottomStyle) - renderedHeight;
  const resizeDelta = 200;
  const resizedTop = topStyle === 'auto'
    ? (window.innerHeight + resizeDelta) - Number.parseFloat(bottomStyle) - renderedHeight
    : top;
  const resizedAnchorTop = rect.top + resizeDelta;
  return {
    top,
    topStyle,
    bottomStyle,
    resizeGap: resizedAnchorTop - (resizedTop + renderedHeight),
    maxHeight,
    overflowY: style.overflowY || '',
    renderedBottom: top + renderedHeight,
  };
}

const results = {
  screenshotLike: runCase({top: 680, bottom: 720, left: 410, right: 440}, 260),
  roomBelow: runCase({top: 100, bottom: 140, left: 410, right: 440}, 260),
  neitherSideFits: runCase({top: 450, bottom: 490, left: 410, right: 440}, 760),
};
console.log(JSON.stringify(results));
"""


def _run_picker_cases() -> dict:
    assert NODE is not None
    script = _DRIVER_PREFIX + _show_project_picker_source() + _DRIVER_SUFFIX
    result = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_project_picker_uses_its_rendered_height_to_stay_in_viewport():
    cases = _run_picker_cases()

    screenshot_like = cases["screenshotLike"]
    assert screenshot_like["top"] < 680, (
        "A 260px picker anchored near the bottom must flip above the session row; "
        "the old fixed 160px threshold incorrectly opens it below."
    )
    assert screenshot_like["renderedBottom"] <= 892
    assert screenshot_like["topStyle"] == "auto"
    assert screenshot_like["bottomStyle"] == "224px"
    assert screenshot_like["resizeGap"] == 4

    room_below = cases["roomBelow"]
    assert room_below["top"] == 144
    assert room_below["renderedBottom"] <= 892

    clamped = cases["neitherSideFits"]
    assert clamped["maxHeight"] is not None, (
        "When neither side can fit the natural picker height, the picker must be "
        "height-limited instead of extending beyond the viewport."
    )
    assert clamped["overflowY"] == "auto"
    assert clamped["top"] >= 8
    assert clamped["renderedBottom"] <= 892
