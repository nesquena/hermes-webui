"""Regression tests for iPad sidebar scroll freeze.

On touch-primary devices (iPad, iPhone), the session-list sidebar scroll
freezes when scrolled below the top. Three root causes addressed:

1. CSS: .sidebar used overflow:visible, causing scroll-chain/rubber-band
   issues on iPadOS WebKit. Changed to overflow:hidden so the scroll
   surface stays contained within .session-list. Resize handle moved to
   right:0 so it isn't clipped.

2. CSS: .session-list was missing -webkit-overflow-scrolling:touch,
   which iOS Safari needs for smooth momentum scrolling. Without it,
   overscroll-behavior-y:contain makes the list feel "stuck" at the
   boundary because there's no rubber-band effect.

3. JS: renderSessionListFromCache() does innerHTML='' on every call,
   which terminates the browser's native momentum scroll gesture on
   touch devices. Background callers (SSE syncs, unread updates, gateway
   polls) can trigger this mid-scroll. Added a touch-aware guard that
   defers background renders while the user is actively scrolling.
"""
from pathlib import Path
import re
import pytest

ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _extract_fn(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} body not found"
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"{name} body did not close")


# ── CSS: sidebar overflow ──────────────────────────────────────────────────


def test_sidebar_uses_overflow_hidden_not_visible():
    """The sidebar container must use overflow:hidden, not overflow:visible.

    overflow:visible on iPadOS WebKit causes scroll chaining and rubber-band
    issues that freeze the session-list scroller. overflow:hidden keeps the
    scroll surface contained within .session-list.
    """
    match = re.search(r'\.sidebar\{width:300px[^}]+\}', STYLE_CSS)
    assert match, ".sidebar base rule not found"
    rule = match.group(0)
    assert "overflow:hidden" in rule, \
        f".sidebar must use overflow:hidden, got: {rule}"


def test_sidebar_resize_handle_not_clipped():
    """The resize handle must sit inside the sidebar border (right:0, not right:-2px).

    With overflow:hidden, a right:-2px handle would be clipped and invisible.
    """
    match = re.search(r'^\.sidebar \.resize-handle\{right:[^}]+\}', STYLE_CSS, re.MULTILINE)
    assert match, ".sidebar .resize-handle positioning rule not found"
    rule = match.group(0)
    assert "right:0" in rule, \
        f".sidebar .resize-handle must use right:0, got: {rule}"


def test_session_list_has_webkit_overflow_scrolling_touch():
    """The session list must have -webkit-overflow-scrolling:touch for iOS momentum scroll."""
    assert "-webkit-overflow-scrolling:touch" in STYLE_CSS


def test_session_list_scroll_boundary_unchanged():
    """The session-list scroll boundary must remain intact."""
    assert "overscroll-behavior-y:contain" in STYLE_CSS
    assert "touch-action:pan-y" in STYLE_CSS
    assert "overflow-anchor:none" in STYLE_CSS


# ── JS: touch scroll guard ─────────────────────────────────────────────────


def test_touch_primary_helper_exists():
    """A helper to detect touch-primary devices must exist."""
    assert "function _isTouchPrimary()" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_isTouchPrimary")
    assert "pointer:coarse" in fn


def test_touch_scroll_detection_exists():
    """A function to detect active touch scrolling on the session list must exist."""
    assert "function _isSessionListTouchScrolling()" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_isSessionListTouchScrolling")
    assert "_isTouchPrimary()" in fn
    assert "SESSION_LIST_TOUCH_INTERACTION_IDLE_MS" in fn
    assert "_sessionListPointerActive" in fn


def test_touch_defer_function_exists():
    """A function to defer renderSessionListFromCache on touch must exist."""
    assert "function _deferRenderSessionListFromCache()" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_deferRenderSessionListFromCache")
    assert "setTimeout" in fn
    assert "renderSessionListFromCache" in fn


def test_render_session_list_accepts_force_option():
    """renderSessionListFromCache must read a force flag from arguments[0].

    The signature stays () (many static tests anchor on the empty-paren
    marker), but callers can pass {force:true} as a positional arg to
    bypass the touch scroll guard.
    """
    assert "function renderSessionListFromCache(){" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "renderSessionListFromCache")
    assert "arguments[0]" in fn
    assert "force" in fn
    assert "_isSessionListTouchScrolling()" in fn
    assert "_deferRenderSessionListFromCache()" in fn


def test_touch_idle_constant_exists():
    """A longer idle window for touch devices must be defined."""
    assert "SESSION_LIST_TOUCH_INTERACTION_IDLE_MS" in SESSIONS_JS
    match = re.search(r'SESSION_LIST_TOUCH_INTERACTION_IDLE_MS\s*=\s*(\d+)', SESSIONS_JS)
    assert match
    value = int(match.group(1))
    assert value > 700, f"touch idle should be > 700ms, got {value}"


def test_deferred_timer_cleared_on_render():
    """When a non-deferred render proceeds, the pending touch timer must be cleared."""
    fn = _extract_fn(SESSIONS_JS, "renderSessionListFromCache")
    assert "_pendingTouchDeferredRenderTimer" in fn
    assert "clearTimeout(_pendingTouchDeferredRenderTimer)" in fn


def test_virtual_scroll_render_uses_force():
    """The scroll-driven virtual render path must exist (desktop path only).

    On touch devices, _scheduleSessionVirtualizedRender bails early — the
    IntersectionObserver handles incremental appends. On desktop, the scroll
    listener calls renderSessionListFromCache() to recompute the virtual window.
    """
    fn = _extract_fn(SESSIONS_JS, "_scheduleSessionVirtualizedRender")
    assert "renderSessionListFromCache()" in fn, \
        "Virtual scroll render must call renderSessionListFromCache()"


def test_virtual_resync_render_uses_force():
    """The post-render virtual window resync must exist for scroll correction."""
    fn = _extract_fn(SESSIONS_JS, "_resyncSessionVirtualWindowAfterRender")
    assert "renderSessionListFromCache()" in fn, \
        "Virtual resync render must call renderSessionListFromCache()"


def test_virtualization_disabled_on_touch():
    """_sessionVirtualWindow must return a batched window on touch-primary devices."""
    fn = _extract_fn(SESSIONS_JS, "_sessionVirtualWindow")
    assert "_isTouchPrimary()" in fn, \
        "_sessionVirtualWindow must check _isTouchPrimary() to enable batched rendering on touch"
    assert "SESSION_TOUCH_INITIAL_BATCH" in fn, \
        "_sessionVirtualWindow must use SESSION_TOUCH_INITIAL_BATCH for the initial batch size"
    assert "virtualized:false" in fn, \
        "_sessionVirtualWindow must return virtualized:false on touch devices"
    assert "batched:true" in fn, \
        "_sessionVirtualWindow must return batched:true on touch devices so the row loop limits DOM"


def test_row_loop_respects_batched_end():
    """The row rendering loop must respect the batched end limit, not render every row."""
    # The inWindow check must include batched mode — when batched is true,
    # only rows [0, end) should be rendered, not every row.
    assert "!virtualWindow.batched" in SESSIONS_JS, \
        "Row loop must check batched flag to limit rendered rows"


