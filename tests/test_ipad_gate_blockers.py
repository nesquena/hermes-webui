"""Gate-blocker regression tests for the iPad sidebar scroll PR.

Each test targets one blocking finding from the most recent gate certificate:
consecutive deep-active renders, tap-vs-scroll cooldown separation,
directional loading affordances, and forced direct group-collapse renders.
"""
import json

from tests.test_ipad_sidebar_scroll_stuck import (  # noqa: F401
    SESSIONS_JS,
    _extract_fn,
    _node_test_preamble,
    _node_tests,
    _run_node_vm,
)

# Re-exported marker is consumed via the decorator below.



def test_deep_active_window_preserved_on_consecutive_unchanged_scope_renders():
    """Source-level pin: the touch branch of _sessionVirtualWindow must seed
    the window from the CURRENT _sessionTouchStartIndex/_sessionTouchLoadedCount
    (preserving the bounded interval), not unconditionally reset start=0.

    The gate reproduced: a bounded first paint followed by an ordinary
    same-scope repaint (timestamp/SSE/background refresh) repainting
    [0, loadedCount) — synchronously re-rendering thousands of rows.
    """
    fn = _extract_fn(SESSIONS_JS, "_sessionVirtualWindow")
    touch_idx = fn.find("_isTouchPrimary()")
    touch_fn = fn[touch_idx:]
    assert "_sessionTouchStartIndex" in touch_fn, \
        "Touch branch must seed start from the current bounded window"
    # The old reset must be gone: no unconditional start=0 seed.
    assert "let start=0;" not in touch_fn, \
        "Touch branch must NOT reset start to 0 on every render"


def test_touch_window_reset_only_on_scope_change():
    """renderSessionListFromCache must reset the touch bounds only when the
    scope fingerprint changes — unchanged scope keeps the bounded interval.
    """
    fn = _extract_fn(SESSIONS_JS, "renderSessionListFromCache")
    fp_idx = fn.find("scopeFingerprint=")
    assert fp_idx >= 0, "Scope fingerprint must be computed"
    window = fn[fp_idx:fp_idx + 3000]
    assert "prevFingerprint!==scopeFingerprint" in window, \
        "Bounds reset must be gated on a fingerprint CHANGE"
    assert "_sessionTouchLoadedCount=SESSION_TOUCH_INITIAL_BATCH" in window, \
        "Reset must restore the initial batch size"
    assert "_sessionTouchStartIndex=0" in window, \
        "Reset must also zero the start index — a stale deep-active start " \
        "collapses the next scope's window to [staleStart, staleStart)"


