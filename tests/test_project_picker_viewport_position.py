"""Browserless behavioral regression for the single-session project picker.

The real ``_showProjectPicker`` function is executed in Node with a minimal DOM
fixture.  The fixture gives the picker a measured rendered height and a mutable
viewport/anchor geometry, so the test observes the final fixed-position styles
and the picker's listener lifecycle rather than checking for implementation
strings.
"""

from pathlib import Path
import json
import re
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
  setAttribute(name, value) { this.attrs = this.attrs || {}; this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs && Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  addEventListener(type, fn) { this.listeners = this.listeners || {}; (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) { const list = this.listeners && this.listeners[type]; if (list) this.listeners[type] = list.filter(f => f !== fn); }
  querySelector(selector) {
    if (selector === '.project-picker-item.active' || selector === '.project-picker-item')
      return {getBoundingClientRect: () => ({top: 0, bottom: 0, left: 0, right: 0}), scrollIntoView() {}};
    return null;
  }
  querySelectorAll() { return []; }
  focus() { this.focused = true; }
  scrollIntoView() {}
  getBoundingClientRect() { return {top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0}; }
  get clientTop() { return 0; }
  get clientHeight() { return 0; }
  get offsetHeight() {
    const cap = Number.parseFloat(this.style.maxHeight);
    const natural = this.naturalHeight;
    return Number.isFinite(cap) ? Math.min(natural, cap) : natural;
  }
  get offsetWidth() {
    const cap = Number.parseFloat(this.style.maxWidth);
    return Number.isFinite(cap) ? Math.min(this.scrollWidth, cap) : this.scrollWidth;
  }
}

function makeEmitter(bucket) {
  return {
    addEventListener(type, fn, options) { (bucket[type] = bucket[type] || []).push({fn, capture: options === true || !!(options && options.capture)}); },
    removeEventListener(type, fn, options) {
      const capture = options === true || !!(options && options.capture);
      bucket[type] = (bucket[type] || []).filter(f => f.fn !== fn || f.capture !== capture);
    },
    dispatch(type, event, captureOnly = false) {
      (bucket[type] || []).slice().forEach(f => {
        if (!captureOnly || f.capture) f.fn(event || {type});
      });
    },
    count() { return Object.keys(bucket).reduce((n, k) => n + bucket[k].length, 0); },
  };
}

const listenerBuckets = {window: {}, visualViewport: {}, document: {}};
const windowEmitter = makeEmitter(listenerBuckets.window);
const vvEmitter = makeEmitter(listenerBuckets.visualViewport);
const docEmitter = makeEmitter(listenerBuckets.document);
let listRect = {top: 0, bottom: 900, left: 0, right: 1440};
const sessionList = {
  getBoundingClientRect: () => listRect,
  contains: target => target === anchorEl,
  // Element scroll does not bubble; only a capture listener on document sees it.
  scroll() { docEmitter.dispatch('scroll', {target: sessionList}, true); },
};
const observers = new Set();
class MutationObserver {
  constructor(callback) { this.callback = callback; }
  observe(target, options) { this.target = target; this.options = options; observers.add(this); }
  disconnect() { observers.delete(this); }
}
function flushMutations() {
  for (const observer of [...observers]) {
    if (observer.options.childList && observer.options.subtree) observer.callback([{target: sessionList}]);
  }
}

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
  querySelector() { return currentSessionRow; },
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
let currentSessionRow = null;
const apiCalls = [];
const api = async (url, opts) => { apiCalls.push({url: String(url), body: opts && opts.body}); return {}; };
const showToast = () => {};
const showPromptDialog = async () => null;
const renderSessionList = async () => {};
let repaints = 0;
let onRepaint = null;
let cachedLabels = {parent: 'old parent', child: 'old child'};
let paintedLabels = {...cachedLabels};
const renderSessionListFromCache = () => {
  repaints += 1;
  paintedLabels = {...cachedLabels};
  if (onRepaint) onRepaint();
};
const t = key => key;
let nextTask = 0;
const frames = new Map();
const timers = new Map();
const setTimeout = (fn, delay = 0) => { const id = ++nextTask; timers.set(id, {fn, delay: Number(delay) || 0}); return id; };
const clearTimeout = id => timers.delete(id);
const requestAnimationFrame = fn => { const id = ++nextTask; frames.set(id, fn); return id; };
const cancelAnimationFrame = id => frames.delete(id);
function flushFrames() { const batch = [...frames.values()]; frames.clear(); batch.forEach(fn => fn()); }
function flushTimers() { const batch = [...timers.values()]; timers.clear(); batch.forEach(entry => entry.fn()); }
function flushTimersDue(maxDelay) {
  for (const [id, entry] of [...timers.entries()]) {
    if (entry.delay <= maxDelay) { timers.delete(id); entry.fn(); }
  }
}

// Module-scope teardown hook declared just above _showProjectPicker in
// static/sessions.js; the extracted function body assigns it.
let _projectPickerTeardown = null;

// Module-scope helpers the merged picker section reaches through the extracted
// chunk: the resize hook re-finds a repainted row, and dismiss()/focus helpers
// hand focus back through these names.
function _findSessionRenameRow() { return null; }
function _focusSessionActionMenuRestoreTarget(target) {
  if(!target || typeof target.focus !== 'function') return false;
  target.focus({preventScroll:true});
  return document.activeElement === target;
}

let anchorRect = {top: 680, bottom: 720, left: 410, right: 440, width: 30, height: 40};
let anchorConnected = true;
const anchorEl = {
  get isConnected() { return anchorConnected; },
  getBoundingClientRect: () => anchorRect,
  closest: selector => selector === '.session-list' ? sessionList : null,
  contains: target => target === anchorEl,
  offsetParent: {},
  querySelector: () => null,
};
const session = {session_id: 'session-a', project_id: null, profile: 'default'};

function setViewport(height, width) {
  window.innerHeight = height;
  viewport.height = height;
  listRect.bottom = height;
  if (width) { window.innerWidth = width; viewport.width = width; }
  listRect.right = window.innerWidth;
}

function setAnchor(rect) { anchorRect = Object.assign({width: 30, height: 40}, rect); }

function openPicker(naturalHeight) {
  mountedPicker = null;
  nextPickerHeight = naturalHeight;
  _showProjectPicker(session, anchorEl);
  flushTimers();
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
    left: Number.parseFloat(el.style.left),
    right: Number.parseFloat(el.style.left) + el.offsetWidth,
    width: el.offsetWidth,
    observers: observers.size,
    frames: frames.size,
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
  flushFrames();
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
openPicker(1200);
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


def _run_picker_cases(suffix: str = _DRIVER_SUFFIX) -> dict:
    assert NODE is not None
    script = _DRIVER_PREFIX + _show_project_picker_source() + suffix
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
        "window": 1,
        "visualViewport": 0,
        "document": 0,
    }, f"Teardown leaked listeners: {detached['listenerCounts']}"
    assert data["detachedResizeThrew"] is False, (
        "A later viewport change must not reach a disposed picker."
    )


def test_project_picker_teardown_runs_on_selection_outside_click_and_replacement():
    data = _run_picker_cases()

    expected = {"window": 1, "visualViewport": 0, "document": 0}
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
        "window": 2,
        "visualViewport": 2,
        "document": 4,
    }, (
        "Exactly one picker's listeners may be live after a replacement; got "
        f"{replacement['listenerCounts']}"
    )


@pytest.mark.parametrize("offset_top", [0, 100])
@pytest.mark.parametrize("picker_height", [120, 760])
def test_visual_viewport_alone_changes_placement(offset_top, picker_height):
    data = _run_picker_cases(f"""
setAnchor({{top: 450, bottom: 490, left: 410, right: 440}});
openPicker({picker_height});
viewport.height = 500;
viewport.offsetTop = {offset_top};
vvEmitter.dispatch('resize');
flushFrames();
console.log(JSON.stringify({{...placement(), layoutHeight: window.innerHeight}}));
""")
    assert data["layoutHeight"] == 900, "Only the visual viewport may change."
    assert not data["removed"]
    assert data["top"] >= offset_top + 8
    assert data["bottom"] <= offset_top + 500 - 8
    assert data["gap"] == 4


@pytest.mark.parametrize("anchor_left", [100, 270])
@pytest.mark.parametrize("viewport_width", [120, 220])
def test_visual_viewport_horizontal_edges_and_width_cap(anchor_left, viewport_width):
    data = _run_picker_cases(f"""
setAnchor({{top: 100, bottom: 140, left: {anchor_left}, right: {anchor_left + 30}}});
openPicker(260);
viewport.offsetLeft = 100;
viewport.width = {viewport_width};
vvEmitter.dispatch('scroll');
flushFrames();
console.log(JSON.stringify(placement()));
""")
    assert not data["removed"], (
        "A visual-viewport scroll re-places the picker; it must stay on screen "
        "and inside the bounds instead of closing."
    )
    assert data["left"] >= 108
    assert data["right"] <= 100 + viewport_width - 8
    assert data["top"] >= 8


def test_anchor_hidden_by_keyboard_repositions_without_layout_resize():
    data = _run_picker_cases("""
openPicker(260);
viewport.height = 500;
vvEmitter.dispatch('resize');
flushFrames();
const parked = placement();
docEmitter.dispatch('click', {target: {}});
flushTimers();
console.log(JSON.stringify({parked, closed: placement()}));
""")
    parked = data["parked"]
    assert not parked["removed"], (
        "A keyboard-shrunk visual viewport re-places the picker instead of "
        "closing it."
    )
    assert parked["top"] >= 8
    assert parked["bottom"] <= 492, "The picker must stay inside the visual viewport."
    closed = data["closed"]
    assert closed["removed"]
    assert closed["listenerCounts"] == {"window": 1, "visualViewport": 0, "document": 0}
    assert closed["observers"] == 0


def test_coarse_pointer_context_menu_uses_visible_row_for_zero_size_anchor():
    data = _run_picker_cases("""
const rowRect = {top: 500, bottom: 548, left: 16, right: 374, width: 358, height: 48};
const visibleRow = {
  isConnected: true,
  getBoundingClientRect: () => rowRect,
};
anchorEl.closest = selector => selector === '.session-child-session-fork,.session-item'
  ? visibleRow
  : (selector === '.session-list' ? sessionList : null);
// Touch-primary CSS hides `.session-actions`; the real row context-menu path
// still passes it as the project-picker anchor.
setViewport(620, 390);
setAnchor({top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0});
openPicker(260);
const state = placement();
const rowGap = state.top >= rowRect.bottom
  ? state.top - rowRect.bottom
  : rowRect.top - state.bottom;
console.log(JSON.stringify({...state, rowGap}));
""")
    assert data["removed"] is False
    assert data["rowGap"] == 4
    assert data["top"] >= 8
    assert data["bottom"] <= 612


def test_coarse_pointer_expanded_parent_uses_context_point_and_tracks_row():
    data = _run_picker_cases("""
const expandedRow = {
  isConnected: true,
  rect: {top: -208, bottom: 902, left: 16, right: 374, width: 358, height: 1110},
  getBoundingClientRect() { return this.rect; },
};
anchorEl.closest = selector => selector === '.session-child-session-fork,.session-item'
  ? expandedRow
  : (selector === '.session-list' ? sessionList : null);
// The hidden actions element remembers the real mouse context-menu point.
anchorEl._projectPickerContextPoint = {clientX: 310, clientY: 420};
setViewport(620, 390);
setAnchor({top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0});
openPicker(260);
const opened = placement();
// Once the owning expanded row leaves the list, the stored point must not keep
// the picker alive at stale viewport coordinates.
expandedRow.rect = {top: -1200, bottom: -90, left: 16, right: 374, width: 358, height: 1110};
sessionList.scroll();
flushFrames();
console.log(JSON.stringify({opened, afterRowLeaves: placement()}));
""")
    assert data["opened"]["removed"] is False
    assert data["opened"]["top"] >= 8
    assert data["opened"]["bottom"] <= 612
    assert data["afterRowLeaves"]["removed"] is True


def test_coarse_pointer_fork_child_uses_nearest_child_before_tall_parent():
    data = _run_picker_cases("""
const forkChild = {
  isConnected: true,
  rect: {top: 329, bottom: 357, left: 28, right: 374, width: 346, height: 28},
  getBoundingClientRect() { return this.rect; },
};
const expandedParent = {
  isConnected: true,
  getBoundingClientRect: () => ({
    top: -260, bottom: 1009, left: 16, right: 374, width: 358, height: 1269,
  }),
};
anchorEl.closest = selector => {
  if (selector === '.session-child-session-fork,.session-item') return forkChild;
  if (selector === '.session-item') return expandedParent;
  if (selector === '.session-list') return sessionList;
  return null;
};
anchorEl._projectPickerContextPoint = {clientX: 310, clientY: 356};
setViewport(620, 390);
setAnchor({top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0});
openPicker(260);
const opened = placement();
forkChild.rect = {top: -80, bottom: -52, left: 28, right: 374, width: 346, height: 28};
sessionList.scroll();
flushFrames();
console.log(JSON.stringify({opened, afterChildLeaves: placement()}));
""")
    assert data["opened"]["removed"] is False
    assert data["opened"]["top"] >= 8
    assert data["opened"]["bottom"] <= 612
    assert data["afterChildLeaves"]["removed"] is True


def test_session_list_scroll_repositions_then_closes_at_container_edge():
    data = _run_picker_cases("""
listRect = {top: 200, bottom: 650, left: 0, right: 500};
setAnchor({top: 400, bottom: 440, left: 410, right: 440});
openPicker(120);
setAnchor({top: 300, bottom: 340, left: 410, right: 440});
sessionList.scroll();
flushFrames();
const moved = placement();
// Still inside the WINDOW, but fully clipped by the scroll container.
setAnchor({top: 150, bottom: 190, left: 410, right: 440});
sessionList.scroll();
flushFrames();
console.log(JSON.stringify({moved, clipped: placement()}));
""")
    assert data["moved"]["gap"] == 4
    assert not data["moved"]["removed"]
    assert data["clipped"]["removed"]
    assert data["clipped"]["observers"] == 0
    assert data["clipped"]["listenerCounts"] == {"window": 1, "visualViewport": 0, "document": 0}


def test_sidebar_render_closes_disconnected_anchor_without_viewport_event():
    data = _run_picker_cases("""
openPicker(260);
anchorConnected = false;
flushMutations();
console.log(JSON.stringify(placement()));
""")
    assert data["removed"], "Sidebar replacement must close before a later resize/scroll."
    assert data["observers"] == 0
    assert data["listenerCounts"] == {"window": 1, "visualViewport": 0, "document": 0}


@pytest.mark.parametrize("exit_action", [
    "mountedPicker.children[0].onclick()",
    "mountedPicker.children[2].onclick()",
    "mountedPicker.children.at(-1).onclick()",  # cancelled New project dialog
    "docEmitter.dispatch('click', {target: {}})",
    "anchorConnected = false; flushMutations()",
])
def test_teardown_cancels_queued_frame_and_disconnects_observer(exit_action):
    data = _run_picker_cases(f"""
openPicker(260);
windowEmitter.dispatch('resize');
vvEmitter.dispatch('resize');
const queued = frames.size;
{exit_action};
flushFrames();
flushFrames();
flushTimers();
console.log(JSON.stringify({{queued, after: placement()}}));
""")
    # One coalesced frame for the picker's own listeners and one for the
    # module-level resize hook; neither may outlive the closed picker.
    assert data["queued"] == 2, "Each resize listener coalesces into one frame."
    assert data["after"]["removed"]
    assert data["after"]["frames"] == 0
    assert data["after"]["observers"] == 0
    assert data["after"]["listenerCounts"] == {"window": 1, "visualViewport": 0, "document": 0}


def test_replacement_retires_pending_work_and_preserves_new_owner():
    data = _run_picker_cases("""
openPicker(260);
windowEmitter.dispatch('resize');
const oldPicker = mountedPicker;
openPicker(120);
flushFrames();
flushMutations();
console.log(JSON.stringify({oldRemoved: oldPicker.removed, ...placement()}));
""")
    assert data["oldRemoved"]
    assert not data["removed"]
    assert data["frames"] == 0
    assert data["observers"] == 1
    assert data["listenerCounts"] == {"window": 2, "visualViewport": 2, "document": 4}


def test_picker_scroll_does_not_schedule_reposition_or_close():
    data = _run_picker_cases("""
openPicker(1200);
docEmitter.dispatch('scroll', {target: mountedPicker}, true);
console.log(JSON.stringify(placement()));
""")
    assert not data["removed"]
    assert data["frames"] == 0
    assert data["overflowY"] == "auto"


def test_window_dimensions_remain_the_fallback_without_visual_viewport():
    data = _run_picker_cases("""
window.visualViewport = null;
setAnchor({top: 680, bottom: 720, left: 410, right: 440});
openPicker(260);
console.log(JSON.stringify(placement()));
""")
    assert not data["removed"]
    assert data["gap"] == 4
    assert data["top"] >= 8
    assert data["bottom"] <= 892
    assert data["left"] == 260, "Fallback placement must align the picker's right edge to its anchor."
    assert data["right"] == 440