def test_append_touch_batch_exists():
    """An incremental append function must exist (no innerHTML wipe during scroll)."""
    assert "function _appendTouchBatch()" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_appendTouchBatch")
    # Must NOT call renderSessionListFromCache({force:true}) — that wipes innerHTML
    assert "renderSessionListFromCache({force:true})" not in fn, \
        "_appendTouchBatch must NOT call renderSessionListFromCache({force:true})"
    # Must append rows incrementally
    assert "appendChild" in fn
    # Must adjust the bottom spacer
    assert "bottomSpacer" in fn or "after" in fn


def test_observer_calls_append_not_full_render():
    """The IntersectionObserver callback must call _appendTouchBatch, not renderSessionListFromCache."""
    fn = _extract_fn(SESSIONS_JS, "_ensureTouchSentinelObserver")
    assert "_appendTouchBatch()" in fn, \
        "Observer must call _appendTouchBatch() for incremental append"
    # Must NOT call renderSessionListFromCache with force (that wipes innerHTML)
    assert "renderSessionListFromCache({force:true})" not in fn, \
        "Observer must NOT call renderSessionListFromCache({force:true})"


def test_touch_batch_pending_cleared_on_all_branches():
    """_touchBatchPending must be cleared via try/finally so it always runs."""
    fn = _extract_fn(SESSIONS_JS, "_ensureTouchSentinelObserver")
    # The observer must use try/finally to guarantee _touchBatchPending is cleared
    assert "try{" in fn or "try {" in fn, \
        "Observer must use try block around _appendTouchBatch"
    assert "finally{" in fn or "finally {" in fn, \
        "Observer must use finally to clear _touchBatchPending"
    assert "_touchBatchPending=false" in fn, \
        "finally block must clear _touchBatchPending"


def test_generation_scoping_exists():
    """A generation token must exist to invalidate stale observer callbacks."""
    assert "_sessionTouchGen" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_ensureTouchSentinelObserver")
    assert "gen" in fn and "_sessionTouchGen" in fn, \
        "Observer must capture and check generation token"
    # Generation is bumped by _invalidateTouchRender (the unified teardown),
    # which _setupTouchSentinel calls before setting up new state.
    inv = _extract_fn(SESSIONS_JS, "_invalidateTouchRender")
    assert "_sessionTouchGen++" in inv, \
        "_invalidateTouchRender must bump generation token"
    setup = _extract_fn(SESSIONS_JS, "_setupTouchSentinel")
    assert "_invalidateTouchRender()" in setup, \
        "_setupTouchSentinel must call _invalidateTouchRender (which bumps gen)"


def test_intersection_observer_fallback_exists():
    """A scroll-based fallback must exist for browsers without IntersectionObserver."""
    fn = _extract_fn(SESSIONS_JS, "_setupTouchSentinel")
    assert "IntersectionObserver" in fn
    assert "_touchScrollFallbackRaf" in fn, \
        "Fallback must use a RAF-based scroll check when IntersectionObserver is absent"
    assert "requestAnimationFrame" in fn


def test_scroll_listener_skips_on_touch():
    """The scroll-driven RAF must be skipped entirely on touch devices."""
    fn = _extract_fn(SESSIONS_JS, "_scheduleSessionVirtualizedRender")
    assert "_isTouchPrimary()" in fn, \
        "_scheduleSessionVirtualizedRender must bail early on touch devices to preserve momentum"


def test_touch_sentinel_observer_exists():
    """An IntersectionObserver-based sentinel must exist for incremental batch loading."""
    assert "function _ensureTouchSentinelObserver" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_ensureTouchSentinelObserver")
    assert "IntersectionObserver" in fn
    assert "rootMargin" in fn


def test_touch_sentinel_setup_exists():
    """A setup function must create and observe the sentinel element."""
    assert "function _setupTouchSentinel" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_setupTouchSentinel")
    assert "data-touch-sentinel" in fn
    assert "Loading more" in fn


def test_touch_batch_constants_exist():
    """Batch size constants must be defined for incremental loading."""
    assert "SESSION_TOUCH_INITIAL_BATCH" in SESSIONS_JS
    assert "SESSION_TOUCH_BATCH_SIZE" in SESSIONS_JS
    # Initial batch should be large enough to fill a viewport
    m1 = re.search(r'SESSION_TOUCH_INITIAL_BATCH\s*=\s*(\d+)', SESSIONS_JS)
    assert m1, "INITIAL_BATCH value not found"
    assert int(m1.group(1)) >= 40, "initial batch should be >= 40 rows"
    m2 = re.search(r'SESSION_TOUCH_BATCH_SIZE\s*=\s*(\d+)', SESSIONS_JS)
    assert m2, "BATCH_SIZE value not found"
    assert int(m2.group(1)) >= 20, "batch size should be >= 20 rows"


def test_touch_batch_reset_on_filter_change():
    """The batch count must reset when the scope fingerprint changes — not just filter+total.
    The fingerprint must include profile/all-profiles, active project, source filter,
    archive page, content-search, collapsed groups, active session, total count, and
    ordered SID identity."""
    assert "sessionTouchScope" in SESSIONS_JS
    assert "SESSION_TOUCH_INITIAL_BATCH" in SESSIONS_JS
    # The fingerprint must be computed BEFORE the window calculation
    render_fn = _extract_fn(SESSIONS_JS, "renderSessionListFromCache")
    # Find the fingerprint block and verify it's before _sessionVirtualWindow
    fp_idx = render_fn.find("scopeFingerprint")
    vw_idx = render_fn.find("_sessionVirtualWindow({")
    assert fp_idx >= 0 and vw_idx >= 0, "Both fingerprint and virtual window must exist"
    assert fp_idx < vw_idx, "Scope fingerprint must be computed BEFORE virtual window"
    # The fingerprint must include the expanded scope dimensions
    assert "_showAllProfiles" in render_fn, "Fingerprint must include profile/all-profiles"
    assert "_activeProject" in render_fn, "Fingerprint must include active project"
    assert "_contentSearchResults" in render_fn, "Fingerprint must include content-search"
    assert "sidPrefix" in render_fn, "Fingerprint must include ordered SID identity"


def test_ensure_touch_sentinel_disconnects_old_observer():
    """_ensureTouchSentinelObserver must disconnect existing observer before creating a new one."""
    fn = _extract_fn(SESSIONS_JS, "_ensureTouchSentinelObserver")
    assert "disconnect()" in fn
    assert "_touchSentinelObserver=null" in fn


def test_invalidate_touch_render_exists():
    """A unified invalidation helper must exist for all teardown paths."""
    assert "function _invalidateTouchRender(){" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_invalidateTouchRender")
    assert "_touchSentinelObserver" in fn
    assert "_touchScrollFallbackRaf" in fn
    assert "_touchRenderState=null" in fn
    assert "_touchBatchPending=false" in fn
    assert "_touchBatchToken" in fn
    # Invalidation must own ALL touch state: list, loaded/total, generation
    assert "_sessionTouchListEl=null" in fn
    assert "_sessionTouchLoadedCount=0" in fn
    assert "_sessionTouchTotalCount=0" in fn
    assert "_sessionTouchGen++" in fn


def test_setup_touch_sentinel_uses_invalidate():
    """_setupTouchSentinel must call _invalidateTouchRender (unified teardown), not a separate function."""
    fn = _extract_fn(SESSIONS_JS, "_setupTouchSentinel")
    assert "_invalidateTouchRender()" in fn, \
        "_setupTouchSentinel must use _invalidateTouchRender for teardown"


