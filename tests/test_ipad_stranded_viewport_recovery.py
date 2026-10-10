"""Stranded-viewport recovery regression tests (#6426 re-gate CORE finding).

Reproduction: touch-primary sidebar with a bounded deep-active window
([start, loaded) far below the total), then a jump to scrollTop=0. The DOM
window stays at the deep interval, zero rows are visible, the top sentinel is
outside the viewport, and no scroll-driven batch can reach a position outside
the window — the sidebar stays blank forever. These tests pin the recovery
hook: scroll events arm a deferred, revalidated repair that re-anchors the
touch bounds around the live scroll position without rebuilding DOM during a
momentum gesture.
"""
import json

from tests.test_ipad_sidebar_scroll_stuck import (  # noqa: F401
    SESSIONS_JS,
    _extract_fn,
    _node_tests,
    _run_node_vm,
)


def test_stranded_recovery_wired_into_touch_scroll_path():
    """The touch early-return of _scheduleSessionVirtualizedRender must call
    the recovery hook — without it, a stranded viewport has no repair path
    (master's window recalculation is disabled by the same early-return).
    """
    fn = _extract_fn(SESSIONS_JS, "_scheduleSessionVirtualizedRender")
    touch_idx = fn.find("if(_isTouchPrimary())")
    assert touch_idx >= 0, "Touch early-return must exist"
    touch_block = fn[touch_idx:touch_idx + 400]
    assert "_recoverStrandedTouchViewport(" in touch_block, \
        "Touch scroll path must invoke the stranded-viewport recovery hook"


def test_recovery_timer_released_on_invalidation():
    """_invalidateTouchRender is the unified teardown — it must clear the
    recovery timer so a pending repair cannot fire against torn-down state.
    """
    fn = _extract_fn(SESSIONS_JS, "_invalidateTouchRender")
    assert "_strandedTouchRecoveryTimer" in fn, \
        "Unified invalidation must clear the stranded-recovery timer"
    assert "clearTimeout(_strandedTouchRecoveryTimer)" in fn


def test_recovery_never_renders_inline_on_scroll_event():
    """The scroll-event entry must NOT rebuild DOM synchronously (that is the
    momentum-freeze class of bug this PR exists to fix). The repair may only
    run inside the deferred timer callback.
    """
    fn = _extract_fn(SESSIONS_JS, "_recoverStrandedTouchViewport")
    assert "setTimeout(" in fn, "Recovery must be deferred, not inline"
    timer_cb_start = fn.find("setTimeout(")
    timer_cb = fn[timer_cb_start:]
    assert timer_cb.count("renderSessionListFromCache(") >= 1, \
        "The deferred callback performs the repair render"
    # No render call may appear BEFORE the setTimeout (inline path).
    pre_timer = fn[:timer_cb_start]
    assert "renderSessionListFromCache(" not in pre_timer, \
        "Scroll-event path must never render inline"


def test_threshold_guard_exempts_touch_lists():
    """Round-4 re-gate fix 2: the desktop total<=80 early return must not run
    on touch-primary devices. Touch batching starts at 60 rows, so an 61–80
    session touch list runs the real batch machinery and can strand — the old
    ordering made those lists unrecoverable (~9593 finding).
    """
    fn = _extract_fn(SESSIONS_JS, "_scheduleSessionVirtualizedRender")
    threshold_idx = fn.find("SESSION_VIRTUAL_THRESHOLD_ROWS) return;")
    assert threshold_idx >= 0, "Threshold early-return must exist"
    guard = fn[max(0, threshold_idx - 120):threshold_idx]
    assert "_isTouchPrimary()" in guard, \
        "Threshold early-return must exempt touch-primary devices"


