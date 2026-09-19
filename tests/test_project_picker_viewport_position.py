"""Browserless behavioral regression for the single-session project picker.

The real ``_showProjectPicker`` function is executed in Node with a minimal DOM
fixture.  The fixture gives the picker a measured rendered height and a mutable
viewport/anchor geometry, so the test observes the final fixed-position styles
and the picker's listener lifecycle rather than checking for implementation
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
    this.naturalHeight = 0;
    this.isConnected = false;
    this.removed = false;
    this.parent = null;
  }
  appendChild(child) { child.parent = this; child.isConnected = true; this.children.push(child); return child; }
  remove() {
    this.removed = true;
    this.isConnected = false;
    if (this.parent) {
      const i = this.parent.children.indexOf(this);
      if (i >= 0) this.parent.children.splice(i, 1);
      this.parent = null;
    }
  }
  contains(target) { return target === this || this.children.includes(target); }
  get offsetHeight() {
    const cap = Number.parseFloat(this.style.maxHeight);
    const natural = this.naturalHeight;
    return Number.isFinite(cap) && cap > 0 ? Math.min(natural, cap) : natural;
  }
}

function makeEmitter(bucket) {
  return {
    addEventListener(type, fn) { (bucket[type] = bucket[type] || []).push(fn); },
    removeEventListener(type, fn) { bucket[type] = (bucket[type] || []).filter(f => f !== fn); },
    dispatch(type, event) { (bucket[type] || []).slice().forEach(fn => fn(event || {type})); },
    count() { return Object.keys(bucket).reduce((n, k) => n + bucket[k].length, 0); },
  };
}

const listenerBuckets = {window: {}, visualViewport: {}, document: {}};
const windowEmitter = makeEmitter(listenerBuckets.window);
const vvEmitter = makeEmitter(listenerBuckets.visualViewport);
const docEmitter = makeEmitter(listenerBuckets.document);

let mountedPicker = null;
let nextPickerHeight = 0;

const documentBody = {
  children: [],
  appendChild(el) {
    if (el.className === 'project-picker') {
      el.naturalHeight = nextPickerHeight;
      mountedPicker = el;
    }
    el.parent = this;
    el.isConnected = true;
    this.children.push(el);
    return el;
  },
  contains(el) { return this.children.includes(el); },
};

const document = {
  querySelectorAll() { return []; },
  createElement(tag) { return new FakeElement(tag); },
  addEventListener: docEmitter.addEventListener,
  removeEventListener: docEmitter.removeEventListener,
  body: documentBody,
};

const viewport = {width: 1440, height: 900, offsetTop: 0, offsetLeft: 0};
const visualViewport = {
  get width() { return viewport.width; },
  get height() { return viewport.height; },
  get offsetTop() { return viewport.offsetTop; },
  get offsetLeft() { return viewport.offsetLeft; },
  addEventListener: vvEmitter.addEventListener,
  removeEventListener: vvEmitter.removeEventListener,
};
const window = {
  innerWidth: 1440,
  innerHeight: 900,
  visualViewport,
  addEventListener: windowEmitter.addEventListener,
  removeEventListener: windowEmitter.removeEventListener,
};

const _allProjects = Array.from({length: 12}, (_, i) => ({
  project_id: `p${i}`,
  name: `Project ${i}`,
  profile: 'default',
  color: '#7cb9ff',
}));
const _allSessions = [];
const api = async () => ({});
const showToast = () => {};
const showPromptDialog = async () => null;
const renderSessionList = async () => {};
const renderSessionListFromCache = () => {};
const t = key => key;
const setTimeout = fn => { fn(); return 0; };
const requestAnimationFrame = fn => { fn(); return 0; };

// Module-scope teardown hook declared just above _showProjectPicker in
// static/sessions.js; the extracted function body assigns it.
let _projectPickerTeardown = null;

let anchorRect = {top: 680, bottom: 720, left: 410, right: 440, width: 30, height: 40};
let anchorConnected = true;
const anchorEl = {
  get isConnected() { return anchorConnected; },
  getBoundingClientRect: () => anchorRect,
};
const session = {session_id: 'session-a', project_id: null, profile: 'default'};

function setViewport(height, width) {
  window.innerHeight = height;
  viewport.height = height;
  if (width) { window.innerWidth = width; viewport.width = width; }
}

function setAnchor(rect) { anchorRect = Object.assign({width: 30, height: 40}, rect); }

function openPicker(naturalHeight) {
  mountedPicker = null;
  nextPickerHeight = naturalHeight;
  _showProjectPicker(session, anchorEl);
  if (!mountedPicker) throw new Error('project picker was not mounted');
  return mountedPicker;
}

function placement() {
  const el = mountedPicker;
  const rawCap = el.style.maxHeight;
  const maxHeight = rawCap && rawCap !== 'none' ? Number.parseFloat(rawCap) : null;
  const renderedHeight = maxHeight === null ? el.naturalHeight : Math.min(el.naturalHeight, maxHeight);
  const topStyle = el.style.top || '';
  const bottomStyle = el.style.bottom || '';
  const top = topStyle && topStyle !== 'auto'
    ? Number.parseFloat(topStyle)
    : window.innerHeight - Number.parseFloat(bottomStyle) - renderedHeight;
  const bottom = top + renderedHeight;
  const overlaps = top < anchorRect.bottom && bottom > anchorRect.top;
  const gap = overlaps ? null : (top >= anchorRect.bottom ? top - anchorRect.bottom : anchorRect.top - bottom);
  return {
    top,
    bottom,
    topStyle,
    bottomStyle,
    maxHeight,
    renderedHeight,
    overflowY: el.style.overflowY || '',
    gap,
    overlaps,
    removed: el.removed,
    listenerCounts: {
      window: windowEmitter.count(),
      visualViewport: vvEmitter.count(),
      document: docEmitter.count(),
    },
  };
}

function dispatchViewportChange() {
  windowEmitter.dispatch('resize');
  vvEmitter.dispatch('resize');
  vvEmitter.dispatch('scroll');
}
"""