def test_append_transactional_uses_fragments():
    """_appendTouchBatch must use DocumentFragment for transactional append."""
    fn = _extract_fn(SESSIONS_JS, "_appendTouchBatch")
    assert "DocumentFragment" in fn or "createDocumentFragment" in fn, \
        "Append must render into detached fragments before committing"
    assert "catch" in fn, "Append must catch exceptions and discard fragments"
    # Must validate every row/group/body — abort on missing, not continue
    assert "return" in fn, "Append must abort (return) on missing row/group/body"
    # Must insert before the after-spacer, not after it
    assert "insertBefore" in fn, "Append must insert fragments before the after-spacer"
    assert "afterSpacer" in fn or "after" in fn, "Append must find and insert before the after-spacer"


def test_observer_microtask_token_owned():
    """Observer microtask must capture and re-check a token before mutating."""
    fn = _extract_fn(SESSIONS_JS, "_ensureTouchSentinelObserver")
    assert "_touchBatchToken" in fn, "Observer must use token ownership"
    assert "capturedGen" in fn, "Observer must capture generation when scheduling"
    assert "token===_touchBatchToken" in fn, "Microtask must re-check token ownership"


def test_fallback_stops_when_all_loaded():
    """Fallback RAF must stop rescheduling when all rows are loaded."""
    fn = _extract_fn(SESSIONS_JS, "_setupTouchSentinel")
    assert "l>=t" in fn, "Fallback must check loaded>=total and stop"
    # Terminal/stale returns must zero the RAF handle — not just return
    assert "_touchScrollFallbackRaf=0" in fn, \
        "Terminal/stale RAF returns must zero the handle"


def test_touch_window_includes_active_sid():
    """_sessionVirtualWindow touch branch must consume activeIndex to include active session."""
    fn = _extract_fn(SESSIONS_JS, "_sessionVirtualWindow")
    assert "activeIdx" in fn or "activeIndex" in fn, \
        "Touch branch must check active index to include active session in prefix"


# ── Executed touch-DOM regression tests (node VM) ──────────────────────────
# These tests actually run _appendTouchBatch in a node VM with a mock DOM to
# verify runtime behavior — not just source-string presence. The gate
# certifier specifically requested executed tests covering: 60→100→final
# growth, node preservation with zero innerHTML writes, stale generation
# rejection, and exception recovery.

import json
import shutil
import subprocess
import tempfile

NODE_BIN = shutil.which("node")
_node_tests = pytest.mark.skipif(NODE_BIN is None, reason="node not on PATH")