def test_window_fallback_caps_width_and_contains_narrow_picker():
    data = _run_picker_cases("""
window.visualViewport = null;
setViewport(500, 120);
setAnchor({top: 100, bottom: 140, left: 82, right: 112});
openPicker(120);
console.log(JSON.stringify(placement()));
""")
    assert data["removed"] is False
    assert data["left"] >= 8
    assert data["right"] <= 112
    assert data["width"] == 104


@pytest.mark.parametrize("first_event", ["viewport", "scroll"])
def test_coalesced_viewport_and_scroll_events_preserve_close_requirement(first_event):
    first = (
        "vvEmitter.dispatch('resize'); sessionList.scroll();"
        if first_event == "viewport"
        else "sessionList.scroll(); vvEmitter.dispatch('resize');"
    )
    data = _run_picker_cases(f"""
listRect = {{top: 200, bottom: 650, left: 0, right: 500}};
setAnchor({{top: 400, bottom: 440, left: 410, right: 440}});
openPicker(120);
setAnchor({{top: 150, bottom: 190, left: 410, right: 440}});
{first}
const queued = frames.size;
flushFrames();
console.log(JSON.stringify({{queued, after: placement()}}));
""")
    assert data["queued"] == 1, "The picker must coalesce both events into one frame."
    assert data["after"]["removed"] is True
    assert data["after"]["observers"] == 0
    assert data["after"]["listenerCounts"] == {
        "window": 1,
        "visualViewport": 0,
        "document": 0,
    }


def test_context_point_tracks_its_row_during_partial_scroll():
    data = _run_picker_cases("""
const expandedRow = {
  isConnected: true,
  rect: {top: -208, bottom: 902, left: 16, right: 374, width: 358, height: 1110},
  getBoundingClientRect() { return this.rect; },
};
anchorEl.closest = selector => selector === '.session-child-session-fork,.session-item'
  ? expandedRow
  : (selector === '.session-list' ? sessionList : null);
_recordProjectPickerContextPoint(anchorEl, {clientX: 310, clientY: 420});
setViewport(620, 390);
setAnchor({top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0});
openPicker(120);
const before = placement();
expandedRow.rect = {top: -308, bottom: 802, left: 16, right: 374, width: 358, height: 1110};
sessionList.scroll();
flushFrames();
const after = placement();
console.log(JSON.stringify({before, after}));
""")
    assert data["before"]["removed"] is False
    assert data["after"]["removed"] is False
    assert data["before"]["top"] - data["after"]["top"] == 100


def test_replacement_before_delayed_click_registration_keeps_one_owner():
    data = _run_picker_cases("""
nextPickerHeight = 260;
_showProjectPicker(session, anchorEl);
const first = mountedPicker;
_showProjectPicker(session, anchorEl);
flushTimers();
const live = placement();
docEmitter.dispatch('click', {target: {}});
console.log(JSON.stringify({firstRemoved: first.removed, live, closed: placement(), timers: timers.size}));
""")
    assert data["firstRemoved"]
    assert data["live"]["observers"] == 1
    assert data["live"]["listenerCounts"] == {"window": 2, "visualViewport": 2, "document": 4}
    assert data["closed"]["removed"]
    assert data["closed"]["observers"] == 0
    assert data["closed"]["listenerCounts"] == {"window": 1, "visualViewport": 0, "document": 0}
    assert data["timers"] == 0


_REPAINT_REPLAY_PREFIX = """
let _sessionListRepaintDeferredByPicker = false;
"""


def test_skipped_sidebar_repaint_is_replayed_once_when_the_picker_closes():
    # renderSessionListFromCache() skips while the picker is open (so background
    # churn such as the 60s relative-time refresh cannot tear the picker's anchor
    # away mid-choice) and sets the deferral flag; the picker's teardown must
    # replay exactly one repaint so the sidebar does not stay stale.
    data = _run_picker_cases(_REPAINT_REPLAY_PREFIX + """
openPicker(260);
_sessionListRepaintDeferredByPicker = true;   // a repaint was skipped while open
const whileOpen = repaints;
docEmitter.dispatch('click', {target: {}});   // outside click closes it
flushTimers();
console.log(JSON.stringify({whileOpen, afterClose: repaints,
  flagCleared: _sessionListRepaintDeferredByPicker === false,
  removed: placement().removed}));
""")
    assert data["whileOpen"] == 0
    assert data["removed"] is True
    assert data["afterClose"] == 1, "closing the picker must replay the skipped repaint once"
    assert data["flagCleared"] is True


def test_no_repaint_replay_when_nothing_was_skipped():
    data = _run_picker_cases(_REPAINT_REPLAY_PREFIX + """
openPicker(260);
docEmitter.dispatch('click', {target: {}});
flushTimers();
console.log(JSON.stringify({repaints}));
""")
    assert data["repaints"] == 0


def test_replacement_picker_inherits_a_pending_repaint_deferral():
    # Opening a second picker retires the first; the deferred repaint must not
    # fire under the new picker (that would detach its anchor) and must not be
    # dropped either: it replays when the replacement closes.
    data = _run_picker_cases(_REPAINT_REPLAY_PREFIX + """
openPicker(260);
_sessionListRepaintDeferredByPicker = true;
openPicker(260);                                // replacement
flushTimers();
const underReplacement = repaints;
const stillDeferred = _sessionListRepaintDeferredByPicker;
docEmitter.dispatch('click', {target: {}});
flushTimers();
console.log(JSON.stringify({underReplacement, stillDeferred, afterClose: repaints,
  flagCleared: _sessionListRepaintDeferredByPicker === false}));
""")
    assert data["underReplacement"] == 0
    assert data["stillDeferred"] is True
    assert data["afterClose"] == 1
    assert data["flagCleared"] is True


def test_picker_dismissed_by_another_rows_menu_keeps_then_drains_the_deferral():
    # Opening another row's ⋮ menu dismisses the picker, but the open menu also
    # blocks sidebar renders. The picker's replay must leave the deferral for the
    # menu to drain rather than clearing it into a blocked (dropped) render.
    data = _run_picker_cases(_REPAINT_REPLAY_PREFIX + """
let _sessionActionMenu = null;
openPicker(260);
_sessionListRepaintDeferredByPicker = true;
_sessionActionMenu = {remove(){}};              // another row's menu opens...
docEmitter.dispatch('click', {target: {}});     // ...which dismisses the picker
flushTimers();
console.log(JSON.stringify({repaintsWhileMenuOpen: repaints,
  stillDeferred: _sessionListRepaintDeferredByPicker}));
""")
    assert data["repaintsWhileMenuOpen"] == 0
    assert data["stillDeferred"] is True


def _close_session_action_menu_source() -> str:
    start = SESSIONS_JS.find("function closeSessionActionMenu(")
    assert start >= 0, "closeSessionActionMenu not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    return SESSIONS_JS[start:end]


def _open_session_action_menu_source() -> str:
    start = SESSIONS_JS.find("function _openSessionActionMenu(")
    assert start >= 0, "_openSessionActionMenu not found in static/sessions.js"
    end = SESSIONS_JS.find("\ndocument.addEventListener('click'", start)
    assert end > start
    return SESSIONS_JS[start:end]


def _project_picker_session_action_handoff_source() -> str:
    start = SESSIONS_JS.find("function _projectPickerSessionActionHandoff(")
    assert start >= 0, "_projectPickerSessionActionHandoff not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction _openSessionActionMenu(", start)
    assert end > start
    return SESSIONS_JS[start:end]


def _retire_project_picker_for_explicit_repaint_source() -> str:
    start = SESSIONS_JS.find("function _retireProjectPickerForExplicitRepaint(")
    assert start >= 0, "_retireProjectPickerForExplicitRepaint not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    # Keep any trailing comments from swallowing the next concatenated helper.
    return SESSIONS_JS[start:end] + "\n"


@pytest.mark.parametrize("kind", ["trigger", "row", "actions", "topbar"])
def test_picker_handoff_preserves_anchor_kind_and_parent_scope(kind):
    """A child trigger appears before the parent's own trigger in DOM order."""
    script = r"""
const kind = KIND;
const classes = (...names) => ({contains: name => names.includes(name)});
const oldRow = {isConnected:true, classList:classes('session-item')};
const anchor = kind === 'row' ? oldRow : {
  isConnected:true,
  classList:classes(kind === 'trigger' ? 'session-actions-trigger' :
    kind === 'actions' ? 'session-actions' : 'topbar-title'),
};
anchor._projectPickerContextPoint = {clientX:310,clientY:420};
const childTrigger = {isConnected:true, owner:'child-39'};
const ownTrigger = {isConnected:true, owner:'review-one'};
const ownActions = {isConnected:true, owner:'review-one'};
const newRow = {
  isConnected:true, classList:classes('session-item'),
  querySelector(selector) {
    if(selector === '.session-actions-trigger') return childTrigger;
    if(selector === ':scope > .session-actions > .session-actions-trigger') return ownTrigger;
    if(selector === ':scope > .session-actions') return ownActions;
    return null;
  },
};
let _projectPickerTeardown = () => {};
let _sessionListRepaintDeferredByPicker = true;
const _allSessions = [{session_id:'review-one',pinned:true}];
function renderSessionListFromCache() {
  oldRow.isConnected=false;
  if(kind !== 'topbar') anchor.isConnected=false;
}
function _findSessionRenameRow() {return newRow;}
""" .replace("KIND", json.dumps(kind)) + _project_picker_session_action_handoff_source() + r"""
const result = _projectPickerSessionActionHandoff({session_id:'review-one'}, anchor);
const expected = kind === 'row' ? newRow : kind === 'actions' ? ownActions :
  kind === 'topbar' ? anchor : ownTrigger;
console.log(JSON.stringify({
  correctAnchor:result && result.anchorEl === expected,
  canonicalSession:result && result.session === _allSessions[0],
  point:result && result.anchorEl._projectPickerContextPoint,
}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["correctAnchor"] is True
    assert data["canonicalSession"] is True
    assert data["point"] == {"clientX": 310, "clientY": 420}


def test_picker_handoff_resolves_an_uncached_new_chat_from_state():
    """A visible New Chat with no messages lives only in S.session."""
    assert NODE is not None
    script = r"""