def test_repair_reanchors_bounds_instead_of_preserving_deep_window():
    """The repair must write _sessionTouchStartIndex/_sessionTouchLoadedCount
    around the live scroll position before rendering. A plain from-cache
    render would PRESERVE the stale deep window (unchanged-scope fingerprint),
    which is exactly the stranded state.
    """
    fn = _extract_fn(SESSIONS_JS, "_recoverStrandedTouchViewport")
    cb_idx = fn.find("setTimeout(")
    cb = fn[cb_idx:]
    assert "_sessionTouchStartIndex=" in cb, \
        "Repair must re-anchor the canonical start bound"
    assert "_sessionTouchLoadedCount=" in cb, \
        "Repair must re-anchor the canonical loaded bound"
    assert "firstVisible" in cb, \
        "Re-anchor must key off the live scroll position"
    # The start must be derived from firstVisible with a buffer, and clamped
    # so the window cannot exceed the list.
    assert "SESSION_TOUCH_INITIAL_BATCH" in cb
    assert "SESSION_VIRTUAL_BUFFER_ROWS" in cb


@_node_tests
def test_production_stranded_top_jump_recovers_with_bounded_window():
    """Production-path regression for the exact re-gate reproduction:
    200 sessions, deep-active window [140,200), jump to scrollTop=0 → the
    repair re-anchors the bounds to a bounded top window and re-renders.
    Uses the REAL _touchViewportStranding + _recoverStrandedTouchViewport +
    _touchNextBatchDirection extracted from static/sessions.js.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + """
function extractFunc(name) {
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = SESSIONS_JS.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = SESSIONS_JS.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}

// ── Minimal browser mocks ──────────────────────────────────────────────
const timers = [];
let _now = 1000000;
const realSetTimeout = setTimeout;
const realClearTimeout = clearTimeout;
// Route the production setTimeout/clearTimeout through fakes so the test
// fires the repair synchronously without waiting 1250ms of wall clock.
const sandboxSetTimeout = function(fn, ms) {
  const id = timers.length + 1;
  timers.push({id, fn, ms, armedAt: _now, cancelled: false});
  return id;
};
const sandboxClearTimeout = function(id) {
  const t = timers.find(t => t.id === id);
  if (t) t.cancelled = true;
};

const SESSION_LIST_TOUCH_INTERACTION_IDLE_MS = 1200;
const SESSION_TOUCH_INITIAL_BATCH = 60;
const SESSION_TOUCH_BATCH_SIZE = 40;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 8;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
let _sessionTouchGen = 1;
let _sessionTouchStartIndex = 140;
let _sessionTouchLoadedCount = 200;
let _sessionTouchTotalCount = 200;
let _sessionTouchListEl = null;
let _touchRenderState = null;
let _touchBatchPending = false;
let _touchContinuousBatchOwner = null;
let _strandedTouchRecoveryTimer = 0;
let _sessionListLastScrollAt = 0;
let _pointerActive = false;
let renderCalls = [];
let renderOptsLog = [];

function _isTouchPrimary() { return true; }
function _isSessionListTouchScrolling() {
  const now = Date.now();
  return Boolean(
    _pointerActive ||
    (_sessionListLastScrollAt && now - _sessionListLastScrollAt < SESSION_LIST_TOUCH_INTERACTION_IDLE_MS)
  );
}
function _deferRenderSessionListFromCache() { renderOptsLog.push('deferred'); }

function makeSentinel(display) {
  return {style: {display: display}, getBoundingClientRect() {
    return {top: 900, bottom: 940, left: 0, right: 300, width: 300, height: 40};
  }};
}

// A list whose geometry matches the stranding reproduction: scrollTop=0,
// 200 rows worth of scrollable height, clientHeight 600.
const list = {
  scrollTop: 0,
  clientHeight: 600,
  getBoundingClientRect() {
    return {top: 0, bottom: 600, left: 0, right: 300, width: 300, height: 600};
  },
  querySelector(sel) {
    if (sel === '[data-touch-sentinel-top]') return makeSentinel('none');
    if (sel === '[data-touch-sentinel]') return makeSentinel('');
    return null;
  },
};

// Real boundary helpers, extracted from production.
const bndStart = extractFunc('_touchStartBoundaryNearViewport');
const bndLoaded = extractFunc('_touchLoadedBoundaryNearViewport');
eval(bndStart);
eval(bndLoaded);
eval(extractFunc('_touchNextBatchDirection'));
eval(extractFunc('_sentinelIntersectsViewport'));
eval(extractFunc('_touchViewportStranding'));
// _recoverStrandedTouchViewport references renderSessionListFromCache and the
// timer globals — bind the sandbox timers for it.
const _origSetTimeout = globalThis.setTimeout;
const _origClearTimeout = globalThis.clearTimeout;
globalThis.setTimeout = sandboxSetTimeout;
globalThis.clearTimeout = sandboxClearTimeout;
eval(extractFunc('_recoverStrandedTouchViewport').replace(
  'renderSessionListFromCache({force:true});',
  'renderCalls.push({start:_sessionTouchStartIndex, loaded:_sessionTouchLoadedCount}); ' +
  'renderOptsLog.push("force");'
));

