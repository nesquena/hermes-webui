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
let repaints = 0;
const renderSessionListFromCache = () => { repaints += 1; };
const t = key => key;
let nextTask = 0;
const frames = new Map();
const timers = new Map();
const setTimeout = fn => { const id = ++nextTask; timers.set(id, fn); return id; };
const clearTimeout = id => timers.delete(id);
const requestAnimationFrame = fn => { const id = ++nextTask; frames.set(id, fn); return id; };
const cancelAnimationFrame = id => frames.delete(id);
function flushFrames() { const batch = [...frames.values()]; frames.clear(); batch.forEach(fn => fn()); }
function flushTimers() { const batch = [...timers.values()]; timers.clear(); batch.forEach(fn => fn()); }

// Module-scope teardown hook declared just above _showProjectPicker in
// static/sessions.js; the extracted function body assigns it.
let _projectPickerTeardown = null;

let anchorRect = {top: 680, bottom: 720, left: 410, right: 440, width: 30, height: 40};
let anchorConnected = true;
const anchorEl = {
  get isConnected() { return anchorConnected; },
  getBoundingClientRect: () => anchorRect,
  closest: selector => selector === '.session-list' ? sessionList : null,
  contains: target => target === anchorEl,
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
        "document": 2,
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
    if anchor_left >= 100 + viewport_width:
        assert data["removed"], "An anchor outside the visual viewport must close."
    else:
        assert not data["removed"]
        assert data["left"] >= 108
        assert data["right"] <= 100 + viewport_width - 8


def test_anchor_hidden_by_keyboard_closes_without_layout_resize():
    data = _run_picker_cases("""
openPicker(260);
viewport.height = 500;
vvEmitter.dispatch('resize');
flushFrames();
console.log(JSON.stringify(placement()));
""")
    assert data["removed"]
    assert data["listenerCounts"] == {"window": 0, "visualViewport": 0, "document": 0}
    assert data["observers"] == 0


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
    assert data["clipped"]["listenerCounts"] == {"window": 0, "visualViewport": 0, "document": 0}


def test_sidebar_render_closes_disconnected_anchor_without_viewport_event():
    data = _run_picker_cases("""
openPicker(260);
anchorConnected = false;
flushMutations();
console.log(JSON.stringify(placement()));
""")
    assert data["removed"], "Sidebar replacement must close before a later resize/scroll."
    assert data["observers"] == 0
    assert data["listenerCounts"] == {"window": 0, "visualViewport": 0, "document": 0}


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
const closed = placement();
flushFrames();
flushTimers();
console.log(JSON.stringify({{queued, closed, after: placement()}}));
""")
    assert data["queued"] == 1, "Viewport events must coalesce into one frame."
    for key in ("closed", "after"):
        assert data[key]["removed"]
        assert data[key]["frames"] == 0
        assert data[key]["observers"] == 0
        assert data[key]["listenerCounts"] == {"window": 0, "visualViewport": 0, "document": 0}


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
    assert data["listenerCounts"] == {"window": 1, "visualViewport": 2, "document": 2}


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
    assert data["live"]["listenerCounts"] == {"window": 1, "visualViewport": 2, "document": 2}
    assert data["closed"]["removed"]
    assert data["closed"]["observers"] == 0
    assert data["closed"]["listenerCounts"] == {"window": 0, "visualViewport": 0, "document": 0}
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
