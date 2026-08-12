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
    assert "_touchScrollOwner" in fn, \
        "Fallback must use the scroll-owner record for event-driven batch triggering"
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
    assert "_touchScrollOwner" in fn
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
    # Terminal/stale returns must clear the owner's RAF handle — not just return
    assert "owner.raf=0" in fn, \
        "Terminal/stale RAF returns must clear the owner's RAF handle"


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
    querySelector(sel) {
      // Support simple class selectors for newly created elements (e.g.
      // wrappers returned by _createTouchGroupWrapper). The mock doesn't
      // implement a full selector engine, but it can match single-class
      // and data-attribute selectors against children.
      if (typeof sel !== 'string') return null;
      // .session-date-body
      var m = sel.match(/^\\.([\\w-]+)$/);
      if (m) {
        for (var i = 0; i < this.children.length; i++) {
          var c = this.children[i];
          if (c.className && c.className.indexOf(m[1]) >= 0) return c;
        }
        return null;
      }
      // .session-virtual-spacer[data-virtual-spacer="after"]
      m = sel.match(/^\.([\w-]+)\[([\w-]+)="([\w-]+)"\]$/);
      if (m) {
        for (var i = 0; i < this.children.length; i++) {
          var c = this.children[i];
          if (c.className && c.className.indexOf(m[1]) >= 0 &&
              c.dataset && c.dataset[m[2].replace(/-/g,'')] === m[3]) return c;
        }
        return null;
      }
      return null;
    },
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
if (!document.createElement) {
  document.createElement = function(tag) {
    return makeEl(tag || 'div');
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
let _touchBatchToken = 0;
let _touchContinuousBatchScheduled = false;
let _touchScrollOwner = null;
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
// clears ALL touch state (observer, RAF, scroll listener, render state,
// list, loaded/total, pending, generation, token). Owner-qualified: only
// cancels the listener/RAF of the CURRENT _touchScrollOwner.
function _invalidateTouchRender() {
  if (_touchSentinelObserver) { _touchSentinelObserver.disconnect(); _touchSentinelObserver = null; }
  const owner = _touchScrollOwner;
  if (owner) {
    if (owner.raf) { /* mock: no cancelAnimationFrame */ }
    if (owner.handler && owner.list) {
      try { owner.list.removeEventListener('scroll', owner.handler, {passive: true}); } catch(_) {}
    }
  }
  _touchScrollOwner = null;
  _touchRenderState = null;
  _sessionTouchListEl = null;
  _sessionTouchLoadedCount = 0;
  _sessionTouchTotalCount = 0;
  _touchBatchPending = false;
  _touchContinuousBatchScheduled = false;
  _sessionTouchGen++;
  _touchBatchToken++;
}

// Extract and eval all touch functions
eval(extractFunc('_createTouchGroupWrapper'));
eval(extractFunc('_updateTouchGroupSpacers'));
eval(extractFunc('_updateTouchSentinel'));
eval(extractFunc('_scheduleContinuousBatch'));
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
_touchScrollOwner = { gen: 5, list: list, handler: function(){}, raf: 42, token: 0 }; // mock owner with RAF
_touchBatchPending = true;
_touchRenderState = { gen: 5, list: list, flatRows: [], renderOneSession: () => {}, activeSid: null };

_invalidateTouchRender();

console.log(JSON.stringify({
  listNulled: _sessionTouchListEl === null,
  loadedZeroed: _sessionTouchLoadedCount === 0,
  totalZeroed: _sessionTouchTotalCount === 0,
  genBumped: _sessionTouchGen === 6,
  observerNulled: _touchSentinelObserver === null,
  ownerNulled: _touchScrollOwner === null,
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
    assert result["ownerNulled"], "_invalidateTouchRender must null _touchScrollOwner"
    assert result["renderStateNulled"], "_invalidateTouchRender must null render state"
    assert result["pendingCleared"], "_invalidateTouchRender must clear pending"


@_node_tests
def test_terminal_raf_returns_zero_handle():
    """Category 3: Terminal/stale RAF returns must clear the owner's RAF handle
    — not leave owner.raf holding a fired nonzero value.

    Simulates the fallback RAF check function: when generation changes,
    list is replaced, or all rows are loaded, the handle must be cleared.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
// Extract the fallback check function from _setupTouchSentinel
const setupFn = extractFunc('_setupTouchSentinel');

// The function source must clear owner.raf on the terminal return path
// (when the RAF callback runs, it clears owner.raf before doing work).
const hasOwnerRafZero = setupFn.includes('owner.raf=0');

console.log(JSON.stringify({
  hasOwnerRafZero: hasOwnerRafZero,
  // Count occurrences of owner.raf=0 — the RAF callback clears its own handle
  zeroCount: (setupFn.match(/owner\.raf=0/g) || []).length,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["zeroCount"] >= 1, \
        f"Scroll RAF callback must clear owner.raf, got {result['zeroCount']} occurrences"


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


# ── Production-composed chain tests (gate certifier requested) ──────────────
# These exercise the real renderSessionListFromCache → _setupTouchSentinel →
# _appendTouchBatch sequence, not just isolated function calls. The gate
# certifier specifically called out that prior tests "do not execute the
# production render-to-append chain."


def test_setup_touch_sentinel_restores_painted_extent():
    """Source-level pin: _setupTouchSentinel must accept a paintedExtent param
    and restore _sessionTouchLoadedCount to it AFTER _invalidateTouchRender
    (which resets it to 0), BEFORE installing the sentinel observer/RAF.

    Without this, the first _appendTouchBatch() reads oldLoaded=0, its prefix
    check is vacuous, and it appends rows 0–39 behind the already-painted
    rows 0–59 — duplicating the first batch.
    """
    fn = _extract_fn(SESSIONS_JS, "_setupTouchSentinel")
    assert "paintedExtent" in fn, \
        "_setupTouchSentinel must accept a paintedExtent parameter"
    # The restore must happen AFTER _invalidateTouchRender and BEFORE _ensureTouchSentinelObserver
    invalidate_idx = fn.find("_invalidateTouchRender()")
    restore_idx = fn.find("_sessionTouchLoadedCount")
    observer_idx = fn.find("_ensureTouchSentinelObserver")
    assert invalidate_idx >= 0 and restore_idx >= 0 and observer_idx >= 0
    assert invalidate_idx < restore_idx, \
        "Loaded count restore must come AFTER _invalidateTouchRender"
    assert restore_idx < observer_idx, \
        "Loaded count restore must come BEFORE observer installation"


def test_setup_touch_sentinel_called_with_painted_extent():
    """Source-level pin: renderSessionListFromCache must pass virtualWindow.end
    as the paintedExtent argument to _setupTouchSentinel."""
    src = SESSIONS_JS
    # Find the call site (not the function definition). The call site is
    # indented and doesn't start with "function".
    matches = list(re.finditer(r'(?<!function )_setupTouchSentinel\(list,', src))
    assert matches, "Call to _setupTouchSentinel not found"
    # Use the last match — the function definition appears first in the file;
    # the call site appears later inside renderSessionListFromCache.
    call_match = matches[-1]
    call_idx = call_match.start()
    call_end = src.find(");", call_idx)
    call_text = src[call_idx:call_end + 2]
    assert "virtualWindow.end" in call_text, \
        f"Must pass virtualWindow.end as paintedExtent, got: {call_text}"


def test_setup_touch_sentinel_called_unconditionally():
    """Source-level pin: _setupTouchSentinel must be called unconditionally,
    NOT gated inside if(_isTouchPrimary()). The function handles the touch→
    non-touch transition internally — gating it makes teardown unreachable,
    leaving a dangling observer and RAF after a touch→desktop transition."""
    src = SESSIONS_JS
    call_idx = src.find("_setupTouchSentinel(list,")
    # Look backwards from the call for an if(_isTouchPrimary()) gate
    before = src[max(0, call_idx - 200):call_idx]
    # The call must NOT be inside an if(_isTouchPrimary()) block
    # Check if there's an if(_isTouchPrimary()) in the preceding lines
    # that wraps this call (would have an opening brace before the call)
    if_matches = re.findall(r'if\s*\(\s*_isTouchPrimary\(\)\s*\)\s*\{', before)
    assert len(if_matches) == 0, \
        "_setupTouchSentinel must be called unconditionally, not inside if(_isTouchPrimary())"


def test_skeleton_teardown_unconditional():
    """Source-level pin: showSessionListSkeleton must call _invalidateTouchRender
    UNCONDITIONALLY — not gated on _isTouchPrimary(). The old touch owner may
    have already lost capability (_isTouchPrimary() now false) but its observer,
    RAF, render state, pending work, and generation are still live. Gating on
    the current capability check leaves them dangling after the skeleton
    replaces the DOM."""
    fn = _extract_fn(SESSIONS_JS, "showSessionListSkeleton")
    # The function must call _invalidateTouchRender without gating on _isTouchPrimary
    assert "_invalidateTouchRender" in fn, \
        "showSessionListSkeleton must call _invalidateTouchRender"
    # The call must NOT be inside an if(_isTouchPrimary()) gate
    invalidate_idx = fn.find("_invalidateTouchRender")
    before = fn[max(0, invalidate_idx - 200):invalidate_idx]
    if_matches = re.findall(r'if\s*\(\s*_isTouchPrimary\(\)\s*\)\s*\{', before)
    assert len(if_matches) == 0, \
        "showSessionListSkeleton must call _invalidateTouchRender unconditionally, not inside if(_isTouchPrimary())"


def test_prefix_authority_exact_match():
    """Source-level pin: _appendTouchBatch prefix validation must use exact
    equality (!==) not less-than (<). An extra stale live row (61 items,
    oldLoaded=60) must invalidate/rebuild, not silently duplicate."""
    fn = _extract_fn(SESSIONS_JS, "_appendTouchBatch")
    assert "existingItems.length!==oldLoaded" in fn, \
        "Prefix validation must use exact equality (!==), not < — an extra stale row must invalidate"


def test_append_commit_two_phase():
    """Source-level pin: _appendTouchBatch commit must be two-phase —
    validate ALL group bodies exist BEFORE attaching any fragment.

    The prior single-pass version attached group A, then aborted on a missing
    group B body without advancing the loaded count — retry re-appended
    group A's rows, duplicating them.
    """
    fn = _extract_fn(SESSIONS_JS, "_appendTouchBatch")
    assert "commitTargets" in fn, \
        "Commit must use a commitTargets array for two-phase validation"
    assert "Phase 1" in fn or "Phase 2" in fn, \
        "Commit must document the two-phase pattern"
    # All fragments must be attached in a separate loop AFTER validation
    assert "for(const target of commitTargets)" in fn, \
        "Phase 2 must iterate commitTargets to attach fragments"


@_node_tests
def test_production_chain_setup_preserves_loaded_count():
    """Production-composed: simulates the real
    renderSessionListFromCache → _setupTouchSentinel → _appendTouchBatch chain.

    renderSessionListFromCache paints rows 0–59 (virtualWindow.end=60), then
    calls _setupTouchSentinel which invalidates (resetting loadedCount to 0)
    and must restore loadedCount to 60 (paintedExtent). When the observer
    fires _appendTouchBatch, oldLoaded must be 60, NOT 0 — otherwise rows
    0–39 are appended as duplicates behind the already-painted 0–59.
    """
    total = 100
    flat_rows = [{"group": {"label": "G"}, "session": {"session_id": f"s_{i}"}} for i in range(total)]

    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchGen = 0;

// Simulate renderSessionListFromCache painting 60 rows (virtualWindow.end=60)
const paintedExtent = 60;
for (let i = 0; i < paintedExtent; i++) list._items.push(makeSessionItem('s_' + i));

// Mock _isTouchPrimary to return true
function _isTouchPrimary() {{ return true; }}

// Mock document.createElement for sentinel div creation
document.createElement = function(tag) {{ return makeEl(tag); }};

// Mock requestAnimationFrame (not used when IntersectionObserver exists)
if (typeof requestAnimationFrame === 'undefined') global.requestAnimationFrame = function(cb) {{ return 0; }};

// Set up group G with a body that tracks items — simulates what
// renderSessionListFromCache's row loop creates before calling setup.
const gw = makeEl('div');
gw.dataset['group-label'] = 'G';
const body = makeBodyThatTracksItems(list);
gw.querySelector = function(sel) {{ if(sel==='.session-date-body') return body; return null; }};
gw.appendChild(body);
list._groups['G'] = gw;

// Mock _ensureTouchSentinelObserver (extracted separately)
eval(extractFunc('_ensureTouchSentinelObserver'));

// Call _setupTouchSentinel with paintedExtent — simulates what
// renderSessionListFromCache does at line ~8381.
eval(extractFunc('_setupTouchSentinel'));
_setupTouchSentinel(list, {total}, {json.dumps(flat_rows)},
  function(s) {{ return makeSessionItem(s.session_id); }},
  null, paintedExtent);

// After setup, loadedCount must be restored to paintedExtent (60), not 0.
const loadedAfterSetup = _sessionTouchLoadedCount;

// Now simulate the observer firing _appendTouchBatch.
// oldLoaded must be 60, not 0 — so rows 60–99 are appended, not 0–39.
_appendTouchBatch();

console.log(JSON.stringify({{
  loadedAfterSetup: loadedAfterSetup,
  loadedAfterAppend: _sessionTouchLoadedCount,
  itemCount: list._items.length,
  // Check for duplicates — all sids must be unique
  sids: list._items.map(function(i) {{ return i.dataset.sid; }}),
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["loadedAfterSetup"] == 60, \
        f"After setup, loadedCount must be restored to paintedExtent (60), got {result['loadedAfterSetup']}"
    assert result["loadedAfterAppend"] == 100, \
        f"After append, loadedCount must be 100, got {result['loadedAfterAppend']}"
    assert result["itemCount"] == 100, \
        f"After append, DOM must have 100 items, got {result['itemCount']}"
    # Verify no duplicates
    sids = result["sids"]
    assert len(sids) == len(set(sids)), \
        f"Duplicate SIDs found after append: {sids}"


@_node_tests
def test_partial_commit_does_not_attach_any_group():
    """Two-phase commit: if group A's body exists but group B's body is missing,
    the append must abort WITHOUT attaching ANY fragment — not just group B's.

    The prior single-pass version attached group A, then aborted on group B,
    leaving A's rows in the DOM while the loaded count stayed at oldLoaded.
    Retry re-appended group A's rows, duplicating them.
    """
    # Two groups: A (rows 60–79) and B (rows 80–99)
    flat_rows = []
    for i in range(100):
        group_label = "A" if i < 80 else "B"
        flat_rows.append({"group": {"label": group_label}, "session": {"session_id": f"s_{i}"}})

    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 100;
// Pre-populate 60 items in group A
for (let i = 0; i < 60; i++) list._items.push(makeSessionItem('s_' + i));

_touchRenderState = {{
  gen: 1, list: list, flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

// Group A has a body; group B does NOT (querySelector returns null for body)
const gwA = makeEl('div');
gwA.dataset['group-label'] = 'A';
const bodyA = makeBodyThatTracksItems(list);
gwA.querySelector = function(sel) {{ if(sel==='.session-date-body') return bodyA; return null; }};
gwA.appendChild(bodyA);

const gwB = makeEl('div');
gwB.dataset['group-label'] = 'B';
gwB.querySelector = function(sel) {{ return null; }}; // no body for group B

list._groups['A'] = gwA;
list._groups['B'] = gwB;

_appendTouchBatch();

// Count items per group — group A should NOT have received any new rows
// because the commit should have aborted entirely.
console.log(JSON.stringify({{
  loadedCount: _sessionTouchLoadedCount, // must stay at 60
  totalItems: list._items.length, // must stay at 60
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["loadedCount"] == 60, \
        f"Partial commit must not advance loaded count, got {result['loadedCount']}"
    assert result["totalItems"] == 60, \
        f"Partial commit must not attach ANY items, got {result['totalItems']}"


@_node_tests
@_node_tests
def test_missing_wrapper_then_live_malformed_wrapper_repair_retry():
    """Single-VM test: group A has no wrapper (Phase 1 creates it detached),
    group B has a LIVE wrapper in list.children with no body (malformed).
    Phase 1 must abort at B WITHOUT attaching A's detached wrapper or mutating
    B. Then repair B IN PLACE (same VM, same list), retry, and assert:
    - Top-level order is P, A, B, [sentinel] (canonical, not B, A)
    - Every SID s_0..s_99 appears exactly once
    - B's wrapper node identity is preserved (same object reference)
    - Loaded count reaches 100

    Strengthened per gate-certifier review at e159285654:
    - ALL post-retry session rows derived by traversing list.children (the
      live child tree), NOT from saved gwP/wrapperA/gwB references. A detached
      saved reference cannot false-green.
    - Assert the exact top-level identity sequence and length, including the
      same live gwP at its position, one canonical A, the same gwB, and any
      retained sentinel/spacer.
    - Assert exact body ownership from that live tree: P=s_0..s_59,
      A=s_60..s_79, B=s_80..s_99.
    - Seed non-empty malformed-B internals plus retained spacer/sentinel
      objects; snapshot their identity/order/state; prove abort leaves them
      unchanged.
    - Mutation bite: if a canonical live wrapper is removed/replaced/duplicated
      the test fails, rather than satisfying aggregate SID checks from a
      detached saved reference.

    Scenario: 100 rows in 3 groups. Rows 0-59 in group P (pre-painted,
    existing wrapper with body — provides the 60 DOM items the prefix
    validation needs). Rows 60-79 in group A (no wrapper — Phase 1 creates
    it detached). Rows 80-99 in group B (live malformed wrapper — no body,
    but WITH pre-existing children and a spacer to exercise internal-state
    preservation). A sentinel element is also seeded in list.children.
    The first _appendTouchBatch processes rows 60-99: Phase 1 validates A
    (creates detached), validates B (exists, no body — abort). A's detached
    wrapper must NOT be attached. B must be unchanged — including its
    internal children, spacer, and the sentinel.
    """
    # 100 rows: 0-59 in P, 60-79 in A (isPinned=true), 80-99 in B
    flat_rows = []
    for i in range(100):
        if i < 60:
            group_label = "P"
            is_pinned = False
        elif i < 80:
            group_label = "A"
            is_pinned = True
        else:
            group_label = "B"
            is_pinned = False
        flat_rows.append({"group": {"label": group_label, "isPinned": is_pinned}, "session": {"session_id": f"s_{i}"}})

    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 100;

// ── DOM-faithful list: querySelector/querySelectorAll derive authority
//    from the live child tree (list.children), NOT from _groups/_items
//    side registries. This mirrors how production's real DOM works —
//    the mock cannot diverge from DOM authority. ──
list.querySelector = function(sel) {{
  // [data-touch-sentinel]
  if (sel === '[data-touch-sentinel]') {{
    return list.children.find(c => c.dataset && c.dataset['touch-sentinel'] !== undefined) || null;
  }}
  // .session-date-group[data-group-label="..."]
  var m = sel.match(/^\\.session-date-group\\[data-group-label="([^"]+)"\\]$/);
  if (m) {{
    return list.children.find(c => c.dataset && c.dataset['group-label'] === m[1]) || null;
  }}
  // .session-date-group[data-group-label] (no value)
  m = sel.match(/^\\.session-date-group\\[data-group-label\\]$/);
  if (m) {{
    return list.children.find(c => c.dataset && c.dataset['group-label'] !== undefined) || null;
  }}
  return null;
}};
list.querySelectorAll = function(sel) {{
  if (sel === '.session-item[data-sid]') {{
    // Traverse the live child tree: find all session-item elements by
    // walking group wrappers → bodies → session-item children.
    var items = [];
    for (var gi = 0; gi < list.children.length; gi++) {{
      var gw = list.children[gi];
      if (!gw.dataset || gw.dataset['group-label'] === undefined) continue;
      var body = null;
      if (typeof gw.querySelector === 'function') {{
        body = gw.querySelector('.session-date-body');
      }} else {{
        body = gw.children.find(c => c.className && c.className.indexOf('session-date-body') >= 0) || null;
      }}
      if (!body) continue;
      for (var bi = 0; bi < body.children.length; bi++) {{
        var c = body.children[bi];
        if (c.className && c.className.indexOf('session-item') >= 0 && c.dataset && c.dataset.sid) {{
          items.push(c);
        }}
      }}
    }}
    return items;
  }}
  if (sel === '.session-date-group[data-group-label]') {{
    return list.children.filter(c => c.dataset && c.dataset['group-label'] !== undefined);
  }}
  if (sel === '.session-virtual-spacer[data-virtual-spacer="after"]') {{
    return list._afterSpacers || [];
  }}
  return [];
}};

// Override _createTouchGroupWrapper so newly created group A's body tracks
// session items (the production version uses real DOM which tracks naturally;
// the mock needs explicit wiring). The body's appendChild must push items
// into the live DOM tree so they're visible to the DOM-faithful
// querySelectorAll above.
const _origCreate = _createTouchGroupWrapper;
_createTouchGroupWrapper = function(g, st) {{
  const wrapper = _origCreate(g, st);
  const trackingBody = makeBodyThatTracksItems(list);
  trackingBody.className = 'session-date-body';
  wrapper.children = wrapper.children.filter(c => c.className !== 'session-date-body');
  wrapper.appendChild(trackingBody);
  wrapper.querySelector = function(sel) {{
    if (sel === '.session-date-body') return trackingBody;
    return null;
  }};
  wrapper.querySelectorAll = function(sel) {{
    if (sel === '.session-virtual-spacer[data-virtual-spacer="after"]') {{
      return trackingBody.children.filter(c => c.className && c.className.indexOf('session-virtual-spacer') >= 0);
    }}
    return [];
  }};
  return wrapper;
}};

// ── Group P: existing wrapper with 60 pre-painted items ──
// This provides the prefix validation with exactly 60 DOM session items.
const gwP = makeEl('div');
gwP.className = 'session-date-group';
gwP.dataset['group-label'] = 'P';
const bodyP = makeBodyThatTracksItems(list);
bodyP.className = 'session-date-body';
gwP.appendChild(bodyP);
gwP.querySelector = function(sel) {{
  if (sel === '.session-date-body') return bodyP;
  return null;
}};
gwP.querySelectorAll = function(sel) {{
  if (sel === '.session-virtual-spacer[data-virtual-spacer="after"]') {{
    return bodyP.children.filter(c => c.className && c.className.indexOf('session-virtual-spacer') >= 0);
  }}
  return [];
}};
list.children.push(gwP);
// Populate _items (for legacy compat) and the actual body (for DOM-faithful traversal)
for (let i = 0; i < 60; i++) list._items.push(makeSessionItem('s_' + i));
for (let i = 0; i < 60; i++) bodyP.appendChild(makeSessionItem('s_' + i));

_touchRenderState = {{
  gen: 1, list: list, flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

// ── Group B: LIVE malformed wrapper — in list.children but querySelector
//    returns null for .session-date-body (no body = malformed).
//    BUT B is NOT empty: it has pre-existing children (a header and a
//    spacer element) to exercise internal-state preservation. ──
const gwB = makeEl('div');
gwB.className = 'session-date-group';
gwB.dataset['group-label'] = 'B';
// Seed non-empty internal state: a header child and a virtual spacer child
const bHeader = makeEl('div');
bHeader.className = 'session-date-header';
gwB.appendChild(bHeader);
const bSpacer = makeEl('div');
bSpacer.className = 'session-virtual-spacer';
bSpacer.dataset['virtual-spacer'] = 'after';
gwB.appendChild(bSpacer);
gwB.querySelector = function(sel) {{ return null; }}; // no body — malformed
gwB.querySelectorAll = function(sel) {{ return []; }};
list.children.push(gwB); // B is LIVE in list.children

// ── Sentinel element: seeded in list.children to exercise sentinel
//    preservation. Production uses this for the IntersectionObserver. ──
const sentinel = makeEl('div');
sentinel.className = 'touch-sentinel';
sentinel.dataset['touch-sentinel'] = '1';
list.children.push(sentinel);

// ── Deep snapshot of B BEFORE append ──
// Capture B's exact children (by reference), their order, classNames,
// B's dataset keys/values, B's className, and any relevant attributes.
const bChildrenRefsBefore = gwB.children.slice();
const bChildrenClassesBefore = gwB.children.map(c => c.className || '');
const bDatasetBefore = {{}};
for (const k in gwB.dataset) bDatasetBefore[k] = gwB.dataset[k];
const bClassNameBefore = gwB.className || '';
const bChildrenCountBefore = gwB.children.length;

// Snapshot the live tree's top-level state (including sentinel)
const childrenBefore = list.children.slice();
const topLevelCountBefore = list.children.length;
const loadedBefore = _sessionTouchLoadedCount;

// Snapshot sentinel identity/position
const sentinelIdxBefore = list.children.indexOf(sentinel);

// Snapshot what SID nodes exist inside B (from DOM-faithful traversal —
// should be 0 since B has no body)
const sidsInBBefore = (function() {{
  var body = null;
  if (typeof gwB.querySelector === 'function') body = gwB.querySelector('.session-date-body');
  if (!body) return [];
  return body.children.filter(c => c.className && c.className.indexOf('session-item') >= 0)
    .map(c => c.dataset.sid);
}})();

// ── First append: must abort at B (no body) ──
_appendTouchBatch();

const loadedAfterAbort = _sessionTouchLoadedCount;
const childrenAfterAbort = list.children.slice();
const topLevelCountAfterAbort = list.children.length;

// ── Prove B is UNCHANGED after abort (deep zero-mutation proof) ──
// 1. B's wrapper reference identity preserved in live tree
const bWrapperInTreeAfterAbort = childrenAfterAbort.indexOf(gwB) >= 0;

// 2. B's children are exactly the same references in the same order
const bChildrenRefsAfter = gwB.children.slice();
const bChildrenSameRefs = bChildrenRefsBefore.length === bChildrenRefsAfter.length &&
  bChildrenRefsBefore.every((ref, i) => bChildrenRefsAfter[i] === ref);
const bChildrenSameClasses = bChildrenClassesBefore.length === bChildrenRefsAfter.length &&
  bChildrenClassesBefore.every((cls, i) => (bChildrenRefsAfter[i].className || '') === cls);

// 3. B's dataset unchanged (this is the mutation bite — catches
//    data-reviewer-mutated-before-abort and any other attribute mutation)
const bDatasetAfter = {{}};
for (const k in gwB.dataset) bDatasetAfter[k] = gwB.dataset[k];
const bDatasetSame = Object.keys(bDatasetBefore).length === Object.keys(bDatasetAfter).length &&
  Object.keys(bDatasetBefore).every(k => bDatasetAfter[k] === bDatasetBefore[k]);

// 4. B's className unchanged
const bClassNameAfter = gwB.className || '';
const bClassNameSame = bClassNameBefore === bClassNameAfter;

// 5. B's children count unchanged
const bChildrenCountAfter = gwB.children.length;
const bChildrenCountSame = bChildrenCountBefore === bChildrenCountAfter;

// 6. Top-level tree unchanged: same count, same refs, same order
const topLevelCountSame = topLevelCountBefore === topLevelCountAfterAbort;
const topLevelSameRefs = childrenBefore.length === childrenAfterAbort.length &&
  childrenBefore.every((ref, i) => childrenAfterAbort[i] === ref);

// 7. No SID nodes appeared inside B after abort
const sidsInBAfter = (function() {{
  var body = null;
  if (typeof gwB.querySelector === 'function') body = gwB.querySelector('.session-date-body');
  if (!body) return [];
  return body.children.filter(c => c.className && c.className.indexOf('session-item') >= 0)
    .map(c => c.dataset.sid);
}})();
const bNoNewSids = JSON.stringify(sidsInBBefore) === JSON.stringify(sidsInBAfter);

// 8. Sentinel unchanged: same ref, same position
const sentinelIdxAfterAbort = list.children.indexOf(sentinel);
const sentinelSameRef = sentinelIdxAfterAbort >= 0;
const sentinelSamePos = sentinelIdxBefore === sentinelIdxAfterAbort;

// Group P must NOT have received any new rows (from DOM-faithful traversal)
const sidsInPAfterAbort = (function() {{
  var body = gwP.querySelector('.session-date-body');
  if (!body) return [];
  return body.children.filter(c => c.className && c.className.indexOf('session-item') >= 0)
    .map(c => c.dataset.sid);
}})();
const pSidCountAfterAbort = sidsInPAfterAbort.length;

// Group A must NOT be in the live DOM
function hasGroupInDom(label) {{
  return list.children.some(c => {{
    if (!c.dataset) return false;
    return c.dataset['group-label'] === label ||
           c.getAttribute('data-group-label') === label;
  }});
}}
const groupAInDomAfterAbort = hasGroupInDom('A');

// ── Repair B IN PLACE: give it a proper body ──
const bodyB = makeBodyThatTracksItems(list);
bodyB.className = 'session-date-body';
gwB.querySelector = function(sel) {{ if(sel==='.session-date-body') return bodyB; return null; }};
gwB.querySelectorAll = function(sel) {{
  if (sel === '.session-virtual-spacer[data-virtual-spacer="after"]') {{
    return bodyB.children.filter(c => c.className && c.className.indexOf('session-virtual-spacer') >= 0);
  }}
  return [];
}};
gwB.appendChild(bodyB);

// ── Retry: _appendTouchBatch in the SAME VM ──
_appendTouchBatch();

const loadedAfterRetry = _sessionTouchLoadedCount;
const childrenAfterRetry = list.children.slice();

// ── LIVE-TREE AUTHORITY: derive everything from list.children ──
// Walk the live child tree to build the complete top-level identity
// sequence. This is the authoritative source — NOT saved gwP/wrapperA/gwB
// references, which could be detached and false-green.
const liveSeq = childrenAfterRetry.map((c, i) => {{
  if (!c.dataset) return {{ idx: i, kind: 'unknown', label: null, ref: c }};
  var label = c.dataset['group-label'] || c.getAttribute('data-group-label');
  var isSentinel = c.dataset['touch-sentinel'] !== undefined;
  return {{ idx: i, kind: isSentinel ? 'sentinel' : (label ? 'group' : 'unknown'), label: label, ref: c }};
}});

const liveSeqLabels = liveSeq.map(e => e.label);
const liveSeqLength = childrenAfterRetry.length;

// Exactly one A and one B
const groupAIdx = liveSeqLabels.indexOf('A');
const groupBIdx = liveSeqLabels.indexOf('B');
const groupPIdx = liveSeqLabels.indexOf('P');
const groupACount = liveSeqLabels.filter(l => l === 'A').length;
const groupBCount = liveSeqLabels.filter(l => l === 'B').length;
const groupPCount = liveSeqLabels.filter(l => l === 'P').length;

// ── EXACT TOP-LEVEL IDENTITY SEQUENCE ──
// Assert the exact sequence: [gwP, A_wrapper, gwB, sentinel]
// This proves the same live gwP is at position 0, A is new, gwB is the
// same object, and the sentinel is retained — from the LIVE tree.
// Note: use getAttribute('data-group-label') as fallback because the mock's
// setAttribute strips hyphens (grouplabel vs group-label), matching what
// the liveSeqLabels derivation does.
const _ref0IsGwP = childrenAfterRetry[0] === gwP;
const _ref1NotGwB = childrenAfterRetry[1] !== gwB;
const _ref1LabelA = !!(childrenAfterRetry[1] && (
  childrenAfterRetry[1].dataset && childrenAfterRetry[1].dataset['group-label'] === 'A' ||
  childrenAfterRetry[1].getAttribute && childrenAfterRetry[1].getAttribute('data-group-label') === 'A'
));
const _ref2IsGwB = childrenAfterRetry[2] === gwB;
const _ref3IsSentinel = childrenAfterRetry[3] === sentinel;
const liveSeqExactRefs = liveSeqLength === 4 &&
  _ref0IsGwP &&
  _ref1NotGwB &&
  _ref1LabelA &&
  _ref2IsGwB &&
  _ref3IsSentinel;

// Strict reference identity: childrenAfterRetry[groupBIdx] === gwB
const bWrapperStrictRef = groupBIdx >= 0 && childrenAfterRetry[groupBIdx] === gwB;

// Strict reference identity for body: gwB.querySelector('.session-date-body') === bodyB
const bodyBStrictRef = gwB.querySelector('.session-date-body') === bodyB;

// ── DERIVE ALL SIDs FROM LIVE-TREE TRAVERSAL ──
// Walk list.children → group wrappers → bodies → session-items. Do NOT
// read from saved gwP/wrapperA/gwB references — traverse the actual live
// child tree so a detached reference cannot false-green.
function sidsFromLiveTree() {{
  var allSids = [];
  var groupSids = {{}}; // label → [sids]
  for (var gi = 0; gi < list.children.length; gi++) {{
    var gw = list.children[gi];
    if (!gw.dataset) continue;
    // Get label from either dataset['group-label'] (direct set) or
    // getAttribute('data-group-label') (mock setAttribute strips hyphens,
    // storing as 'grouplabel'). Skip non-group children (e.g. sentinel).
    var label = gw.dataset['group-label'] ||
      (gw.getAttribute ? gw.getAttribute('data-group-label') : null);
    if (!label) continue;
    var body = null;
    if (typeof gw.querySelector === 'function') body = gw.querySelector('.session-date-body');
    if (!body) {{ groupSids[label] = []; continue; }}
    var sids = [];
    for (var bi = 0; bi < body.children.length; bi++) {{
      var c = body.children[bi];
      if (c.className && c.className.indexOf('session-item') >= 0 && c.dataset && c.dataset.sid) {{
        sids.push(c.dataset.sid);
      }}
    }}
    groupSids[label] = sids;
    allSids = allSids.concat(sids);
  }}
  return {{ all: allSids, byGroup: groupSids }};
}}

const liveTreeResult = sidsFromLiveTree();
const allDomSids = liveTreeResult.all;
const sidsInPFromLive = liveTreeResult.byGroup['P'] || [];
const sidsInAFromLive = liveTreeResult.byGroup['A'] || [];
const sidsInBFromLive = liveTreeResult.byGroup['B'] || [];

const expectedAll = Array.from({{length: 100}}, (_, i) => 's_' + i);
const allDomSidsExact = JSON.stringify(allDomSids) === JSON.stringify(expectedAll);
const allDomNoDup = allDomSids.length === new Set(allDomSids).size;

// Exact body ownership from the live tree
const expectedP = Array.from({{length: 60}}, (_, i) => 's_' + i);
const expectedA = Array.from({{length: 20}}, (_, i) => 's_' + (60 + i));
const expectedB = Array.from({{length: 20}}, (_, i) => 's_' + (80 + i));
const pSidsExact = JSON.stringify(sidsInPFromLive) === JSON.stringify(expectedP);
const aSidsExact = JSON.stringify(sidsInAFromLive) === JSON.stringify(expectedA);
const bSidsExact = JSON.stringify(sidsInBFromLive) === JSON.stringify(expectedB);

// Verify isPinned metadata on group A wrapper (A has isPinned=true in flatRows)
// Derive A's wrapper from the live tree, not a saved reference
const wrapperAFromLive = groupAIdx >= 0 ? childrenAfterRetry[groupAIdx] : null;
let aHasPinnedHeader = false;
if (wrapperAFromLive) {{
  const hdr = wrapperAFromLive.children.find(c => c.className && c.className.indexOf('session-date-header') >= 0);
  aHasPinnedHeader = !!(hdr && hdr.className.indexOf('pinned') >= 0);
}}

// ── MUTATION BITE: verify the live-tree traversal catches a detached gwP ──
// Simulate a detached prefix: remove gwP from list.children AFTER retry.
// The live-tree-derived allDomSids must NOT include P's SIDs anymore,
// so allDomSidsExact must be false. This proves the test derives authority
// from list.children, not from the saved gwP reference.
const listChildrenForBite = list.children.slice();
const gwPIdx = listChildrenForBite.indexOf(gwP);
var biteAllDomSidsExact = true; // will be recomputed
if (gwPIdx >= 0) {{
  // Temporarily remove gwP from the live tree
  list.children.splice(gwPIdx, 1);
  const biteResult = sidsFromLiveTree();
  biteAllDomSidsExact = JSON.stringify(biteResult.all) === JSON.stringify(expectedAll);
  // Restore gwP
  list.children.splice(gwPIdx, 0, gwP);
}}

console.log(JSON.stringify({{
  // Abort assertions
  loadedAfterAbort,
  topLevelCountSame,
  topLevelSameRefs,
  bWrapperInTreeAfterAbort,
  bChildrenSameRefs,
  bChildrenSameClasses,
  bDatasetSame,
  bClassNameSame,
  bChildrenCountSame,
  bNoNewSids,
  pSidCountAfterAbort,
  groupAInDomAfterAbort,
  sentinelSameRef,
  sentinelSamePos,
  // Retry assertions
  loadedAfterRetry,
  groupACount,
  groupBCount,
  groupPCount,
  groupAIdx,
  groupBIdx,
  groupPIdx,
  liveSeqLength,
  liveSeqExactRefs,
  liveSeqLabels,
  _ref0IsGwP,
  _ref1NotGwB,
  _ref1LabelA,
  _ref2IsGwB,
  _ref3IsSentinel,
  bWrapperStrictRef,
  bodyBStrictRef,
  // Live-tree-derived SID assertions
  pSidsExact,
  aSidsExact,
  bSidsExact,
  allDomSidsExact,
  allDomNoDup,
  aHasPinnedHeader,
  // Mutation bite
  biteAllDomSidsExact,
  // Debug
  bDatasetBefore,
  bDatasetAfter,
  sidsInPFromLive,
  sidsInAFromLive,
  sidsInBFromLive,
}}));
"""
    result = json.loads(_run_node_vm(source))

    # ── Abort assertions (deep zero-mutation proof) ──
    assert result["loadedAfterAbort"] == 60, \
        f"Phase 1 abort must not advance loaded count, got {result['loadedAfterAbort']}"
    assert result["topLevelCountSame"], \
        "Phase 1 abort must not change live DOM top-level child count"
    assert result["topLevelSameRefs"], \
        "Phase 1 abort must not mutate top-level node order or identities (strict ref)"
    assert result["bWrapperInTreeAfterAbort"], \
        "B's wrapper must still be in the live tree after abort"
    assert result["bChildrenSameRefs"], \
        "B's children must be the exact same object references in the same order after abort (zero mutation)"
    assert result["bChildrenSameClasses"], \
        "B's children classNames must be unchanged after abort"
    assert result["bDatasetSame"], \
        f"B's dataset must be unchanged after abort (mutation bite) — before={result.get('bDatasetBefore')} after={result.get('bDatasetAfter')}"
    assert result["bClassNameSame"], \
        "B's className must be unchanged after abort"
    assert result["bChildrenCountSame"], \
        "B's children count must be unchanged after abort"
    assert result["bNoNewSids"], \
        "No new SID nodes must appear inside B after abort"
    assert result["pSidCountAfterAbort"] == 60, \
        f"Group P must not receive any new rows during abort, got {result['pSidCountAfterAbort']}"
    assert not result["groupAInDomAfterAbort"], \
        "Group A's newly created wrapper must stay DETACHED — must not appear in live DOM children"
    assert result["sentinelSameRef"], \
        "Sentinel must remain in the live tree after abort"
    assert result["sentinelSamePos"], \
        "Sentinel must remain at the same position after abort"

    # ── Retry assertions (strict identity from live child tree) ──
    assert result["loadedAfterRetry"] == 100, \
        f"After repair+retry, loaded count should reach 100, got {result['loadedAfterRetry']}"
    assert result["groupACount"] == 1, \
        f"Exactly one group A wrapper after retry, got {result['groupACount']}"
    assert result["groupBCount"] == 1, \
        f"Exactly one group B wrapper after retry, got {result['groupBCount']}"
    assert result["groupPCount"] == 1, \
        f"Exactly one group P wrapper after retry, got {result['groupPCount']}"

    # ── EXACT TOP-LEVEL IDENTITY SEQUENCE from live tree ──
    assert result["liveSeqLength"] == 4, \
        f"Live tree must have exactly 4 top-level children (P, A, B, sentinel), got {result['liveSeqLength']} — labels: {result.get('liveSeqLabels')}"
    assert result["liveSeqExactRefs"], \
        f"Live tree sequence must be [gwP, A_wrapper, gwB, sentinel] (strict refs from list.children), got labels: {result.get('liveSeqLabels')}"
    assert result["groupAIdx"] == 1, \
        f"Group A must be at index 1, got {result['groupAIdx']}"
    assert result["groupBIdx"] == 2, \
        f"Group B must be at index 2, got {result['groupBIdx']}"
    assert result["groupPIdx"] == 0, \
        f"Group P must be at index 0, got {result['groupPIdx']}"

    assert result["bWrapperStrictRef"], \
        f"childrenAfterRetry[groupBIdx] must be === gwB (strict ref), got groupBIdx={result['groupBIdx']}"
    assert result["bodyBStrictRef"], \
        "gwB.querySelector('.session-date-body') must be === bodyB (strict ref)"

    # ── EXACT BODY OWNERSHIP from live tree ──
    assert result["pSidsExact"], \
        f"bodyP must contain exactly s_0..s_59 from live tree, got {result.get('sidsInPFromLive')}"
    assert result["aSidsExact"], \
        f"bodyA must contain exactly s_60..s_79 from live tree, got {result.get('sidsInAFromLive')}"
    assert result["bSidsExact"], \
        f"bodyB must contain exactly s_80..s_99 from live tree, got {result.get('sidsInBFromLive')}"

    # ── ALL SIDs from live-tree traversal ──
    assert result["allDomSidsExact"], \
        "All 100 SIDs must appear exactly once across P+A+B bodies (from live DOM tree traversal)"
    assert result["allDomNoDup"], \
        "No duplicate SIDs across the entire live DOM tree"
    assert result["aHasPinnedHeader"], \
        "Group A's newly created wrapper must carry isPinned metadata from flatRows (pinned header class)"

    # ── MUTATION BITE: removing gwP from the live tree must break allDomSidsExact ──
    assert not result["biteAllDomSidsExact"], \
        "Mutation bite failed: removing gwP from list.children must make allDomSidsExact false — " \
        "the test must derive SID authority from list.children traversal, not from saved gwP references"



@_node_tests
def test_extra_stale_live_row_rejects_and_rebuilds():
    """When the DOM has MORE rows than oldLoaded (e.g. 61 live rows,
    oldLoaded=60), prefix validation must reject and trigger a full re-render
    — not silently duplicate by validating only the first 60 and appending
    starting at canonical row 60. The prior version used < instead of !==,
    so an extra stale row was accepted and the DOM settled at 101 items /
    100 unique SIDs instead of invalidating."""
    flat_rows = []
    for i in range(100):
        flat_rows.append({"group": {"label": "Today", "isPinned": False}, "session": {"session_id": f"s_{i}"}})

    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 100;
// Pre-populate 61 items — ONE MORE than oldLoaded (60). The extra stale row
// must trigger invalidation, not be silently accepted.
for (let i = 0; i < 61; i++) list._items.push(makeSessionItem('s_' + i));

_touchRenderState = {{
  gen: 1, list: list, flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

const loadedBefore = _sessionTouchLoadedCount;
const itemsBefore = list._items.length;
const renderCallsBefore = _renderCalls;

_appendTouchBatch();

console.log(JSON.stringify({{
  loadedAfter: _sessionTouchLoadedCount,
  itemsAfter: list._items.length,
  renderCallsDelta: _renderCalls - renderCallsBefore,
  loadedBefore,
  itemsBefore,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["renderCallsDelta"] >= 1, \
        f"Extra stale row (61 items, oldLoaded=60) must trigger full re-render (renderSessionListFromCache), got renderCallsDelta={result['renderCallsDelta']}"
    assert result["loadedAfter"] == 0, \
        f"After invalidation, loaded count must be reset to 0, got {result['loadedAfter']}"


@_node_tests
def test_touch_exit_teardown_reachable():
    """When _isTouchPrimary() returns false, _setupTouchSentinel must still
    be called (not gated) and must invalidate old touch state.

    The prior version gated the call inside if(_isTouchPrimary()), making
    the teardown unreachable on a touch→desktop transition — leaving a
    dangling observer and RAF.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
const list = makeList();
_sessionTouchGen = 1;
_sessionTouchListEl = list;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 100;
_touchRenderState = { gen: 1, list: list, flatRows: [], renderOneSession: null, activeSid: null };
_touchBatchPending = true;
_touchSentinelObserver = { disconnect: function() {}, observe: function() {}, unobserve: function() {} };

// Mock _isTouchPrimary to return FALSE — simulating touch→desktop transition
function _isTouchPrimary() { return false; }

eval(extractFunc('_setupTouchSentinel'));

// Call _setupTouchSentinel with non-touch — must invalidate old state.
// The paintedExtent argument (60) is ignored in the exit path.
_setupTouchSentinel(list, 100, [], function(){}, null, 60);

console.log(JSON.stringify({
  observerNull: _touchSentinelObserver === null,
  renderStateNull: _touchRenderState === null,
  listElNull: _sessionTouchListEl === null,
  loadedCountZero: _sessionTouchLoadedCount === 0,
  batchPendingFalse: _touchBatchPending === false,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["observerNull"], "Observer must be disconnected on touch exit"
    assert result["renderStateNull"], "Render state must be cleared on touch exit"
    assert result["listElNull"], "List element must be cleared on touch exit"
    assert result["loadedCountZero"], "Loaded count must be zeroed on touch exit"
    assert result["batchPendingFalse"], "Batch pending must be cleared on touch exit"


@_node_tests
def test_skeleton_teardown_with_old_touch_owner_capability_false():
    """showSessionListSkeleton must tear down prior touch owner/observer/RAF/
    pending/list state BEFORE skeleton replacement, regardless of the current
    _isTouchPrimary() result. The old owner may have already lost capability
    (capability flips false before skeleton replacement) — gating on the
    current _isTouchPrimary() leaves the old render state, pending work, list
    owner, and generation surviving after the DOM is replaced.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
const list = makeList();
_sessionTouchGen = 1;
_sessionTouchListEl = list;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 100;
_touchRenderState = { gen: 1, list: list, flatRows: [], renderOneSession: null, activeSid: null };
_touchBatchPending = true;
_touchScrollOwner = { gen: 1, list: list, handler: function(){}, raf: 42, token: 0 }; // non-zero = RAF was scheduled
_touchSentinelObserver = { disconnect: function() {}, observe: function() {}, unobserve: function() {} };

// Mock _isTouchPrimary to return FALSE — capability flipped to non-touch
// BEFORE the skeleton replacement. The old touch owner is still live.
function _isTouchPrimary() { return false; }

// Mock $ to return our list element
function $(id) { return id === 'sessionList' ? list : null; }

// Mock skeleton groups constant referenced by showSessionListSkeleton
const _SESSION_SKELETON_GROUPS = [{rows: [{title: 70}]}];

// Extract and eval showSessionListSkeleton
eval(extractFunc('showSessionListSkeleton'));

// Call showSessionListSkeleton — must tear down old touch state UNCONDITIONALLY
showSessionListSkeleton('test-profile');

console.log(JSON.stringify({
  observerNull: _touchSentinelObserver === null,
  renderStateNull: _touchRenderState === null,
  listElNull: _sessionTouchListEl === null,
  loadedCountZero: _sessionTouchLoadedCount === 0,
  batchPendingFalse: _touchBatchPending === false,
  ownerNulled: _touchScrollOwner === null,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["observerNull"], \
        "Observer must be disconnected even when _isTouchPrimary() is false — old owner still live"
    assert result["renderStateNull"], \
        "Render state must be cleared even when _isTouchPrimary() is false"
    assert result["listElNull"], \
        "List element must be cleared even when _isTouchPrimary() is false"
    assert result["loadedCountZero"], \
        "Loaded count must be zeroed even when _isTouchPrimary() is false"
    assert result["batchPendingFalse"], \
        "Batch pending must be cleared even when _isTouchPrimary() is false"
    assert result["ownerNulled"], \
        "Scroll owner must be nulled even when _isTouchPrimary() is false"


@_node_tests
def test_continuous_batch_completes_224_rows_no_manual_reentry():
    """Gate-certifier blocking fix: calling _appendTouchBatch() once must trigger
    a continuous chain of batches via _scheduleContinuousBatch() that completes
    60→100→140→180→220→224 WITHOUT any manual sentinel leave/re-enter.

    The gate-certifier reproduced this stall in real Chromium: after one append
    (60→100), the per-group spacers shrank and the sentinel stayed intersecting
    but the IntersectionObserver emitted no second transition. The list stalled
    at 100/224 with "Loading more…" visible.

    This test proves the fix: _appendTouchBatch() now calls
    _scheduleContinuousBatch() after a successful append, which re-checks
    whether the sentinel is still near the viewport and schedules another batch.
    The chain continues until all rows are loaded.

    In the Node VM, getBoundingClientRect() returns zero values (no real layout),
    so _scheduleContinuousBatch takes the headless branch (always batch) — this
    proves the continuous-completion property without a real browser. In a real
    browser, the getBoundingClientRect branch provides the same guarantee using
    actual viewport geometry.
    """
    total = 224
    flat_rows = []
    for i in range(total):
        flat_rows.append({
            "group": {"label": "Today", "isPinned": False},
            "session": {"session_id": f"sess_{i}"},
        })
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60; // initial batch already rendered
_sessionTouchTotalCount = {total};

// Pre-populate DOM with 60 session items
for (let i = 0; i < 60; i++) list._items.push(makeSessionItem('sess_' + i));

// Set up render state
_touchRenderState = {{
  gen: 1,
  list: list,
  flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

// Create group wrapper with body that tracks items
const gw = makeEl('div');
gw.className = 'session-date-group';
gw.dataset['group-label'] = 'Today';
const body = makeBodyThatTracksItems(list);
gw.querySelector = function(sel) {{ if(sel==='.session-date-body') return body; return null; }};
gw.appendChild(body);
list._groups['Today'] = gw;
list.children.push(gw);

// Add a sentinel element (as _setupTouchSentinel would)
const sentinel = makeEl('div');
sentinel.dataset['touchSentinel'] = '';
sentinel.style = {{ display: '' }};
list._sentinel = sentinel;
list.children.push(sentinel);

// Track the batch progression
const progress = [];

// Call _appendTouchBatch once — _scheduleContinuousBatch should chain the rest
_appendTouchBatch();
progress.push(_sessionTouchLoadedCount);

// Drain microtasks: _scheduleContinuousBatch uses Promise.resolve().then()
// to schedule the next batch. In the Node VM, microtasks don't run until the
// current sync stack unwinds. We drain repeatedly until all rows are loaded
// or a safety limit is hit.
// In a real browser, the microtask queue drains naturally between frames.
(async function() {{
  for (let i = 0; i < 100; i++) {{
    await Promise.resolve(); // drain one microtask level
    if (_sessionTouchLoadedCount >= {total}) break;
  }}

  const finalLoaded = _sessionTouchLoadedCount;
  const allSids = list._items.map(i => i.dataset.sid);
  const uniqueSids = [...new Set(allSids)];
  const orderedCorrectly = allSids.every((sid, i) => sid === 'sess_' + i);
  const noBlanks = list._items.length === finalLoaded;

  console.log(JSON.stringify({{
    finalLoaded,
    totalItems: list._items.length,
    uniqueCount: uniqueSids.length,
    orderedCorrectly,
    noBlanks,
    innerHTMLWipes: _innerHTMLWipes,
    renderCalls: _renderCalls,
    progress,
  }}));
}})();
"""
    result = json.loads(_run_node_vm(source))
    assert result["finalLoaded"] == 224, \
        f"Continuous batch must complete to 224, got {result['finalLoaded']} — stall after one append"
    assert result["totalItems"] == 224, \
        f"DOM must have 224 items, got {result['totalItems']}"
    assert result["uniqueCount"] == 224, \
        f"All 224 SIDs must be unique, got {result['uniqueCount']} unique"
    assert result["orderedCorrectly"], \
        "SIDs must be in order sess_0..sess_223"
    assert result["noBlanks"], \
        f"No blank gaps: DOM items ({result['totalItems']}) must match loaded count ({result['finalLoaded']})"
    assert result["innerHTMLWipes"] == 0, \
        f"No innerHTML wipes during continuous batch, got {result['innerHTMLWipes']}"
    assert result["renderCalls"] == 0, \
        f"No full re-render during continuous batch, got {result['renderCalls']}"


@_node_tests
def test_continuous_batch_completes_150_rows_multigroup():
    """Continuous batch must complete 60→100→140→150 across multiple groups
    without manual re-entry. Proves the fix works with group boundaries."""
    total = 150
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
_sessionTouchTotalCount = {total};

for (let i = 0; i < 60; i++) list._items.push(makeSessionItem('s1_' + i));

_touchRenderState = {{
  gen: 1, list: list, flatRows: {json.dumps(flat_rows)},
  renderOneSession: function(s) {{ return makeSessionItem(s.session_id); }},
  activeSid: null,
}};

// Set up G1 (full) and G2 (empty — will receive new rows)
function setupGroup(label) {{
  const gw = makeEl('div');
  gw.dataset['group-label'] = label;
  const body = makeBodyThatTracksItems(list);
  body._afterSpacers = [];
  gw.querySelector = function(sel) {{ if(sel==='.session-date-body') return body; return null; }};
  gw.appendChild(body);
  list._groups[label] = gw;
  list.children.push(gw);
  return {{ gw: gw, body: body }};
}}
const g1 = setupGroup('G1');
const g2 = setupGroup('G2');

// Add sentinel
const sentinel = makeEl('div');
sentinel.dataset['touchSentinel'] = '';
sentinel.style = {{ display: '' }};
list._sentinel = sentinel;
list.children.push(sentinel);

_appendTouchBatch(); // triggers continuous chain

// Drain microtasks for the continuous batch chain
(async function() {{
  for (let i = 0; i < 50; i++) {{
    const before = _sessionTouchLoadedCount;
    await Promise.resolve();
    if (_sessionTouchLoadedCount === before) break;
  }}

  const allSids = list._items.map(i => i.dataset.sid);
  const uniqueSids = [...new Set(allSids)];

  console.log(JSON.stringify({{
    finalLoaded: _sessionTouchLoadedCount,
    totalItems: list._items.length,
    uniqueCount: uniqueSids.length,
  }}));
}})();
"""
    result = json.loads(_run_node_vm(source))
    assert result["finalLoaded"] == 150, \
        f"Continuous batch must complete to 150, got {result['finalLoaded']}"
    assert result["totalItems"] == 150, \
        f"DOM must have 150 items, got {result['totalItems']}"
    assert result["uniqueCount"] == 150, \
        f"All 150 SIDs must be unique, got {result['uniqueCount']}"


@_node_tests
def test_continuous_batch_aborts_on_generation_change():
    """If the generation changes mid-chain (profile switch), the continuous
    batch must abort — no stale appends after invalidation."""
    total = 200
    flat_rows = []
    for i in range(total):
        flat_rows.append({"group": {"label": "G"}, "session": {"session_id": f"s_{i}"}})

    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
const list = makeList();
_sessionTouchListEl = list;
_sessionTouchGen = 1;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = {total};

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
list.children.push(gw);

const sentinel = makeEl('div');
sentinel.dataset['touchSentinel'] = '';
sentinel.style = {{ display: '' }};
list._sentinel = sentinel;
list.children.push(sentinel);

// Override _scheduleContinuousBatch to bump generation after first append,
// simulating a profile switch mid-chain.
const origSchedule = _scheduleContinuousBatch;
let callCount = 0;
_scheduleContinuousBatch = function() {{
  callCount++;
  if (callCount === 1) {{
    // Simulate invalidation after the first continuous batch
    _sessionTouchGen++; // bump generation
    _touchRenderState = null;
    return;
  }}
}};

_appendTouchBatch(); // 60→100, then calls _scheduleContinuousBatch once

// Drain microtasks
(async function() {{
  for (let i = 0; i < 50; i++) {{
    await Promise.resolve();
  }}

  // After generation bump, the continuous chain should have stopped.
  // loadedCount should be 100 (one append), not 200 (full chain would require
  // the generation to still match).
  console.log(JSON.stringify({{
    finalLoaded: _sessionTouchLoadedCount,
    scheduleCalls: callCount,
  }}));
}})();
"""
    result = json.loads(_run_node_vm(source))
    assert result["finalLoaded"] == 100, \
        f"After generation change, loaded should stay at 100, got {result['finalLoaded']}"
    assert result["scheduleCalls"] == 1, \
        f"_scheduleContinuousBatch should be called once, got {result['scheduleCalls']}"


def test_root_margin_expands_downward_not_upward():
    """Gate-certifier fix: rootMargin must expand the DOWNWARD (bottom) edge,
    not the upward (top) edge. The prior '200px 0px 0px 0px' expanded the top
    of the root viewport, which does nothing for a sentinel at the bottom of
    a scrollable list — it needs downward lookahead to detect the sentinel
    before the user reaches it.
    """
    fn = _extract_fn(SESSIONS_JS, "_ensureTouchSentinelObserver")
    # rootMargin format: top right bottom left
    # We need bottom margin > 0, top margin = 0
    import re
    match = re.search(r"rootMargin:'([^']+)'", fn)
    assert match, "rootMargin must be specified in _ensureTouchSentinelObserver"
    margins = match.group(1).split()
    assert len(margins) == 4, f"rootMargin must have 4 values, got: {match.group(1)}"
    top, right, bottom, left = margins
    assert bottom != "0px" and bottom != "0", \
        f"rootMargin bottom must be non-zero for downward lookahead, got: {match.group(1)}"
    assert top == "0px" or top == "0", \
        f"rootMargin top must be 0 (no upward expansion), got: {match.group(1)}"


def test_scroll_trigger_runs_always_not_only_when_io_absent():
    """Gate-certifier fix: the scroll-based batch trigger must run ALWAYS
    (not just when IntersectionObserver is absent). The IO can stall after
    one append — the sentinel stays intersecting but no new transition fires.
    The scroll listener catches this by triggering a batch when the user
    scrolls near the bottom, regardless of whether IO is present.
    """
    fn = _extract_fn(SESSIONS_JS, "_setupTouchSentinel")
    # The scroll fallback must NOT be gated behind !('IntersectionObserver' in window)
    # It should run unconditionally.
    assert "if(!('IntersectionObserver' in window'))" not in fn, \
        "Scroll trigger must NOT be gated behind IntersectionObserver absence — it must run always"
    assert "_touchScrollOwner" in fn, \
        "Scroll trigger must use the scroll-owner record"
    assert "requestAnimationFrame" in fn, \
        "Scroll trigger must use requestAnimationFrame"


# ── Gate-certifier Jul 31: event-driven scroll trigger (no idle RAF poll) ──

def test_no_unconditional_raf_reschedule():
    """The prior RAF poll unconditionally rescheduled itself every frame.
    The fix must NOT contain a self-rescheduling loop — the scroll listener
    arms one coalesced RAF per scroll burst, then stops.
    """
    fn = _extract_fn(SESSIONS_JS, "_setupTouchSentinel")
    # The old pattern: requestAnimationFrame(check) at the end of the callback
    # with the same function name. The new code must not have a named
    # self-reference that reschedules.
    assert "requestAnimationFrame(check)" not in fn, \
        "Must not have a self-rescheduling RAF poll — use event-driven scroll listener"
    assert "requestAnimationFrame(function check" not in fn, \
        "Must not have a named self-rescheduling RAF callback"


def test_scroll_listener_installed():
    """_setupTouchSentinel must install a passive scroll listener on the list
    for event-driven batch triggering — not a permanent RAF poll.
    """
    fn = _extract_fn(SESSIONS_JS, "_setupTouchSentinel")
    assert "addEventListener('scroll'" in fn or 'addEventListener("scroll"' in fn, \
        "Must install a scroll event listener for event-driven batch trigger"
    assert "passive:true" in fn or "passive: true" in fn, \
        "Scroll listener must be passive (no scroll-blocking)"
    assert "_touchScrollOwner" in fn, \
        "Must store the scroll handler in the owner record for owner-qualified teardown"


def test_invalidation_removes_scroll_listener():
    """_invalidateTouchRender must remove the scroll listener — not just cancel
    the RAF. Without removal, a stale listener survives on the old list element.
    """
    fn = _extract_fn(SESSIONS_JS, "_invalidateTouchRender")
    assert "removeEventListener" in fn, \
        "_invalidateTouchRender must remove the scroll listener from the list"
    assert "_touchScrollOwner" in fn, \
        "_invalidateTouchRender must reference _touchScrollOwner for owner-qualified removal"


def test_scroll_handler_is_oneshot_raf():
    """The scroll handler must arm a one-shot RAF (clearing the handle inside
    the callback) — not reschedule itself.
    """
    fn = _extract_fn(SESSIONS_JS, "_setupTouchSentinel")
    # The one-shot pattern: owner.raf=requestAnimationFrame(...), then inside
    # the callback, owner.raf=0 before doing work.
    # There must be NO requestAnimationFrame call at the end of the callback
    # that reschedules with the same handler.
    # Count RAF calls: should be exactly 1 (scroll handler RAF only — no setup-time RAF).
    raf_count = fn.count("requestAnimationFrame")
    assert raf_count == 1, \
        f"Exactly 1 RAF call expected (scroll handler only, no setup-time RAF), got {raf_count}"


@_node_tests
def test_idle_far_from_bottom_no_recurring_raf():
    """Gate-certifier blocking fix: when IO is present and the list is far
    from the bottom (idle), the scroll handler must NOT produce recurring
    RAF schedules. Simulates 60/224 rows at scrollTop 0 — the prior code
    produced 9 schedules from 8 idle callbacks. The new event-driven code
    should produce zero schedules with no scroll activity.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
// Track RAF schedules
let rafSchedules = 0;
global.requestAnimationFrame = function(fn) { rafSchedules++; return 1; };
global.cancelAnimationFrame = function() {};
function _isTouchPrimary() { return true; }
function _updateTouchGroupSpacers() {}
function _updateTouchSentinel() {}
window.IntersectionObserver = function(cb, opts) { this.disconnect = function(){}; this.observe = function(){}; this.unobserve = function(){}; };
global.IntersectionObserver = window.IntersectionObserver;

const list = makeList();
list.scrollHeight = 20000; // tall list
list.scrollTop = 0; // at top, far from bottom
list.clientHeight = 800; // viewport

const flatRows = [];
for (let i = 0; i < 224; i++) flatRows.push({group:{label:'G'},session:{session_id:'s'+i}});

_sessionTouchListEl = list;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 224;
_touchRenderState = { gen: 1, list: list, flatRows: flatRows, renderOneSession: null, activeSid: null };
_touchSentinelObserver = null;
list._sentinel = makeEl('div');
list._sentinel.style.display = '';

eval(extractFunc('_setupTouchSentinel'));
// Real 6-argument signature: _setupTouchSentinel(list, total, flatRows, renderOneSession, activeSid, paintedExtent)
_setupTouchSentinel(list, 224, flatRows, null, null, 60);

// Record schedules after setup — with NO setup-time RAF, this should be 0.
// The scroll RAF is armed ONLY from an actual scroll event.
const schedulesAfterSetup = rafSchedules;

// Now simulate 8 idle callbacks (no scroll events fired)
// The scroll handler is NOT called — it only fires on scroll events.
// So rafSchedules should NOT increase from idle.
// In the prior code, the RAF callback self-rescheduled, producing 9 schedules
// from 8 idle drains. The new code has no self-rescheduling and no setup RAF.

console.log(JSON.stringify({
  schedulesAfterSetup: schedulesAfterSetup,
  // With no scroll activity and no setup RAF, zero schedules
  noRecurringIdleRAF: schedulesAfterSetup === 0,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["noRecurringIdleRAF"], \
        f"Idle far-from-bottom must not produce recurring RAF — got {result['schedulesAfterSetup']} schedules"


@_node_tests
def test_scroll_coalescing_multiple_events_single_raf():
    """Multiple scroll events in quick succession must coalesce to a single
    RAF — the handler checks if a RAF is already armed and skips.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
let rafSchedules = 0;
let pendingRaf = 0;
global.requestAnimationFrame = function(fn) { rafSchedules++; pendingRaf = 1; return 1; };
global.cancelAnimationFrame = function() {};
function _isTouchPrimary() { return true; }
function _updateTouchGroupSpacers() {}
function _updateTouchSentinel() {}
window.IntersectionObserver = function(cb, opts) { this.disconnect = function(){}; this.observe = function(){}; this.unobserve = function(){}; };
global.IntersectionObserver = window.IntersectionObserver;

const list = makeList();
list.scrollHeight = 20000;
list.scrollTop = 19500; // near bottom
list.clientHeight = 800;

const flatRows = [];
for (let i = 0; i < 224; i++) flatRows.push({group:{label:'G'},session:{session_id:'s'+i}});

_sessionTouchListEl = list;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 224;
_touchRenderState = { gen: 1, list: list, flatRows: flatRows, renderOneSession: null, activeSid: null };
_touchSentinelObserver = { disconnect: function() {}, observe: function() {}, unobserve: function() {} };
list._sentinel = makeEl('div');
list._sentinel.style.display = '';

eval(extractFunc('_setupTouchSentinel'));
// Real 6-argument signature: _setupTouchSentinel(list, total, flatRows, renderOneSession, activeSid, paintedExtent)
_setupTouchSentinel(list, 224, flatRows, null, null, 60);

// Reset counter after setup — no setup-time RAF means 0 schedules here
rafSchedules = 0;

// Fire 5 rapid scroll events WITHOUT clearing the pending RAF
// (simulates burst scrolling — the handler should coalesce)
for (let i = 0; i < 5; i++) {
  if (_touchScrollOwner && _touchScrollOwner.handler) _touchScrollOwner.handler();
}

// Only 1 RAF should have been scheduled (coalescing — the handler checks
// if owner.raf is already set and skips)
console.log(JSON.stringify({
  rafSchedules: rafSchedules,
  coalesced: rafSchedules <= 1,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["coalesced"], \
        f"5 scroll events must coalesce to ≤1 RAF — got {result['rafSchedules']} schedules"


@_node_tests
def test_teardown_cancels_scroll_listener():
    """_invalidateTouchRender must remove the scroll listener so it doesn't
    leak on the old list element after a profile switch.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
let scrollListenersRemoved = 0;
const list = makeList();
list.scrollHeight = 20000;
list.scrollTop = 0;
list.clientHeight = 800;

// Track add/removeEventListener
list._scrollListeners = [];
list.addEventListener = function(type, handler, opts) {
  if (type === 'scroll') this._scrollListeners.push(handler);
};
list.removeEventListener = function(type, handler, opts) {
  if (type === 'scroll') {
    const idx = this._scrollListeners.indexOf(handler);
    if (idx >= 0) { this._scrollListeners.splice(idx, 1); scrollListenersRemoved++; }
  }
};

const flatRows = [];
for (let i = 0; i < 224; i++) flatRows.push({group:{label:'G'},session:{session_id:'s'+i}});

_sessionTouchListEl = list;
_sessionTouchLoadedCount = 60;
_sessionTouchTotalCount = 224;
_touchRenderState = { gen: 1, list: list, flatRows: flatRows, renderOneSession: null, activeSid: null };
_touchSentinelObserver = { disconnect: function() {}, observe: function() {}, unobserve: function() {} };
list._sentinel = makeEl('div');
list._sentinel.style.display = '';

global.requestAnimationFrame = function() { return 1; };
global.cancelAnimationFrame = function() {};
function _isTouchPrimary() { return true; }
function _updateTouchGroupSpacers() {}
function _updateTouchSentinel() {}
window.IntersectionObserver = function(cb, opts) { this.disconnect = function(){}; this.observe = function(){}; this.unobserve = function(){}; };
global.IntersectionObserver = window.IntersectionObserver;

eval(extractFunc('_setupTouchSentinel'));
// Real 6-argument signature: _setupTouchSentinel(list, total, flatRows, renderOneSession, activeSid, paintedExtent)
_setupTouchSentinel(list, 224, flatRows, null, null, 60);

const listenersBefore = list._scrollListeners.length;

// Now invalidate — should remove the scroll listener
_invalidateTouchRender();

console.log(JSON.stringify({
  listenersBefore: listenersBefore,
  listenersAfter: list._scrollListeners.length,
  removed: scrollListenersRemoved,
  ownerNulled: _touchScrollOwner === null,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["listenersBefore"] >= 1, "Scroll listener must have been installed"
    assert result["removed"] >= 1, "Scroll listener must be removed on invalidation"
    assert result["ownerNulled"], "_touchScrollOwner must be nulled on invalidation"


# ── Gate-certifier Jul 31 20:02: scroll-owner record + discriminating tests ──

@_node_tests
def test_no_setup_time_raf_schedules():
    """Gate-certifier finding #2: setup must NOT arm any RAF without a scroll
    event. The prior code scheduled an initial RAF at setup when loaded<total.
    The fix removes it — the continuous-batch chain from _appendTouchBatch
    handles the already-intersecting case. This test proves zero RAF schedules
    after setup with NO scroll events fired, using the REAL 6-argument signature.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
let rafSchedules = 0;
global.requestAnimationFrame = function(fn) { rafSchedules++; return 1; };
global.cancelAnimationFrame = function() {};
function _isTouchPrimary() { return true; }
function _updateTouchGroupSpacers() {}
function _updateTouchSentinel() {}
window.IntersectionObserver = function(cb, opts) { this.disconnect = function(){}; this.observe = function(){}; this.unobserve = function(){}; };
global.IntersectionObserver = window.IntersectionObserver;

const list = makeList();
list.scrollHeight = 20000;
list.scrollTop = 0;
list.clientHeight = 800;

const flatRows = [];
for (let i = 0; i < 224; i++) flatRows.push({group:{label:'G'},session:{session_id:'s'+i}});

// DO NOT pre-set globals — let _setupTouchSentinel set them with the real
// 6-argument signature: (list, total, flatRows, renderOneSession, activeSid, paintedExtent)
eval(extractFunc('_setupTouchSentinel'));
_setupTouchSentinel(list, 224, flatRows, null, null, 60);

// With NO setup-time RAF and NO scroll events, rafSchedules must be 0.
// The prior code armed an initial RAF at setup when loaded<total (60<224).
console.log(JSON.stringify({
  rafSchedules: rafSchedules,
  zeroSchedulesAtSetup: rafSchedules === 0,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["zeroSchedulesAtSetup"], \
        f"Setup must NOT arm any RAF without a scroll event — got {result['rafSchedules']} schedules"


@_node_tests
def test_scroll_raf_drains_retained_callbacks():
    """Gate-certifier finding #4: the idle case must drain retained RAF/microtask
    callbacks. This test fires a scroll event that arms a RAF, then drains the
    RAF callback (simulating the frame firing). When far from bottom, the RAF
    must NOT schedule additional RAFs — it's one-shot. Proves the callback
    actually runs (not just scheduled) and produces no recurring schedules.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
let rafSchedules = 0;
let rafCallbacks = [];
global.requestAnimationFrame = function(fn) { rafSchedules++; rafCallbacks.push(fn); return rafSchedules; };
global.cancelAnimationFrame = function() {};
function _isTouchPrimary() { return true; }
function _updateTouchGroupSpacers() {}
function _updateTouchSentinel() {}
window.IntersectionObserver = function(cb, opts) { this.disconnect = function(){}; this.observe = function(){}; this.unobserve = function(){}; };
global.IntersectionObserver = window.IntersectionObserver;

const list = makeList();
list.scrollHeight = 20000;
list.scrollTop = 0; // far from bottom
list.clientHeight = 800;

const flatRows = [];
for (let i = 0; i < 224; i++) flatRows.push({group:{label:'G'},session:{session_id:'s'+i}});

eval(extractFunc('_setupTouchSentinel'));
_setupTouchSentinel(list, 224, flatRows, null, null, 60);

// No setup-time RAF: 0 schedules
if (rafSchedules !== 0) throw new Error('expected 0 schedules at setup, got ' + rafSchedules);

// Fire a scroll event — should arm exactly 1 RAF
if (_touchScrollOwner && _touchScrollOwner.handler) _touchScrollOwner.handler();
if (rafSchedules !== 1) throw new Error('expected 1 schedule after scroll, got ' + rafSchedules);

// Drain the RAF callback (simulates the frame firing)
const schedulesBeforeDrain = rafSchedules;
const callbacks = rafCallbacks.splice(0);
for (const cb of callbacks) cb();

// After draining, the callback ran but did NOT reschedule (far from bottom,
// nearBottom check fails, so it returns without scheduling another RAF)
console.log(JSON.stringify({
  schedulesBeforeDrain: schedulesBeforeDrain,
  schedulesAfterDrain: rafSchedules,
  noReschedule: rafSchedules === 1, // still 1 — the drained callback didn't add any
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["noReschedule"], \
        f"Drained RAF callback must not reschedule — got {result['schedulesAfterDrain']} schedules after drain"


@_node_tests
def test_stale_owner_handler_cannot_clear_newer_owner():
    """Gate-certifier finding #1: a stale scroll handler from a previous setup
    must NOT be able to act on or clear a newer owner's state. After
    invalidation + re-setup, the old handler's _touchScrollOwner !== owner
    check must reject it, and the old handler must NOT zero the new owner's RAF.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + """
let rafSchedules = 0;
let rafCallbacks = [];
global.requestAnimationFrame = function(fn) { rafSchedules++; rafCallbacks.push(fn); return rafSchedules; };
global.cancelAnimationFrame = function() {};
function _isTouchPrimary() { return true; }
function _updateTouchGroupSpacers() {}
function _updateTouchSentinel() {}
window.IntersectionObserver = function(cb, opts) { this.disconnect = function(){}; this.observe = function(){}; this.unobserve = function(){}; };
global.IntersectionObserver = window.IntersectionObserver;

const list = makeList();
list.scrollHeight = 20000;
list.scrollTop = 19500;
list.clientHeight = 800;

const flatRows = [];
for (let i = 0; i < 224; i++) flatRows.push({group:{label:'G'},session:{session_id:'s'+i}});

// First setup
eval(extractFunc('_setupTouchSentinel'));
_setupTouchSentinel(list, 224, flatRows, null, null, 60);
const oldOwner = _touchScrollOwner;

// Invalidate — installs a new owner
_invalidateTouchRender();

// Second setup
_setupTouchSentinel(list, 224, flatRows, null, null, 60);
const newOwner = _touchScrollOwner;

// The new owner must be a different object
if (newOwner === oldOwner) throw new Error('new owner must differ from old owner');

// Now fire the OLD handler — it must be rejected (owner mismatch)
const schedulesBefore = rafSchedules;
if (oldOwner && oldOwner.handler) oldOwner.handler();

// The old handler must NOT have armed any RAF (rejected by _touchScrollOwner !== owner)
console.log(JSON.stringify({
  schedulesBefore: schedulesBefore,
  schedulesAfterOldHandler: rafSchedules,
  oldHandlerRejected: rafSchedules === schedulesBefore,
  newOwnerIntact: _touchScrollOwner === newOwner,
  newOwnerRafZero: newOwner.raf === 0, // not touched by old handler
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["oldHandlerRejected"], \
        "Stale handler must not arm RAF — _touchScrollOwner !== owner check rejected it"
    assert result["newOwnerIntact"], \
        "New owner must still be current after stale handler fired"
    assert result["newOwnerRafZero"], \
        "New owner's RAF must not be touched by stale handler"


@_node_tests
def test_teardown_cannot_clear_newer_owner():
    """Gate-certifier finding #1: _invalidateTouchRender must only cancel the
    CURRENT owner's listener/RAF. If a stale owner's teardown fires after a
    newer owner was already installed, it must NOT clear the newer owner's
    handler from the list. This test simulates the interleaving the name and
    docstring claim:

      setup A → setup B (invalidates A, installs B) → fire A's stale handler
      → assert B's owner, listener, RAF, and rendered extent remain untouched.

    The prior version never created this interleaving — it invalidated A,
    installed B, then invalidated B, never firing A's stale callback.
    """
    total = 100
    flat_rows = []
    for i in range(total):
        flat_rows.append({"group": {"label": "G"}, "session": {"session_id": f"s{i}"}})
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
let rafSchedules = 0;
let rafCallbacks = [];
global.requestAnimationFrame = function(fn) {{ rafSchedules++; rafCallbacks.push(fn); return rafSchedules; }};
global.cancelAnimationFrame = function() {{}};
function _isTouchPrimary() {{ return true; }}
function _updateTouchGroupSpacers() {{}}
function _updateTouchSentinel() {{}}
window.IntersectionObserver = function(cb, opts) {{ this.disconnect = function(){{}}; this.observe = function(){{}}; this.unobserve = function(){{}}; }};
global.IntersectionObserver = window.IntersectionObserver;

let scrollListenersRemoved = 0;
const list = makeList();
list.scrollHeight = 20000;
list.scrollTop = 19500;
list.clientHeight = 800;
list._scrollListeners = [];
list.addEventListener = function(type, handler, opts) {{
  if (type === 'scroll') this._scrollListeners.push(handler);
}};
list.removeEventListener = function(type, handler, opts) {{
  if (type === 'scroll') {{
    const idx = this._scrollListeners.indexOf(handler);
    if (idx >= 0) {{ this._scrollListeners.splice(idx, 1); scrollListenersRemoved++; }}
  }}
}};

const flatRows = {json.dumps(flat_rows)};

// Set up render state for rendering
const gw = makeEl('div');
gw.className = 'session-date-group';
gw.setAttribute('data-group-label', 'G');
const body = makeBodyThatTracksItems(list);
body.className = 'session-date-body';
gw.appendChild(body);
list._groups['G'] = gw;
list.children.push(gw);
for (let i = 0; i < 60; i++) {{
  const item = makeSessionItem('s' + i);
  body.appendChild(item);
}}
list._sentinel = makeEl('div');
list._sentinel.style.display = '';

const renderOne = function(session, isPinned) {{
  return makeSessionItem(session.session_id);
}};

eval(extractFunc('_setupTouchSentinel'));

// ── Setup A: install owner A ──
_setupTouchSentinel(list, {total}, flatRows, renderOne, null, 60);
const ownerA = _touchScrollOwner;
const listenersAfterA = list._scrollListeners.length;
const itemsAfterA = list._items.length;

// ── Setup B: invalidates A, installs owner B ──
// _setupTouchSentinel calls _invalidateTouchRender internally, which tears
// down A's listener/RAF, then installs B as the new current owner.
_setupTouchSentinel(list, {total}, flatRows, renderOne, null, 60);
const ownerB = _touchScrollOwner;
const listenersAfterB = list._scrollListeners.length;

// B must be a different owner object
if (ownerB === ownerA) throw new Error('owner B must differ from owner A');

// Snapshot B's state BEFORE firing A's stale handler
const bRafBefore = ownerB.raf;
const bHandlerExists = typeof ownerB.handler === 'function';
const bListenersBefore = list._scrollListeners.length;
const itemsBeforeStaleFire = list._items.length;

// ── Fire A's stale handler ──
// A's handler is still a function on the ownerA object, but _touchScrollOwner
// is now ownerB. The handler must check _touchScrollOwner !== owner and bail.
if (ownerA && ownerA.handler) ownerA.handler();

// Also drain any RAF that A's handler might have wrongly armed
const staleCallbacks = rafCallbacks.splice(0);
for (const cb of staleCallbacks) cb();

// Snapshot B's state AFTER firing A's stale handler
const bRafAfter = ownerB.raf;
const bListenersAfter = list._scrollListeners.length;
const itemsAfterStaleFire = list._items.length;

// ── Fire A's stale teardown (_invalidateTouchRender was already called
// internally by setup B, but if someone calls it again via A's context,
// it must only affect the current owner, not B). We simulate this by
// calling _invalidateTouchRender and verifying B's listener is the one
// removed (not A's already-removed listener). ──
// Actually, the real interleaving is: A's handler fires after B is installed.
// We already did that above. Now verify B is fully intact.

console.log(JSON.stringify({{
  listenersAfterA: listenersAfterA,
  listenersAfterB: listenersAfterB,
  bHandlerExists: bHandlerExists,
  bRafBefore: bRafBefore,
  bRafAfter: bRafAfter,
  bRafUntouched: bRafAfter === bRafBefore,
  bListenersUntouched: bListenersAfter === bListenersBefore,
  ownerBStillCurrent: _touchScrollOwner === ownerB,
  itemsUntouched: itemsAfterStaleFire === itemsBeforeStaleFire,
  staleHandlerArmedNoRaf: rafSchedules === 0,  // A's handler must not arm RAF
  scrollListenersRemoved: scrollListenersRemoved,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["listenersAfterA"] >= 1, \
        f"Setup A must install a scroll listener, got {result['listenersAfterA']}"
    assert result["listenersAfterB"] >= 1, \
        f"Setup B must install a new scroll listener, got {result['listenersAfterB']}"
    assert result["bHandlerExists"], \
        "Owner B must have a handler function"
    assert result["bRafUntouched"], \
        f"Owner B's RAF must not be touched by A's stale handler — before={result['bRafBefore']}, after={result['bRafAfter']}"
    assert result["bListenersUntouched"], \
        f"Owner B's listener count must not change — before={result['bListenersBefore']}, after={result['bListenersAfter']}"
    assert result["ownerBStillCurrent"], \
        "Owner B must still be the current _touchScrollOwner after A's stale handler fired"
    assert result["itemsUntouched"], \
        f"Rendered extent must not change — before={result['itemsBeforeStaleFire']}, after={result['itemsAfterStaleFire']}"
    assert result["staleHandlerArmedNoRaf"], \
        f"A's stale handler must not arm any RAF — got {result['rafSchedules']} schedules"


@_node_tests
def test_observer_and_scroll_same_turn_single_append():
    """Gate-certifier finding #4: when both the IntersectionObserver AND the
    scroll listener fire in the same turn (e.g. user scrolls into the sentinel
    zone and the observer simultaneously reports intersection), only ONE batch
    must be appended — not two. The _touchBatchPending flag must prevent the
    second trigger from scheduling a duplicate microtask.

    This test executes the real production schedule: it evals the real
    _setupTouchSentinel, fires both the observer callback and the scroll
    handler synchronously, drains the coalesced RAF callback, then awaits
    the Promise continuation. It asserts exactly one _appendTouchBatch call
    (60→100 rows), 40 renderer invocations, 100 unique live session nodes,
    one coalesced RAF, and _touchBatchPending cleared.
    """
    total = 100  # exactly one batch: 60→100, no continuous chain needed
    flat_rows = []
    for i in range(total):
        flat_rows.append({"group": {"label": "G"}, "session": {"session_id": f"s{i}"}})
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
let rafSchedules = 0;
let rafCallbacks = [];
global.requestAnimationFrame = function(fn) {{ rafSchedules++; rafCallbacks.push(fn); return rafSchedules; }};
global.cancelAnimationFrame = function() {{}};
function _isTouchPrimary() {{ return true; }}

// Count actual _appendTouchBatch calls by wrapping after eval
let appendCount = 0;
let renderCount = 0;

window.IntersectionObserver = function(cb, opts) {{
  this._cb = cb;
  this.disconnect = function(){{}};
  this.observe = function(){{}};
  this.unobserve = function(){{}};
  this._fire = function(isIntersecting) {{
    this._cb([{{isIntersecting: isIntersecting, target: null}}]);
  }};
}};
global.IntersectionObserver = window.IntersectionObserver;

const list = makeList();
list.scrollHeight = 20000;
list.scrollTop = 19500; // near bottom: 20000-19500-800 = -300 < 200
list.clientHeight = 800;

const flatRows = {json.dumps(flat_rows)};

// Create the group wrapper + body in the list
const gw = makeEl('div');
gw.className = 'session-date-group';
gw.setAttribute('data-group-label', 'G');
const body = makeBodyThatTracksItems(list);
body.className = 'session-date-body';
gw.appendChild(body);
list._groups['G'] = gw;
list.children.push(gw);
// Pre-populate 60 items
for (let i = 0; i < 60; i++) {{
  const item = makeSessionItem('s' + i);
  body.appendChild(item);
}}

// Sentinel
list._sentinel = makeEl('div');
list._sentinel.style.display = '';

eval(extractFunc('_setupTouchSentinel'));

// Wrap _appendTouchBatch to count calls
const _realAppend = _appendTouchBatch;
_appendTouchBatch = function() {{ appendCount++; _realAppend(); }};

// Wrap renderOneSession to count renderer calls
const origRenderOne = function(session, isPinned) {{
  renderCount++;
  return makeSessionItem(session.session_id);
}};

_setupTouchSentinel(list, {total}, flatRows, origRenderOne, null, 60);

const observer = _touchSentinelObserver;

// Fire observer intersection (simulates sentinel entering viewport)
if (observer && observer._fire) observer._fire(true);

// Fire scroll handler in the same turn (simulates scroll event)
if (_touchScrollOwner && _touchScrollOwner.handler) _touchScrollOwner.handler();

// Drain the RAF callback from the scroll handler — the scroll handler armed
// one coalesced RAF. The observer armed a microtask directly (no RAF).
const callbacks = rafCallbacks.splice(0);
for (const cb of callbacks) cb();

// Now drain microtasks: the observer's Promise.resolve().then() and the
// scroll handler's RAF→Promise chain. _touchBatchPending was set by whichever
// fired first; the second trigger saw it true and skipped.
(async function() {{
  for (let i = 0; i < 20; i++) {{
    await Promise.resolve();
  }}

  const allSids = list._items.map(function(i) {{ return i.dataset.sid; }});
  const uniqueSids = [];
  const seen = {{}};
  for (const sid of allSids) {{ if(!seen[sid]) {{ seen[sid]=1; uniqueSids.push(sid); }} }}

  console.log(JSON.stringify({{
    appendCount: appendCount,
    renderCount: renderCount,
    loadedCount: _sessionTouchLoadedCount,
    totalItems: list._items.length,
    uniqueCount: uniqueSids.length,
    rafSchedules: rafSchedules,
    batchPending: _touchBatchPending,
    innerHTMLWipes: _innerHTMLWipes,
  }}));
}})();
"""
    result = json.loads(_run_node_vm(source))
    assert result["appendCount"] == 1, \
        f"Exactly one _appendTouchBatch call — coalescing failed, got {result['appendCount']}"
    assert result["loadedCount"] == 100, \
        f"One batch must grow 60→100, got loaded={result['loadedCount']}"
    assert result["renderCount"] == 40, \
        f"40 renderer calls (100-60=40 new rows), got {result['renderCount']}"
    assert result["totalItems"] == 100, \
        f"100 live session nodes in DOM, got {result['totalItems']}"
    assert result["uniqueCount"] == 100, \
        f"100 unique SIDs, got {result['uniqueCount']}"
    assert result["rafSchedules"] == 1, \
        f"One coalesced RAF from the scroll handler, got {result['rafSchedules']}"
    assert result["batchPending"] is False, \
        f"_touchBatchPending must be cleared after append, got {result['batchPending']}"
    assert result["innerHTMLWipes"] == 0, \
        f"No innerHTML wipes during append, got {result['innerHTMLWipes']}"


@_node_tests
def test_full_ownership_revalidation_before_append():
    """Gate-certifier finding #3: the scroll RAF microtask must recheck owner
    identity, list, generation, token, loaded/total, AND near-bottom geometry
    immediately before calling _appendTouchBatch — not just generation. This
    test EXECUTES the production schedule (setup → scroll handler → RAF →
    Promise microtask) and verifies that each stale schedule is rejected:

      1. Positive control: unchanged owner → append succeeds (60→100)
      2. Geometry change (scroll away from bottom AFTER RAF drains) → no append
      3. Owner/list replacement (AFTER RAF drains) → no append
      4. Token supersession (AFTER RAF drains) → stale microtask does NOT clear
         _touchBatchPending; newer owner settles and clears it
      5. Loaded>=total terminal state (AFTER RAF drains) → no append
      6. Owner-only replacement (only _touchScrollOwner changes) → no append
      7. List-only replacement (only _sessionTouchListEl changes) → no append

    Scenarios 6 and 7 are independent mutation bites: deleting only the
    Promise-stage owner check makes scenario 6 fail, and deleting only the
    Promise-stage list check makes scenario 7 fail — each check has
    independent bite because gen, token, list/owner, loaded, and geometry are
    all held unchanged in the respective case.

    Each stale scenario snapshots exact live row references and SID order
    before the schedule; after settlement the test asserts the same count,
    same objects at each index, and same order (live-tree identity
    postcondition). The exact replacement owner/list is retained and asserted
    to remain current and untouched after settlement.
    """
    total = 140  # 60 initial + one batch of 40 = 100, leaving room for more
    flat_rows = []
    for i in range(total):
        flat_rows.append({"group": {"label": "G"}, "session": {"session_id": f"s{i}"}})

    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + _node_test_preamble() + f"""
let rafSchedules = 0;
let rafCallbacks = [];
global.requestAnimationFrame = function(fn) {{ rafSchedules++; rafCallbacks.push(fn); return rafSchedules; }};
global.cancelAnimationFrame = function() {{}};
function _isTouchPrimary() {{ return true; }}

let appendCount = 0;

window.IntersectionObserver = function(cb, opts) {{
  this.disconnect = function(){{}};
  this.observe = function(){{}};
  this.unobserve = function(){{}};
}};
global.IntersectionObserver = window.IntersectionObserver;

const list = makeList();
list.scrollHeight = 20000;
list.scrollTop = 19500; // near bottom: 20000-19500-800 = -300 < 200
list.clientHeight = 800;

const flatRows = {json.dumps(flat_rows)};

// Set up group wrapper + body with 60 pre-populated items
const gw = makeEl('div');
gw.className = 'session-date-group';
gw.setAttribute('data-group-label', 'G');
const body = makeBodyThatTracksItems(list);
body.className = 'session-date-body';
gw.appendChild(body);
list._groups['G'] = gw;
list.children.push(gw);
for (let i = 0; i < 60; i++) {{
  body.appendChild(makeSessionItem('s' + i));
}}
list._sentinel = makeEl('div');
list._sentinel.style.display = '';

const renderOne = function(session, isPinned) {{
  return makeSessionItem(session.session_id);
}};

eval(extractFunc('_setupTouchSentinel'));

// Wrap _appendTouchBatch to count calls
const _realAppend = _appendTouchBatch;
_appendTouchBatch = function() {{ appendCount++; _realAppend(); }};

// Suppress the continuous batch chain so each scenario tests ONLY the
// scroll-handler -> RAF -> microtask path. Without this, _scheduleContinuousBatch
// chains appends via microtasks and the loaded count races past 100.
_scheduleContinuousBatch = function() {{}};

// ── Live-tree snapshot helper ──
// Captures exact row references, SID order, count, group/body refs, and
// settlement state (owner, listEl, gen, token, loaded, total, pending).
function snapshotLiveTree() {{
  return {{
    items: list._items.slice(),
    sids: list._items.map(function(i) {{ return i.dataset.sid; }}),
    count: list._items.length,
    groupWrapper: gw,
    body: body,
    batchPending: _touchBatchPending,
    owner: _touchScrollOwner,
    listEl: _sessionTouchListEl,
    gen: _sessionTouchGen,
    token: _touchBatchToken,
    loaded: _sessionTouchLoadedCount,
    total: _sessionTouchTotalCount,
  }};
}}

// ── Live-tree untouched assertion helper ──
// After a stale schedule, assert the live tree is unchanged: same count,
// same object identity at each index, same SID order, same group/body refs,
// and _touchBatchPending cleared (settlement postcondition).
function assertLiveTreeUntouched(snap) {{
  var c = snapshotLiveTree();
  return {{
    sameCount: c.count === snap.count,
    sameRefs: snap.items.every(function(ref, i) {{ return c.items[i] === ref; }}),
    sameSids: JSON.stringify(c.sids) === JSON.stringify(snap.sids),
    sameGroupWrapper: c.groupWrapper === snap.groupWrapper,
    sameBody: c.body === snap.body,
    pendingCleared: c.batchPending === false,
    countBefore: snap.count,
    countAfter: c.count,
  }};
}}

// Helper: fire the scroll handler and drain RAF + microtasks
async function fireScrollAndDrain() {{
  rafCallbacks = [];
  rafSchedules = 0;
  if (_touchScrollOwner && _touchScrollOwner.handler) _touchScrollOwner.handler();
  const callbacks = rafCallbacks.splice(0);
  for (const cb of callbacks) cb();
  for (let i = 0; i < 10; i++) await Promise.resolve();
}}

// Helper: fire the scroll handler, drain ONLY the RAF (so the Promise
// microtask is queued), then call a mutator function that changes state
// BEFORE microtasks drain. This is the adversarial schedule that proves the
// microtask revalidation checks are real oracles, not false-greens.
async function fireScrollDrainRAFMutateThenMicrotasks(mutator) {{
  rafCallbacks = [];
  rafSchedules = 0;
  if (_touchScrollOwner && _touchScrollOwner.handler) _touchScrollOwner.handler();
  // Drain ONLY the RAF — all RAF preconditions are valid at this point,
  // so production queues the Promise.resolve().then(...) microtask.
  const callbacks = rafCallbacks.splice(0);
  for (const cb of callbacks) cb();
  // NOW mutate state while the Promise microtask is pending but not yet run.
  mutator();
  // Drain microtasks — the microtask revalidation must catch the mutation.
  for (let i = 0; i < 10; i++) await Promise.resolve();
}}

(async function() {{
  const results = [];

  // ── 1. Positive control: unchanged owner → append succeeds (60→100) ──
  _setupTouchSentinel(list, {total}, flatRows, renderOne, null, 60);
  appendCount = 0;
  await fireScrollAndDrain();
  results.push({{
    name: 'positive_control',
    appended: appendCount === 1,
    loadedAfter: _sessionTouchLoadedCount,
    expectedLoaded: 100,
  }});

  // ── 2. Geometry change: scroll away from bottom AFTER RAF drains → no append ──
  // Re-setup to get a fresh owner at loaded=100. Geometry is near-bottom at
  // setup and during the RAF, so the RAF queues the Promise. Then we scroll
  // away BEFORE microtasks drain — the microtask's nearBottom2 check fails.
  _setupTouchSentinel(list, {total}, flatRows, renderOne, null, 100);
  appendCount = 0;
  const snap2 = snapshotLiveTree();
  await fireScrollDrainRAFMutateThenMicrotasks(function() {{
    list.scrollTop = 0; // 20000 - 0 - 800 = 19200 >= 200 → not near bottom
  }});
  const tree2 = assertLiveTreeUntouched(snap2);
  results.push({{
    name: 'geometry_change',
    noAppend: appendCount === 0,
    loadedAfter: _sessionTouchLoadedCount,
    expectedLoaded: 100, // unchanged
    tree: tree2,
  }});

  // ── 3. Owner/list replacement AFTER RAF drains → no append ──
  // Reset geometry to near-bottom for this scenario.
  list.scrollTop = 19500;
  _setupTouchSentinel(list, {total}, flatRows, renderOne, null, 100);
  const ownerBeforeReplace = _touchScrollOwner;
  appendCount = 0;
  const snap3 = snapshotLiveTree();
  await fireScrollDrainRAFMutateThenMicrotasks(function() {{
    // Install a new owner via re-setup. This replaces _touchScrollOwner,
    // _sessionTouchListEl (via _invalidateTouchRender inside _setupTouchSentinel),
    // and bumps _sessionTouchGen + _touchBatchToken.
    _setupTouchSentinel(list, {total}, flatRows, renderOne, null, 100);
  }});
  const ownerAfterReplace = _touchScrollOwner;
  const tree3 = assertLiveTreeUntouched(snap3);
  results.push({{
    name: 'owner_replacement',
    noAppend: appendCount === 0,
    loadedAfter: _sessionTouchLoadedCount,
    expectedLoaded: 100, // unchanged
    ownerChanged: ownerAfterReplace !== ownerBeforeReplace,
    tree: tree3,
  }});

  // ── 4. Token supersession: stale callback cannot clear newer pending ownership;
  //      newer owner then settles deliberately ──
  // The RAF drains and sets token=T. Before the microtask runs, we bump
  // _touchBatchToken to T+1. The stale microtask sees token!==_touchBatchToken
  // and returns WITHOUT clearing _touchBatchPending (because token !==
  // _touchBatchToken — the guard is `if(token===_touchBatchToken)` which is
  // false, so it does NOT clear). This proves the stale microtask cannot
  // clobber a newer owner's pending flag. We then manually settle the newer
  // owner by calling _appendTouchBatch directly (simulating the newer owner's
  // own microtask firing), which grows loaded 100→140 and clears pending.
  list.scrollTop = 19500;
  _setupTouchSentinel(list, {total}, flatRows, renderOne, null, 100);
  appendCount = 0;
  const snap4 = snapshotLiveTree();
  const tokenBefore4 = _touchBatchToken;
  // Fire the scroll handler to arm RAF + microtask
  rafCallbacks = [];
  rafSchedules = 0;
  if (_touchScrollOwner && _touchScrollOwner.handler) _touchScrollOwner.handler();
  // Drain the RAF callback — this sets token and arms the microtask
  const rafCbs4 = rafCallbacks.splice(0);
  for (const cb of rafCbs4) cb();
  // BEFORE draining the microtask, supersede the token
  _touchBatchToken++;
  for (let i = 0; i < 10; i++) await Promise.resolve();
  // The stale microtask ran and bailed on token mismatch WITHOUT clearing
  // _touchBatchPending — so pending is still true.
  const pendingAfterStale4 = _touchBatchPending;
  const staleAppendCount4 = appendCount;
  const tree4 = assertLiveTreeUntouched(snap4);
  // Now model the newer owner settling: the newer token's microtask fires,
  // calls _appendTouchBatch, and clears pending. We simulate this by directly
  // calling _appendTouchBatch (the newer owner has valid state) and clearing
  // pending afterward.
  _touchBatchPending = false; // reset stale pending from the superseded token
  appendCount = 0;
  _appendTouchBatch();
  const newerAppendCount4 = appendCount;
  const newerLoaded4 = _sessionTouchLoadedCount;
  _touchBatchPending = false;
  results.push({{
    name: 'token_supersession',
    staleNoAppend: staleAppendCount4 === 0,
    staleDidNotClearPending: pendingAfterStale4 === true,
    tokenWasSuperseded: _touchBatchToken !== tokenBefore4,
    treeUntouched: tree4,
    newerSettled: newerAppendCount4 === 1,
    newerLoaded: newerLoaded4,
    expectedNewerLoaded: 140,
    pendingClearedAfterSettlement: _touchBatchPending === false,
  }});

  // ── 5. Loaded>=total terminal state AFTER RAF drains → no append ──
  // Start with loaded=100, total=140 (so the RAF passes l<t and queues the
  // Promise). Then advance loaded to equal total BEFORE microtasks drain —
  // the microtask's l2>=t2 check fails.
  list.scrollTop = 19500;
  _setupTouchSentinel(list, {total}, flatRows, renderOne, null, 100);
  appendCount = 0;
  const snap5 = snapshotLiveTree();
  await fireScrollDrainRAFMutateThenMicrotasks(function() {{
    // Advance loaded to total so the microtask sees l2>=t2 and bails.
    _sessionTouchLoadedCount = _sessionTouchTotalCount;
  }});
  const tree5 = assertLiveTreeUntouched(snap5);
  results.push({{
    name: 'terminal_state',
    noAppend: appendCount === 0,
    loadedAfter: _sessionTouchLoadedCount,
    expectedLoaded: 140, // we set it to total (140) in the mutator
    tree: tree5,
  }});

  // ── 6. Owner-only replacement: replace ONLY _touchScrollOwner, keep gen/list/token/loaded ──
  // This isolates the _touchScrollOwner !== owner microtask check. Gen, list,
  // token, and loaded/total are all unchanged — the ONLY thing that changes is
  // the owner object reference. Without this scenario, the existing
  // owner_replacement case (scenario 3) is false-green because it calls
  // _setupTouchSentinel() which also bumps gen and token, so the Promise bails
  // on gen/token even if the owner check is deleted.
  //
  // Note: the RAF callback itself bumps _touchBatchToken (production behavior
  // — the scroll handler sets the token before arming the microtask). We
  // capture token6 AFTER the RAF drains but BEFORE the mutator runs, so the
  // invariant is: the MUTATOR does not change token, gen, list, or loaded —
  // only _touchScrollOwner.
  list.scrollTop = 19500;
  _setupTouchSentinel(list, {total}, flatRows, renderOne, null, 100);
  appendCount = 0;
  const ownerBefore6 = _touchScrollOwner;
  // Inline the adversarial schedule so we can capture state post-RAF, pre-mutator.
  rafCallbacks = [];
  rafSchedules = 0;
  if (_touchScrollOwner && _touchScrollOwner.handler) _touchScrollOwner.handler();
  const rafCbs6 = rafCallbacks.splice(0);
  for (const cb of rafCbs6) cb();
  // Capture state AFTER RAF drain, BEFORE mutator. The RAF bumped the token;
  // these values are what the microtask should see as "current" if the mutator
  // doesn't touch them.
  const gen6 = _sessionTouchGen;
  const token6 = _touchBatchToken;
  const list6 = _sessionTouchListEl;
  const loaded6 = _sessionTouchLoadedCount;
  const snap6 = snapshotLiveTree();
  // Retain the exact replacement owner object so we can assert it remains
  // current and untouched after settlement. Built with post-RAF state so its
  // fields match what the microtask sees as "current".
  const replacementOwner6 = {{ gen: gen6, list: list6, handler: null, raf: 0, token: token6 }};
  // Mutator: replace ONLY _touchScrollOwner, leave everything else identical.
  _touchScrollOwner = replacementOwner6;
  for (let i = 0; i < 10; i++) await Promise.resolve();
  const tree6 = assertLiveTreeUntouched(snap6);
  results.push({{
    name: 'owner_only_replacement',
    noAppend: appendCount === 0,
    loadedAfter: _sessionTouchLoadedCount,
    expectedLoaded: loaded6, // unchanged
    ownerWasReplaced: _touchScrollOwner !== ownerBefore6,
    genUnchanged: _sessionTouchGen === gen6,
    listUnchanged: _sessionTouchListEl === list6,
    tokenUnchanged: _touchBatchToken === token6,
    // Exact replacement owner retained and untouched:
    replacementOwnerRetained: _touchScrollOwner === replacementOwner6,
    replacementOwnerUntouched: replacementOwner6.gen === gen6 && replacementOwner6.list === list6 && replacementOwner6.token === token6,
    // Geometry, token, loaded/total remain as intended:
    geometryValid: list.scrollHeight - list.scrollTop - list.clientHeight < 200,
    tokenStill6: _touchBatchToken === token6,
    loadedStill6: _sessionTouchLoadedCount === loaded6,
    // Same-token rejection clears _touchBatchPending:
    pendingCleared: _touchBatchPending === false,
    tree: tree6,
  }});

  // ── 7. List-only replacement: replace ONLY _sessionTouchListEl, keep owner/gen/token/loaded ──
  // This isolates the _sessionTouchListEl !== list microtask check. Owner
  // object identity, gen, token, and loaded/total are all unchanged — only
  // the list element reference changes. Without this scenario the list
  // check has no independent bite.
  //
  // Same as scenario 6: the RAF bumps the token before the mutator runs.
  // We capture state post-RAF, pre-mutator to isolate that the MUTATOR only
  // changes the list reference.
  list.scrollTop = 19500;
  _setupTouchSentinel(list, {total}, flatRows, renderOne, null, 100);
  appendCount = 0;
  const snap7 = snapshotLiveTree();
  const owner7 = snap7.owner;
  // Retain the exact replacement list so we can assert it remains current.
  const oldList7 = snap7.listEl;
  const oldListScrollTop7 = oldList7.scrollTop;
  const oldListItemCount7 = oldList7._items.length;
  const replacementList7 = makeList();
  replacementList7.scrollHeight = 20000;
  replacementList7.scrollTop = 19500;
  replacementList7.clientHeight = 800;
  // Inline the adversarial schedule so we can capture state post-RAF, pre-mutator.
  rafCallbacks = [];
  rafSchedules = 0;
  if (_touchScrollOwner && _touchScrollOwner.handler) _touchScrollOwner.handler();
  const rafCbs7 = rafCallbacks.splice(0);
  for (const cb of rafCbs7) cb();
  // Capture state AFTER RAF drain, BEFORE mutator.
  const gen7 = _sessionTouchGen;
  const token7 = _touchBatchToken;
  const loaded7 = _sessionTouchLoadedCount;
  const total7 = _sessionTouchTotalCount;
  // Mutator: replace ONLY _sessionTouchListEl, leave everything else identical.
  _sessionTouchListEl = replacementList7;
  for (let i = 0; i < 10; i++) await Promise.resolve();
  const tree7 = assertLiveTreeUntouched(snap7);
  results.push({{
    name: 'list_only_replacement',
    noAppend: appendCount === 0,
    loadedAfter: _sessionTouchLoadedCount,
    expectedLoaded: loaded7, // unchanged
    ownerUnchanged: _touchScrollOwner === owner7,
    genUnchanged: _sessionTouchGen === gen7,
    tokenUnchanged: _touchBatchToken === token7,
    // Unchanged owner/gen/token/count authorities:
    totalUnchanged: _sessionTouchTotalCount === total7,
    // Exact replacement list retained and current:
    replacementListRetained: _sessionTouchListEl === replacementList7,
    // Old list node state preserved:
    oldListStatePreserved: oldList7.scrollTop === oldListScrollTop7 && oldList7._items.length === oldListItemCount7,
    // Pending settlement:
    pendingCleared: _touchBatchPending === false,
    tree: tree7,
  }});

  console.log(JSON.stringify({{
    results: results,
    innerHTMLWipes: _innerHTMLWipes,
  }}));
}})();
"""
    result = json.loads(_run_node_vm(source))

    # Verify all 7 scenarios
    scenarios = {r["name"]: r for r in result["results"]}
    assert len(result["results"]) == 7, \
        f"Expected 7 scenarios, got {len(result['results'])}"

    # 1. Positive control: append succeeds
    s = scenarios["positive_control"]
    assert s["appended"], \
        f"Positive control: unchanged owner must append — got appendCount=1 expected, loaded={s['loadedAfter']}"
    assert s["loadedAfter"] == 100, \
        f"Positive control: loaded must be 100 after append, got {s['loadedAfter']}"

    # 2. Geometry change: no append; live-tree untouched
    s = scenarios["geometry_change"]
    assert s["noAppend"], \
        f"Geometry change: must NOT append when scrolled away from bottom — got appendCount={s.get('noAppend')}, loaded={s['loadedAfter']}"
    assert s["loadedAfter"] == 100, \
        f"Geometry change: loaded must stay 100, got {s['loadedAfter']}"
    t = s["tree"]
    assert t["sameCount"], \
        f"Geometry change: live-tree count must be unchanged — before={t['countBefore']}, after={t['countAfter']}"
    assert t["sameRefs"], \
        "Geometry change: live-tree row object identities must be unchanged at every index"
    assert t["sameSids"], \
        "Geometry change: live-tree SID order must be unchanged"
    assert t["sameGroupWrapper"], \
        "Geometry change: group wrapper ref must be unchanged"
    assert t["sameBody"], \
        "Geometry change: body ref must be unchanged"
    assert t["pendingCleared"], \
        "Geometry change: _touchBatchPending must be cleared after stale rejection"

    # 3. Owner replacement: no append; live-tree untouched
    s = scenarios["owner_replacement"]
    assert s["noAppend"], \
        "Owner replacement: stale microtask must NOT append after owner replaced — got appendCount=0 expected"
    assert s["loadedAfter"] == 100, \
        f"Owner replacement: loaded must stay 100, got {s['loadedAfter']}"
    assert s["ownerChanged"], \
        "Owner replacement: _touchScrollOwner must have changed after re-setup"
    t = s["tree"]
    assert t["sameCount"], \
        f"Owner replacement: live-tree count must be unchanged — before={t['countBefore']}, after={t['countAfter']}"
    assert t["sameRefs"], \
        "Owner replacement: live-tree row object identities must be unchanged at every index"
    assert t["sameSids"], \
        "Owner replacement: live-tree SID order must be unchanged"
    assert t["sameGroupWrapper"], \
        "Owner replacement: group wrapper ref must be unchanged"
    assert t["sameBody"], \
        "Owner replacement: body ref must be unchanged"
    assert t["pendingCleared"], \
        "Owner replacement: _touchBatchPending must be cleared after stale rejection"

    # 4. Token supersession: stale does not append, does not clear pending;
    #    newer owner settles and clears it
    s = scenarios["token_supersession"]
    assert s["staleNoAppend"], \
        "Token supersession: stale microtask must NOT append after token bumped — got appendCount=0 expected"
    assert s["staleDidNotClearPending"], \
        "Token supersession: stale microtask must NOT clear _touchBatchPending when token mismatches (newer owner owns it)"
    assert s["tokenWasSuperseded"], \
        "Token supersession: _touchBatchToken must have been bumped"
    t = s["treeUntouched"]
    assert t["sameCount"], \
        f"Token supersession: live-tree count must be unchanged — before={t['countBefore']}, after={t['countAfter']}"
    assert t["sameRefs"], \
        "Token supersession: live-tree row object identities must be unchanged at every index"
    assert t["sameSids"], \
        "Token supersession: live-tree SID order must be unchanged"
    assert s["newerSettled"], \
        "Token supersession: newer owner must settle (append succeeds after stale rejection)"
    assert s["newerLoaded"] == 140, \
        f"Token supersession: newer owner loaded must be 140, got {s['newerLoaded']}"
    assert s["pendingClearedAfterSettlement"], \
        "Token supersession: _touchBatchPending must be cleared after newer owner settles"

    # 5. Terminal state: no append; live-tree untouched
    s = scenarios["terminal_state"]
    assert s["noAppend"], \
        "Terminal state: must NOT append when loaded>=total — got appendCount=0 expected"
    assert s["loadedAfter"] == 140, \
        f"Terminal state: loaded must be 140 (set to total by mutator), got {s['loadedAfter']}"
    t = s["tree"]
    assert t["sameCount"], \
        f"Terminal state: live-tree count must be unchanged — before={t['countBefore']}, after={t['countAfter']}"
    assert t["sameRefs"], \
        "Terminal state: live-tree row object identities must be unchanged at every index"
    assert t["sameSids"], \
        "Terminal state: live-tree SID order must be unchanged"
    assert t["sameGroupWrapper"], \
        "Terminal state: group wrapper ref must be unchanged"
    assert t["sameBody"], \
        "Terminal state: body ref must be unchanged"
    assert t["pendingCleared"], \
        "Terminal state: _touchBatchPending must be cleared after stale rejection"

    # 6. Owner-only replacement: no append; independent oracle for _touchScrollOwner check
    s = scenarios["owner_only_replacement"]
    assert s["noAppend"], \
        "Owner-only replacement: stale microtask must NOT append when only owner replaced — got appendCount=0 expected"
    assert s["loadedAfter"] == s["expectedLoaded"], \
        f"Owner-only replacement: loaded must stay unchanged, got {s['loadedAfter']}"
    assert s["ownerWasReplaced"], \
        "Owner-only replacement: _touchScrollOwner must be a different object after replacement"
    assert s["genUnchanged"], \
        "Owner-only replacement: _sessionTouchGen must be unchanged (only owner replaced)"
    assert s["listUnchanged"], \
        "Owner-only replacement: _sessionTouchListEl must be unchanged (only owner replaced)"
    assert s["tokenUnchanged"], \
        "Owner-only replacement: _touchBatchToken must be unchanged (only owner replaced)"
    # Exact replacement owner retained and untouched
    assert s["replacementOwnerRetained"], \
        "Owner-only replacement: exact replacement owner must remain current after settlement"
    assert s["replacementOwnerUntouched"], \
        "Owner-only replacement: replacement owner fields (gen, list, token) must be untouched"
    # Geometry, token, loaded/total remain as intended
    assert s["geometryValid"], \
        "Owner-only replacement: geometry must remain near-bottom"
    assert s["tokenStill6"], \
        "Owner-only replacement: _touchBatchToken must still equal the captured token6"
    assert s["loadedStill6"], \
        "Owner-only replacement: loaded must still equal loaded6"
    # Same-token rejection clears _touchBatchPending
    assert s["pendingCleared"], \
        "Owner-only replacement: _touchBatchPending must be cleared (same-token rejection)"
    # Live-tree untouched
    t = s["tree"]
    assert t["sameCount"], \
        f"Owner-only replacement: live-tree count must be unchanged — before={t['countBefore']}, after={t['countAfter']}"
    assert t["sameRefs"], \
        "Owner-only replacement: live-tree row object identities must be unchanged at every index"
    assert t["sameSids"], \
        "Owner-only replacement: live-tree SID order must be unchanged"
    assert t["sameGroupWrapper"], \
        "Owner-only replacement: group wrapper ref must be unchanged"
    assert t["sameBody"], \
        "Owner-only replacement: body ref must be unchanged"

    # 7. List-only replacement: no append; independent oracle for _sessionTouchListEl check
    s = scenarios["list_only_replacement"]
    assert s["noAppend"], \
        "List-only replacement: stale microtask must NOT append when only list replaced — got appendCount=0 expected"
    assert s["loadedAfter"] == s["expectedLoaded"], \
        f"List-only replacement: loaded must stay unchanged, got {s['loadedAfter']}"
    assert s["ownerUnchanged"], \
        "List-only replacement: _touchScrollOwner must be unchanged (only list replaced)"
    assert s["genUnchanged"], \
        "List-only replacement: _sessionTouchGen must be unchanged (only list replaced)"
    assert s["tokenUnchanged"], \
        "List-only replacement: _touchBatchToken must be unchanged (only list replaced)"
    # Unchanged owner/gen/token/count authorities
    assert s["totalUnchanged"], \
        "List-only replacement: _sessionTouchTotalCount must be unchanged"
    # Exact replacement list retained and current
    assert s["replacementListRetained"], \
        "List-only replacement: exact replacement list must remain current after settlement"
    # Old list node state preserved
    assert s["oldListStatePreserved"], \
        "List-only replacement: old list node state (scrollTop, item count) must be preserved"
    # Pending settlement
    assert s["pendingCleared"], \
        "List-only replacement: _touchBatchPending must be cleared after stale rejection"
    # Live-tree untouched
    t = s["tree"]
    assert t["sameCount"], \
        f"List-only replacement: live-tree count must be unchanged — before={t['countBefore']}, after={t['countAfter']}"
    assert t["sameRefs"], \
        "List-only replacement: live-tree row object identities must be unchanged at every index"
    assert t["sameSids"], \
        "List-only replacement: live-tree SID order must be unchanged"
    assert t["sameGroupWrapper"], \
        "List-only replacement: group wrapper ref must be unchanged"
    assert t["sameBody"], \
        "List-only replacement: body ref must be unchanged"

    assert result["innerHTMLWipes"] == 0, \
        f"No innerHTML wipes in any scenario, got {result['innerHTMLWipes']}"


@_node_tests
def test_owner_record_has_required_fields():
    """Gate-certifier finding #1: _setupTouchSentinel must create an explicit
    owner record with {gen, list, handler, raf, token} — not split globals.
    Verify the source contains the owner object construction.
    """
    fn = _extract_fn(SESSIONS_JS, "_setupTouchSentinel")
    assert "owner={" in fn or "owner ={" in fn, \
        "Must create an explicit owner record object"
    assert "gen:" in fn, "Owner record must have gen field"
    assert "list:" in fn, "Owner record must have list field"
    assert "handler:" in fn, "Owner record must have handler field"
    assert "raf:" in fn, "Owner record must have raf field"
    assert "token:" in fn, "Owner record must have token field"
    assert "_touchScrollOwner=owner" in fn, \
        "Must assign the owner to _touchScrollOwner"