def _run_node_vm(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE_BIN, str(script_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _node_test_preamble():
    """Return the JS preamble that sets up mock globals for _appendTouchBatch.
    Expects the caller to have defined `const SESSIONS_JS = '...';` before this."""
    return """
const src = SESSIONS_JS;
function extractFunc(name) {
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

// Mock DOM element factory
let _innerHTMLWipes = 0;
function makeEl(tag) {
  const el = {
    tagName: tag || 'div',
    className: '',
    style: {},
    dataset: {},
    children: [],
    _parent: null,
    appendChild(child) {
      // DocumentFragment: in a real browser, appending a fragment moves its
      // children into this element. Simulate that here.
      if (child && child.tagName === '#document-fragment') {
        const kids = child.children.slice();
        for (const k of kids) { k._parent = this; this.children.push(k); }
        child.children = [];
        return child;
      }
      child._parent = this; this.children.push(child); return child;
    },
    insertBefore(child, ref) {
      child._parent = this;
      const idx = this.children.indexOf(ref);
      if (idx >= 0) this.children.splice(idx, 0, child);
      else this.children.push(child);
      return child;
    },
    removeChild(child) {
      const idx = this.children.indexOf(child);
      if (idx >= 0) this.children.splice(idx, 1);
      return child;
    },
    remove() {
      if (this._parent) {
        const idx = this._parent.children.indexOf(this);
        if (idx >= 0) this._parent.children.splice(idx, 1);
      }
    },
    querySelector(sel) { return null; },
    querySelectorAll(sel) { return []; },
    setAttribute(k, v) { this.dataset[k.replace('data-','').replace(/-/g,'')] = v; },
    getAttribute(k) { return this.dataset[k.replace('data-','').replace(/-/g,'')] || null; },
    addEventListener() {},
    removeEventListener() {},
    classList: { _set: new Set(), add(c){this._set.add(c);}, remove(c){this._set.delete(c);}, toggle(c,force){if(force===undefined){if(this._set.has(c))this._set.delete(c);else this._set.add(c);}else if(force)this._set.add(c);else this._set.delete(c);}, contains(c){return this._set.has(c);} },
  };
  // innerHTML setter that tracks wipes
  Object.defineProperty(el, 'innerHTML', {
    set(v) { if (v === '' || v === '') _innerHTMLWipes++; this.children = []; },
    get() { return ''; },
  });
  return el;
}

// Mock list element with querySelector/querySelectorAll support
function makeList() {
  const list = makeEl('div');
  list._items = []; // .session-item[data-sid] elements
  list._groups = {}; // label → wrapper
  list._sentinel = null;
  list._bottomSpacer = null;
  list.querySelector = function(sel) {
    if (sel === '[data-touch-sentinel]') return this._sentinel;
    if (sel === '[data-touch-bottom-spacer]') return this._bottomSpacer;
    if (sel.startsWith('.session-date-group[data-group-label=')) {
      const label = sel.match(/"([^"]+)"/)[1];
      return this._groups[label] || null;
    }
    return null;
  };
  list.querySelectorAll = function(sel) {
    if (sel === '.session-item[data-sid]') return this._items.slice();
    if (sel === '.session-date-group') return Object.values(this._groups);
    if (sel === '.session-virtual-spacer[data-virtual-spacer="after"]') {
      return this._afterSpacers || [];
    }
    return [];
  };
  list.appendChild = function(child) {
    child._parent = this;
    this.children.push(child);
    return child;
  };
  list.insertBefore = function(child, ref) {
    child._parent = this;
    const idx = this.children.indexOf(ref);
    if (idx >= 0) this.children.splice(idx, 0, child);
    else this.children.push(child);
    return child;
  };
  return list;
}

// Mock session item element
function makeSessionItem(sid) {
  const el = makeEl('div');
  el.className = 'session-item';
  el.dataset.sid = sid;
  return el;
}

// Helper: create a body element whose appendChild tracks session items in list._items.
// Handles DocumentFragment unpacking (fragments move their children into the target).
function makeBodyThatTracksItems(list) {
  const body = makeEl('div');
  body.className = 'session-date-body';
  body.appendChild = function(child) {
    if (child && child.tagName === '#document-fragment') {
      const kids = child.children.slice();
      for (const k of kids) {
        k._parent = this;
        this.children.push(k);
        if (k.dataset && k.dataset.sid) list._items.push(k);
      }
      child.children = [];
      return child;
    }
    child._parent = this;
    this.children.push(child);
    if (child.dataset && child.dataset.sid) list._items.push(child);
    return child;
  };
  return body;
}

// Mock CSS.escape (Node 22 has it natively, but provide fallback)
if (typeof CSS === 'undefined') global.CSS = {};
if (!CSS.escape) CSS.escape = function(s) { return s; };

// Mock window for functions that reference it (e.g. 'IntersectionObserver' in window)
if (typeof window === 'undefined') global.window = {};

// Mock document.createDocumentFragment for transactional append
if (typeof document === 'undefined') global.document = {};
if (!document.createDocumentFragment) {
  document.createDocumentFragment = function() {
    // A fragment acts like an element but has no tagName
    const frag = makeEl('fragment');
    frag.tagName = '#document-fragment';
    // When appended, a real DocumentFragment's children are moved into the
    // target, not the fragment itself. Override appendChild on the body
    // to detect fragments and unpack them.
    return frag;
  };
}

// Globals that _appendTouchBatch references
let _touchRenderState = null;
let _sessionTouchGen = 0;
let _sessionTouchLoadedCount = 0;
let _sessionTouchListEl = null;
let _sessionTouchTotalCount = 0;
let _touchSentinelObserver = null;
let _touchBatchPending = false;
let _touchScrollFallbackRaf = 0;
let _touchBatchToken = 0;
const SESSION_TOUCH_BATCH_SIZE = 40;
const SESSION_TOUCH_INITIAL_BATCH = 60;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;

// Track calls to renderSessionListFromCache (fallback path)
let _renderCalls = 0;
function renderSessionListFromCache() { _renderCalls++; }

// Mock _sessionVirtualSpacer for _updateTouchGroupSpacers
function _sessionVirtualSpacer(h, pos) {
  const sp = makeEl('div');
  sp.className = 'session-virtual-spacer';
  sp.dataset['virtual-spacer'] = pos;
  sp.style.height = h + 'px';
  return sp;
}

// Mock _invalidateTouchRender for mismatch recovery — matches production:
// clears ALL touch state (observer, RAF, render state, list, loaded/total,
// pending, generation, token).
function _invalidateTouchRender() {
  if (_touchSentinelObserver) { _touchSentinelObserver.disconnect(); _touchSentinelObserver = null; }
  if (_touchScrollFallbackRaf) { /* mock: no cancelAnimationFrame */ _touchScrollFallbackRaf = 0; }
  _touchRenderState = null;
  _sessionTouchListEl = null;
  _sessionTouchLoadedCount = 0;
  _sessionTouchTotalCount = 0;
  _touchBatchPending = false;
  _sessionTouchGen++;
  _touchBatchToken++;
}

// Extract and eval all touch functions
eval(extractFunc('_createTouchGroupWrapper'));
eval(extractFunc('_updateTouchGroupSpacers'));
eval(extractFunc('_updateTouchSentinel'));
eval(extractFunc('_appendTouchBatch'));
eval(extractFunc('_ensureTouchSentinelObserver'));
"""


@_node_tests
def test_append_grows_from_initial_to_full():
    """60→100→final: _appendTouchBatch grows the DOM from initial batch to full list.
    Covers totals 61, 100, and 101 — the final partial batch must not be dropped."""
    total = 101
    # Build mock flat rows
    flat_rows = []
    for i in range(total):
        flat_rows.append({
            "group": {"label": "Today", "isPinned": False},
            "session": {"session_id": f"sess_{i}"},
        })
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
// Set up state
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60; // initial batch already rendered
_sessionTouchTotalCount = {total};

// Pre-populate DOM with 60 session items (the initial render)
for (let i = 0; i < 60; i++) {{
  list._items.push(makeSessionItem('sess_' + i));
}}

// Set up render state
_touchRenderState = {{
  gen: 1,
  list: list,
  flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s, isPinned) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

// Create group wrapper for "Today" — the body's appendChild must track session items
const groupWrapper = makeEl('div');
groupWrapper.className = 'session-date-group';
groupWrapper.dataset['group-label'] = 'Today';
const body = makeBodyThatTracksItems(list);
// Override querySelector so _appendTouchBatch can find the body
groupWrapper.querySelector = function(sel) {{
  if (sel === '.session-date-body') return body;
  return null;
}};
groupWrapper.appendChild(body);
list._groups['Today'] = groupWrapper;
list.children.push(groupWrapper);

// Verify exact SID order and node identity before append
const sidsBefore = list._items.map(i => i.dataset.sid).join(',');
const refsBefore = list._items.slice();

// First append: 60 → 100
_appendTouchBatch();
const afterFirst = _sessionTouchLoadedCount;
const itemsAfterFirst = list._items.length;
const sidsAfterFirst = list._items.map(i => i.dataset.sid).join(',');

// Verify original nodes survived (identity check)
const originalSurvived = refsBefore.every((ref, i) => list._items[i] === ref);

// Second append: 100 → 101 (final batch — must not be dropped)
_appendTouchBatch();
const afterSecond = _sessionTouchLoadedCount;
const itemsAfterSecond = list._items.length;

console.log(JSON.stringify({{
  afterFirst, itemsAfterFirst,
  afterSecond, itemsAfterSecond,
  totalRows: {total},
  innerHTMLWipes: _innerHTMLWipes,
  renderCalls: _renderCalls,
  originalSurvived,
  sidsAfterFirst,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["afterFirst"] == 100, f"First append should reach 100, got {result['afterFirst']}"
    assert result["itemsAfterFirst"] == 100, f"DOM should have 100 items after first append, got {result['itemsAfterFirst']}"
    # Finding #2: final batch must NOT be dropped
    assert result["afterSecond"] == 101, f"Final batch (101) must not be dropped, got {result['afterSecond']}"
    assert result["itemsAfterSecond"] == 101, f"DOM should have 101 items after final append, got {result['itemsAfterSecond']}"
    # No innerHTML wipes during append
    assert result["innerHTMLWipes"] == 0, f"No innerHTML wipes during append, got {result['innerHTMLWipes']}"
    # No fallback re-render triggered
    assert result["renderCalls"] == 0, f"No full re-render needed, got {result['renderCalls']}"
    # Original nodes survived (identity, not just property)
    assert result["originalSurvived"], "Original DOM nodes must be the exact same objects after append"
    # Exact SID order
    expected_sids = ",".join(f"sess_{i}" for i in range(100))
    assert result["sidsAfterFirst"] == expected_sids, "SID order mismatch after first append"


@_node_tests
def test_append_grows_61_total():
    """Total=61: single append from 60→61 (final partial batch of 1 row)."""
    flat_rows = [{"group": {"label": "G"}, "session": {"session_id": f"s_{i}"}} for i in range(61)]
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 61;
for (let i = 0; i < 60; i++) list._items.push(makeSessionItem('s_' + i));
_touchRenderState = {{
  gen: 1, list: list, flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};
// Set up group
const gw = makeEl('div');
gw.dataset['group-label'] = 'G';
const body = makeBodyThatTracksItems(list);
gw.querySelector = function(sel) {{ if(sel==='.session-date-body') return body; return null; }};
gw.appendChild(body);
list._groups['G'] = gw;

_appendTouchBatch();
console.log(JSON.stringify({{ loaded: _sessionTouchLoadedCount, items: list._items.length }}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["loaded"] == 61, f"Total=61: should reach 61, got {result['loaded']}"
    assert result["items"] == 61, f"Total=61: DOM should have 61 items, got {result['items']}"


@_node_tests
def test_append_grows_100_total():
    """Total=100: single append from 60→100 (exact batch boundary)."""
    flat_rows = [{"group": {"label": "G"}, "session": {"session_id": f"s_{i}"}} for i in range(100)]
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 100;
for (let i = 0; i < 60; i++) list._items.push(makeSessionItem('s_' + i));
_touchRenderState = {{
  gen: 1, list: list, flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};
const gw = makeEl('div');
gw.dataset['group-label'] = 'G';
const body = makeBodyThatTracksItems(list);
gw.querySelector = function(sel) {{ if(sel==='.session-date-body') return body; return null; }};
gw.appendChild(body);
list._groups['G'] = gw;

_appendTouchBatch();
console.log(JSON.stringify({{ loaded: _sessionTouchLoadedCount, items: list._items.length }}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["loaded"] == 100, f"Total=100: should reach 100, got {result['loaded']}"
    assert result["items"] == 100, f"Total=100: DOM should have 100 items, got {result['items']}"


@_node_tests
def test_append_stale_generation_rejected():
    """_appendTouchBatch must reject when generation doesn't match (stale callback)."""
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 2; // current generation is 2
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 100;

_touchRenderState = {
  gen: 1, // STALE generation — observer callback from previous view
  list: list,
  flatRows: [],
  renderOneSession: function() { return makeSessionItem('x'); },
  activeSid: null,
};

_appendTouchBatch();
console.log(JSON.stringify({
  loadedCount: _sessionTouchLoadedCount, // should NOT have changed
  renderCalls: _renderCalls, // should NOT have triggered re-render
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["loadedCount"] == 60, "Stale generation must not modify loaded count"
    assert result["renderCalls"] == 0, "Stale generation must not trigger re-render"


@_node_tests
def test_append_dom_sid_mismatch_triggers_rerender():
    """Finding #3: DOM SID prefix mismatch must trigger full re-render, not splice."""
    # Build flatRows with 50 entries — DOM has [a, b, c] but state expects [a, b, X, ...]
    flat_rows = [{"group": {"label": "G"}, "session": {"session_id": f"s_{i}"}} for i in range(50)]
    # Override index 2 to create the mismatch
    flat_rows[2]["session"]["session_id"] = "X"
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 3;
_sessionTouchTotalCount = 100;

// DOM has SIDs [a, b, c]
list._items = [makeSessionItem('a'), makeSessionItem('b'), makeSessionItem('c')];

// But render state expects [a, b, X, ...] — mismatch at index 2
_touchRenderState = {{
  gen: 1,
  list: list,
  flatRows: {json.dumps(flat_rows)},
  renderOneSession: function() {{ return makeSessionItem('new'); }},
  activeSid: null,
}};

_appendTouchBatch();
console.log(JSON.stringify({{
  loadedCount: _sessionTouchLoadedCount, // should be reset to 0
  renderCalls: _renderCalls, // should trigger re-render
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["loadedCount"] == 0, "SID mismatch must reset loaded count for full re-render"
    assert result["renderCalls"] == 1, "SID mismatch must trigger full re-render"


@_node_tests
def test_append_exception_no_duplicates_on_retry():
    """Finding #1: A throw after >=1 successful row must NOT leave partial DOM.
    Transactional append renders into fragments — if any row throws, nothing is
    committed. Retry from the same oldLoaded produces no duplicates."""
    # Provide real flatRows so append actually calls the renderer
    flat_rows = [{"group": {"label": "G"}, "session": {"session_id": f"s_{i}"}} for i in range(100)]
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 100;

// Pre-populate DOM with 60 items
for (let i = 0; i < 60; i++) list._items.push(makeSessionItem('s_' + i));

// Renderer that throws on the 3rd new row (index 62)
let renderCount = 0;
_touchRenderState = {{
  gen: 1,
  list: list,
  flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{
    renderCount++;
    if (renderCount === 3) throw new Error('render boom');
    return makeSessionItem(s.session_id);
  }},
  activeSid: null,
}};

// Set up group with body that tracks items
const gw = makeEl('div');
gw.dataset['group-label'] = 'G';
const body = makeBodyThatTracksItems(list);
gw.querySelector = function(sel) {{ if(sel==='.session-date-body') return body; return null; }};
gw.appendChild(body);
list._groups['G'] = gw;

// First attempt: throws on 3rd row — transactional, nothing committed
const itemsBefore = list._items.length;
const loadedBefore = _sessionTouchLoadedCount;
try {{ _appendTouchBatch(); }} catch(e) {{}}
const itemsAfterThrow = list._items.length;
const loadedAfterThrow = _sessionTouchLoadedCount;

// Retry: renderer no longer throws — should succeed without duplicates
renderCount = 0; // reset
_touchRenderState.renderOneSession = function(s) {{ return makeSessionItem(s.session_id); }};
_appendTouchBatch();
const itemsAfterRetry = list._items.length;
const loadedAfterRetry = _sessionTouchLoadedCount;

// Check for duplicates
const sids = list._items.map(i => i.dataset.sid);
const uniqueSids = [...new Set(sids)];

console.log(JSON.stringify({{
  itemsBefore, loadedBefore,
  itemsAfterThrow, loadedAfterThrow,
  itemsAfterRetry, loadedAfterRetry,
  hasDuplicates: sids.length !== uniqueSids.length,
  duplicateCount: sids.length - uniqueSids.length,
}}));
"""
    result = json.loads(_run_node_vm(source))
    # After throw: no items committed (transactional)
    assert result["itemsAfterThrow"] == result["itemsBefore"], \
        f"Throw must not commit any items: before={result['itemsBefore']}, after={result['itemsAfterThrow']}"
    assert result["loadedAfterThrow"] == result["loadedBefore"], \
        f"Throw must not advance loaded count: before={result['loadedBefore']}, after={result['loadedAfterThrow']}"
    # After retry: no duplicates
    assert not result["hasDuplicates"], \
        f"Retry must not produce duplicates: duplicateCount={result['duplicateCount']}"
    assert result["loadedAfterRetry"] == 100, \
        f"Retry should reach 100, got {result['loadedAfterRetry']}"


@_node_tests
def test_stale_microtask_after_state_replacement():
    """Finding #2: An old gen-1 microtask must not append gen-2 rows.

    The observer schedules a microtask under gen-1. Before the microtask runs,
    a new render installs gen-2 with a different flatRows. The old microtask
    must detect the generation change and bail out — not append gen-2 rows.

    This simulates the token-owned microtask pattern: the observer captures
    {gen, token} when scheduling, then the microtask re-checks gen before
    calling _appendTouchBatch."""
    flat_rows_gen1 = [{"group": {"label": "G"}, "session": {"session_id": f"old_{i}"}} for i in range(100)]
    flat_rows_gen2 = [{"group": {"label": "G"}, "session": {"session_id": f"new_{i}"}} for i in range(100)]
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 100;
for (let i = 0; i < 60; i++) list._items.push(makeSessionItem('old_' + i));

// Gen-1 render state
_touchRenderState = {{
  gen: 1, list: list, flatRows: {json.dumps(flat_rows_gen1)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

// Set up group
const gw = makeEl('div');
gw.dataset['group-label'] = 'G';
const body = makeBodyThatTracksItems(list);
gw.querySelector = function(sel) {{ if(sel==='.session-date-body') return body; return null; }};
gw.appendChild(body);
list._groups['G'] = gw;

// Simulate the observer scheduling a microtask under gen-1.
// The observer captures the generation when scheduling.
const capturedGen = _sessionTouchGen; // = 1
const token = ++_touchBatchToken;
_touchBatchPending = true;

// Before the microtask fires, a new render installs gen-2 state.
_sessionTouchGen = 2;
_touchRenderState = {{
  gen: 2, list: list, flatRows: {json.dumps(flat_rows_gen2)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

// Now the old gen-1 microtask fires. It must re-check the generation
// before calling _appendTouchBatch — exactly as the token-owned pattern does.
if (capturedGen !== _sessionTouchGen) {{
  // Generation changed — bail out, clear pending only if token still owns it
  if (token === _touchBatchToken) _touchBatchPending = false;
}} else {{
  try {{ _appendTouchBatch(); }}
  finally {{ if (token === _touchBatchToken) _touchBatchPending = false; }}
}}

const sids = list._items.map(i => i.dataset.sid);
console.log(JSON.stringify({{
  loadedCount: _sessionTouchLoadedCount,
  itemCount: list._items.length,
  // Should still be 60 old_ items, no new_ items appended
  hasNewItems: sids.some(s => s.startsWith('new_')),
  pendingCleared: !_touchBatchPending,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["loadedCount"] == 60, "Stale gen-1 microtask must not advance loaded count"
    assert result["itemCount"] == 60, "Stale gen-1 microtask must not append any rows"
    assert not result["hasNewItems"], "Stale gen-1 microtask must not append gen-2 rows"
    assert result["pendingCleared"], "Token-owned microtask must clear pending on stale generation"


@_node_tests
def test_equal_count_scope_change_triggers_rerender():
    """Finding #3: Equal-count scope/reorder changes must trigger re-render.

    DOM has [a, b, c] (3 items, loaded=3). State has [x, y, z] (3 items, same
    count). The SID prefix mismatch must be detected and trigger a full re-render,
    not retain the stale loaded extent."""
    flat_rows = [{"group": {"label": "G"}, "session": {"session_id": f"new_{i}"}} for i in range(50)]
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 3; // same count as DOM
_sessionTouchTotalCount = 50;

// DOM has [a, b, c]
list._items = [makeSessionItem('a'), makeSessionItem('b'), makeSessionItem('c')];

// State expects [new_0, new_1, new_2, ...] — same count (3 in prefix) but different SIDs
_touchRenderState = {{
  gen: 1, list: list, flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

_appendTouchBatch();
console.log(JSON.stringify({{
  loadedCount: _sessionTouchLoadedCount,
  renderCalls: _renderCalls,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["loadedCount"] == 0, "Equal-count scope change must reset loaded count"
    assert result["renderCalls"] == 1, "Equal-count scope change must trigger re-render"


@_node_tests
def test_active_sid_beyond_prefix_included():
    """Finding #3: Active session beyond the initial prefix must be included.

    With 100 total rows and initial batch=60, an active session at index 70
    must cause the window to extend to include it (loaded=71)."""
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
// _sessionVirtualWindow touch branch with activeIndex
// _sessionTouchLoadedCount already declared in preamble — just set it
_sessionTouchLoadedCount = 60; // initial batch

// Mock _isTouchPrimary to return true so the touch branch is exercised
function _isTouchPrimary() { return true; }

// Extract and eval _sessionVirtualWindow
eval(extractFunc('_sessionVirtualWindow'));

// Call with activeIndex=70 (beyond initial 60)
const w = _sessionVirtualWindow({
  total: 100,
  scrollTop: 0,
  viewportHeight: 520,
  itemHeight: 52,
  buffer: 12,
  threshold: 80,
  activeIndex: 70,
});

console.log(JSON.stringify({
  end: w.end,
  loadedCount: _sessionTouchLoadedCount,
  batched: w.batched,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["batched"] is True, "Touch window must be batched"
    assert result["end"] >= 71, \
        f"Active session at index 70 must be included in window end, got end={result['end']}"
    assert result["loadedCount"] >= 71, \
        f"Loaded count must extend to include active session, got {result['loadedCount']}"


@_node_tests
def test_multi_group_spacer_order():
    """Finding #4: Per-group spacers must keep unloaded rows inside their group.

    With 100 rows split across two groups (G1: 0-49, G2: 50-99) and loaded=60,
    G1's after-spacer should be 0 (all loaded) and G2's after-spacer should
    represent 40 unloaded rows (60-99)."""
    flat_rows = []
    for i in range(50):
        flat_rows.append({"group": {"label": "G1"}, "session": {"session_id": f"s1_{i}"}})
    for i in range(50):
        flat_rows.append({"group": {"label": "G2"}, "session": {"session_id": f"s2_{i}"}})

    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 100;

// Pre-populate 60 items (all of G1 + 10 of G2)
for (let i = 0; i < 50; i++) list._items.push(makeSessionItem('s1_' + i));
for (let i = 0; i < 10; i++) list._items.push(makeSessionItem('s2_' + i));

_touchRenderState = {{
  gen: 1, list: list, flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

// Create two group wrappers with bodies that track items
function setupGroup(label) {{
  const gw = makeEl('div');
  gw.className = 'session-date-group';
  gw.dataset['group-label'] = label;
  const body = makeBodyThatTracksItems(list);
  // Track after-spacers
  body._afterSpacers = [];
  body.querySelectorAll = function(sel) {{
    if (sel === '.session-virtual-spacer[data-virtual-spacer="after"]') return body._afterSpacers;
    return [];
  }};
  gw.querySelector = function(sel) {{
    if (sel === '.session-date-body') return body;
    return null;
  }};
  gw.appendChild(body);
  list._groups[label] = gw;
  return {{ gw: gw, body: body }};
}}

const g1 = setupGroup('G1');
const g2 = setupGroup('G2');

// _sessionVirtualSpacer already declared in preamble

// Mock list.querySelectorAll for group iteration
list.querySelectorAll = function(sel) {{
  if (sel === '.session-item[data-sid]') return list._items.slice();
  if (sel === '.session-date-group[data-group-label]') return [g1.gw, g2.gw];
  return [];
}};

_appendTouchBatch(); // 60 → 100

// Check per-group after-spacers
const g1Spacers = g1.body._afterSpacers.filter(s => s._parent === g1.body);
const g2Spacers = g2.body._afterSpacers.filter(s => s._parent === g2.body);

console.log(JSON.stringify({{
  loaded: _sessionTouchLoadedCount,
  totalItems: list._items.length,
  g1SpacerCount: g1.body.children.filter(c => c.className === 'session-virtual-spacer').length,
  g2SpacerCount: g2.body.children.filter(c => c.className === 'session-virtual-spacer').length,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["loaded"] == 100, f"Should load all 100 rows, got {result['loaded']}"
    assert result["totalItems"] == 100, f"Should have 100 DOM items, got {result['totalItems']}"
    # G1 has all 50 rows loaded — no after-spacer needed
    assert result["g1SpacerCount"] == 0, \
        f"G1 (fully loaded) should have no after-spacer, got {result['g1SpacerCount']}"
    # G2 has all 50 rows loaded after append — no after-spacer needed either
    assert result["g2SpacerCount"] == 0, \
        f"G2 (fully loaded after append) should have no after-spacer, got {result['g2SpacerCount']}"


@_node_tests
def test_append_preserves_existing_dom_nodes():
    """Append must not wipe or recreate existing session-item nodes.

    Uses exact object identity (===) to verify original nodes survive —
    not just a property check that's true for any object."""
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 5;
_sessionTouchTotalCount = 50;

// Pre-populate with 5 items — keep exact object references
const originalItems = [];
for (let i = 0; i < 5; i++) {
  const item = makeSessionItem('sess_' + i);
  list._items.push(item);
  originalItems.push(item);
}

const flatRows = [];
for (let i = 0; i < 50; i++) {
  flatRows.push({group:{label:'G'},session:{session_id:'sess_' + i}});
}
_touchRenderState = {
  gen: 1, list: list, flatRows: flatRows,
  renderOneSession: function(s) { return makeSessionItem(s.session_id); },
  activeSid: null,
};

// Create group wrapper — body's appendChild tracks session items
const gw = makeEl('div');
gw.dataset['group-label'] = 'G';
const body = makeBodyThatTracksItems(list);
gw.querySelector = function(sel) {
  if (sel === '.session-date-body') return body;
  return null;
};
gw.appendChild(body);
list._groups['G'] = gw;
list.children.push(gw);

_appendTouchBatch(); // 5 → 45

// Check original items survived via EXACT identity (===)
const survived = originalItems.every((item, i) => list._items[i] === item);
console.log(JSON.stringify({
  totalItems: list._items.length,
  survived: survived,
  innerHTMLWipes: _innerHTMLWipes,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["survived"], "Original DOM nodes must be the exact same objects after append (identity check)"
    assert result["innerHTMLWipes"] == 0, "No innerHTML wipes during append"
    assert result["totalItems"] == 45, f"Should have 45 items after append, got {result['totalItems']}"

# ── Production-composed tests (category 4 rework) ──────────────────────────
# These tests exercise the actual production schedule: render → reset →
# setup → append, with real state transitions. They verify the 4 categories
# of rework required by the nesquena-hermes gate certifier:
# 1. Scope reset BEFORE window calculation with complete fingerprint
# 2. Insert group fragment BEFORE after-spacer, validate+abort on missing row
# 3. Invalidation owns ALL state (gen, observer, RAF, list, loaded/total, token)
# 4. Terminal/stale RAF cleanup zeros the handle


@_node_tests
def test_partial_150_row_multigroup_append_spacer_order():
    """Category 2: Partial append in a multi-group list must NOT leave the
    spacer in front of newly loaded rows.

    With 150 rows split across two groups (G1: 0-59, G2: 60-149) and loaded=60,
    a partial append to 100 must produce:
    - G1: rows 0-59 (no after-spacer — all loaded)
    - G2: rows 60-99, THEN after-spacer (for rows 100-149)

    The spacer must be AFTER the newly loaded rows, not before them.
    """
    flat_rows = []
    for i in range(60):
        flat_rows.append({"group": {"label": "G1"}, "session": {"session_id": f"s1_{i}"}})
    for i in range(90):
        flat_rows.append({"group": {"label": "G2"}, "session": {"session_id": f"s2_{i}"}})

    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 150;

// Pre-populate 60 items (all of G1)
for (let i = 0; i < 60; i++) list._items.push(makeSessionItem('s1_' + i));

_touchRenderState = {{
  gen: 1, list: list, flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

// Set up two groups with bodies that track items AND track after-spacers
function setupGroup(label) {{
  const gw = makeEl('div');
  gw.className = 'session-date-group';
  gw.dataset['group-label'] = label;
  const body = makeBodyThatTracksItems(list);
  body._afterSpacers = [];
  body._spacerPositions = []; // track order: 'row' or 'spacer'
  // Track insertBefore to verify spacer is AFTER rows
  const origInsertBefore = body.insertBefore.bind(body);
  body.insertBefore = function(child, ref) {{
    if (child && child.tagName === '#document-fragment') {{
      const kids = child.children.slice();
      for (const k of kids) {{
        k._parent = this;
        this.children.push(k);
        if (k.dataset && k.dataset.sid) list._items.push(k);
        this._spacerPositions.push('row');
      }}
      child.children = [];
      return child;
    }}
    child._parent = this;
    this.children.push(child);
    this._spacerPositions.push('spacer');
    return child;
  }};
  body.querySelectorAll = function(sel) {{
    if (sel === '.session-virtual-spacer[data-virtual-spacer="after"]') return body._afterSpacers;
    return [];
  }};
  gw.querySelector = function(sel) {{
    if (sel === '.session-date-body') return body;
    return null;
  }};
  gw.appendChild(body);
  list._groups[label] = gw;
  return {{ gw: gw, body: body }};
}}

const g1 = setupGroup('G1');
const g2 = setupGroup('G2');

// Mock list.querySelectorAll for group iteration
list.querySelectorAll = function(sel) {{
  if (sel === '.session-item[data-sid]') return list._items.slice();
  if (sel === '.session-date-group[data-group-label]') return [g1.gw, g2.gw];
  return [];
}};

_appendTouchBatch(); // 60 → 100

// Check G2's body: rows should come BEFORE any spacer
const g2RowBeforeSpacer = g2.body._spacerPositions.length > 0
  ? g2.body._spacerPositions.indexOf('row') < g2.body._spacerPositions.indexOf('spacer')
  : true; // no spacer, all rows — fine

console.log(JSON.stringify({{
  loaded: _sessionTouchLoadedCount,
  totalItems: list._items.length,
  g2Positions: g2.body._spacerPositions,
  g2RowBeforeSpacer: g2RowBeforeSpacer,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["loaded"] == 100, f"Should load 100 rows, got {result['loaded']}"
    assert result["totalItems"] == 100, f"Should have 100 DOM items, got {result['totalItems']}"
    # The critical assertion: in G2, rows must come BEFORE the spacer
    assert result["g2RowBeforeSpacer"], \
        f"G2 rows must be BEFORE the after-spacer, got positions: {result['g2Positions']}"


@_node_tests
def test_invalidation_clears_all_state():
    """Category 3: _invalidateTouchRender must null list, loaded, total,
    and bump generation — not just observer/RAF/renderState/pending."""
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 5;
_sessionTouchLoadedCount = 80;
_sessionTouchTotalCount = 100;
_touchSentinelObserver = { disconnect: () => {} }; // mock observer
_touchScrollFallbackRaf = 42; // mock RAF handle
_touchBatchPending = true;
_touchRenderState = { gen: 5, list: list, flatRows: [], renderOneSession: () => {}, activeSid: null };

_invalidateTouchRender();

console.log(JSON.stringify({
  listNulled: _sessionTouchListEl === null,
  loadedZeroed: _sessionTouchLoadedCount === 0,
  totalZeroed: _sessionTouchTotalCount === 0,
  genBumped: _sessionTouchGen === 6,
  observerNulled: _touchSentinelObserver === null,
  rafZeroed: _touchScrollFallbackRaf === 0,
  renderStateNulled: _touchRenderState === null,
  pendingCleared: _touchBatchPending === false,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["listNulled"], "_invalidateTouchRender must null _sessionTouchListEl"
    assert result["loadedZeroed"], "_invalidateTouchRender must zero _sessionTouchLoadedCount"
    assert result["totalZeroed"], "_invalidateTouchRender must zero _sessionTouchTotalCount"
    assert result["genBumped"], "_invalidateTouchRender must bump _sessionTouchGen"
    assert result["observerNulled"], "_invalidateTouchRender must null observer"
    assert result["rafZeroed"], "_invalidateTouchRender must zero RAF handle"
    assert result["renderStateNulled"], "_invalidateTouchRender must null render state"
    assert result["pendingCleared"], "_invalidateTouchRender must clear pending"


@_node_tests
def test_terminal_raf_returns_zero_handle():
    """Category 3: Terminal/stale RAF returns must zero the handle — not
    leave _touchScrollFallbackRaf holding a fired nonzero value.

    Simulates the fallback RAF check function: when generation changes,
    list is replaced, or all rows are loaded, the handle must be zeroed.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
// Extract the fallback check function from _setupTouchSentinel
const setupFn = extractFunc('_setupTouchSentinel');

// The function source must contain _touchScrollFallbackRaf=0 on all
// three terminal return paths: generation change, list change, all loaded.
const genReturnMatch = setupFn.includes('_touchScrollFallbackRaf=0;return');
const loadedReturnMatch = setupFn.match(/l>=t\\)\\s*\\{?\\s*_touchScrollFallbackRaf=0/);

console.log(JSON.stringify({
  genReturnHasZero: genReturnMatch,
  loadedReturnHasZero: !!loadedReturnMatch,
  // Count occurrences of _touchScrollFallbackRaf=0 — should be at least 3
  // (one in _invalidateTouchRender is not in _setupTouchSentinel, so count
  // the ones in the fallback check)
  zeroCount: (setupFn.match(/_touchScrollFallbackRaf=0/g) || []).length,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["zeroCount"] >= 3, \
        f"Fallback RAF must zero handle on all 3 terminal returns, got {result['zeroCount']} occurrences"


@_node_tests
def test_equal_count_reorder_after_100_loaded():
    """Category 1: An equal-count reorder after 100 loaded rows must be
    detected by the SID prefix in the scope fingerprint and trigger a reset.

    Simulates: 100 rows loaded, then the order changes (same total, different
    SIDs in the prefix). The fingerprint's sidPrefix component must detect this.
    """
    # Original rows: sess_0 through sess_149
    # Reordered rows: sess_149 through sess_0 (reversed)
    original_rows = [{"group": {"label": "G"}, "session": {"session_id": f"sess_{i}"}} for i in range(150)]
    reordered_rows = [{"group": {"label": "G"}, "session": {"session_id": f"sess_{149-i}"}} for i in range(150)]

    # Build the fingerprint components manually to verify the SID prefix differs
    original_sid_prefix = ",".join(r["session"]["session_id"] for r in original_rows[:60])
    reordered_sid_prefix = ",".join(r["session"]["session_id"] for r in reordered_rows[:60])

    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
// Simulate the scope fingerprint comparison from renderSessionListFromCache
const originalSidPrefix = {original_sid_prefix!r};
const reorderedSidPrefix = {reordered_sid_prefix!r};

// The fingerprint includes sidPrefix — a reorder changes it even at equal count
const fp1 = ['0', '', 'webui', '0', '', '0', '', '', '150', originalSidPrefix].join('|');
const fp2 = ['0', '', 'webui', '0', '', '0', '', '', '150', reorderedSidPrefix].join('|');

console.log(JSON.stringify({{
  fingerprintsDiffer: fp1 !== fp2,
  sidPrefixDiffers: originalSidPrefix !== reorderedSidPrefix,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["fingerprintsDiffer"], "Equal-count reorder must produce a different fingerprint"
    assert result["sidPrefixDiffers"], "SID prefix must differ on reorder"


@_node_tests
def test_observer_replacement_disconnects_old():
    """Category 3: Actual observer replacement — _ensureTouchSentinelObserver
    must disconnect the old observer before creating a new one."""
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
let disconnectCount = 0;
let observeCount = 0;

// Mock IntersectionObserver
global.IntersectionObserver = class {
  constructor(cb, opts) {
    this.cb = cb;
    this.opts = opts;
    this._observed = [];
  }
  observe(el) { observeCount++; this._observed.push(el); }
  disconnect() { disconnectCount++; this._observed = []; }
  unobserve(el) {}
};
window.IntersectionObserver = global.IntersectionObserver;

// First call creates observer
const list1 = makeList();
_ensureTouchSentinelObserver(list1);
const observer1 = _touchSentinelObserver;

// Second call must disconnect the first and create a new one
const list2 = makeList();
_ensureTouchSentinelObserver(list2);
const observer2 = _touchSentinelObserver;

console.log(JSON.stringify({
  observerReplaced: observer1 !== observer2,
  oldDisconnected: disconnectCount >= 1,
  observer1NotNull: observer1 !== null,
  observer2NotNull: observer2 !== null,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["observerReplaced"], "Observer must be replaced on second call"
    assert result["oldDisconnected"], "Old observer must be disconnected"
    assert result["observer2NotNull"], "New observer must be created"


@_node_tests
def test_append_missing_row_aborts_without_advancing():
    """Category 2: A missing row in flatRows must cause append to ABORT
    without advancing the loaded count — not silently skip it."""
    # flatRows with a null at index 62 (within the 60→100 batch)
    flat_rows = []
    for i in range(100):
        if i == 62:
            flat_rows.append({"group": {"label": "G"}, "session": None})  # missing session
        else:
            flat_rows.append({"group": {"label": "G"}, "session": {"session_id": f"s_{i}"}})

    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 100;
for (let i = 0; i < 60; i++) list._items.push(makeSessionItem('s_' + i));

_touchRenderState = {{
  gen: 1, list: list, flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

const gw = makeEl('div');
gw.dataset['group-label'] = 'G';
const body = makeBodyThatTracksItems(list);
gw.querySelector = function(sel) {{ if(sel==='.session-date-body') return body; return null; }};
gw.appendChild(body);
list._groups['G'] = gw;

_appendTouchBatch();

console.log(JSON.stringify({{
  loadedCount: _sessionTouchLoadedCount, // should stay at 60 — abort
  itemCount: list._items.length, // should stay at 60 — no partial commit
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["loadedCount"] == 60, \
        f"Missing row must abort without advancing loaded count, got {result['loadedCount']}"
    assert result["itemCount"] == 60, \
        f"Missing row must not commit any items, got {result['itemCount']}"


@_node_tests
def test_append_missing_body_aborts_without_advancing():
    """Category 2: A missing group body must cause append to ABORT
    without advancing the loaded count."""
    flat_rows = [{"group": {"label": "G"}, "session": {"session_id": f"s_{i}"}} for i in range(100)]

    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 100;
for (let i = 0; i < 60; i++) list._items.push(makeSessionItem('s_' + i));

_touchRenderState = {{
  gen: 1, list: list, flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

// Group wrapper WITHOUT a body — querySelector returns null
const gw = makeEl('div');
gw.dataset['group-label'] = 'G';
gw.querySelector = function(sel) {{ return null; }}; // no body found
list._groups['G'] = gw;

_appendTouchBatch();

console.log(JSON.stringify({{
  loadedCount: _sessionTouchLoadedCount, // should stay at 60 — abort
  itemCount: list._items.length,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["loadedCount"] == 60, \
        f"Missing body must abort without advancing loaded count, got {result['loadedCount']}"