// ── Wire production state to the reproduction ─────────────────────────
_sessionTouchListEl = list;
_touchRenderState = {
  gen: _sessionTouchGen,
  list: list,
  flatRows: new Array(200),
  itemHeight: SESSION_VIRTUAL_ROW_HEIGHT,
};

// The scroll event that reveals the blank viewport:
list.scrollTop = 0;
_sessionListLastScrollAt = Date.now();
_recoverStrandedTouchViewport(list);

const armedCount = timers.filter(t => !t.cancelled).length;
// Fire the armed repair synchronously (gesture long since decayed).
_sessionListLastScrollAt = 0;
for (const t of timers) {
  if (!t.cancelled) { t.cancelled = true; t.fn(); }
}

console.log(JSON.stringify({
  armedCount,
  renderCalls,
  renderOptsLog,
  timersTotal: timers.length,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["armedCount"] == 1, \
        f"Exactly one repair must arm on the stranding scroll event, got {result}"
    assert len(result["renderCalls"]) == 1, \
        f"Repair must render exactly once, got {result}"
    assert result["renderOptsLog"] == ["force"], \
        f"Repair render must be a forced from-cache render, got {result}"
    window = result["renderCalls"][0]
    assert window["start"] == 0, \
        f"Re-anchor must move the window start to the top of the list, got {window}"
    assert window["loaded"] > 0, \
        f"Re-anchored window must paint rows, got {window}"
    # SESSION_TOUCH_INITIAL_BATCH = 60 — the re-anchored window must be a
    # bounded initial batch, never a full-list synchronous paint.
    assert window["loaded"] <= 60, \
        f"Re-anchored window must stay bounded, got {window}"


@_node_tests
def test_production_visible_bottom_sentinel_blocks_downward_recovery():
    """If the BOTTOM sentinel is visible and intersecting (batch machinery
    still covers downward appends), recovery must NOT arm — no racing the
    incremental path.

    Round-4 re-gate inversion: the TOP sentinel must NOT veto recovery. The
    top affordance is never observed by the IntersectionObserver and sits as
    the list's first child whenever start>0, so at scrollTop=0 it is
    permanently intersecting while no upward batch can actually fire — the
    old top-sentinel veto left the blank sidebar unfixed (measured:
    scrollTop 0 and 20 stayed blank forever).
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + """
function extractFunc(name) {
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = SESSIONS_JS.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = SESSIONS_JS.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}

const timers = [];
const sandboxSetTimeout = function(fn, ms) {
  timers.push({fn, cancelled: false});
  return timers.length;
};
const sandboxClearTimeout = function(id) {
  if (timers[id - 1]) timers[id - 1].cancelled = true;
};

const SESSION_LIST_TOUCH_INTERACTION_IDLE_MS = 1200;
const SESSION_TOUCH_INITIAL_BATCH = 60;
const SESSION_TOUCH_BATCH_SIZE = 40;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 8;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
let _sessionTouchGen = 1;
let _sessionTouchStartIndex = 140;
let _sessionTouchLoadedCount = 200;
let _sessionTouchTotalCount = 200;
let _sessionTouchListEl = null;
let _touchRenderState = null;
let _touchBatchPending = false;
let _touchContinuousBatchOwner = null;
let _strandedTouchRecoveryTimer = 0;
let _sessionListLastScrollAt = 0;
let _pointerActive = false;
let renderCalls = [];

function _isTouchPrimary() { return true; }
function _isSessionListTouchScrolling() { return false; }

// Visible BOTTOM sentinel intersecting the viewport with downward-adjacent
// geometry: the IntersectionObserver path is live for appends, so recovery
// must stand down. The TOP sentinel is visible here too — and must NOT veto
// (round-4 inversion: it never had working upward machinery behind it).
function makeSentinel(display) {
  return {style: {display: display}, getBoundingClientRect() {
    return {top: 10, bottom: 50, left: 0, right: 300, width: 300, height: 40};
  }};
}
const list = {
  scrollTop: 0,
  clientHeight: 600,
  getBoundingClientRect() {
    return {top: 0, bottom: 600, left: 0, right: 300, width: 300, height: 600};
  },
  querySelector(sel) {
    if (sel === '[data-touch-sentinel-top]') return makeSentinel('');
    if (sel === '[data-touch-sentinel]') return makeSentinel('');
    return null;
  },
  // Real-rendered-row geometry placing every row BELOW the viewport would
  // strand 'up'; to exercise the downward veto instead, rows span the
  // viewport (first at top edge) so geometry says visible.
  querySelectorAll(sel) {
    if (sel === '.session-item[data-sid]') {
      return [0, 1, 2].map(i => ({getBoundingClientRect() {
        return {top: i*40, bottom: i*40+40, left: 0, right: 300, width: 300, height: 40};
      }}));
    }
    return [];
  },
};

const bndStart = extractFunc('_touchStartBoundaryNearViewport');
const bndLoaded = extractFunc('_touchLoadedBoundaryNearViewport');
eval(bndStart);
eval(bndLoaded);
eval(extractFunc('_touchNextBatchDirection'));
eval(extractFunc('_sentinelIntersectsViewport'));
eval(extractFunc('_touchViewportStranding'));
const _origSetTimeout = globalThis.setTimeout;
const _origClearTimeout = globalThis.clearTimeout;
globalThis.setTimeout = sandboxSetTimeout;
globalThis.clearTimeout = sandboxClearTimeout;
eval(extractFunc('_recoverStrandedTouchViewport').replace(
  'renderSessionListFromCache({force:true});',
  'renderCalls.push(1);'
));

_sessionTouchListEl = list;
_touchRenderState = {gen: _sessionTouchGen, list: list, flatRows: new Array(200), itemHeight: SESSION_VIRTUAL_ROW_HEIGHT};

list.scrollTop = 0;
_sessionListLastScrollAt = Date.now();
_recoverStrandedTouchViewport(list);

console.log(JSON.stringify({
  armed: timers.filter(t => !t.cancelled).length,
  renderCalls: renderCalls.length,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["armed"] == 0 and result["renderCalls"] == 0, \
        f"Visible bottom sentinel must veto recovery, got {result}"


@_node_tests
def test_production_real_geometry_80row_tablet_list_recovers():
    """The 61–80-session touch list case from the re-gate: the desktop
    total<=80 early return used to run BEFORE the touch branch, and stranding
    was computed from the 52px projection that ignored the ~38px sentinel and
    group headers (first real row at 1106px in a 1049px viewport while the
    projection claimed row 20 visible). With real row geometry, window
    [20,80), scrollTop=0, first row below the viewport fold → stranded 'up'
    and the repair arms.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + """
function extractFunc(name) {
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = SESSIONS_JS.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = SESSIONS_JS.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}

const timers = [];
const sandboxSetTimeout = function(fn, ms) {
  timers.push({fn, cancelled: false});
  return timers.length;
};
const sandboxClearTimeout = function(id) {
  if (timers[id - 1]) timers[id - 1].cancelled = true;
};

const SESSION_LIST_TOUCH_INTERACTION_IDLE_MS = 1200;
const SESSION_TOUCH_INITIAL_BATCH = 60;
const SESSION_TOUCH_BATCH_SIZE = 40;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 8;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
let _sessionTouchGen = 1;
let _sessionTouchStartIndex = 20;
let _sessionTouchLoadedCount = 80;
let _sessionTouchTotalCount = 80;
let _sessionTouchListEl = null;
let _touchRenderState = null;
let _touchBatchPending = false;
let _touchContinuousBatchOwner = null;
let _strandedTouchRecoveryTimer = 0;
let _sessionListLastScrollAt = 0;
let _pointerActive = false;
let renderCalls = [];
let renderOptsLog = [];

function _isTouchPrimary() { return true; }
function _isSessionListTouchScrolling() { return false; }

function makeSentinel(display) {
  return {style: {display: display}, getBoundingClientRect() {
    return {top: 900, bottom: 940, left: 0, right: 300, width: 300, height: 40};
  }};
}
const list = {
  scrollTop: 0,
  clientHeight: 1049,
  getBoundingClientRect() {
    return {top: 0, bottom: 1049, left: 0, right: 300, width: 300, height: 1049};
  },
  querySelector(sel) {
    if (sel === '[data-touch-sentinel-top]') return makeSentinel('none');
    if (sel === '[data-touch-sentinel]') return makeSentinel('none');
    return null;
  },
  querySelectorAll(sel) {
    if (sel === '.session-item[data-sid]') {
      // The measured real-device geometry: the first rendered row sits at
      // 1106px — BELOW the 1049px viewport fold. Zero rows visible.
      return Array.from({length: 60}, (_, i) => ({getBoundingClientRect() {
        return {top: 1106 + i*40, bottom: 1106 + i*40 + 40, left: 0, right: 300, width: 300, height: 40};
      }}));
    }
    return [];
  },
};

const bndStart = extractFunc('_touchStartBoundaryNearViewport');
const bndLoaded = extractFunc('_touchLoadedBoundaryNearViewport');
eval(bndStart);
eval(bndLoaded);
eval(extractFunc('_touchNextBatchDirection'));
eval(extractFunc('_sentinelIntersectsViewport'));
eval(extractFunc('_touchViewportStranding'));
const _origSetTimeout = globalThis.setTimeout;
const _origClearTimeout = globalThis.clearTimeout;
globalThis.setTimeout = sandboxSetTimeout;
globalThis.clearTimeout = sandboxClearTimeout;
eval(extractFunc('_recoverStrandedTouchViewport').replace(
  'renderSessionListFromCache({force:true});',
  'renderCalls.push({start:_sessionTouchStartIndex, loaded:_sessionTouchLoadedCount}); renderOptsLog.push("force");'
));

_sessionTouchListEl = list;
_touchRenderState = {gen: _sessionTouchGen, list: list, flatRows: new Array(80), itemHeight: SESSION_VIRTUAL_ROW_HEIGHT};

list.scrollTop = 0;
_sessionListLastScrollAt = Date.now();
_recoverStrandedTouchViewport(list);

const armedCount = timers.filter(t => !t.cancelled).length;
_sessionListLastScrollAt = 0;
for (const t of timers) {
  if (!t.cancelled) { t.cancelled = true; t.fn(); }
}

console.log(JSON.stringify({
  armedCount,
  renderCalls,
  renderOptsLog,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["armedCount"] == 1, \
        f"80-row touch list stranded at the top must arm recovery, got {result}"
    assert len(result["renderCalls"]) == 1, \
        f"Repair must render exactly once, got {result}"
    assert result["renderOptsLog"] == ["force"], \
        f"Repair render must be forced, got {result}"
    window = result["renderCalls"][0]
    assert window["start"] == 0, \
        f"Re-anchor must move the window start to 0 on an 80-row list, got {window}"
    assert 0 < window["loaded"] <= 60, \
        f"Re-anchored window must be a bounded initial batch, got {window}"


@_node_tests
def test_production_real_geometry_visible_rows_do_not_strand():
    """Inverse of the 80-row case: with the same window bounds but rendered
    rows actually inside the viewport, real-geometry stranding must be FALSE
    — the projection fallback's blind spot must not invent work.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + """
function extractFunc(name) {
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = SESSIONS_JS.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = SESSIONS_JS.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}

const SESSION_LIST_TOUCH_INTERACTION_IDLE_MS = 1200;
const SESSION_TOUCH_INITIAL_BATCH = 60;
const SESSION_TOUCH_BATCH_SIZE = 40;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 8;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
let _sessionTouchGen = 1;
let _sessionTouchStartIndex = 20;
let _sessionTouchLoadedCount = 80;
let _sessionTouchTotalCount = 80;
let _sessionTouchListEl = null;
let _touchRenderState = null;
let _touchBatchPending = false;
let _touchContinuousBatchOwner = null;

function makeSentinel(display) {
  return {style: {display: display}, getBoundingClientRect() {
    return {top: 900, bottom: 940, left: 0, right: 300, width: 300, height: 40};
  }};
}
const list = {
  scrollTop: 200,
  clientHeight: 600,
  getBoundingClientRect() {
    return {top: 0, bottom: 600, left: 0, right: 300, width: 300, height: 600};
  },
  querySelector(sel) {
    if (sel === '[data-touch-sentinel-top]') return makeSentinel('none');
    if (sel === '[data-touch-sentinel]') return makeSentinel('none');
    return null;
  },
  querySelectorAll(sel) {
    if (sel === '.session-item[data-sid]') {
      // Rows painted across the viewport: first at -100 (clipped above),
      // through +500. Geometry says the user sees real rows.
      return Array.from({length: 20}, (_, i) => ({getBoundingClientRect() {
        return {top: -100 + i*40, bottom: -100 + i*40 + 40, left: 0, right: 300, width: 300, height: 40};
      }}));
    }
    return [];
  },
};

const bndStart = extractFunc('_touchStartBoundaryNearViewport');
const bndLoaded = extractFunc('_touchLoadedBoundaryNearViewport');
eval(bndStart);
eval(bndLoaded);
eval(extractFunc('_touchNextBatchDirection'));
eval(extractFunc('_sentinelIntersectsViewport'));
eval(extractFunc('_touchViewportStranding'));

_sessionTouchListEl = list;
_touchRenderState = {gen: _sessionTouchGen, list: list, flatRows: new Array(80), itemHeight: SESSION_VIRTUAL_ROW_HEIGHT};

const verdict = _touchViewportStranding(list);
console.log(JSON.stringify(verdict));
"""
    result = json.loads(_run_node_vm(source))
    assert result["stranded"] is False, \
        f"Rows visible in the viewport must NOT strand, got {result}"


@_node_tests
def test_append_setup_re_arms_stranded_recovery_after_background_repaint():
    """Re-gate CORE finding 1: a background repaint (session-updated SSE,
    focus refresh, poll apply) between a stranding scroll and its recovery
    timer runs through _setupTouchSentinel, whose _invalidateTouchRender
    CANCELS the armed repair while preserving the deep window [140,200) —
    and _scheduleContinuousBatch schedules nothing because the stranded
    viewport is near neither batch boundary. The setup path must re-assess
    stranding so the repaint cannot leave the sidebar permanently blank.
    Reproduced in Chromium: 200 sessions, jump to scrollTop=0, repaint 1.2s
    later → zero rows visible forever without this hook."""
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + """
function extractFunc(name) {
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = SESSIONS_JS.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = SESSIONS_JS.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}

const timers = [];
const sandboxSetTimeout = function(fn, ms) {
  timers.push({fn, cancelled: false});
  return timers.length;
};
const sandboxClearTimeout = function(id) {
  if (timers[id - 1]) timers[id - 1].cancelled = true;
};

const SESSION_LIST_TOUCH_INTERACTION_IDLE_MS = 1200;
const SESSION_TOUCH_INITIAL_BATCH = 60;
const SESSION_TOUCH_BATCH_SIZE = 40;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 8;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
let _sessionTouchGen = 1;
let _sessionTouchStartIndex = 140;   // deep-active window preserved by repaint
let _sessionTouchLoadedCount = 200;
let _sessionTouchTotalCount = 200;
let _sessionTouchListEl = null;
let _touchRenderState = null;
let _touchBatchPending = false;
let _touchContinuousBatchOwner = null;
let _strandedTouchRecoveryTimer = 0;
let _touchSentinelObserver = null;
let _sessionListLastScrollAt = 0;
let _pointerActive = false;
let renderCalls = [];
let renderOptsLog = [];

function _isTouchPrimary() { return true; }
function _isSessionListTouchScrolling() { return false; }

function makeSentinel(display) {
  return {style: {display: display}, getBoundingClientRect() {
    return {top: 1200, bottom: 1240, left: 0, right: 300, width: 300, height: 40};
  }};
}
// Repaint-outcome list: the deep window survived, so the rendered rows sit
// FAR below the viewport (scrollTop=0 shows nothing).
const list = {
  scrollTop: 0,
  clientHeight: 600,
  getBoundingClientRect() {
    return {top: 0, bottom: 600, left: 0, right: 300, width: 300, height: 600};
  },
  querySelector(sel) {
    if (sel === '[data-touch-sentinel-top]') return makeSentinel('none');
    if (sel === '[data-touch-sentinel]') return makeSentinel('none');
    return null;
  },
  querySelectorAll(sel) {
    if (sel === '.session-item[data-sid]') {
      return Array.from({length: 60}, (_, i) => ({getBoundingClientRect() {
        return {top: 1106 + i*40, bottom: 1106 + i*40 + 40, left: 0, right: 300, width: 300, height: 40};
      }}));
    }
    if (sel === '.session-date-group') return [];
    return [];
  },
  addEventListener() {},
  removeEventListener() {},
  appendChild() {},
  insertBefore() {},
};

const setupFn = extractFunc('_setupTouchSentinel')
  // Neuter the touch-exit block ENTIRELY (guard AND its return): replacing
  // only the guard with an always-true condition would leave the block's
  // `return;` executing on every call — the function would exit at line 2.
  .replace("if(!list||!_isTouchPrimary()){", "if(false){")
  .replace('_invalidateTouchRender();', '_invalidateTouchRender && _invalidateTouchRender();')
  .replace('if(_touchSentinelObserver||_touchRenderState||_touchScrollOwner||_touchBatchPending||_sessionTouchListEl){', 'if(false){');
eval(setupFn);
const bndStart = extractFunc('_touchStartBoundaryNearViewport');
const bndLoaded = extractFunc('_touchLoadedBoundaryNearViewport');
eval(bndStart);
eval(bndLoaded);
eval(extractFunc('_touchNextBatchDirection'));
eval(extractFunc('_sentinelIntersectsViewport'));
eval(extractFunc('_touchViewportStranding'));
const _origSetTimeout = globalThis.setTimeout;
const _origClearTimeout = globalThis.clearTimeout;
globalThis.setTimeout = sandboxSetTimeout;
globalThis.clearTimeout = sandboxClearTimeout;
eval(extractFunc('_recoverStrandedTouchViewport').replace(
  'renderSessionListFromCache({force:true});',
  'renderCalls.push({start:_sessionTouchStartIndex, loaded:_sessionTouchLoadedCount}); renderOptsLog.push("force");'
));
function _invalidateTouchRender() {}
function _ensureTouchSentinelObserver() {}
function _scheduleContinuousBatch() {}
function _touchIntervalState(total, startIndex, endIndex) {
  const tt=Math.max(0, Number(total)||0);
  const st=Math.min(tt, Math.max(0, Number(startIndex)||0));
  const en=Math.min(tt, Math.max(st, Number(endIndex)||0));
  return {start:st, end:en, total:tt, complete:st===0&&en===tt};
}
const _touchBatchToken = 0;
const requestAnimationFrame = function() { return 0; };
function t(key) { return key; }
const document = {createElement: () => ({
  style: {}, className: '', setAttribute() {},
  querySelector: () => null, appendChild() {},
})};

_sessionTouchListEl = list;
_touchRenderState = {gen: _sessionTouchGen, list: list, flatRows: new Array(200), itemHeight: SESSION_VIRTUAL_ROW_HEIGHT};
list.scrollTop = 0;

// The background repaint: setup runs with the stranded deep window live.
_setupTouchSentinel(list, 200, new Array(200), () => null, null, 200, 140);

const armedCount = timers.filter(t => !t.cancelled).length;
// Fire the re-armed repair synchronously.
_sessionListLastScrollAt = 0;
for (const tm of timers) {
  if (!tm.cancelled) { tm.cancelled = true; tm.fn(); }
}

console.log(JSON.stringify({armedCount, renderCalls, renderOptsLog}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["armedCount"] == 1, \
        (f"Background repaint over a stranded viewport must re-arm recovery, "
         f"got {result}")
    assert len(result["renderCalls"]) == 1, \
        f"Re-armed repair must render exactly once, got {result}"
    window = result["renderCalls"][0]
    assert window["start"] == 0, \
        f"Re-armed repair must re-anchor the window to the top, got {window}"