const classes = (...names) => ({contains: name => names.includes(name)});
const anchor = {isConnected:true, classList:classes('session-actions-trigger')};
const replacementTrigger = {isConnected:true, owner:'new-chat'};
const newRow = {
  isConnected:true,
  classList:classes('session-item'),
  querySelector(selector) {
    return selector === ':scope > .session-actions > .session-actions-trigger'
      ? replacementTrigger : null;
  },
};
let _projectPickerTeardown = () => {};
let _sessionListRepaintDeferredByPicker = true;
const _allSessions = [];
const S = {session: {session_id: 'new-chat', title: 'New conversation'}};
function renderSessionListFromCache() { anchor.isConnected = false; }
function _findSessionRenameRow() { return newRow; }
""" + _project_picker_session_action_handoff_source() + r"""
const result = _projectPickerSessionActionHandoff({session_id: 'new-chat'}, anchor);
console.log(JSON.stringify({
  resolvedFromState: result && result.session === S.session,
  anchorReplaced: result && result.anchorEl === replacementTrigger,
}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["resolvedFromState"] is True
    assert data["anchorReplaced"] is True


def test_picker_handoff_postpones_the_repaint_while_fork_release_is_pending():
    """A fork long-press owns its one-click suppression until the release click.

    Rebuilding the rows during that window can move the release target out
    from under the finger, so the handoff must keep the live row (and the
    deferred repaint) in place for the menu-close drain instead. The menu
    still binds current canonical state: the action owner resolves from the
    caches, not the pressed row's stale closure, while the repaint stays
    postponed.
    """
    assert NODE is not None
    script = r"""
const classes = (...names) => ({contains: name => names.includes(name)});
const oldRow = {isConnected:true, classList:classes('session-child-session'), _skipNextChildOpen:true};
const newRow = {isConnected:true, classList:classes('session-child-session')};
let repaints = 0;
let _projectPickerTeardown = () => {};
let _sessionListRepaintDeferredByPicker = true;
const staleChild = {session_id:'fork-child', pinned:false};
const freshChild = {session_id:'fork-child', pinned:true};
const _allSessions = [{session_id:'parent', _child_sessions:[freshChild]}];
function renderSessionListFromCache() { repaints += 1; oldRow.isConnected = false; }
function _findSessionRenameRow() { return newRow; }
""" + _project_picker_session_action_handoff_source() + r"""
const result = _projectPickerSessionActionHandoff(staleChild, oldRow);
console.log(JSON.stringify({
  repaints,
  resolvedCurrentSession: result && result.session === freshChild,
  staleClosureNotBound: result && result.session !== staleChild,
  keptAnchor: result && result.anchorEl === oldRow,
  keptSuppression: oldRow._skipNextChildOpen === true,
  stillDeferred: _sessionListRepaintDeferredByPicker === true,
}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {
        "repaints": 0,
        "resolvedCurrentSession": True,
        "staleClosureNotBound": True,
        "keptAnchor": True,
        "keptSuppression": True,
        "stillDeferred": True,
    }


def test_picker_handoff_resolves_a_content_search_only_session_and_keeps_cache_precedence():
    """An older session returned by content search lives only in _contentSearchResults."""
    assert NODE is not None
    script = r"""
const classes = (...names) => ({contains: name => names.includes(name)});
const makeRow = (trigger) => ({
  isConnected: true,
  classList: classes('session-item'),
  querySelector(selector) {
    return selector === ':scope > .session-actions > .session-actions-trigger' ? trigger : null;
  },
});
const makeTrigger = (owner) => ({isConnected: true, owner});
let _projectPickerTeardown = () => {};
let _sessionListRepaintDeferredByPicker = true;
let _allSessions = [{session_id: 'other-row'}];
const S = {session: null};
const searchHit = {session_id: 'tg-search-hit', match_type: 'content', match_preview: 'older Telegram chat'};
const _contentSearchResults = [searchHit];
let rowTrigger = makeTrigger('search-trigger');
function renderSessionListFromCache() {}
function _findSessionRenameRow(sessionId) {
  return sessionId === 'tg-search-hit' ? makeRow(rowTrigger) : null;
}
""" + _project_picker_session_action_handoff_source() + r"""
const firstAnchor = {isConnected: true, classList: classes('session-actions-trigger')};
const first = _projectPickerSessionActionHandoff({session_id: 'tg-search-hit'}, firstAnchor);
const firstTrigger = rowTrigger;
// The same id present in the list cache keeps cache precedence over search.
const cachedHit = {session_id: 'tg-search-hit', fromCache: true};
_allSessions = [cachedHit];
rowTrigger = makeTrigger('cached-trigger');
_projectPickerTeardown = () => {};
_sessionListRepaintDeferredByPicker = true;
const secondAnchor = {isConnected: true, classList: classes('session-actions-trigger')};
const second = _projectPickerSessionActionHandoff({session_id: 'tg-search-hit'}, secondAnchor);
console.log(JSON.stringify({
  resolvedFromSearchResults: Boolean(first) && first.session === searchHit,
  firstAnchorReplaced: Boolean(first) && first.anchorEl === firstTrigger,
  cacheKeepsPrecedence: Boolean(second) && second.session === cachedHit,
}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {
        "resolvedFromSearchResults": True,
        "firstAnchorReplaced": True,
        "cacheKeepsPrecedence": True,
    }


def test_closing_the_action_menu_drains_a_picker_deferred_repaint():
    assert NODE is not None
    script = r"""
const timers = [];
const setTimeout = fn => { timers.push(fn); return timers.length; };
let repaints = 0;
function renderSessionListFromCache() { repaints += 1; }
function _focusSessionActionMenuRestoreTarget() { return true; }
let _sessionActionMenu = {remove(){}};
let _sessionActionAnchor = null;
let _sessionActionSessionId = 's1';
let _sessionActionPreviousFocus = null;
let _projectPickerTeardown = null;
let _sessionListRepaintDeferredByPicker = true;
""" + _close_session_action_menu_source() + r"""
closeSessionActionMenu();
timers.splice(0).forEach(fn => fn());
const drained = {repaints, flagCleared: _sessionListRepaintDeferredByPicker === false};
// Nothing deferred: closing the menu must not repaint.
_sessionActionMenu = {remove(){}};
closeSessionActionMenu();
timers.splice(0).forEach(fn => fn());
// Menu action that opens the picker (guard re-armed before the drain tick):
_sessionListRepaintDeferredByPicker = true;
_sessionActionMenu = {remove(){}};
closeSessionActionMenu();
_projectPickerTeardown = () => {};
timers.splice(0).forEach(fn => fn());
console.log(JSON.stringify({drained, repaintsAfterNoop: repaints,
  keptForPicker: _sessionListRepaintDeferredByPicker === true}));
"""
    result = subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True, timeout=20)
    data = json.loads(result.stdout)
    assert data["drained"] == {"repaints": 1, "flagCleared": True}
    assert data["repaintsAfterNoop"] == 1
    assert data["keptForPicker"] is True


def test_closing_the_action_menu_replays_an_explicit_completion_with_a_newer_picker_open():
    """A non-background repaint skipped by the action-menu guard is not lost.

    Rename can finish while another row's action menu is open.  That menu may
    open a newer project picker before its close tick runs; the completion must
    still repaint through the keep-picker lane instead of waiting for Escape.
    """
    assert NODE is not None
    script = r"""
const timers = [];
const setTimeout = fn => { timers.push(fn); return timers.length; };
let keptRepaints = 0;
const document = {body: {id: 'body'}, activeElement: null};
const replacementAction = {
  isConnected: true,
  focus(){ document.activeElement = replacementAction; },
};
const replacementRow = {
  querySelector(selector){
    return selector.includes('session-actions-trigger') ? replacementAction : null;
  },
};
const staleAction = {
  isConnected: true,
  classList: {contains(){ return true; }, remove(){}},
  setAttribute(){}, removeAttribute(){},
  closest(){ return null; },
  focus(){ document.activeElement = staleAction; },
};
function _repaintSidebarKeepingProjectPicker() {
  keptRepaints += 1;
  staleAction.isConnected = false;
  document.activeElement = document.body;
}
function renderSessionListFromCache() { throw new Error('plain repaint used'); }
function _focusSessionActionMenuRestoreTarget(target) {
  if(!target || !target.isConnected || typeof target.focus !== 'function') return false;
  target.focus();
  return document.activeElement === target;
}
function _findSessionRenameRow(sessionId) {
  return sessionId === 's1' ? replacementRow : null;
}
let _sessionActionMenu = {remove(){}};
let _sessionActionAnchor = staleAction;
let _sessionActionSessionId = 's1';
let _sessionActionPreviousFocus = null;
let _projectPickerTeardown = () => {}; // newer picker opened by the menu action
let _sessionListRepaintDeferredByPicker = false;
let _sessionListRepaintDeferredByActionMenu = true;
""" + _close_session_action_menu_source() + r"""
closeSessionActionMenu({restoreFocus:true});
timers.splice(0).forEach(fn => fn());
console.log(JSON.stringify({
  keptRepaints,
  flagCleared: _sessionListRepaintDeferredByActionMenu === false,
  pickerKept: _projectPickerTeardown !== null,
  focusedReplacement: document.activeElement === replacementAction,
}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "keptRepaints": 1,
        "flagCleared": True,
        "pickerKept": True,
        "focusedReplacement": True,
    }


def test_project_mutation_completions_use_the_immediate_keep_picker_contract():
    """Every project mutation completion paints beneath a newer picker."""
    picker = _show_project_picker_source()

    context_start = SESSIONS_JS.find("function _showProjectContextMenu(")
    delete_start = SESSIONS_JS.find("async function _confirmDeleteProject(", context_start)
    delete_end = SESSIONS_JS.find("// Global Escape handler", delete_start)
    assert context_start >= 0 and delete_start > context_start and delete_end > delete_start
    context_menu = SESSIONS_JS[context_start:delete_start]
    delete_project = SESSIONS_JS[delete_start:delete_end]

    contract = "await renderSessionList({deferWhileInteracting:false,keepProjectPicker:true});"
    assert picker.count(contract) == 2, "picker project-create success and move-failure must both repaint"
    assert context_menu.count(contract) == 1, "project colour completion must repaint"
    assert delete_project.count(contract) == 1, "project deletion completion must repaint"
    assert "await renderSessionList();" not in context_menu
    assert "await renderSessionList();" not in delete_project


def test_explicit_repaint_retires_the_open_picker_and_folds_its_deferral():
    # An explicit control (child-count toggle, tag filter, inline project
    # create) retires the picker first so its repaint is not swallowed by the
    # picker's background-repaint deferral. The deferred flag folds into the
    # caller's repaint: the teardown replay stays silent and the picker's
    # listeners and observer are all released.
    data = _run_picker_cases(
        _REPAINT_REPLAY_PREFIX
        + _retire_project_picker_for_explicit_repaint_source()
        + """
openPicker(260);
_sessionListRepaintDeferredByPicker = true;
const retired = _retireProjectPickerForExplicitRepaint();
const closed = placement();
flushTimers();
console.log(JSON.stringify({
  retired,
  removed: closed.removed,
  observers: closed.observers,
  listenerCounts: closed.listenerCounts,
  flagCleared: _sessionListRepaintDeferredByPicker === false,
  repaints,
  retiredAgain: _retireProjectPickerForExplicitRepaint(),
}));
"""
    )
    assert data == {
        "retired": True,
        "removed": True,
        "observers": 0,
        "listenerCounts": {"window": 1, "visualViewport": 0, "document": 0},
        "flagCleared": True,
        "repaints": 0,
        "retiredAgain": False,
    }


def test_another_rows_real_action_menu_retires_picker_and_repaints_nested_rows():
    """The ⋮ handoff paints canonical nested-row state before menu creation."""
    assert NODE is not None
    script = _DRIVER_PREFIX + _show_project_picker_source() + r"""
let _sessionListRepaintDeferredByPicker = false;
let _sessionActionMenu = null;
let _sessionActionAnchor = null;
let _sessionActionSessionId = null;
let _sessionActionPreviousFocus = null;
let _sessionActionMenuId = 0;
function _focusSessionActionMenuRestoreTarget() { return true; }
function _isReadOnlySession() { return true; }
function _isMessagingSession() { return false; }
function _isCliSession() { return false; }
function _appendSessionCopyLinkAction() {}
function _appendSessionExportHtmlAction() {}
function _findSessionRenameRow() { return currentSessionRow; }
function _mountSessionActionMenu(menu, nextSession, nextAnchor) {
  _sessionActionMenu = menu;
  _sessionActionSessionId = nextSession.session_id;
  _sessionActionAnchor = nextAnchor;
  mountedSessionPinned = nextSession.pinned;
  mountedWithNewAnchor = nextAnchor === replacementMenuButton;
}
FakeElement.prototype.setAttribute = function(name, value) { this[name] = value; };
FakeElement.prototype.removeAttribute = function(name) { delete this[name]; };
""" + _close_session_action_menu_source() + _project_picker_session_action_handoff_source() + _open_session_action_menu_source() + r"""
const row = {classList: {remove() {}}};
const otherRowMenuButton = {
  isConnected: true,
  _projectPickerContextPoint: {clientX: 310, clientY: 420},
  // The opener is a row's own ⋮ trigger, and the handoff probes the held-gesture
  // classes too, so contains() must answer accurately instead of always-true.
  classList: {contains: name => name === 'session-actions-trigger', remove() {}, add() {}},
  setAttribute() {},
  removeAttribute() {},
  closest: () => row,
  contains: target => target === otherRowMenuButton,
  click() {
    this.onclick({stopPropagation() {}});
  },
};
let mountedSessionPinned = null;
let mountedWithNewAnchor = false;
const replacementMenuButton = {
  isConnected: true,
  classList: {contains: () => true, remove() {}, add() {}},
  setAttribute() {},
  removeAttribute() {},
  closest: () => row,
};
const replacementRow = {
  querySelector: selector => selector === ':scope > .session-actions > .session-actions-trigger' ? replacementMenuButton : null,
};
otherRowMenuButton.onclick = event => {
  event.stopPropagation();
  _openSessionActionMenu({session_id: 'nested-child'}, otherRowMenuButton);
};

openPicker(260);
cachedLabels = {parent: 'renamed parent', child: 'renamed child'};
_allSessions.push({
  session_id: 'parent',
  _child_sessions: [{session_id: 'nested-child', pinned: true}],
});
onRepaint = () => {
  otherRowMenuButton.isConnected = false;
  currentSessionRow = replacementRow;
};
// The production list guard records the skipped repaint while the picker owns
// its row; exercise the popover handoff through the real action-menu function.
_sessionListRepaintDeferredByPicker = true;
otherRowMenuButton.click();
flushTimers();
const whileMenuOpen = {
  pickerRetired: _projectPickerTeardown === null,
  repaints,
  labels: {...paintedLabels},
  flagCleared: _sessionListRepaintDeferredByPicker === false,
  mountedSessionPinned,
  mountedWithNewAnchor,
  handedOffContextPoint: replacementMenuButton._projectPickerContextPoint,
};
closeSessionActionMenu();
flushTimers();
console.log(JSON.stringify({
  whileMenuOpen,
  afterClose: {
    repaints,
    labels: paintedLabels,
    flagCleared: _sessionListRepaintDeferredByPicker === false,
  },
}));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["whileMenuOpen"] == {
        "pickerRetired": True,
        "repaints": 1,
        "labels": {"parent": "renamed parent", "child": "renamed child"},
        "flagCleared": True,
        "mountedSessionPinned": True,
        "mountedWithNewAnchor": True,
        "handedOffContextPoint": {"clientX": 310, "clientY": 420},
    }
    assert data["afterClose"] == {
        "repaints": 1,
        "labels": {"parent": "renamed parent", "child": "renamed child"},
        "flagCleared": True,
    }


def _repaint_sidebar_for_background_churn_source() -> str:
    start = SESSIONS_JS.find("function _repaintSidebarForBackgroundChurn(")
    assert start >= 0, "_repaintSidebarForBackgroundChurn not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    return SESSIONS_JS[start:end] + "\n"


def test_session_list_refresh_defaults_to_immediate_picker_preserving_repaint():
    """User-owned refreshes are the default; background churn must opt in.

    A bare ``renderSessionList()`` is used by many durable user actions.  Keep
    that path immediate and preserve/re-anchor an open project picker.  The
    comparatively small polling/SSE surface must be the only code allowed to
    request interaction deferral, through one named helper.
    """
    render_start = SESSIONS_JS.index("async function renderSessionList(")
    render_end = SESSIONS_JS.index("\n// ── Gateway session SSE", render_start)
    render_body = SESSIONS_JS[render_start:render_end]
    assert "_normalizeRenderSessionListOptions(opts)" in render_body

    normalize_start = SESSIONS_JS.index("function _normalizeRenderSessionListOptions(")
    normalize_end = SESSIONS_JS.index("\nfunction ", normalize_start + 1)
    normalize_body = SESSIONS_JS[normalize_start:normalize_end]
    assert "deferWhileInteracting:false" in normalize_body
    assert "keepProjectPicker:true" in normalize_body

    helper_start = SESSIONS_JS.index("function _refreshSidebarForBackgroundChurn(")
    helper_end = SESSIONS_JS.index("\nfunction ", helper_start + 1)
    helper_body = SESSIONS_JS[helper_start:helper_end]
    deferred_call = "renderSessionList({deferWhileInteracting:true,keepProjectPicker:false})"
    assert deferred_call in helper_body
    assert SESSIONS_JS.count(deferred_call) == 1

    outside_helper = SESSIONS_JS[:helper_start] + SESSIONS_JS[helper_end:]
    assert re.search(
        r"renderSessionList\(\{[^}]*deferWhileInteracting:true",
        outside_helper,
    ) is None


def test_forced_session_events_refresh_paints_immediately_not_deferred():
    """A forced SSE refresh must land while the list is hovered or focused.

    ``force:true`` (the sessions_changed path) reached
    ``_refreshSidebarForBackgroundChurn()``, whose ``deferWhileInteracting:true``
    parked the payload until the pointer or focus left the list — a remote
    rename stayed stale where master painted it immediately. Forced refreshes
    now pass the explicit immediate pair and still enter as background-class
    (no keep-picker claim), so an open picker is protected by its own repaint
    deferral instead.
    """
    start = SESSIONS_JS.index("async function refreshSessionList(")
    end = SESSIONS_JS.index("\nfunction ", start + 1)
    body = SESSIONS_JS[start:end]
    assert "const force = !!(opts && opts.force);" in body
    immediate = "renderSessionList({deferWhileInteracting:false,keepProjectPicker:false});"
    assert immediate in body
    # The forced branch sits between the user-owned path and the deferred
    # background-churn path: only the latter may defer behind interaction.
    assert body.index("if(userOwnedRefresh){") < body.index("else if(force){")
    assert body.index("else if(force){") < body.index("_refreshSidebarForBackgroundChurn();")
    assert body.index(immediate) < body.index("_refreshSidebarForBackgroundChurn();")


def _repaint_sidebar_keeping_project_picker_source() -> str:
    start = SESSIONS_JS.find("function _repaintSidebarKeepingProjectPicker(")
    assert start >= 0, "_repaintSidebarKeepingProjectPicker not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    return SESSIONS_JS[start:end] + "\n"


def _project_picker_blocks_repaint_source() -> str:
    start = SESSIONS_JS.find("function _projectPickerBlocksRepaint(")
    assert start >= 0, "_projectPickerBlocksRepaint not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    return SESSIONS_JS[start:end] + "\n"


def _project_picker_owner_eligible_for_sidebar_source() -> str:
    start = SESSIONS_JS.find("function _projectPickerOwnerEligibleForSidebar(")
    assert start >= 0, "_projectPickerOwnerEligibleForSidebar not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    return SESSIONS_JS[start:end] + "\n"


def _render_sidebar_rows_from_partition_source() -> str:
    start = SESSIONS_JS.find("function _renderSidebarRowsFromPartition(")
    assert start >= 0, "_renderSidebarRowsFromPartition not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    return SESSIONS_JS[start:end] + "\n"


def test_created_items_force_an_immediate_picker_preserving_payload_paint():
    """A completed create paints now even when a newer picker is open.

    The payload application chokepoint must distinguish ordinary background
    refreshes (which defer behind an open picker) from the two create
    completions.  The latter repaint immediately while preserving/reanchoring
    a picker opened after the request began.
    """
    js = SESSIONS_JS
    apply_start = js.index("function _applySessionListPayload(")
    apply_end = js.index("\nfunction ", apply_start + 1)
    apply_body = js[apply_start:apply_end]
    assert "if(applyOpts.keepProjectPicker) _repaintSidebarKeepingProjectPicker();" in apply_body
    assert "else _repaintSidebarForBackgroundChurn();" in apply_body
    assert "refreshSessionList('new-session',{force:true,keepProjectPicker:true})" in js
    assert "await renderSessionList({keepProjectPicker:true});" in js
    assert "if(userOwnedRefresh){" in js
    assert "await _refreshSidebarForBackgroundChurn();" in js
    assert "if(opts&&opts.keepProjectPicker===true) _applySessionListPayload(sessData,projData,{unreadGen,keepProjectPicker:true});" in js


def test_sidebar_repaint_guard_defers_background_churn_and_keeps_the_overlay_for_unclaimed_repaints():
    """The completion-safe picker guard: churn defers, unclaimed repaints keep.

    Background producers (list refresh, stream/unread sync, poll timers,
    panel-resync repairs) run their repaints through the background-churn
    wrapper, so the guard defers them while the picker is open and the picker's
    close replays them. Every other repaint proceeds without inheriting a
    dismissal: it retires the picker only when it claims an interaction newer
    than the picker's opening generation (claims arrive via
    _retireProjectPickerForExplicitRepaint(claimEpoch)), so legacy async
    completions keep the overlay and rely on the render re-anchoring it
    (_reanchorProjectPickerAfterRepaint).
    """
    assert NODE is not None
    script = r"""
let _sessionListRepaintDeferredByPicker = false;
let _sidebarRepaintBackgroundChurn = false;
let _sidebarRepaintKeepProjectPicker = false;
let _projectPickerTeardown = null;
let _openProjectPicker = null;
let retired = 0;
const renderCalls = [];
const keepCalls = [];
function renderSessionListFromCache(){ renderCalls.push(_sidebarRepaintBackgroundChurn); keepCalls.push(_sidebarRepaintKeepProjectPicker); }
""" + _retire_project_picker_for_explicit_repaint_source() + _repaint_sidebar_for_background_churn_source() + _repaint_sidebar_keeping_project_picker_source() + _project_picker_blocks_repaint_source() + r"""
_repaintSidebarForBackgroundChurn();
const wrapper = {callSawFlag: renderCalls[0] === true, flagRestored: _sidebarRepaintBackgroundChurn === false};
_repaintSidebarKeepingProjectPicker();
const keepWrapper = {callSawFlag: keepCalls[1] === true, flagRestored: _sidebarRepaintKeepProjectPicker === false};
const retireSpy = () => { retired += 1; _projectPickerTeardown = null; };
const noPicker = {blocks: _projectPickerBlocksRepaint(), retired};
_projectPickerTeardown = retireSpy;
_sidebarRepaintBackgroundChurn = true;
const background = {blocks: _projectPickerBlocksRepaint(), retired, teardownKept: _projectPickerTeardown === retireSpy};
_sidebarRepaintBackgroundChurn = false;
_sessionListRepaintDeferredByPicker = true;
const unclaimed = {blocks: _projectPickerBlocksRepaint(), retired, teardownKept: _projectPickerTeardown === retireSpy};
_projectPickerTeardown = retireSpy;
_sidebarRepaintKeepProjectPicker = true;
const kept = {blocks: _projectPickerBlocksRepaint(), retired, teardownKept: _projectPickerTeardown === retireSpy};
_sidebarRepaintKeepProjectPicker = false;
// Overlay generation: a claim at least as new as the picker's opening may
// retire it; an older claim is refused and the picker stays.
_openProjectPicker = {picker: {}, anchorEl: {isConnected: true}, sessionId: 's', openEpoch: 5};
_sidebarRepaintBackgroundChurn = false;
const staleClaim = {retired: _retireProjectPickerForExplicitRepaint(4), refused: _projectPickerTeardown === retireSpy};
const freshClaim = {retired: _retireProjectPickerForExplicitRepaint(5), cleared: _projectPickerTeardown === null, flagFolded: _sessionListRepaintDeferredByPicker === false};
console.log(JSON.stringify({wrapper, keepWrapper, noPicker, background, unclaimed, kept, staleClaim, freshClaim}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {
        "wrapper": {"callSawFlag": True, "flagRestored": True},
        "keepWrapper": {"callSawFlag": True, "flagRestored": True},
        "noPicker": {"blocks": False, "retired": 0},
        "background": {"blocks": True, "retired": 0, "teardownKept": True},
        "unclaimed": {"blocks": False, "retired": 0, "teardownKept": True},
        "kept": {"blocks": False, "retired": 0, "teardownKept": True},
        "staleClaim": {"retired": False, "refused": True},
        "freshClaim": {"retired": True, "cleared": True, "flagFolded": True},
    }


def _reanchor_project_picker_after_repaint_source() -> str:
    start = SESSIONS_JS.find("function _reanchorProjectPickerAfterRepaint(")
    assert start >= 0, "_reanchorProjectPickerAfterRepaint not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    return SESSIONS_JS[start:end] + "\n"


def _sync_project_picker_selection_source() -> str:
    start = SESSIONS_JS.find("function _syncProjectPickerSelection(")
    assert start >= 0, "_syncProjectPickerSelection not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    return SESSIONS_JS[start:end] + "\n"


def test_kept_same_owner_picker_refreshes_selected_project_without_moving_focus():
    """A held Move completion updates the reopened picker's radio state in place,
    re-places it against its live anchor, and keeps keyboard focus."""
    assert NODE is not None
    script = r"""
let _allSessions=[{session_id:'s',project_id:'project-b'}];
let _openProjectPicker=null;
let positioned=0;
let refreshed=0;
let focused='project-a';
const makeItem=(id,active)=>{
  const attrs={'data-project-id':id,'aria-checked':active?'true':'false'};
  const classes=new Set(active?['active']:[]);
  return {
    getAttribute:name=>attrs[name],
    setAttribute:(name,value)=>{attrs[name]=String(value);},
    classList:{toggle:(name,on)=>{if(on)classes.add(name);else classes.delete(name);}},
    state:()=>({active:classes.has('active'),checked:attrs['aria-checked']}),
  };
};
const a=makeItem('project-a',true);
const b=makeItem('project-b',false);
const picker={isConnected:true,querySelectorAll:()=>[a,b]};
const anchor={isConnected:true};
_openProjectPicker={picker,anchorEl:anchor,sessionId:'s',refreshChoices:()=>{refreshed+=1;}};
function _positionProjectPicker(){positioned+=1;}
""" + _sync_project_picker_selection_source() + _reanchor_project_picker_after_repaint_source() + r"""
const result=_reanchorProjectPickerAfterRepaint();
console.log(JSON.stringify({result,a:a.state(),b:b.state(),focused,positioned,refreshed}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "result": False,
        "a": {"active": False, "checked": "false"},
        "b": {"active": True, "checked": "true"},
        "focused": "project-a",
        "positioned": 1,
        "refreshed": 1,
    }


def test_kept_picker_refreshes_created_renamed_and_deleted_project_choices():
    """A kept picker mirrors the latest project cache and preserves focus."""
    assert NODE is not None
    script = _DRIVER_PREFIX + r"""
document.activeElement = null;
FakeElement.prototype.contains = function(target) {
  if (target === this) return true;
  return this.children.some(child => child === target || (child.contains && child.contains(target)));
};
FakeElement.prototype.insertBefore = function(child, before) {
  // Chromium blurs a focused node the moment it is DOM-moved; the fake must do
  // the same or a rebuild that relocates surviving rows would pass here while
  // dropping the user's focus in a real browser.
  const currentIndex = this.children.indexOf(child);
  const alreadyInPlace = currentIndex >= 0 && this.children[currentIndex + 1] === before;
  if (!alreadyInPlace && document.activeElement === child) document.activeElement = documentBody;
  if (child.parent) child.remove();
  const idx = this.children.indexOf(before);
  child.parent = this;
  child.isConnected = true;
  if (idx < 0) this.children.push(child); else this.children.splice(idx, 0, child);
  return child;
};
const focusCalls = [];
FakeElement.prototype.focus = function(options) {
  document.activeElement = this;
  this.focused = true;
  focusCalls.push({target:this,preventScroll:!!(options&&options.preventScroll)});
};
FakeElement.prototype.querySelectorAll = function(selector) {
  const out = [];
  const visit = node => {
    for (const child of node.children || []) {
      const classes = new Set(String(child.className || '').split(/\s+/).filter(Boolean));
      const id = child.getAttribute && child.getAttribute('data-project-id');
      const projectRow = classes.has('project-picker-item') && id !== null;
      if (selector === '.project-picker-item' && classes.has('project-picker-item')) out.push(child);
      if (selector === '.project-picker-item[data-project-id]' && projectRow) out.push(child);
      visit(child);
    }
  };
  visit(this);
  return out;
};
""" + _show_project_picker_source() + r"""
const rows = () => mountedPicker.querySelectorAll('.project-picker-item[data-project-id]');
const byId = id => rows().find(item => item.getAttribute('data-project-id') === id);
const labels = () => rows().map(item => ({
  id: item.getAttribute('data-project-id'),
  name: item.children.map(child => child.textContent || '').filter(Boolean).join(''),
}));
openPicker(260);
byId('p1').focus();
focusCalls.length = 0;
const originalFocusedChoice = byId('p1');
_openProjectPicker.refreshChoices();             // unchanged cache: strict no-op
const afterUnchanged = {
  sameNode: byId('p1') === originalFocusedChoice,
  focusCalls: focusCalls.length,
};
_allProjects.splice(0, 1);                  // delete p0
_allProjects.find(p => p.project_id === 'p1').name = 'Renamed project';
_allProjects.push({project_id:'new-project',name:'New project',profile:'default',color:'#123456'});
_openProjectPicker.refreshChoices();
const afterMutation = {
  labels: labels(),
  focusedId: document.activeElement.getAttribute('data-project-id'),
  sameNode: byId('p1') === originalFocusedChoice,
  focusCalls: focusCalls.length,
};
_allProjects.splice(_allProjects.findIndex(p => p.project_id === 'p1'), 1);
_openProjectPicker.refreshChoices();
const afterFocusedDelete = {
  labels: labels(),
  focusedId: document.activeElement.getAttribute('data-project-id'),
  preventScroll: focusCalls.at(-1)?.preventScroll === true,
};
console.log(JSON.stringify({afterUnchanged,afterMutation,afterFocusedDelete}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["afterUnchanged"] == {"sameNode": True, "focusCalls": 0}
    assert all(row["id"] != "p0" for row in data["afterMutation"]["labels"])
    assert {row["id"]: row["name"] for row in data["afterMutation"]["labels"]}["p1"] == "Renamed project"
    assert "new-project" in {row["id"] for row in data["afterMutation"]["labels"]}
    assert data["afterMutation"]["focusedId"] == "p1"
    assert data["afterMutation"]["sameNode"] is True
    assert data["afterMutation"]["focusCalls"] == 0
    assert "p1" not in {row["id"] for row in data["afterFocusedDelete"]["labels"]}
    assert data["afterFocusedDelete"]["focusedId"] in {
        row["id"] for row in data["afterFocusedDelete"]["labels"]
    }
    assert data["afterFocusedDelete"]["preventScroll"] is True


_REFRESH_FAKE_EXTENSIONS = r"""
document.activeElement = null;
FakeElement.prototype.contains = function(target) {
  if (target === this) return true;
  return this.children.some(child => child === target || (child.contains && child.contains(target)));
};
const refreshMoves = [];
FakeElement.prototype.insertBefore = function(child, before) {
  // Chromium blurs a focused node the moment it is DOM-moved; mirror that so
  // these tests catch a rebuild that relocates surviving rows.
  const currentIndex = this.children.indexOf(child);
  const alreadyInPlace = currentIndex >= 0 && this.children[currentIndex + 1] === before;
  if (!alreadyInPlace) {
    refreshMoves.push(child.getAttribute && child.getAttribute('data-project-id'));
    if (document.activeElement === child) document.activeElement = documentBody;
  }
  if (child.parent) child.remove();
  const idx = this.children.indexOf(before);
  child.parent = this;
  child.isConnected = true;
  if (idx < 0) this.children.push(child); else this.children.splice(idx, 0, child);
  return child;
};
const focusCalls = [];
FakeElement.prototype.focus = function(options) {
  document.activeElement = this;
  this.focused = true;
  focusCalls.push({target: this, preventScroll: !!(options && options.preventScroll)});
};
FakeElement.prototype.querySelectorAll = function(selector) {
  const out = [];
  if (selector === '.project-picker-item' || selector === '.project-picker-item[data-project-id]') {
    const visit = node => {
      for (const child of node.children || []) {
        const classes = new Set(String(child.className || '').split(/\s+/).filter(Boolean));
        const id = child.getAttribute && child.getAttribute('data-project-id');
        if (selector === '.project-picker-item' && classes.has('project-picker-item')) out.push(child);
        if (selector === '.project-picker-item[data-project-id]' && classes.has('project-picker-item') && id !== null) out.push(child);
        visit(child);
      }
    };
    visit(this);
  }
  return out;
};
"""


def test_picker_refresh_leaves_an_unchanged_list_and_its_focus_alone():
    """An unchanged refresh must keep every choice node and never re-focus it.

    The court report for the kept picker was that unchanged re-anchors
    replaced the focused project row and called bare focus(). This pins the
    strict no-op: same nodes, no moves, no removes, no focus calls.
    """
    assert NODE is not None
    script = _DRIVER_PREFIX + _REFRESH_FAKE_EXTENSIONS + _show_project_picker_source() + r"""
openPicker(260);
const rowsBefore = mountedPicker.querySelectorAll('.project-picker-item[data-project-id]');
rowsBefore.find(row => row.getAttribute('data-project-id') === 'p2').focus();
const focusBefore = focusCalls.length;
_openProjectPicker.refreshChoices();
const rowsAfter = mountedPicker.querySelectorAll('.project-picker-item[data-project-id]');
console.log(JSON.stringify({
  keptNodes: rowsBefore.length === rowsAfter.length && rowsBefore.every((row, i) => row === rowsAfter[i]),
  removedCount: rowsBefore.filter(row => row.removed).length,
  moves: refreshMoves,
  focusAdded: focusCalls.length - focusBefore,
  activeId: document.activeElement && document.activeElement.getAttribute('data-project-id'),
}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["keptNodes"] is True
    assert data["removedCount"] == 0
    assert data["moves"] == []
    assert data["focusAdded"] == 0, "An unchanged refresh must not call focus()"
    assert data["activeId"] == "p2"


def test_picker_refresh_reconciles_by_identity_and_no_scroll_focus_for_replacement():
    """A deleted focused choice hands focus to its replacement via the helper.

    The reconcile keeps surviving choices in place (no DOM move, so a focused
    row keeps its focus in real browsers), and when the focused choice itself
    is deleted the replacement focus runs through the no-scroll focus helper
    instead of a bare focus() jump.
    """
    assert NODE is not None
    script = _DRIVER_PREFIX + _REFRESH_FAKE_EXTENSIONS + _show_project_picker_source() + r"""
openPicker(260);
const rowsBefore = mountedPicker.querySelectorAll('.project-picker-item[data-project-id]');
rowsBefore.find(row => row.getAttribute('data-project-id') === 'p1').focus();
_allProjects.splice(_allProjects.findIndex(p => p.project_id === 'p1'), 1);
const focusBefore = focusCalls.length;
_openProjectPicker.refreshChoices();
const rowsAfter = mountedPicker.querySelectorAll('.project-picker-item[data-project-id]');
const survivorsKept = rowsAfter.every(row => {
  const before = rowsBefore.find(r => r.getAttribute('data-project-id') === row.getAttribute('data-project-id'));
  return before === row;
});
const refreshFocus = focusCalls.slice(focusBefore);
console.log(JSON.stringify({
  survivorsKept,
  moves: refreshMoves,
  refreshFocusCount: refreshFocus.length,
  lastPreventScroll: refreshFocus.length ? refreshFocus[refreshFocus.length - 1].preventScroll : null,
  activeId: document.activeElement && document.activeElement.getAttribute('data-project-id'),
  ids: rowsAfter.map(r => r.getAttribute('data-project-id')),
}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["survivorsKept"] is True, "Surviving choices must keep their nodes"
    assert data["moves"] == [], "Surviving choices must not be DOM-moved"
    assert data["refreshFocusCount"] == 1, "Only the replacement focus may run"
    assert data["lastPreventScroll"] is True, "The replacement focus must be a no-scroll focus"
    assert data["activeId"] == "p2"
    assert "p1" not in data["ids"]


def test_picker_refresh_never_steals_focus_from_outside_the_picker():
    """A refresh must leave focus alone when the user is elsewhere."""
    assert NODE is not None
    script = _DRIVER_PREFIX + _REFRESH_FAKE_EXTENSIONS + _show_project_picker_source() + r"""
openPicker(260);
const outside = new FakeElement('button');
outside.isConnected = true;
document.activeElement = outside;
_allProjects.splice(0, 1);
const focusBefore = focusCalls.length;
_openProjectPicker.refreshChoices();
console.log(JSON.stringify({
  moves: refreshMoves,
  focusAdded: focusCalls.length - focusBefore,
  stillOutside: document.activeElement === outside,
}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["moves"] == []
    assert data["focusAdded"] == 0
    assert data["stillOutside"] is True


def test_completion_repaints_keep_and_reanchor_the_newer_picker():
    """The completion default retains the overlay instead of dismissing it.

    A repaint that cannot claim a newer interaction rebuilds the rows, keeps an
    open picker owned by another session, and re-anchors it to that owner's
    rebuilt row — retiring it only when the owner itself is actually gone
    (archived, deleted, filtered out). A claim older than the picker can never
    retire it (overlay generation).
    """
    assert NODE is not None
    script = r"""
let _sessionListRepaintDeferredByPicker = false;
let _sidebarRepaintBackgroundChurn = false;
let _projectPickerTeardown = null;
let _openProjectPicker = null;
let retired = 0;
let dismissed = 0;
let positioned = 0;
const painted = [];
const contextPoint = {clientX: 310, clientY: 420};
const replacementActions = {isConnected: true};
const rebuiltB = {
  isConnected: true,
  querySelector: (selector) => selector === ':scope > .session-actions' ? replacementActions : null,
};
const newRows = {b: rebuiltB};
function renderSessionListFromCache(){
  if(_projectPickerBlocksRepaint()){
    _sessionListRepaintDeferredByPicker = true;
    return;
  }
  painted.push('rows');
  _reanchorProjectPickerAfterRepaint();
}
function _findSessionRenameRow(sessionId){ return newRows[sessionId] || null; }
function _positionProjectPicker(){ positioned += 1; }
""" + _retire_project_picker_for_explicit_repaint_source() + _repaint_sidebar_for_background_churn_source() + _project_picker_blocks_repaint_source() + _reanchor_project_picker_after_repaint_source() + r"""
const openPicker = (sessionId, epoch) => {
  const opening = {
    picker: {isConnected: true},
    sessionId,
    openEpoch: epoch,
    anchorEl: {isConnected: true},
    dismiss: () => { dismissed += 1; _projectPickerTeardown = null; _openProjectPicker = null; },
    retarget: function(next){ this.anchorEl = next; },
  };
  _projectPickerTeardown = () => { retired += 1; _openProjectPicker = null; };
  _openProjectPicker = opening;
  return opening;
};
// A completion repaint with B's picker open: B survives, the rows rebuild, and
// the picker re-anchors to B's rebuilt row.
const b = openPicker('b', 2);
b.anchorEl = {
  isConnected: false,
  classList: {contains: (name) => name === 'session-actions'},
  _projectPickerContextPoint: contextPoint,
}; // the rebuild replaced B's mouse-context action box
renderSessionListFromCache();
const kept = {retired, dismissed, painted: painted.length,
  stillOpen: _openProjectPicker === b,
  reanchored: _openProjectPicker.anchorEl === replacementActions,
  contextPointKept: replacementActions._projectPickerContextPoint === contextPoint,
  positioned};
// A rebuild where B's owner row is actually gone: retire the invalid owner.
const c = openPicker('c', 2);
c.anchorEl.isConnected = false;
renderSessionListFromCache();
const invalidOwner = {retired, dismissed, painted: painted.length,
  closed: _openProjectPicker === null};
// A claim older than the picker's opening can never retire it.
const d = openPicker('d', 9);
const staleClaim = _retireProjectPickerForExplicitRepaint(8);
const keptByGeneration = {teardownKept: _projectPickerTeardown !== null, staleClaim,
  stillOpen: _openProjectPicker === d};
console.log(JSON.stringify({kept, invalidOwner, keptByGeneration}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {
        "kept": {"retired": 0, "dismissed": 0, "painted": 1, "stillOpen": True, "reanchored": True, "contextPointKept": True, "positioned": 1},
        "invalidOwner": {"retired": 0, "dismissed": 1, "painted": 2, "closed": True},
        "keptByGeneration": {"teardownKept": True, "staleClaim": False, "stillOpen": True},
    }


def test_completion_policy_is_wired_through_the_generation_claim_and_reanchor():
    """Source wiring: the default keeps, claims gate dismissal, render re-anchors.

    The unmarked default must not inherit dismissal authority; delayed
    completions (new chat, inline project create) retire only a picker older
    than their captured claim; the render re-anchors a kept overlay; explicit
    synchronous intents keep their dismissal; opening records the generation
    and exposes a retarget.
    """
    js = SESSIONS_JS
    guard_start = js.index("function _projectPickerBlocksRepaint(")
    guard_end = js.index("\nfunction ", guard_start + 1)
    guard_body = js[guard_start:guard_end]
    assert "_retireProjectPickerForExplicitRepaint(" not in guard_body, (
        "the unmarked default must not inherit dismissal authority"
    )
    retire_start = js.index("function _retireProjectPickerForExplicitRepaint(")
    retire_end = js.index("\nfunction ", retire_start + 1)
    retire_body = js[retire_start:retire_end]
    assert "claimEpoch" in retire_body and "openEpoch>claimEpoch" in retire_body, (
        "the retire action must refuse claims older than the picker's opening"
    )
    assert "let newSessionClaim=" in js
    assert "_retireProjectPickerForExplicitRepaint(newSessionClaim)" in js
    assert "const createClaim=" in js
    assert "_retireProjectPickerForExplicitRepaint(createClaim)" in js
    assert "if(typeof _reanchorProjectPickerAfterRepaint==='function') _reanchorProjectPickerAfterRepaint();" in js
    assert "openEpoch:pickerEpoch" in js
    assert "retarget:" in js
    toggle_start = js.index("function toggleSessionSelectMode(){")
    toggle_end = js.index("\nfunction ", toggle_start + 1)
    assert "_retireProjectPickerForExplicitRepaint();" in js[toggle_start:toggle_end]


def test_automatic_repaints_route_through_the_background_wrapper():
    """Greptile P1s on the routing push: automatic repaints must defer, not retire.

    The lineage-report callbacks, the content-search reply and the scroll-driven
    virtual-window rebuild repaint from automatic updates; with the inverted
    guard an unmarked call would retire an open picker while the user is
    choosing a project. All of them must run through the background wrapper —
    and the scroll rebuild must still proceed (without retiring the picker)
    when the picker's anchor is outside the list, or scrolling would show
    blank space.
    """
    js = SESSIONS_JS
    assert js.count("_fetchLineageReportForRow(s,lineageKey).then(()=>_repaintSidebarForBackgroundChurn());") == 2
    assert "_fetchLineageReportForRow(s,lineageKey).then(()=>renderSessionListFromCache())" not in js

    filter_start = js.index("function filterSessions(){")
    filter_end = js.index("\nfunction ", filter_start + 1)
    filter_body = js[filter_start:filter_end]
    # The synchronous first paint stays user intent; the debounced content
    # search reply is an automatic update and must defer.
    assert filter_body.count("renderSessionListFromCache();") == 1
    assert filter_body.count("_repaintSidebarForBackgroundChurn();") == 1

    scroll_start = js.index("function _scheduleSessionVirtualizedRender(){")
    scroll_end = js.index("\nfunction ", scroll_start + 1)
    scroll_body = js[scroll_start:scroll_end]
    # A picker anchored inside the list defers the rebuild; one anchored
    # outside it must not stall the rebuild (blank space while scrolling).
    assert "function _projectPickerAnchorWithinSessionList()" in js
    assert "_projectPickerAnchorWithinSessionList()" in scroll_body
    assert "_repaintSidebarForBackgroundChurn();" in scroll_body
    assert "_repaintSidebarKeepingProjectPicker();" in scroll_body
    assert "renderSessionListFromCache();" not in scroll_body


def _repaint_sidebar_after_session_completion_source() -> str:
    start = SESSIONS_JS.find("function _repaintSidebarAfterSessionCompletion(")
    assert start >= 0, "_repaintSidebarAfterSessionCompletion not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    return SESSIONS_JS[start:end] + "\n"


def _optimistically_archive_session_in_list_source() -> str:
    start = SESSIONS_JS.find("function _optimisticallyArchiveSessionInList(")
    assert start >= 0, "_optimisticallyArchiveSessionInList not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    return SESSIONS_JS[start:end] + "\n"


def _external_hide_handler_source() -> str:
    marker = SESSIONS_JS.index("t('session_hide_external')")
    start = SESSIONS_JS.index("async()=>{", marker)
    brace = SESSIONS_JS.index("{", start)
    depth = 0
    i = brace
    while i < len(SESSIONS_JS):
        char = SESSIONS_JS[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    assert depth == 0, "the external hide handler body did not close"
    return SESSIONS_JS[brace:i + 1]


# A response that lands after an await is a completion, not fresh user intent.
# Each entry pairs the durable cache write that makes the response observable
# with the routed repaint that must paint the result immediately while keeping
# any newer picker (the shared completion helper, or the explicit keep-picker
# repaint). The gate charged these sites as L1-L6 plus the delete-failure
# rollback lead; the external-hide completion was added when a warm-up found it
# was the remaining sibling path.
LATE_COMPLETION_REPAINTS = [
    ("L1 title regenerate", "cached.title=nextTitle;", "_repaintSidebarKeepingProjectPicker();"),
    ("L2 pin toggle", "if(cached) cached.pinned=newPinned;", "_repaintSidebarKeepingProjectPicker();"),
    ("L3 share create/refresh", "cached.share_token=session.share_token;", "_repaintSidebarKeepingProjectPicker();"),
    ("L4 move to project", "_allSessions[idx].project_id=p.project_id;", "_repaintSidebarAfterSessionCompletion(session.session_id,pickerEpoch);"),
    ("L5 unassign project", "_allSessions[idx].project_id=null;", "_repaintSidebarAfterSessionCompletion(session.session_id,pickerEpoch);"),
    ("L6 archive", "if(cached) cached.archived=archived;", "_repaintSidebarAfterSessionCompletion(session.session_id);"),
    ("delete failure rollback", "_allSessions=previousSessions;", "_repaintSidebarAfterSessionCompletion(sid);"),
    ("hide external session", "return {...s,archived:!!archived};", "_repaintSidebarAfterSessionCompletion(sid);"),
]


@pytest.mark.parametrize("label,durable_write,repaint_call", LATE_COMPLETION_REPAINTS)
def test_delayed_response_completions_paint_immediately_while_keeping_a_newer_picker(label, durable_write, repaint_call):
    """Gate L1-L6: an awaited response must paint its result, keeping any newer picker.

    Start an action for A, hold its POST, open B's project picker, then release
    A's response. An unmarked ``renderSessionListFromCache()`` retires B's
    picker as if the user had asked for that repaint, and the background-churn
    wrapper parks the result behind it. Each completion writes its durable
    result into the cache first, then routes the repaint through the immediate
    keep-picker contract so A's result paints now while B's picker survives.
    """
    assert SESSIONS_JS.count(durable_write) == 1, f"{label}: durable write is not unique"
    durable_at = SESSIONS_JS.index(durable_write)
    repaint_at = SESSIONS_JS.index(repaint_call, durable_at)
    assert repaint_at - durable_at < 700, (
        f"{label}: no routed repaint follows the durable cache write; the "
        "completion may still be painting as user intent"
    )
    window = SESSIONS_JS[durable_at:repaint_at]
    assert "renderSessionListFromCache();" not in window, (
        f"{label}: the completion calls renderSessionListFromCache() directly, "
        "which retires a picker the user opened while the response was in flight"
    )
    assert "_retireProjectPickerForExplicitRepaint();" not in window, (
        f"{label}: the completion retires whatever picker is open instead of "
        "only one owned by the session it mutated"
    )
    assert "_repaintSidebarForBackgroundChurn();" not in window, (
        f"{label}: the completion defers its durable result behind a newer "
        "picker instead of painting it while keeping the overlay"
    )


def _completion_slice(start_marker: str, end_marker: str) -> str:
    start = SESSIONS_JS.index(start_marker)
    end = SESSIONS_JS.index(end_marker, start)
    return SESSIONS_JS[start:end]


def _completion_function_body(name: str) -> str:
    """Extract a top-level function body with balanced braces."""
    start = SESSIONS_JS.index(f"function {name}(")
    brace = SESSIONS_JS.index("{", start)
    depth = 0
    i = brace
    while i < len(SESSIONS_JS):
        char = SESSIONS_JS[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    assert depth == 0, f"{name} body did not close"
    return SESSIONS_JS[start:i + 1]


KEEP_PICKER_REFETCH = "await renderSessionList({deferWhileInteracting:false,keepProjectPicker:true});"


def test_batch_completions_ship_the_immediate_keep_picker_contract():
    """All four batch-completion refetches must paint now, keeping newer pickers.

    Archived or deleted rows, and moved assignments, stay on screen as stale
    until an open picker closes when the completion refetch defers. Each batch
    completion must use the explicit nondeferred keep-picker contract instead.
    """
    bar = _completion_function_body("_renderBatchActionBar")
    delete_batch = _completion_function_body("_deleteSelectedSessionsBatch")
    picker = _completion_function_body("_showBatchProjectPicker")
    # The delete completion moved into its own function when the batch handler
    # learned to settle each request (refused sessions must not discard the
    # ones that committed), so the archive refetch is all that stays here.
    assert bar.count(KEEP_PICKER_REFETCH) == 1, (
        "the batch archive completion must use the keep-picker refetch"
    )
    assert delete_batch.count(KEEP_PICKER_REFETCH) == 2, (
        "both batch delete completions (partial and full) must use the keep-picker refetch"
    )
    assert picker.count(KEEP_PICKER_REFETCH) == 2, (
        "the batch unassign and move completions must use the keep-picker refetch"
    )
    assert "await renderSessionList();" not in bar
    assert "await renderSessionList();" not in delete_batch
    assert "await renderSessionList();" not in picker


def test_pin_completion_ships_the_immediate_keep_picker_contract():
    """Pin's success transition must move the row now, keeping a newer picker."""
    body = _completion_slice("const newPinned=!session.pinned;", "t('session_pin_failed')")
    assert "_repaintSidebarKeepingProjectPicker();" in body
    assert "void renderSessionList({deferWhileInteracting:false,keepProjectPicker:true});" in body
    assert "_repaintSidebarForBackgroundChurn();" not in body


def test_title_regenerate_completion_uses_the_keep_picker_repaint():
    """A regenerated title must paint now, keeping a picker opened in flight."""
    body = _completion_slice(
        "const nextTitle=(response&&response.title)",
        "showToast(t('session_title_regenerated'",
    )
    assert "_repaintSidebarKeepingProjectPicker();" in body
    assert "_repaintSidebarForBackgroundChurn();" not in body


def test_rename_release_repaint_keeps_a_newer_picker():
    """The rename completion's final repaint must not defer behind a picker."""
    body = _completion_function_body("_buildSessionRenameStarter")
    assert "_repaintSidebarKeepingProjectPicker();" in body
    assert "_repaintSidebarForBackgroundChurn" not in body


def test_share_refresh_completion_ships_the_immediate_keep_picker_contract():
    """The share completion must paint the share state now and refetch keeping."""
    body = _completion_function_body("_syncSessionShareState")
    assert "_repaintSidebarKeepingProjectPicker();" in body
    assert "void renderSessionList({deferWhileInteracting:false,keepProjectPicker:true});" in body
    assert "_repaintSidebarForBackgroundChurn();" not in body
    assert "void renderSessionList();" not in body


def test_completion_helper_final_repaint_keeps_the_picker():
    """The shared completion helper must never route its final repaint as churn."""
    body = _repaint_sidebar_after_session_completion_source()
    assert body.count("_repaintSidebarKeepingProjectPicker();") == 2, (
        "the newer-same-owner branch and the final repaint must both keep the picker"
    )
    assert "_repaintSidebarForBackgroundChurn();" not in body


def test_share_completion_and_archive_have_no_bare_cache_repaint():
    """The two completion bodies must not leave a second unmarked repaint."""
    for name, sentinel in (
        ("_syncSessionShareState", "\nasync function _createOrRefreshSessionShare("),
        ("_archiveSession", "\nfunction _projectPickerSessionActionHandoff("),
    ):
        start = SESSIONS_JS.index(f"function {name}(")
        end = SESSIONS_JS.index(sentinel, start)
        assert "renderSessionListFromCache();" not in SESSIONS_JS[start:end], (
            f"{name}: a bare renderSessionListFromCache() would retire a newer picker"
        )


def test_picker_move_completions_use_the_session_owned_repaint():
    """Both picker move completions retire only their own session's picker."""
    start = SESSIONS_JS.index("function _showProjectPicker(")
    end = SESSIONS_JS.index("\nfunction _resizeProjectInput(", start)
    body = SESSIONS_JS[start:end]
    assert body.count("_repaintSidebarAfterSessionCompletion(session.session_id,pickerEpoch);") == 2, (
        "both move completions must carry the interaction generation so a newer "
        "same-owner picker is preserved while the durable move paints"
    )


def test_same_owner_move_completion_keeps_a_newer_picker_and_paints_immediately():
    """C1: a held move cannot dismiss a picker reopened after the POST began."""
    assert NODE is not None
    script = r"""
let _sessionListRepaintDeferredByPicker = false;
let _sidebarRepaintBackgroundChurn = false;
let _sidebarRepaintKeepProjectPicker = false;
let _projectPickerTeardown = null;
let _openProjectPicker = null;
let retired = 0;
const paints = [];
function renderSessionListFromCache(){ paints.push({background:_sidebarRepaintBackgroundChurn,keep:_sidebarRepaintKeepProjectPicker}); }
""" + _retire_project_picker_for_explicit_repaint_source() + _repaint_sidebar_for_background_churn_source() + _repaint_sidebar_keeping_project_picker_source() + _repaint_sidebar_after_session_completion_source() + r"""
const openPicker = (epoch) => {
  _projectPickerTeardown = () => { retired += 1; _projectPickerTeardown = null; _openProjectPicker = null; };
  _openProjectPicker = {sessionId:'a',openEpoch:epoch};
};
openPicker(8);
_repaintSidebarAfterSessionCompletion('a',7);
const staleCompletion = {retired,stillOpen:_openProjectPicker!==null,paints:[...paints]};
_repaintSidebarAfterSessionCompletion('a',8);
const currentCompletion = {retired,closed:_openProjectPicker===null,paints:[...paints]};
console.log(JSON.stringify({staleCompletion,currentCompletion}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "staleCompletion": {
            "retired": 0,
            "stillOpen": True,
            "paints": [{"background": False, "keep": True}],
        },
        "currentCompletion": {
            "retired": 1,
            "closed": True,
            "paints": [
                {"background": False, "keep": True},
                {"background": False, "keep": True},
            ],
        },
    }


def test_background_repaint_does_not_defer_for_an_authoritatively_removed_owner():
    """C2: connected stale DOM is not authority after the cache removed B."""
    assert NODE is not None
    script = r"""
let _sessionListRepaintDeferredByPicker = false;
let _sidebarRepaintBackgroundChurn = true;
let _projectPickerTeardown = () => {};
let _openProjectPicker = {sessionId:'deleted-b'};
let _allSessions = [{session_id:'still-a'}];
let _contentSearchResults = [];
let _sidebarReferenceSessions = [];
let _sessionSourceFilter = 'webui';
let _showArchived = false;
let _activeProject = null;
let S = {session:{session_id:'still-a'}};
function $(id){ return id==='sessionSearch'?{value:''}:null; }
function _sessionRowsWithActiveEphemeralSession(rows){ return rows; }
function _sessionSearchMergeMatches(rows){ return rows; }
function _ensureActiveSessionRowPresent(rows){ return rows; }
function _activeSessionIdForSidebar(){ return 'still-a'; }
function _partitionSidebarSessionRows(rows){ return {sessionsRaw:rows,webuiReferenceRaw:[],cliReferenceRaw:[],projectIdFor:()=>null}; }
function _scopedSidebarReferenceRows(){ return []; }
function _renderSidebarRowsFromRawSessions(rows){ return rows; }
function _sessionLineageContainsSession(row,sid){ return row&&row.session_id===sid; }
""" + _render_sidebar_rows_from_partition_source() + _project_picker_owner_eligible_for_sidebar_source() + _project_picker_blocks_repaint_source() + r"""
console.log(JSON.stringify({eligible:_projectPickerOwnerEligibleForSidebar('deleted-b'),blocks:_projectPickerBlocksRepaint()}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"eligible": False, "blocks": False}


def test_retry_completion_publishes_success_and_failure_while_keeping_newer_picker():
    """C3: native Retry requests immediate keep-picker paints on both exits."""
    js = SESSIONS_JS
    note_start = js.index("function _renderSessionListLoadErrorNote(")
    note_end = js.index("\nasync function _runRenderSessionListRefresh(", note_start)
    note_body = js[note_start:note_end]
    assert "renderSessionList({deferWhileInteracting:false,keepProjectPicker:true})" in note_body

    refresh_start = note_end
    refresh_end = js.index("\nasync function _loadSidebarSessionListPayload(", refresh_start)
    refresh_body = js[refresh_start:refresh_end]
    assert refresh_body.count("opts&&opts.keepProjectPicker===true") >= 2
    assert "_repaintSidebarKeepingProjectPicker();" in refresh_body


def test_send_carries_the_originating_picker_generation_into_delayed_new_session():
    """B1: bundle/catalog awaits cannot recapture a picker opened meanwhile."""
    messages = (REPO / "static" / "messages.js").read_text()
    send_start = messages.index("async function send(){")
    send_end = messages.index("\nfunction ", send_start + 1)
    send_body = messages[send_start:send_end]
    assert "const _sendProjectPickerClaim=" in send_body
    assert "newSession(false,{pickerClaimEpoch:_sendProjectPickerClaim})" in send_body
    metadata_paths = send_body[send_body.index("const _agentCmd="):]
    assert "await newSession();" not in metadata_paths
    assert metadata_paths.count("newSession(false,{pickerClaimEpoch:_sendProjectPickerClaim})") >= 6

    new_start = SESSIONS_JS.index("async function newSession(")
    new_end = SESSIONS_JS.index("\n// #2971", new_start)
    new_body = SESSIONS_JS[new_start:new_end]
    assert "options.pickerClaimEpoch" in new_body


def test_bundle_resolution_error_keeps_the_send_originating_picker_claim():
    """B1 follow-up: the resolver error lane must not recapture a newer picker."""
    assert NODE is not None
    messages = (REPO / "static" / "messages.js").read_text()
    send_start = messages.index("async function send(){")
    send_end = messages.index("\nfunction ", send_start + 1)
    send_source = messages[send_start:send_end]
    script = r"""
let _sendInProgress=false;
let _sendInProgressSid=null;
let _projectPickerOpenEpoch=7;
let _pendingSelections=[];
const msg={value:'/bundle'};
const S={session:null,pendingFiles:[],busy:false,messages:[],activeProfile:'default'};
const document={querySelector(){return null;}};
const COMMANDS=[];
const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();
const claims=[];
let releaseResolver;
let resolverStartedResolve;
const resolverStarted=new Promise(resolve=>{resolverStartedResolve=resolve;});
const resolverGate=new Promise(resolve=>{releaseResolver=resolve;});
function $(id){return id==='msg'?msg:{value:'',focus(){}};}
function _composerTextWithPendingSelections(){return msg.value;}
function _flushSelectionBlocksToComposer(){}
function _clearStaleBusyStateBeforeSend(){}
function parseCommand(){return {name:'bundle',args:''};}
async function getAgentCommandMetadata(){return null;}
async function getBundleCommandMetadata(){return {name:'bundle'};}
async function resolveBundleCommand(){resolverStartedResolve();await resolverGate;throw new Error('held resolver failed');}
async function newSession(_flash,options){claims.push(options&&options.pickerClaimEpoch);S.session={session_id:'new-session'};}
function renderMessages(){}
function autoResize(){}
function hideCmdDropdown(){}
function renderTray(){}
function showToast(){}
function _normalizeGoalContinuationId(id){return String(id||'').trim();}
function _takeRestoredDraftGoalContinuationId(){return '';}
""" + send_source + r"""
(async()=>{
  const pending=send();
  await resolverStarted;
  _projectPickerOpenEpoch=8;
  releaseResolver();
  await pending;
  console.log(JSON.stringify({claims,currentEpoch:_projectPickerOpenEpoch,messages:S.messages}));
})().catch(error=>{console.error(error&&error.stack||error);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["claims"] == [7]
    assert output["currentEpoch"] == 8
    assert output["messages"][-1]["content"] == "Bundle command error: held resolver failed"


def test_picker_owner_qualification_uses_the_complete_renderer_projection():
    """C2 follow-up: active-row preparation and partition references stay canonical."""
    owner = _project_picker_owner_eligible_for_sidebar_source()
    projection = _render_sidebar_rows_from_partition_source()
    assert "_sessionRowsWithActiveEphemeralSession(rows)" in owner
    assert "_ensureActiveSessionRowPresent(searchMatches,sidebarRows)" in owner
    assert "_activeSessionIdForSidebar()" in owner
    assert "_renderSidebarRowsFromPartition(partition)" in owner
    assert "partition.cliReferenceRaw:partition.webuiReferenceRaw" in projection
    assert "_scopedSidebarReferenceRows(isCliView, partition.projectIdFor)" in projection


def test_picker_owner_qualification_composes_partition_reference_rows():
    """An archived-parent reference can suppress a fork in the painted projection."""
    assert NODE is not None
    script = r"""
let _allSessions=[{session_id:'a'},{session_id:'fork',parent_session_id:'archived-parent'}];
let _contentSearchResults=[];
let _sidebarReferenceSessions=[];
let _sessionSourceFilter='webui';
let S={session:{session_id:'a'}};
function $(id){return id==='sessionSearch'?{value:''}:null;}
function _sessionRowsWithActiveEphemeralSession(rows){return rows;}
function _sessionSearchMergeMatches(rows){return rows;}
function _ensureActiveSessionRowPresent(rows){return rows;}
function _activeSessionIdForSidebar(){return 'a';}
function _partitionSidebarSessionRows(rows){return {
  sessionsRaw:rows,
  webuiReferenceRaw:[{session_id:'archived-parent',archived:true}],
  cliReferenceRaw:[],
  projectIdFor:()=>null,
};}
function _scopedSidebarReferenceRows(){return [];}
function _renderSidebarRowsFromRawSessions(rows,references){
  const hasArchivedParent=references.some(row=>row&&row.session_id==='archived-parent');
  return hasArchivedParent?rows.filter(row=>row.session_id!=='fork'):rows;
}
function _sessionLineageContainsSession(row,sid){return row&&row.session_id===sid;}
""" + _render_sidebar_rows_from_partition_source() + _project_picker_owner_eligible_for_sidebar_source() + r"""
console.log(JSON.stringify({
  root:_projectPickerOwnerEligibleForSidebar('a'),
  suppressedFork:_projectPickerOwnerEligibleForSidebar('fork'),
}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"root": True, "suppressedFork": False}


def test_completion_repaint_retires_only_the_mutated_sessions_picker():
    """Owner half of the routing fix, driven through the shipped guards.

    A completion for session A must keep a picker the user opened for session B
    and paint A's durable result on the spot: the keep-picker repaint rebuilds
    the rows, keeps B's overlay for the render to re-anchor, and never parks
    the result behind the open picker. A completion for the session that owns
    the open picker retires that picker instead.
    """
    assert NODE is not None
    script = r"""
let _sessionListRepaintDeferredByPicker = false;
let _sidebarRepaintBackgroundChurn = false;
let _sidebarRepaintKeepProjectPicker = false;
let _projectPickerTeardown = null;
let _openProjectPicker = null;
let retired = 0;
const _allSessions = [{session_id: 'a', pinned: false}, {session_id: 'b', pinned: false}];
const painted = [];
function renderSessionListFromCache(){
  // Mirror the shipped guard: background churn defers while a picker is open.
  if(_projectPickerTeardown !== null && _sidebarRepaintBackgroundChurn){
    _sessionListRepaintDeferredByPicker = true;
    return;
  }
  painted.push(_allSessions.map(s => s.pinned));
}
""" + _retire_project_picker_for_explicit_repaint_source() + _repaint_sidebar_for_background_churn_source() + _repaint_sidebar_keeping_project_picker_source() + _repaint_sidebar_after_session_completion_source() + r"""
const openPicker = (sessionId) => {
  const picker = {};
  _projectPickerTeardown = () => {
    retired += 1;
    if(_openProjectPicker && _openProjectPicker.picker === picker) _openProjectPicker = null;
  };
  _openProjectPicker = {picker, sessionId};
};
// B is choosing a project for 'b' while A's pin response lands: A's result must paint now.
openPicker('b');
_allSessions[0].pinned = true;
_repaintSidebarAfterSessionCompletion('a');
const newerPickerKept = {
  retired,
  stillOpen: _openProjectPicker !== null && _openProjectPicker.sessionId === 'b',
  deferralPending: _sessionListRepaintDeferredByPicker === true,
  painted: painted.length,
};
// Closing B's picker replays nothing: A's result already painted.
_projectPickerTeardown = null;
_openProjectPicker = null;
if(_sessionListRepaintDeferredByPicker){
  _sessionListRepaintDeferredByPicker = false;
  renderSessionListFromCache();
}
const replayed = {painted: painted.length, durableA: painted[painted.length - 1][0]};
// A completion for the session that owns the open picker retires it, then paints.
openPicker('a');
_repaintSidebarAfterSessionCompletion('a');
const ownPickerRetired = {
  retired,
  cleared: _projectPickerTeardown === null && _openProjectPicker === null,
  deferralCleared: _sessionListRepaintDeferredByPicker === false,
  painted: painted.length,
  flagRestored: _sidebarRepaintBackgroundChurn === false,
};
console.log(JSON.stringify({newerPickerKept, replayed, ownPickerRetired}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "newerPickerKept": {"retired": 0, "stillOpen": True, "deferralPending": False, "painted": 1},
        "replayed": {"painted": 1, "durableA": True},
        "ownPickerRetired": {"retired": 1, "cleared": True, "deferralCleared": True, "painted": 2, "flagRestored": True},
    }


def test_picker_handoff_resolves_current_state_for_a_menu_opened_during_a_kept_deferral():
    """A second menu opened while a held-gesture menu keeps the repaint deferred.

    The held-gesture handoff retires the picker but keeps the repaint deferred,
    so the rows on screen trail the caches until the menu-close drain. A menu
    opened in that window must resolve its action owner from the caches without
    repainting (and without rebinding the deferred replay).
    """
    assert NODE is not None
    script = r"""
const classes = (...names) => ({contains: name => names.includes(name)});
const staleSession = {session_id: 'kept-row', pinned: false};
const freshSession = {session_id: 'kept-row', pinned: true};
const _allSessions = [freshSession];
const S = {session: null};
let repaints = 0;
let _projectPickerTeardown = null;              // the first menu already retired the picker
let _sessionListRepaintDeferredByPicker = true; // its held gesture keeps the repaint pending
function renderSessionListFromCache() { repaints += 1; }
const anchor = {isConnected: true, classList: classes('session-actions-trigger')};
""" + _project_picker_session_action_handoff_source() + r"""
const result = _projectPickerSessionActionHandoff(staleSession, anchor);
console.log(JSON.stringify({
  repaints,
  resolvedCurrentSession: result && result.session === freshSession,
  staleClosureNotBound: result && result.session !== staleSession,
  keptAnchor: result && result.anchorEl === anchor,
  stillDeferred: _sessionListRepaintDeferredByPicker === true,
}));
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {
        "repaints": 0,
        "resolvedCurrentSession": True,
        "staleClosureNotBound": True,
        "keptAnchor": True,
        "stillDeferred": True,
    }


def test_external_hide_completion_keeps_other_pickers_and_retires_its_own():
    """The hide completion is owner-aware through the shipped routing.

    An external-hide response for session A must paint A's archived result
    immediately while keeping a picker the user opened for session B, and must
    retire only a picker owned by A itself.
    """
    assert NODE is not None
    script = (
        r"""
let _sessionListRepaintDeferredByPicker = false;
let _sidebarRepaintBackgroundChurn = false;
let _sidebarRepaintKeepProjectPicker = false;
let _projectPickerTeardown = null;
let _openProjectPicker = null;
let retired = 0;
let _allSessions = [{session_id: 'a', archived: false}, {session_id: 'b', archived: false}];
const painted = [];
function renderSessionListFromCache(){
  if(_projectPickerBlocksRepaint()){
    _sessionListRepaintDeferredByPicker = true;
    return;
  }
  painted.push(_allSessions.map(s => !!s.archived));
}
"""
        + _retire_project_picker_for_explicit_repaint_source()
        + _repaint_sidebar_for_background_churn_source()
        + _project_picker_blocks_repaint_source()
        + _repaint_sidebar_keeping_project_picker_source()
        + _repaint_sidebar_after_session_completion_source()
        + _optimistically_archive_session_in_list_source()
        + r"""
const openPicker = (sessionId) => {
  const picker = {};
  _projectPickerTeardown = () => {
    retired += 1;
    if(_openProjectPicker && _openProjectPicker.picker === picker) _openProjectPicker = null;
  };
  _openProjectPicker = {picker, sessionId};
};
// B is choosing a project for 'b' while A's hide response lands: A's archived result must paint now.
openPicker('b');
_optimisticallyArchiveSessionInList('a', true);
const newerPickerKept = {
  retired,
  stillOpen: _openProjectPicker !== null && _openProjectPicker.sessionId === 'b',
  deferralPending: _sessionListRepaintDeferredByPicker === true,
  painted: painted.length,
  archivedA: _allSessions[0].archived === true,
};
// Closing B's picker replays nothing: A's archived result already painted.
_projectPickerTeardown = null;
_openProjectPicker = null;
if(_sessionListRepaintDeferredByPicker){
  _sessionListRepaintDeferredByPicker = false;
  renderSessionListFromCache();
}
const replayed = {painted: painted.length, archivedA: painted[painted.length - 1][0]};
// A's own picker is retired when A's hide completion lands.
openPicker('a');
_optimisticallyArchiveSessionInList('a', true);
const ownPickerRetired = {
  retired,
  cleared: _projectPickerTeardown === null && _openProjectPicker === null,
  deferralCleared: _sessionListRepaintDeferredByPicker === false,
  painted: painted.length,
  flagRestored: _sidebarRepaintBackgroundChurn === false,
};
console.log(JSON.stringify({newerPickerKept, replayed, ownPickerRetired}));
"""
    )
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "newerPickerKept": {
            "retired": 0,
            "stillOpen": True,
            "deferralPending": False,
            "painted": 1,
            "archivedA": True,
        },
        "replayed": {"painted": 1, "archivedA": True},
        "ownPickerRetired": {
            "retired": 1,
            "cleared": True,
            "deferralCleared": True,
            "painted": 2,
            "flagRestored": True,
        },
    }


def test_external_hide_handler_success_wiring_and_archive_failure_control():
    """The handler's awaited completion is routed, and its failure path is inert.

    Success: the archive POST resolves, the optimistic cache write and the
    shared completion repaint run immediately (keeping another session's
    picker), and the full list refresh and success toast follow. Failure: the
    POST rejecting must not write the cache, repaint, or retire any picker.
    """
    assert NODE is not None
    handler_body = _external_hide_handler_source()
    assert handler_body.startswith("{") and handler_body.endswith("}")
    assert "_sidebarRepaintBackgroundChurn" not in handler_body
    script = (
        r"""
let _sessionListRepaintDeferredByPicker = false;
let _sidebarRepaintBackgroundChurn = false;
let _sidebarRepaintKeepProjectPicker = false;
let _projectPickerTeardown = null;
let _openProjectPicker = null;
let retired = 0;
let renderListCalls = 0;
let failMode = false;
const toasts = [];
let _allSessions = [{session_id: 'a', archived: false}, {session_id: 'b', archived: false}, {session_id: 'c', archived: false}];
let session = {session_id: 'a', archived: false};
const S = {session: {session_id: 'a', archived: false}};
const painted = [];
function renderSessionListFromCache(){
  if(_projectPickerBlocksRepaint()){
    _sessionListRepaintDeferredByPicker = true;
    return;
  }
  painted.push(_allSessions.map(s => !!s.archived));
}
function closeSessionActionMenu(){}
async function api(url, opts){ if(failMode) throw new Error('boom'); return {ok: true}; }
function renderSessionList(){ renderListCalls += 1; }
function showToast(msg){ toasts.push(String(msg)); }
function t(key){ return key; }
"""
        + _retire_project_picker_for_explicit_repaint_source()
        + _repaint_sidebar_for_background_churn_source()
        + _project_picker_blocks_repaint_source()
        + _repaint_sidebar_keeping_project_picker_source()
        + _repaint_sidebar_after_session_completion_source()
        + _optimistically_archive_session_in_list_source()
        + r"""
const openPicker = (sessionId) => {
  const picker = {};
  _projectPickerTeardown = () => {
    retired += 1;
    if(_openProjectPicker && _openProjectPicker.picker === picker) _openProjectPicker = null;
  };
  _openProjectPicker = {picker, sessionId};
};
const handler = async ()=>"""
        + handler_body
        + r""";
(async () => {
  openPicker('b');
  await handler();
  const success = {
    retired,
    pickerKept: _openProjectPicker !== null && _openProjectPicker.sessionId === 'b',
    deferralPending: _sessionListRepaintDeferredByPicker === true,
    painted: painted.length,
    renderListCalls,
    sessionFlagSet: session.archived === true,
    savedFlagSet: S.session.archived === true,
    archivedA: _allSessions[0].archived === true,
    toast: toasts[toasts.length - 1],
  };
  failMode = true;
  session = {session_id: 'c', archived: false};
  S.session = {session_id: 'c', archived: false};
  await handler();
  const failure = {
    retired,
    pickerKept: _openProjectPicker !== null && _openProjectPicker.sessionId === 'b',
    deferralPending: _sessionListRepaintDeferredByPicker === true,
    painted: painted.length,
    renderListCalls,
    cUntouched: _allSessions[2].archived === false,
    sessionFlagStayedClear: session.archived === false,
    toast: toasts[toasts.length - 1],
  };
  console.log(JSON.stringify({success, failure}));
})();
"""
    )
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "success": {
            "retired": 0,
            "pickerKept": True,
            "deferralPending": False,
            "painted": 1,
            "renderListCalls": 1,
            "sessionFlagSet": True,
            "savedFlagSet": True,
            "archivedA": True,
            "toast": "session_hidden",
        },
        "failure": {
            "retired": 0,
            "pickerKept": True,
            "deferralPending": False,
            "painted": 1,
            "renderListCalls": 1,
            "cUntouched": True,
            "sessionFlagStayedClear": True,
            "toast": "session_archive_failedboom",
        },
    }


def _session_list_queue_and_refresh_sources() -> str:
    """The real coalescing queue — option merge, drain loop, entry point — plus
    the refresh runner and error setter the queue tests drive."""
    parts = [
        ("function _mergeRenderSessionListOptions(", "\nfunction _showSessionListLoadError("),
        ("function _showSessionListLoadError(", "\nfunction _renderSessionListLoadErrorNote("),
        ("async function _runRenderSessionListRefresh(", "\nasync function _loadSidebarSessionListPayload("),
        ("async function _drainRenderSessionListQueue(", "\nasync function renderSessionList("),
        ("async function renderSessionList(", "\n// ── Gateway session SSE"),
    ]
    chunks = []
    for start_marker, end_marker in parts:
        start = SESSIONS_JS.find(start_marker)
        assert start >= 0, f"{start_marker} not found in static/sessions.js"
        end = SESSIONS_JS.find(end_marker, start + 1)
        assert end > start, f"{end_marker} not found after {start_marker}"
        chunks.append(SESSIONS_JS[start:end] + "\n")
    return "".join(chunks)


def _invalidate_session_list_renders_source() -> str:
    start = SESSIONS_JS.find("function _invalidateSessionListRenders(")
    assert start >= 0, "_invalidateSessionListRenders not found in static/sessions.js"
    end = SESSIONS_JS.find("\nif(typeof window", start + 1)
    assert end > start
    return SESSIONS_JS[start:end] + "\n"


def _refresh_session_list_sources() -> str:
    """The creation-refresh sibling lane: option merge and entry point."""
    parts = [
        ("function _mergeSessionListRefreshOptions(", "\nfunction _refreshSessionListAfterSidebarResume("),
        ("async function refreshSessionList(", "\nfunction _scheduleSessionEventsRefresh("),
    ]
    chunks = []
    for start_marker, end_marker in parts:
        start = SESSIONS_JS.find(start_marker)
        assert start >= 0, f"{start_marker} not found in static/sessions.js"
        end = SESSIONS_JS.find(end_marker, start + 1)
        assert end > start, f"{end_marker} not found after {start_marker}"
        chunks.append(SESSIONS_JS[start:end] + "\n")
    return "".join(chunks)


_QUEUE_HARNESS = r"""
let _renderSessionListGen=0, _renderSessionListInFlight=null, _renderSessionListQueuedRequest=null, _renderSessionListActiveRequest=null;
let _pendingSessionListPayload=null;
let _sessionListLoadError=null;
let _contentSearchResults=[];
let _allSessions=[], _sidebarReferenceSessions=[];
let _allSessionsScope={profile:'default',allProfiles:false,sidebarSource:'webui',excludeHidden:false};
let _showAllProfiles=false, _profileSwitchListEmbargo=false, _cronPollGeneration=0;
let _sessionListHasLoadedOnce=true;
let _sessionListRefreshInFlight=false, _sessionListRefreshPendingRequest=null;
const _SESSION_LIST_BOOT_TIMEOUT_MS=8000;
const S={activeProfile:'default'};
console.warn=()=>{};
function $(){return {value:''};}
function _sessionListQueryString(){return '';}
let _queueUserInteracting=false;
function _isSessionListUserInteracting(){return _queueUserInteracting;}
const _queueDeferredApplies=[];
function _schedulePendingSessionListApply(){_queueDeferredApplies.push(true);}
function _requestedSessionSidebarSource(){return 'webui';}
function _sessionListExcludeHiddenEnabled(){return false;}
function _clearSessionSourceTabCounts(){}
function _scheduleSessionEventsRefresh(){}
function refreshActiveSessionIfExternallyUpdated(){return Promise.resolve();}
const applied=[];
function _applySessionListPayload(sessData,projData,opts){applied.push(JSON.parse(JSON.stringify(opts||{})));}
const paints=[];
function _repaintSidebarKeepingProjectPicker(){paints.push('keep');}
function _repaintSidebarForBackgroundChurn(){paints.push('churn');}
const held=[];
function _loadSidebarSessionListPayload(){return new Promise((resolve,reject)=>{held.push({resolve,reject});});}
const tick=()=>new Promise(r=>setTimeout(r,0));
async function waitForHeld(n){for(let i=0;i<400&&held.length<n;i++)await tick();if(held.length<n)throw new Error('held<'+n);}
const payloadOk={sessData:{sessions:[{session_id:'kept'}]},projData:{projects:[]}};
"""


def test_queued_successor_keeps_retry_recovery_intent_on_both_outcomes():
    """A native Retry's keep-picker / nondeferred intent must survive the
    supersession that discards its response: the queued successor carries it
    through the real queue and settles through the keep-picker lanes on both a
    successful and a distinct failed GET."""
    assert NODE is not None
    script = _QUEUE_HARNESS + _session_list_queue_and_refresh_sources() + r"""
(async()=>{
  const out={};

  // A Retry (immediate keep-picker recovery) is in flight; a plain caller
  // queues behind it; the discarded active response is replaced by a successor
  // that settles successfully.
  const retrySuccess=renderSessionList({deferWhileInteracting:false,keepProjectPicker:true});
  await waitForHeld(1);
  renderSessionList();
  out.retrySuccessQueuedOpts=JSON.parse(JSON.stringify(_renderSessionListQueuedRequest.opts));
  held[0].resolve(payloadOk);
  await waitForHeld(2);
  held[1].resolve(payloadOk);
  await retrySuccess;
  out.retrySuccessApplied=applied.slice(); applied.length=0;
  out.retrySuccessPaints=paints.slice(); paints.length=0;

  // Same supersession, but the successor settles with a distinct injected 500.
  _sessionListLoadError={message:'Could not load conversations.',detail:'previous',retrying:true};
  const retryFailure=renderSessionList({deferWhileInteracting:false,keepProjectPicker:true});
  await waitForHeld(3);
  renderSessionList();
  out.retryFailureQueuedOpts=JSON.parse(JSON.stringify(_renderSessionListQueuedRequest.opts));
  held[2].resolve(payloadOk);
  await waitForHeld(4);
  const http500=new Error('sessions endpoint returned 500');
  http500.status=500;
  held[3].reject(http500);
  await retryFailure;
  out.retryFailureApplied=applied.slice(); applied.length=0;
  out.retryFailurePaints=paints.slice(); paints.length=0;
  out.retryFailureError={message:_sessionListLoadError.message,detail:_sessionListLoadError.detail,retrying:!!_sessionListLoadError.retrying};
  _sessionListLoadError=null;

  // Control: a plain request superseded by a plain request gains nothing, and
  // the drained queue leaves no active request behind.
  const plain=renderSessionList({});
  await waitForHeld(5);
  renderSessionList();
  out.plainQueuedOpts=JSON.parse(JSON.stringify(_renderSessionListQueuedRequest.opts));
  held[4].resolve(payloadOk);
  await waitForHeld(6);
  held[5].resolve(payloadOk);
  await plain;
  out.plainApplied=applied.slice();
  out.activeCleared=_renderSessionListActiveRequest===null;

  console.log(JSON.stringify(out));
})().catch(error=>{console.error(error&&error.stack||error);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "retrySuccessQueuedOpts": {"deferWhileInteracting": False, "keepProjectPicker": True},
        "retrySuccessApplied": [{"unreadGen": 0, "keepProjectPicker": True}],
        "retrySuccessPaints": [],
        "retryFailureQueuedOpts": {"deferWhileInteracting": False, "keepProjectPicker": True},
        "retryFailureApplied": [],
        "retryFailurePaints": ["keep"],
        "retryFailureError": {
            "message": "Could not load conversations.",
            "detail": "sessions endpoint returned 500",
            "retrying": False,
        },
        "plainQueuedOpts": {"deferWhileInteracting": False, "keepProjectPicker": True},
        "plainApplied": [{"unreadGen": 0, "keepProjectPicker": True}],
        "activeCleared": True,
    }


def test_queued_successor_keeps_creation_refresh_intent_through_refresh_session_list():
    """Sibling lane: a session-creation refresh (refreshSessionList with
    force+keep-picker) that gets superseded by a passive tick still settles
    through the same carried intent on both outcomes."""
    assert NODE is not None
    script = _QUEUE_HARNESS + _session_list_queue_and_refresh_sources() + _refresh_session_list_sources() + r"""
(async()=>{
  const out={};

  const creationSuccess=refreshSessionList('new-session',{force:true,keepProjectPicker:true});
  await waitForHeld(1);
  renderSessionList();
  out.creationSuccessQueuedOpts=JSON.parse(JSON.stringify(_renderSessionListQueuedRequest.opts));
  held[0].resolve(payloadOk);
  await waitForHeld(2);
  held[1].resolve(payloadOk);
  await creationSuccess;
  out.creationSuccessApplied=applied.slice(); applied.length=0;
  out.creationSuccessPaints=paints.slice(); paints.length=0;

  const creationFailure=refreshSessionList('new-session',{force:true,keepProjectPicker:true});
  await waitForHeld(3);
  renderSessionList();
  out.creationFailureQueuedOpts=JSON.parse(JSON.stringify(_renderSessionListQueuedRequest.opts));
  held[2].resolve(payloadOk);
  await waitForHeld(4);
  held[3].reject(new Error('sessions endpoint returned 500'));
  await creationFailure;
  out.creationFailureApplied=applied.slice();
  out.creationFailurePaints=paints.slice();

  console.log(JSON.stringify(out));
})().catch(error=>{console.error(error&&error.stack||error);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "creationSuccessQueuedOpts": {"deferWhileInteracting": False, "keepProjectPicker": True},
        "creationSuccessApplied": [{"unreadGen": 0, "keepProjectPicker": True}],
        "creationSuccessPaints": [],
        "creationFailureQueuedOpts": {"deferWhileInteracting": False, "keepProjectPicker": True},
        "creationFailureApplied": [],
        "creationFailurePaints": ["keep"],
    }


def test_profile_scope_invalidation_cancels_queued_recovery_inheritance():
    """The carried intent stays cancellable: a profile/scope invalidation drops
    both the queued successor and the active tracker, so a successor queued
    after the switch cannot inherit the cancelled request's intent."""
    assert NODE is not None
    script = _QUEUE_HARNESS + _session_list_queue_and_refresh_sources() + _invalidate_session_list_renders_source() + r"""
(async()=>{
  const out={};

  const active=renderSessionList({deferWhileInteracting:false,keepProjectPicker:true});
  await waitForHeld(1);
  renderSessionList();
  out.beforeInvalidate=JSON.parse(JSON.stringify(_renderSessionListQueuedRequest.opts));

  _invalidateSessionListRenders();
  out.queuedCleared=_renderSessionListQueuedRequest===null;
  out.activeCleared=_renderSessionListActiveRequest===null;

  renderSessionList();
  out.afterInvalidateQueuedOpts=JSON.parse(JSON.stringify(_renderSessionListQueuedRequest.opts));
  held[0].resolve(payloadOk);
  await waitForHeld(2);
  held[1].resolve(payloadOk);
  await active;
  out.appliedAfter=applied.slice();

  console.log(JSON.stringify(out));
})().catch(error=>{console.error(error&&error.stack||error);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "beforeInvalidate": {"deferWhileInteracting": False, "keepProjectPicker": True},
        "queuedCleared": True,
        "activeCleared": True,
        "afterInvalidateQueuedOpts": {"deferWhileInteracting": False, "keepProjectPicker": True},
        "appliedAfter": [{"unreadGen": 0, "keepProjectPicker": True}],
    }


def test_active_poll_does_not_downgrade_a_queued_native_create_refresh():
    """An active background poll must not leak its deferWhileInteracting:true
    into a refresh queued behind it.

    The native create completion (the shipped ``renderSessionList`` call with
    the keep-picker intent, editor still focused) must inherit only the active
    request's upgrades: the successor settles immediately instead of parking
    the payload as a deferred apply while the interaction continues. The
    rename / default-render lane (a plain ``renderSessionList()``) must not
    pick up a leaked deferral either.
    """
    assert NODE is not None
    script = _QUEUE_HARNESS + _session_list_queue_and_refresh_sources() + r"""
(async()=>{
  const out={};

  // Editor still focused: the list sees an active interaction throughout.
  // Exactly two requests exist — one background poll, plus the create
  // completion refresh queued behind it; no second queued poll.
  _queueUserInteracting=true;
  const poll=renderSessionList({deferWhileInteracting:true});
  await waitForHeld(1);

  const createRefresh=renderSessionList({keepProjectPicker:true});
  out.createQueuedOpts=JSON.parse(JSON.stringify(_renderSessionListQueuedRequest.opts));

  // The poll's discarded response settles, then the successor's real refresh.
  held[0].resolve(payloadOk);
  await waitForHeld(2);
  held[1].resolve(payloadOk);
  await createRefresh;
  out.createApplied=applied.slice(); applied.length=0;
  out.pendingPayloadCleared=_pendingSessionListPayload===null;
  out.deferredApplies=_queueDeferredApplies.length;
  out.stillInteracting=_isSessionListUserInteracting();

  // Rename / default-render lane: a plain renderSessionList() queued behind
  // an active poll gains no leaked deferral either.
  const poll2=renderSessionList({deferWhileInteracting:true});
  await waitForHeld(3);
  renderSessionList();
  out.renameQueuedOpts=JSON.parse(JSON.stringify(_renderSessionListQueuedRequest.opts));
  held[2].resolve(payloadOk);
  await waitForHeld(4);
  held[3].resolve(payloadOk);
  await poll2;
  out.renameApplied=applied.slice();

  console.log(JSON.stringify(out));
})().catch(error=>{console.error(error&&error.stack||error);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "createQueuedOpts": {"deferWhileInteracting": False, "keepProjectPicker": True},
        "createApplied": [{"unreadGen": 0, "keepProjectPicker": True}],
        "pendingPayloadCleared": True,
        "deferredApplies": 0,
        "stillInteracting": True,
        "renameQueuedOpts": {"deferWhileInteracting": False, "keepProjectPicker": True},
        "renameApplied": [{"unreadGen": 0, "keepProjectPicker": True}],
    }


def test_deferred_superseding_poll_cannot_downgrade_native_create_intent():
    """A passive poll queued behind native project creation cannot defer it.

    The default user-owned lane is immediate and picker-preserving, so the
    background request coalesced behind it must retain that stronger intent.
    """
    assert NODE is not None
    script = _QUEUE_HARNESS + _session_list_queue_and_refresh_sources() + r"""
(async()=>{
  _queueUserInteracting=true;

  // Project creation owns the active request. While it is held, the user opens
  // a newer picker and a passive poll supersedes the response. The successor
  // correctly carries both the creation keep and the poll's deferral.
  const creation=renderSessionList({keepProjectPicker:true});
  await waitForHeld(1);
  renderSessionList({deferWhileInteracting:true});
  const queuedOpts=JSON.parse(JSON.stringify(_renderSessionListQueuedRequest.opts));

  held[0].resolve(payloadOk);
  await waitForHeld(2);
  held[1].resolve(payloadOk);
  await creation;

  console.log(JSON.stringify({
    queuedOpts,
    pendingPayload:JSON.parse(JSON.stringify(_pendingSessionListPayload)),
    applied:applied.slice(),
    deferredApplies:_queueDeferredApplies.length,
  }));
})().catch(error=>{console.error(error&&error.stack||error);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "queuedOpts": {"keepProjectPicker": True, "deferWhileInteracting": False},
        "pendingPayload": None,
        "applied": [{"unreadGen": 0, "keepProjectPicker": True}],
        "deferredApplies": 0,
    }

    scheduler_start = SESSIONS_JS.find("function _schedulePendingSessionListApply(")
    scheduler_end = SESSIONS_JS.find("\n\nfunction _sessionAttentionSoundSignature(", scheduler_start)
    assert scheduler_start >= 0 and scheduler_end > scheduler_start
    scheduler = SESSIONS_JS[scheduler_start:scheduler_end]
    assert "if(payload.keepProjectPicker===true) applyOpts.keepProjectPicker=true;" in scheduler
    assert "_applySessionListPayload(payload.sessData,payload.projData,applyOpts);" in scheduler

    replay_script = r"""
let _pendingSessionListPayload=null, _pendingSessionListApplyTimer=0;
let _renderSessionListGen=2;
const SESSION_LIST_INTERACTION_IDLE_MS=1;
const applied=[];
function _isSessionListUserInteracting(){return false;}
function _applySessionListPayload(_sess,_proj,opts){applied.push({...opts});}
""" + scheduler + r"""
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
  _pendingSessionListPayload={gen:2,sessData:{},projData:{},unreadGen:0,keepProjectPicker:true};
  _schedulePendingSessionListApply();
  await wait(150);
  _pendingSessionListPayload={gen:2,sessData:{},projData:{},unreadGen:1};
  _schedulePendingSessionListApply();
  await wait(150);
  console.log(JSON.stringify(applied));
})().catch(error=>{console.error(error&&error.stack||error);process.exit(1);});
"""
    replay = subprocess.run([NODE, "-e", replay_script], check=False, capture_output=True, text=True, timeout=20)
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout) == [
        {"unreadGen": 0, "keepProjectPicker": True},
        {"unreadGen": 1},
    ]


def test_rename_success_ships_the_immediate_keep_picker_contract():
    """The project-rename success transition (editor -> chip) must paint now.

    A picker opened while the rename POST was in flight is newer than the
    completion, so the success path ships the explicit user-action contract:
    an immediate (nondeferred) refresh that keeps and re-anchors that picker,
    instead of a plain call that defers behind the open interaction.
    """
    start = SESSIONS_JS.find("function _startProjectRename(")
    assert start >= 0, "_startProjectRename not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    body = SESSIONS_JS[start:end]
    rename_post = body.find("/api/projects/rename")
    completion = body.find("await renderSessionList({deferWhileInteracting:false,keepProjectPicker:true});")
    toast = body.find("showToast('Project renamed')")
    assert rename_post >= 0, "rename POST not found in _startProjectRename"
    assert completion > rename_post, (
        "rename success must repaint with the explicit nondeferred keep-picker contract"
    )
    assert toast > completion, "the renamed toast must follow the completion refresh"
    assert "await renderSessionList();" not in body, (
        "rename success must not fall back to a plain renderSessionList()"
    )


def test_rename_completion_settles_immediately_through_the_queue():
    """The rename completion refresh settles immediately through the real queue.

    Queued behind an active background poll (or superseded by a later poll
    tick) while the interaction continues, the successor must keep the
    explicit nondeferred keep-picker contract: no parked deferred payload, an
    apply carrying the keep intent, and no downgrade to the churn lane.
    """
    assert NODE is not None
    script = _QUEUE_HARNESS + _session_list_queue_and_refresh_sources() + r"""
(async()=>{
  const out={};

  // Rename completion queues behind an active background poll while the user
  // still interacts (the newer picker). The successor must settle immediately
  // with the keep intent instead of parking a deferred payload.
  _queueUserInteracting=true;
  const poll=renderSessionList({deferWhileInteracting:true});
  await waitForHeld(1);
  const renameRefresh=renderSessionList({deferWhileInteracting:false,keepProjectPicker:true});
  out.behindPollQueuedOpts=JSON.parse(JSON.stringify(_renderSessionListQueuedRequest.opts));
  held[0].resolve(payloadOk);
  await waitForHeld(2);
  held[1].resolve(payloadOk);
  await renameRefresh;
  out.behindPollApplied=applied.slice(); applied.length=0;
  out.behindPollPendingCleared=_pendingSessionListPayload===null;
  out.behindPollDeferredApplies=_queueDeferredApplies.length;

  // A later passive poll tick supersedes an in-flight rename completion: the
  // successor still carries the immediate keep-picker contract — a passive
  // tick cannot downgrade an explicit repaint.
  const renameRefresh2=renderSessionList({deferWhileInteracting:false,keepProjectPicker:true});
  await waitForHeld(3);
  renderSessionList({deferWhileInteracting:true});
  out.supersededQueuedOpts=JSON.parse(JSON.stringify(_renderSessionListQueuedRequest.opts));
  held[2].resolve(payloadOk);
  await waitForHeld(4);
  held[3].resolve(payloadOk);
  await renameRefresh2;
  out.supersededApplied=applied.slice(); applied.length=0;
  out.supersededDeferredApplies=_queueDeferredApplies.length;

  console.log(JSON.stringify(out));
})().catch(error=>{console.error(error&&error.stack||error);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "behindPollQueuedOpts": {"deferWhileInteracting": False, "keepProjectPicker": True},
        "behindPollApplied": [{"unreadGen": 0, "keepProjectPicker": True}],
        "behindPollPendingCleared": True,
        "behindPollDeferredApplies": 0,
        "supersededQueuedOpts": {"deferWhileInteracting": False, "keepProjectPicker": True},
        "supersededApplied": [{"unreadGen": 0, "keepProjectPicker": True}],
        "supersededDeferredApplies": 0,
    }


def _position_session_action_menu_source() -> str:
    start = SESSIONS_JS.find("function _positionSessionActionMenu(")
    assert start >= 0, "_positionSessionActionMenu not found in static/sessions.js"
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    assert end > start
    return SESSIONS_JS[start:end]


def _project_picker_anchor_helpers_source() -> str:
    start = SESSIONS_JS.find("function _projectPickerAnchorRect(")
    assert start >= 0, "_projectPickerAnchorRect not found in static/sessions.js"
    end = SESSIONS_JS.find("function _projectPickerItem(", start)
    assert end > start, "_projectPickerItem sentinel not found after the anchor helpers"
    return SESSIONS_JS[start:end]


_ACTION_MENU_ANCHOR_PREFIX = r"""
let _sessionActionMenu = {style: {}, scrollWidth: 180, offsetHeight: 140};
const menuRowRect = {top: 380, bottom: 428, left: 16, right: 374, width: 358, height: 48};
const menuSessionList = {
  getBoundingClientRect: () => ({top: 0, bottom: 620, left: 0, right: 390}),
};
const menuRow = {isConnected: true, getBoundingClientRect: () => menuRowRect};
const menuAnchorEl = {
  isConnected: true,
  getBoundingClientRect: () => ({top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0}),
  closest: selector => selector === '.session-child-session-fork,.session-item'
    ? menuRow
    : (selector === '.session-list' ? menuSessionList : null),
};
const window = {innerWidth: 768, innerHeight: 620,
  visualViewport: {offsetTop: 0, offsetLeft: 0, width: 768, height: 620}};
"""


def test_action_menu_places_from_the_context_point_when_its_anchor_has_no_size():
    """The ⋮ menu must borrow the anchor geometry the picker places from.

    Coarse-pointer CSS hides `.session-actions`, so the menu that launches the
    picker is anchored on a 0×0 element and opened at the window's top-left
    corner, over the chat header and the filter box. It must place from the
    recorded click point, or, without one, from the visible row.
    """
    assert NODE is not None
    script = (
        _ACTION_MENU_ANCHOR_PREFIX
        + _position_session_action_menu_source()
        + _project_picker_anchor_helpers_source()
        + r"""
menuAnchorEl._projectPickerContextPoint = {clientX: 250, clientY: 300};
_positionSessionActionMenu(menuAnchorEl);
const withPoint = {left: _sessionActionMenu.style.left, top: _sessionActionMenu.style.top};
delete menuAnchorEl._projectPickerContextPoint;
_positionSessionActionMenu(menuAnchorEl);
const rowOnly = {left: _sessionActionMenu.style.left, top: _sessionActionMenu.style.top};
console.log(JSON.stringify({withPoint, rowOnly}));
"""
    )
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    # Left edge at the recorded point (250), matching the project picker.
    assert data["withPoint"] == {"left": "250px", "top": "307px"}, (
        "A zero-sized menu anchor must place from the recorded context point; "
        f"got {data['withPoint']}"
    )
    # No recorded point: borrow the visible row — right-aligned to its right
    # edge, opened under it.
    assert data["rowOnly"] == {"left": "154px", "top": "434px"}, (
        "Without a recorded point the menu must borrow the visible row; "
        f"got {data['rowOnly']}"
    )


def test_picker_defers_choice_replacement_until_pointer_click_finishes():
    """A repaint between pointerdown and pointerup must not replace the pressed row."""
    body = _show_project_picker_source()
    assert "if(pointerGestureId!==null&&!(opts&&opts.force===true))" in body
    assert "return 'deferred';" in body
    assert "choiceRefreshTimer=setTimeout" in body
    assert "refreshChoices({force:true});" in body
    assert "document.addEventListener('pointerup',finishPointerGesture,true);" in body
    assert "document.addEventListener('pointercancel',finishPointerGesture,true);" in body

    data = _run_picker_cases("""
openPicker(260);
const pressed = mountedPicker.children[1];
const before = mountedPicker.children.slice();
(mountedPicker.listeners.pointerdown || [])[0]({button:0,pointerId:7});
const refreshResult = _openProjectPicker.refreshChoices();
docEmitter.dispatch('pointerup',{pointerId:7},true);
const beforeClick = {
  samePressedNode: mountedPicker.children[1] === pressed,
  sameChildren: before.every((item,index) => mountedPicker.children[index] === item),
  timers: timers.size,
};
console.log(JSON.stringify({refreshResult,beforeClick}));
""")
    assert data["refreshResult"] == "deferred"
    assert data["beforeClick"]["samePressedNode"] is True
    assert data["beforeClick"]["sameChildren"] is True
    assert data["beforeClick"]["timers"] >= 1


def test_picker_tap_click_in_a_later_task_moves_to_the_pressed_project():
    """A touch tap's click arrives in a task AFTER pointerup.

    Pre-fix, ``finishPointerGesture`` cleared the gesture id at pointerup, so a
    repaint completing between the release and Chromium's synthesized click
    (the pin action's follow-up GET) could re-place the picker under the finger
    and the tap moved the session to whatever row took the pressed row's place
    ("No project" in the 390x844 CDP repro). The gesture must stay open —
    deferring repaints and holding the pressed node — until its own click (or
    the grace timeout) settles the queued refresh.
    """
    data = _run_picker_cases("""
let positions = [];
function _positionProjectPicker() { positions.push(1); }
openPicker(260);
// The repaint path reads the live children: keep the harness honest so an
// unchanged list short-circuits refreshChoices instead of rebuilding.
mountedPicker.querySelectorAll = (selector) => {
  if (selector === '.project-picker-item') return mountedPicker.children.slice();
  if (selector === '.project-picker-item[data-project-id]')
    return mountedPicker.children.filter((item) => item.getAttribute && item.getAttribute('data-project-id'));
  return [];
};
const pressed = mountedPicker.children[1];
const pressedId = pressed.getAttribute('data-project-id');
const listenersBefore = docEmitter.count();
(mountedPicker.listeners.pointerdown || [])[0]({button:0,pointerId:9});
positions.length = 0;
const refreshResult = _openProjectPicker.refreshChoices();
docEmitter.dispatch('pointerup',{pointerId:9},true);
flushTimersDue(0);
// The tap's own click has not happened yet. A repaint completing in this gap
// (the pressed row's mutation GET) must still be deferred: only a settlement
// due now — the pre-fix zero-delay apply, or a shortened grace — would take
// the queued refresh inside it and re-place the picker.
const gapRefresh = _openProjectPicker.refreshChoices();
const beforeClick = {
  positions: positions.length,
  samePressedNode: mountedPicker.children[1] === pressed,
  listeners: docEmitter.count(),
  pendingTimers: [...timers.values()].filter((entry) => entry.delay > 0).length,
  timerKeys: [...timers.keys()],
};
// The tap's click is its own later task; it must be consumed by the gesture.
docEmitter.dispatch('click',{target: pressed}, true);
const afterClick = {
  listeners: docEmitter.count(),
  timerKeys: [...timers.keys()],
};
pressed.onclick();
const moveCall = apiCalls.filter(c => String(c.url).indexOf('/api/session/move') !== -1).pop();
const movedProjectId = moveCall ? JSON.parse(moveCall.body).project_id : null;
flushTimers();
console.log(JSON.stringify({pressedId, refreshResult, gapRefresh, listenersBefore, beforeClick, afterClick,
  movedProjectId, settledTimers: timers.size}));
""")
    assert data["refreshResult"] == "deferred"
    # Nothing may rebuild or re-place the picker between the release and the
    # click the tap is waiting for.
    assert data["beforeClick"]["positions"] == 0
    assert data["beforeClick"]["samePressedNode"] is True
    # The gesture is still open: its click listener (one extra document
    # listener) and the 400 ms grace timer are armed, and nothing settled.
    assert data["beforeClick"]["listeners"] == data["listenersBefore"] + 1
    assert data["beforeClick"]["pendingTimers"] == 1
    # A repaint completing in the gap (the pressed row's mutation GET) must
    # still be deferred: only a settlement due now — the pre-fix zero-delay
    # apply, or a shortened grace — would take the queued refresh inside it.
    assert data["gapRefresh"] == "deferred", (
        "A repaint between pointerup and the tap's click must stay deferred "
        "until the gesture settles; got " + repr(data["gapRefresh"])
    )
    # The later-task click is consumed by the gesture, which re-arms the apply
    # to run after the click.
    assert data["afterClick"]["listeners"] == data["listenersBefore"]
    assert data["afterClick"]["timerKeys"] != data["beforeClick"]["timerKeys"]
    # The click moved the session to the PRESSED project, not the row that
    # would have taken its place.
    assert data["movedProjectId"] == data["pressedId"]
    # The settled gesture releases the picker again.
    assert data["settledTimers"] == 0


def test_picker_new_press_retires_prior_gesture_fallback():
    """A prior gesture's fallback must not refresh beneath a new press."""
    data = _run_picker_cases("""
openPicker(260);
const firstPressed = mountedPicker.children[1];
(mountedPicker.listeners.pointerdown || [])[0]({button:0,pointerId:9});
const refreshResult = _openProjectPicker.refreshChoices();
docEmitter.dispatch('pointercancel',{pointerId:9},true);
const fallbackTimerKeys = [...timers.keys()];
const listenersWithFallback = docEmitter.count();

const secondPressed = mountedPicker.children[2];
const secondPressedId = secondPressed.getAttribute('data-project-id');
(mountedPicker.listeners.pointerdown || [])[0]({button:0,pointerId:10});
const afterSecondDown = {
  timers: timers.size,
  listeners: docEmitter.count(),
  sameFirstNode: mountedPicker.children[1] === firstPressed,
  sameSecondNode: mountedPicker.children[2] === secondPressed,
};
// A cleared fallback cannot run while the second choice is held.
flushTimers();
const whileSecondHeld = {
  sameFirstNode: mountedPicker.children[1] === firstPressed,
  sameSecondNode: mountedPicker.children[2] === secondPressed,
};
docEmitter.dispatch('pointerup',{pointerId:10},true);
docEmitter.dispatch('click',{target:secondPressed},true);
secondPressed.onclick();
const moveCall = apiCalls.filter(c => String(c.url).indexOf('/api/session/move') !== -1).pop();
const movedProjectId = moveCall ? JSON.parse(moveCall.body).project_id : null;
flushTimers();
console.log(JSON.stringify({refreshResult, fallbackTimerKeys, listenersWithFallback,
  afterSecondDown, whileSecondHeld, secondPressedId, movedProjectId,
  settledTimers:timers.size}));
""")
    assert data["refreshResult"] == "deferred"
    assert len(data["fallbackTimerKeys"]) == 1
    # Starting the second press cancels both the old grace timer and the old
    # capture click listener, while keeping the pending refresh queued.
    assert data["afterSecondDown"]["timers"] == 0
    assert data["afterSecondDown"]["listeners"] == data["listenersWithFallback"] - 1
    assert data["afterSecondDown"]["sameFirstNode"] is True
    assert data["afterSecondDown"]["sameSecondNode"] is True
    assert data["whileSecondHeld"]["sameFirstNode"] is True
    assert data["whileSecondHeld"]["sameSecondNode"] is True
    assert data["movedProjectId"] == data["secondPressedId"]
    assert data["settledTimers"] == 0


def test_picker_refresh_restores_focus_only_when_picker_owned_it():
    body = _show_project_picker_source()
    assert "const ownedFocus=!!(active&&typeof picker.contains==='function'&&picker.contains(active));" in body
    assert "if(ownedFocus&&active!==none&&active!==createItem)" in body


def test_context_point_picker_keeps_its_left_edge_at_the_pointer():
    """A point-anchored picker opens like an OS context menu.

    Right-aligning to a 1px point clamped it to the left margin, over the
    icon rail, at tablet widths; the left edge must sit at the pointer.
    """
    data = _run_picker_cases("""
const pointRow = {isConnected: true, getBoundingClientRect: () => ({top: 380, bottom: 428, left: 16, right: 374, width: 358, height: 48})};
anchorEl.closest = selector => selector === '.session-child-session-fork,.session-item'
  ? pointRow
  : (selector === '.session-list' ? sessionList : null);
_recordProjectPickerContextPoint(anchorEl, {clientX: 40, clientY: 420});
setViewport(620, 390);
setAnchor({top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0});
openPicker(120);
console.log(JSON.stringify(placement()));
""")
    assert data["removed"] is False
    assert data["left"] == 40, (
        "The picker's left edge must sit at the pointer (x=40), not clamp to "
        f"the 8px margin; got left={data['left']}"
    )
    assert data["right"] <= 390 - 8


def test_context_point_picker_shifts_left_to_stay_inside_the_viewport():
    data = _run_picker_cases("""
const pointRow = {isConnected: true, getBoundingClientRect: () => ({top: 380, bottom: 428, left: 16, right: 374, width: 358, height: 48})};
anchorEl.closest = selector => selector === '.session-child-session-fork,.session-item'
  ? pointRow
  : (selector === '.session-list' ? sessionList : null);
_recordProjectPickerContextPoint(anchorEl, {clientX: 368, clientY: 420});
setViewport(620, 390);
setAnchor({top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0});
openPicker(120);
console.log(JSON.stringify(placement()));
""")
    assert data["removed"] is False
    assert data["right"] == 390 - 8, (
        "A pointer near the right edge must shift the picker left just enough "
        f"to keep the 8px margin; got {data['right']}"
    )
    assert data["left"] == 390 - 8 - data["width"]
    assert data["left"] < 368