_DRIVER_SUFFIX = r"""
function runCase(rect, pickerHeight) {
  setViewport(900);
  setAnchor(rect);
  anchorConnected = true;
  openPicker(pickerHeight);
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

// ── Resize lifecycle: a short picker whose trigger moves on viewport growth ──
setViewport(700, 900);
setAnchor({top: 560, bottom: 600, left: 410, right: 440});
openPicker(88);
const shortBefore = placement();
if (shortBefore.top >= anchorRect.bottom) {
  // Trigger slides down with the viewport, as in a bottom-fixed session row.
  setAnchor({top: 760, bottom: 800, left: 410, right: 440});
  setViewport(900, 900);
  dispatchViewportChange();
}
const shortAfterResize = placement();

// ── Resize lifecycle: a tall clamped picker across a desktop→mobile resize ──
setViewport(900, 1440);
setAnchor({top: 450, bottom: 490, left: 410, right: 440});
openPicker(760);
const tallDesktop = placement();
setViewport(844, 390);
setAnchor({top: 400, bottom: 440, left: 30, right: 60});
dispatchViewportChange();
const tallMobile = placement();

// ── Detached anchor closes the picker and drops every listener ──
setViewport(900, 1440);
setAnchor({top: 100, bottom: 140, left: 410, right: 440});
openPicker(260);
anchorConnected = false;
dispatchViewportChange();
const detachedAnchor = placement();
let detachedResizeThrew = false;
try {
  dispatchViewportChange();
} catch (err) {
  detachedResizeThrew = String(err);
}

// ── Selection, outside click and replacement all run the same teardown ──
setViewport(900, 1440);
setAnchor({top: 100, bottom: 140, left: 410, right: 440});
anchorConnected = true;
openPicker(260);
const onSelectionPicker = mountedPicker;
const projectItem = onSelectionPicker.children[2];
projectItem.onclick();
const afterSelection = placement();
let selectionResizeThrew = false;
try {
  dispatchViewportChange();
} catch (err) {
  selectionResizeThrew = String(err);
}

openPicker(260);
docEmitter.dispatch('click', {target: {}});
const afterOutsideClick = placement();

anchorConnected = true;
openPicker(260);
const firstPicker = mountedPicker;
anchorConnected = true;
openPicker(260);
const replacement = {
  firstPickerRemoved: firstPicker.removed,
  firstPickerDisconnected: firstPicker.isConnected === false,
  listenerCounts: placement().listenerCounts,
  mountedIsNewest: mountedPicker !== firstPicker,
};

console.log(JSON.stringify({
  results,
  shortBefore,
  shortAfterResize,
  tallDesktop,
  tallMobile,
  detachedAnchor,
  detachedResizeThrew,
  afterSelection,
  selectionResizeThrew,
  afterOutsideClick,
  replacement,
}));
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
    cases = _run_picker_cases()["results"]

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


def test_project_picker_stays_attached_when_its_trigger_moves_on_viewport_growth():
    data = _run_picker_cases()

    before = data["shortBefore"]
    assert before["gap"] == 4, (
        "A short picker must start the required 4px off its trigger; got "
        f"{before!r}"
    )

    after = data["shortAfterResize"]
    assert after["removed"] is False, (
        "Growing the viewport must not dispose a picker whose anchor is still "
        "mounted."
    )
    assert after["overlaps"] is False, (
        "After the trigger moves down 200px the picker overlaid its own row, the "
        "detachment regression the gate reproduced."
    )
    assert after["gap"] == 4, (
        "The picker must be repositioned against the live anchor rect, keeping a "
        f"4px gap; got {after['gap']} ({after!r})"
    )
    assert after["bottom"] <= 892, "Repositioned picker must stay inside the viewport."


def test_project_picker_recomputes_its_height_cap_across_desktop_to_mobile_resize():
    data = _run_picker_cases()

    desktop = data["tallDesktop"]
    assert desktop["maxHeight"] is not None and desktop["overflowY"] == "auto", (
        "A tall picker on a desktop viewport must be clamped and scrollable."
    )
    desktop_cap = desktop["maxHeight"]

    mobile = data["tallMobile"]
    assert mobile["maxHeight"] is not None, (
        "The mobile viewport is smaller, so the picker must stay height-limited."
    )
    assert mobile["maxHeight"] != desktop_cap, (
        "A resize must remeasure the cap; reusing the desktop clamp is the stale "
        f"max-height bug ({desktop_cap} kept on a smaller viewport)."
    )
    assert mobile["bottom"] <= 844 - 8, (
        f"Repositioned picker escaped the mobile viewport: {mobile!r}"
    )
    assert mobile["overlaps"] is False


def test_project_picker_closes_when_its_anchor_is_detached():
    data = _run_picker_cases()

    detached = data["detachedAnchor"]
    assert detached["removed"] is True, (
        "An unmounted anchor must close the picker instead of retaining stale "
        "geometry on screen."
    )
    assert detached["listenerCounts"] == {
        "window": 0,
        "visualViewport": 0,
        "document": 0,
    }, f"Teardown leaked listeners: {detached['listenerCounts']}"
    assert data["detachedResizeThrew"] is False, (
        "A later viewport change must not reach a disposed picker."
    )


def test_project_picker_teardown_runs_on_selection_outside_click_and_replacement():
    data = _run_picker_cases()

    expected = {"window": 0, "visualViewport": 0, "document": 0}
    for label in ("afterSelection", "afterOutsideClick"):
        snapshot = data[label]
        assert snapshot["removed"] is True, f"{label}: picker was not removed"
        assert snapshot["listenerCounts"] == expected, (
            f"{label}: teardown leaked listeners {snapshot['listenerCounts']}"
        )
    assert data["selectionResizeThrew"] is False

    replacement = data["replacement"]
    assert replacement["mountedIsNewest"] is True
    assert replacement["firstPickerRemoved"] is True, (
        "Opening a second picker must retire the first one."
    )
    assert replacement["firstPickerDisconnected"] is True
    assert replacement["listenerCounts"] == {
        "window": 1,
        "visualViewport": 2,
        "document": 1,
    }, (
        "Exactly one picker's listeners may be live after a replacement; got "
        f"{replacement['listenerCounts']}"
    )