@_node_tests
def test_production_consecutive_deep_active_renders_stay_bounded():
    """Production-path regression: two consecutive renderSessionListFromCache-
    shaped deep-active renders (unchanged scope) must both stay bounded.

    Uses the real _sessionVirtualWindow exactly as renderSessionListFromCache
    calls it — first paint with the scope-fingerprint reset applied, second
    paint with bounds retained. The gate reproduced the second call expanding
    to the full list (10,000 rows rendered synchronously).
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
let _sessionTouchLoadedCount = 0;
let _sessionTouchStartIndex = 0;
const SESSION_TOUCH_INITIAL_BATCH = 60;
const SESSION_TOUCH_BATCH_SIZE = 40;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 8;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
function _isTouchPrimary() {{ return true; }}
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = SESSIONS_JS.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = SESSIONS_JS.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < SESSIONS_JS.length) {{
    if (SESSIONS_JS[i] === '{{') depth++;
    else if (SESSIONS_JS[i] === '}}') depth--;
    i++;
  }}
  return SESSIONS_JS.slice(start, i);
}}
eval(extractFunc('_sessionVirtualWindow'));

// Extract the PRODUCTION scope-fingerprint reset from
// renderSessionListFromCache — do NOT hand-copy it here. The gate caught a
// false-green where the local helper reset both bounds while production
// (at the time) reset only _sessionTouchLoadedCount, hiding the zero-row
// regression. Extract the actual block so the test follows production.
const renderFn = extractFunc('renderSessionListFromCache');
const resetStart = renderFn.indexOf('if(prevFingerprint!==scopeFingerprint)');
if (resetStart < 0) throw new Error('scope-fingerprint reset branch not found');
const CLOSE_BRACE = String.fromCharCode(125); // f-string-safe literal close brace
const resetBlock = renderFn.slice(
  resetStart, renderFn.indexOf(CLOSE_BRACE, renderFn.indexOf('_sessionTouchLoadedCount=SESSION_TOUCH_INITIAL_BATCH', resetStart)) + 1
);
if (!resetBlock.includes('_sessionTouchLoadedCount=SESSION_TOUCH_INITIAL_BATCH')) {{
  throw new Error('reset block extraction failed');
}}
let prevFingerprint = '';               // simulate a scope fingerprint CHANGE
let scopeFingerprint = 'scope-A';
let list = {{ dataset: {{}} }};             // production writes the new fingerprint here
function scopeReset() {{ eval(resetBlock); }}

const opts = {{
  total: 10000,
  scrollTop: 0,
  viewportHeight: 1180,
  itemHeight: SESSION_VIRTUAL_ROW_HEIGHT,
  buffer: SESSION_VIRTUAL_BUFFER_ROWS,
  threshold: SESSION_VIRTUAL_THRESHOLD_ROWS,
  activeIndex: 9999,
}};

// Render 1: scope change (deep-active selection) → bounded window.
scopeReset();
const w1 = _sessionVirtualWindow(opts);

// Render 2: UNCHANGED scope — NO reset. Timestamp/SSE/background refresh.
const w2 = _sessionVirtualWindow(opts);

console.log(JSON.stringify({{
  w1rows: w1.end - w1.start,
  w2rows: w2.end - w2.start,
  w2start: w2.start,
  w1start: w1.start,
  w2end: w2.end,
  activeVisible: 9999 >= w2.start && 9999 < w2.end,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["w1rows"] <= 80, \
        f"First deep-active render must be bounded, got {result['w1rows']} rows"
    assert result["w2rows"] <= 80, \
        f"Second unchanged-scope render must STAY bounded, got {result['w2rows']} rows"
    assert result["w2start"] == result["w1start"], result
    assert result["activeVisible"], "Active row must remain inside the bounded window"


@_node_tests
def test_scope_change_resets_bounds_on_production_window():
    """A real scope change (fingerprint reset between renders) must re-anchor
    the window to the initial batch — the reset path still works.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
let _sessionTouchLoadedCount = 0;
let _sessionTouchStartIndex = 0;
const SESSION_TOUCH_INITIAL_BATCH = 60;
const SESSION_TOUCH_BATCH_SIZE = 40;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 8;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
function _isTouchPrimary() {{ return true; }}
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = SESSIONS_JS.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = SESSIONS_JS.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < SESSIONS_JS.length) {{
    if (SESSIONS_JS[i] === '{{') depth++;
    else if (SESSIONS_JS[i] === '}}') depth--;
    i++;
  }}
  return SESSIONS_JS.slice(start, i);
}}
eval(extractFunc('_sessionVirtualWindow'));

// Extract the PRODUCTION scope-fingerprint reset (same rationale as the
// consecutive-renders test above — no hand-copied local helper).
const renderFn = extractFunc('renderSessionListFromCache');
const resetStart = renderFn.indexOf('if(prevFingerprint!==scopeFingerprint)');
if (resetStart < 0) throw new Error('scope-fingerprint reset branch not found');
const CLOSE_BRACE = String.fromCharCode(125); // f-string-safe literal close brace
const resetBlock = renderFn.slice(
  resetStart, renderFn.indexOf(CLOSE_BRACE, renderFn.indexOf('_sessionTouchLoadedCount=SESSION_TOUCH_INITIAL_BATCH', resetStart)) + 1
);
if (!resetBlock.includes('_sessionTouchLoadedCount=SESSION_TOUCH_INITIAL_BATCH')) {{
  throw new Error('reset block extraction failed');
}}
let prevFingerprint = '';               // simulate a scope fingerprint CHANGE
let scopeFingerprint = 'scope-A';
let list = {{ dataset: {{}} }};             // production writes the new fingerprint here
function scopeReset() {{ eval(resetBlock); }}

const opts = {{
  total: 10000,
  scrollTop: 0,
  viewportHeight: 1180,
  itemHeight: SESSION_VIRTUAL_ROW_HEIGHT,
  buffer: SESSION_VIRTUAL_BUFFER_ROWS,
  threshold: SESSION_VIRTUAL_THRESHOLD_ROWS,
  activeIndex: 9999,
}};

scopeReset();
_sessionVirtualWindow(opts); // bounded deep window [9940,10000)

// Scope CHANGE: profile/filter switch resets bounds to the initial batch.
prevFingerprint = scopeFingerprint;
scopeFingerprint = 'scope-B';
scopeReset();
const w = _sessionVirtualWindow({{...opts, activeIndex: -1, total: 100}});
// Model the caller's row loop using the returned production interval.
const painted = Array.from({{length: 100}}, (_, i) => i)
  .filter(i => i >= w.start && i < w.end);
console.log(JSON.stringify({{ start: w.start, end: w.end, painted }}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["start"] == 0, \
        f"Scope change must reset start to 0, got {result['start']}"
    assert result["end"] == 60, \
        f"Scope change must reset end to the initial batch, got {result['end']}"
    assert result["painted"] == list(range(60)), result


@_node_tests
def test_invalidation_cancels_deferred_touch_render_timer():
    """A queued cooldown render must not repaint after skeleton/profile teardown."""
    source = "const SESSIONS_JS = " + repr(SESSIONS_JS) + ";\n" + r"""
function extractFunc(name) {
  const start=SESSIONS_JS.indexOf('function '+name+'(');
  if(start<0) throw Error(name+' missing');
  let i=SESSIONS_JS.indexOf('{',start)+1, depth=1;
  while(depth && i<SESSIONS_JS.length){
    if(SESSIONS_JS[i]==='{') depth++;
    else if(SESSIONS_JS[i]==='}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start,i);
}
let _pendingTouchDeferredRenderTimer=0, _touchSentinelObserver=null;
let _touchScrollOwner=null, _touchRenderState=null, _sessionTouchListEl=null;
let _sessionTouchStartIndex=7, _sessionTouchLoadedCount=60, _sessionTouchTotalCount=100;
let _touchBatchPending=false, _touchContinuousBatchOwner=null;
let _touchContinuousBatchScheduled=false, _sessionTouchGen=1, _touchBatchToken=1;
const SESSION_LIST_TOUCH_INTERACTION_IDLE_MS=1200;
let nextId=0, callbacks=new Map(), renders=0, cancellations=0;
function setTimeout(fn){ callbacks.set(++nextId,fn); return nextId; }
function clearTimeout(id){ if(callbacks.delete(id)) cancellations++; }
function renderSessionListFromCache(){ renders++; }
const requestAnimationFrame=()=>0, cancelAnimationFrame=()=>{};
eval(extractFunc('_deferRenderSessionListFromCache'));
eval(extractFunc('_invalidateTouchRender'));
_deferRenderSessionListFromCache();
const queued=_pendingTouchDeferredRenderTimer;
_invalidateTouchRender();
for(const fn of callbacks.values()) fn();
console.log(JSON.stringify({queued,cancellations,pending:_pendingTouchDeferredRenderTimer,
  remaining:callbacks.size,renders,gen:_sessionTouchGen}));
"""
    result = json.loads(_run_node_vm(source))
    assert result == {"queued": 1, "cancellations": 1, "pending": 0,
                      "remaining": 0, "renders": 0, "gen": 2}, result


def test_pointer_tap_does_not_write_scroll_timestamp():
    """Pointer down/up must NOT touch _sessionListLastScrollAt — only real
    scroll events set the momentum cooldown timestamp. A plain tap (down+up,
    zero scroll events) must leave _isSessionListTouchScrolling() false.
    """
    down = _extract_fn(SESSIONS_JS, "_markSessionListPointerDown")
    up = _extract_fn(SESSIONS_JS, "_markSessionListPointerUp")
    assert "_sessionListLastScrollAt" not in down, \
        "Pointer down must not write the scroll timestamp (tap ≠ scroll)"
    assert "_sessionListLastScrollAt" not in up, \
        "Pointer up must not write the scroll timestamp (tap ≠ scroll)"
    assert "_sessionListPointerActive" in down and "_sessionListPointerActive" in up, \
        "Pointer handlers must still track the active-pointer latch"


@_node_tests
def test_tap_without_scroll_enters_no_cooldown():
    """Executed control: a pointer down/up sequence with ZERO scroll events
    must leave _isSessionListTouchScrolling() false (no momentum cooldown).
    A real scroll event must leave it true. Both controls in one probe.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
let _sessionListLastScrollAt = 0;
let _sessionListPointerActive = false;
const SESSION_LIST_TOUCH_INTERACTION_IDLE_MS = 1200;
function _isTouchPrimary() {{ return true; }}
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = SESSIONS_JS.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = SESSIONS_JS.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < SESSIONS_JS.length) {{
    if (SESSIONS_JS[i] === '{{') depth++;
    else if (SESSIONS_JS[i] === '}}') depth--;
    i++;
  }}
  return SESSIONS_JS.slice(start, i);
}}
eval(extractFunc('_isSessionListTouchScrolling'));
eval(extractFunc('_markSessionListPointerDown'));
eval(extractFunc('_markSessionListPointerUp'));
let _pendingSessionListPayload = null;
function _schedulePendingSessionListApply() {{}}

// TAP: down → up, no scroll event.
_markSessionListPointerDown();
_markSessionListPointerUp();
const tapScrolling = _isSessionListTouchScrolling();
const tapPointerActive = _sessionListPointerActive;

// REAL SCROLL: a scroll event stamps the timestamp (as the list's scroll
// listener / _scheduleSessionVirtualizedRender does in production).
_sessionListLastScrollAt = Date.now() - 10;
const scrolledTouching = _isSessionListTouchScrolling();

// After the idle window, even a recent scroll stops counting.
_sessionListLastScrollAt = Date.now() - (SESSION_LIST_TOUCH_INTERACTION_IDLE_MS + 500);
const settledScrolling = _isSessionListTouchScrolling();

console.log(JSON.stringify({{
  tapScrolling, tapPointerActive, scrolledTouching, settledScrolling,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["tapScrolling"] is False, \
        "Tap without scroll must NOT enter the momentum cooldown"
    assert result["tapPointerActive"] is False, \
        "Pointer latch must clear on pointer up"
    assert result["scrolledTouching"] is True, \
        "A real scroll event must enter the momentum cooldown"
    assert result["settledScrolling"] is False, \
        "After the idle window, scrolling must read false"


def test_bottom_sentinel_hidden_when_end_equals_total():
    """_updateTouchSentinel must hide the bottom affordance whenever
    end>=total — even when start>0 (prefix still virtual). The old contract
    keyed the single bottom sentinel off interval.complete, leaving a
    "Loading more…" spinner pointing at rows that do not exist below a
    deep-active window.
    """
    fn = _extract_fn(SESSIONS_JS, "_updateTouchSentinel")
    assert "interval.end>=interval.total" in fn, \
        "Bottom affordance must key off end>=total"
    assert "interval.complete" not in fn, \
        "The old complete-only gate must be gone"


def test_top_sentinel_affordance_exists_and_directional():
    """A distinct TOP loading affordance must exist, shown when start>0 and
    hidden when the prefix is fully loaded.
    """
    assert "data-touch-sentinel-top" in SESSIONS_JS, \
        "A top sentinel element must exist"
    fn = _extract_fn(SESSIONS_JS, "_updateTouchSentinel")
    assert "data-touch-sentinel-top" in fn, \
        "_updateTouchSentinel must manage the top affordance"
    assert "interval.start<=0" in fn, \
        "Top affordance must key off start<=0"


def test_batch_direction_independent_of_bottom_sentinel():
    """_scheduleContinuousBatch must derive direction from the live DOM
    boundaries first, then only cross-check the affordance for that direction.
    The bottom sentinel's visibility must not gate upward batching.
    """
    fn = _extract_fn(SESSIONS_JS, "_scheduleContinuousBatch")
    dir_idx = fn.find("_touchNextBatchDirection")
    assert dir_idx >= 0
    after = fn[dir_idx:]
    assert "data-touch-sentinel-top" in after, \
        "Upward scheduling must consult the top affordance"
    # The old blanket bottom gate must be gone.
    assert "if(!sentinel || sentinel.style.display==='none') return;" not in fn, \
        "Bottom-sentinel visibility must not gate all batching directions"


@_node_tests
def test_deep_window_bottom_hidden_top_shown_upward_prepend_flows():
    """Executed deep-window control: end==total + start>0 must show the top
    affordance, hide the bottom one, and still schedule an upward prepend —
    direction independent of the bottom sentinel.
    """
    total = 200
    flat_rows = [{"group": {"label": "G"}, "session": {"session_id": f"s{i}"}} for i in range(total)]
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
let rafCallbacks = [];
let rafSchedules = 0;
global.requestAnimationFrame = function(fn) {{ rafSchedules++; rafCallbacks.push(fn); return rafSchedules; }};
global.cancelAnimationFrame = function() {{}};
function _isTouchPrimary() {{ return true; }}
const list = makeList();
list.clientHeight = 520;
list.scrollTop = 120 * SESSION_VIRTUAL_ROW_HEIGHT;
const gw = makeEl('div');
gw.className = 'session-date-group';
gw.setAttribute('data-group-label', 'G');
const body = makeBodyThatTracksItems(list);
body.className = 'session-date-body';
gw.appendChild(body);
list._groups['G'] = gw;
list.children.push(gw);
// Before-spacer only: prefix [0,120) is virtual, suffix fully loaded.
body.appendChild(_sessionVirtualSpacer(120 * SESSION_VIRTUAL_ROW_HEIGHT, 'before'));
for (let i = 120; i < {total}; i++) body.appendChild(makeSessionItem('s' + i));
list._sentinel = makeEl('div');
list._sentinel.style.display = 'none'; // bottom hidden: end==total
list.children.push(list._sentinel);
_sessionTouchGen = 1;
_sessionTouchStartIndex = 120;
_sessionTouchLoadedCount = {total};
_sessionTouchTotalCount = {total};
_sessionTouchListEl = list;
_touchRenderState = {{gen:1,list:list,flatRows:{json.dumps(flat_rows)},renderOneSession:function(s){{return makeSessionItem(s.session_id);}},activeSid:'s150',itemHeight:SESSION_VIRTUAL_ROW_HEIGHT}};

let prependCount = 0;
const realPrepend = _prependTouchBatch;
_prependTouchBatch = function() {{ prependCount++; realPrepend(); }};

_scheduleContinuousBatch();
const scheduled = rafCallbacks.length;
for (const cb of rafCallbacks.splice(0)) cb();

console.log(JSON.stringify({{
  scheduled: scheduled,
  prependCount: prependCount,
  start: _sessionTouchStartIndex,
  end: _sessionTouchLoadedCount,
  sids: list._items.map(function(i) {{ return i.dataset.sid; }}).slice(0, 3),
  firstSid: list._items.length ? list._items[0].dataset.sid : null,
  lastSid: list._items.length ? list._items[list._items.length-1].dataset.sid : null,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["scheduled"] >= 1, \
        "Upward batching must be schedulable while the bottom sentinel is hidden"
    assert result["prependCount"] == 1, \
        "The missing prefix must materialize via prepend, not be stranded"
    assert result["start"] == 80, \
        f"Prepend must advance start by one batch, got {result['start']}"
    assert result["end"] == total
    assert result["firstSid"] == "s80" and result["lastSid"] == "s199", \
        f"Prepended rows must sit before the existing suffix, got {result}"


@_node_tests
def test_upward_overshoot_no_prepend_from_blank_spacer():
    """Upward-overshoot control: an iPadOS scroll-indicator fling can strand
    the viewport entirely ABOVE the rendered window (inside the before-spacer).
    The direction check must not return 'up' from that blank-spacer position.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
const list = makeList();
list.clientHeight = 520;
list.getBoundingClientRect = function() { return {top: 0, bottom: 520, height: 520}; };
// First rendered row (index 60) is far BELOW the viewport — the user
// overshot upward into the before-spacer (blank region above the window).
const firstRow = makeSessionItem('s60');
firstRow.getBoundingClientRect = function() { return {top: 900, bottom: 952, height: 52}; };
list._items = [firstRow];
list.scrollTop = 0;
_sessionTouchStartIndex = 60;
_sessionTouchLoadedCount = 100;
const state = {flatRows: new Array(224), itemHeight: SESSION_VIRTUAL_ROW_HEIGHT};
const boundaryNear = _touchStartBoundaryNearViewport(list, state, 200);
const direction = _touchNextBatchDirection(list, state, 200);
console.log(JSON.stringify({boundaryNear, direction}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["boundaryNear"] is False, \
        "Overshot-above-window position must not arm an upward prepend"
    assert result["direction"] != "up", \
        "Direction must not be 'up' when the viewport never touched rendered content"


def test_group_collapse_forces_render_both_handlers():
    """Both direct group-collapse handlers (incremental wrapper + initial
    render) must call renderSessionListFromCache({force:true}) — a direct user
    action bypasses the touch-scroll cooldown so spacers reconcile immediately.
    """
    initial = _extract_fn(SESSIONS_JS, "_createTouchGroupWrapper")
    assert "renderSessionListFromCache({force:true})" in initial, \
        "Incremental wrapper's collapse handler must force the render"


def test_initial_render_group_collapse_forces_render():
    """The initial render's group header onclick must also force the render.
    """
    fn = _extract_fn(SESSIONS_JS, "renderSessionListFromCache")
    onclick_idx = fn.find("hdr.onclick=")
    assert onclick_idx >= 0, "Initial render group onclick must exist"
    body = fn[onclick_idx:]
    end_idx = body.find("wrapper.appendChild(hdr)")
    body = body[:end_idx]
    assert "renderSessionListFromCache({force:true})" in body, \
        "Initial render's collapse handler must force the render"
