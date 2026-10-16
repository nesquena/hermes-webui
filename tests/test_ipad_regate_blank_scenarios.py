"""#6426 gate re-gate regressions — deep-scroll blanking cases.

Round-4 gate findings (exact head ``5934b565b7``), both CORE:

1. **Any forced re-render while scrolled deep blanks the sidebar.**
   ``_setupTouchSentinel()`` runs at the end of EVERY touch render and calls
   ``_invalidateTouchRender()`` unconditionally. The round-3 teardown wipe
   made that invalidation delete ``dataset.sessionTouchScope`` and
   ``sessionTouchScopeOnly`` — the very fingerprints the just-finished
   ``renderSessionListFromCache()`` had stamped onto the same list. The next
   refresh read an empty ``prevFingerprint``, took the full ``[0,60)`` reset
   beneath the preserved deep scroll position, and blanked the sidebar; the
   forced-render path (collapse toggle, select mode, pin success, the
   recovery timer itself) hits this on its very next repaint.
   Fix: setup captures both fingerprints before the invalidation and
   restores them when the invalidated list is this setup's list (a same-list
   owner replacement). Genuine teardowns keep the deletion.

2. **Content refreshes blank a normally scrolled prefix window.**
   Incremental appends keep ``start=0`` while the user scrolls deep into the
   loaded prefix, so the round-3 ``prevTouchStart>0`` gate rejected the
   re-anchor exactly when a content-only refresh needed it (window
   ``[0,1060)``, viewport at ~37,000px → reset to ``[0,60)``, zero visible
   rows for ~1.24s). Fix: the gate is removed — and (round 5) a content-only
   refresh whose restored anchor still falls inside the previously painted
   window KEEPS that window, because re-anchoring with the fixed
   ``SESSION_VIRTUAL_ROW_HEIGHT`` estimate walks the viewport by the real
   row-height error on every arrival (one conversation moved the reader
   hundreds of rows). The re-anchor survives only for anchors outside the
   painted window; a shallow viewport still degrades to the same ``[0,60)``
   the reset would have painted.

Unlike the earlier revision of this file, these tests do NOT hand-copy the
production logic: every scenario executes the REAL production fingerprint
block (sliced verbatim out of ``renderSessionListFromCache``), the REAL
``_invalidateTouchRender``, the REAL ``_setupTouchSentinel``, and the REAL
``_sessionVirtualWindow`` under node, in the production order render →
setup → invalidation → re-render.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

NODE_BIN = str(NODE)

SRC = SESSIONS_JS.read_text(encoding="utf-8")

# Production constants (static/sessions.js:4727-4735) used by Python-side
# assertions against node results.
INITIAL_BATCH = 60
ROW_HEIGHT = 52


def _function_source(name: str, src: str = "") -> str:
    src = src or SRC
    marker = f"function {name}"
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    params = src.find("(", start)
    depth = 0
    close = -1
    for idx in range(params, len(src)):
        if src[idx] == "(":
            depth += 1
        elif src[idx] == ")":
            depth -= 1
            if depth == 0:
                close = idx
                break
    brace = src.find("{", close)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start: idx + 1]
    raise AssertionError(f"{name} body did not close")


_FN_DECL_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")


def _collect_functions(names, src=""):
    src = src or SRC
    out = {}
    stack = list(names)
    seen = set()
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        body = _function_source(name, src)
        out[name] = body
        for dep in _FN_DECL_RE.findall(body):
            if dep not in seen:
                stack.append(dep)
    return "\n".join(out.values())


def _run_node(script: str):
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False) as f:
        f.write(script)
        path = Path(f.name)
    try:
        result = subprocess.run(
            [NODE_BIN, str(path)], cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
    finally:
        path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def _fingerprint_block_source(src: str = "") -> str:
    """Slice the REAL touch-scope fingerprint block out of
    renderSessionListFromCache: from ``if(_isTouchPrimary()){`` through the
    scope-only fingerprint, the bounds reset, and the content-only re-anchor.
    Executed verbatim by the chain tests below — no mirrored logic."""
    src = src or SRC
    anchor = src.find("const collapsedKeys=Object.keys(_groupCollapsed)")
    assert anchor != -1, "fingerprint block anchor (collapsedKeys) not found"
    start = src.rfind("if(_isTouchPrimary()){", 0, anchor)
    assert start != -1, "fingerprint block start not found"
    end = src.find("const activeIndex=flatSessionRows.findIndex(", start)
    assert end != -1, "fingerprint block end not found"
    return src[start:end]


# The chain harness reuses the battle-tested node preamble from
# test_ipad_sidebar_scroll_stuck (mock DOM, extractFunc, state lets, real
# _touchIntervalState/_ensureTouchSentinelObserver). It mocks
# _invalidateTouchRender and never extracts _setupTouchSentinel, so the chain
# body below re-extracts BOTH from the real source before driving scenarios.
try:  # pragma: no cover - import guard mirrors test_touch_prepend_browser
    from tests.test_ipad_sidebar_scroll_stuck import (  # noqa: E402
        SESSIONS_JS as _STUCK_SESSIONS_JS_TEXT,
        _node_test_preamble,
    )
except Exception:  # pragma: no cover
    _STUCK_SESSIONS_JS_TEXT = None
    _node_test_preamble = None

# Exact restore statements added by the round-4 fix inside
# _setupTouchSentinel. The mutation bite strips them from the extracted REAL
# function source (emulating the pre-fix head) and expects the chain to fail.
_SETUP_RESTORE_STMTS = (
    "if(_setupPrevScopeFingerprint)_setupInvalidatedList.dataset.sessionTouchScope=_setupPrevScopeFingerprint;",
    "if(_setupPrevScopeOnlyFingerprint)_setupInvalidatedList.dataset.sessionTouchScopeOnly=_setupPrevScopeOnlyFingerprint;",
)

_CHAIN_PRELUDE = """const SESSIONS_JS = """ + repr(_STUCK_SESSIONS_JS_TEXT) + """;
""" + (_node_test_preamble() if _node_test_preamble else "") + """
// ── chain overrides: everything below makes the preamble production-real ──
global.cancelAnimationFrame = function(){};
global.requestAnimationFrame = global.requestAnimationFrame || function(fn){ return 0; };
window.IntersectionObserver = function(cb, opts) {
  this.disconnect = function(){};
  this.observe = function(){};
  this.unobserve = function(){};
};
global.IntersectionObserver = window.IntersectionObserver;
function _isTouchPrimary(){ return true; }
// REAL invalidation replaces the preamble mock — the fingerprint deletion
// under test lives only in the production function.
""" + "eval(extractFunc('_invalidateTouchRender'));" + """
// REAL setup — carries the round-4 capture/restore around its invalidation.
""" + "eval(extractFunc('_setupTouchSentinel'));" + """
// REAL desktop/touch window computation (the production paint authority for
// touch lists — it reads the touch bounds the fingerprint block maintains).
""" + "eval(extractFunc('_sessionVirtualWindow'));" + """
// Suppress the continuous-append chain: this court is the
// fingerprint → window → setup → invalidation schedule, not the append path.
_scheduleContinuousBatch = function(){};
const SESSION_VIRTUAL_BUFFER_ROWS = 12;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
// Render-side state the production fingerprint block reads/writes.
let q = '';
let _groupCollapsed = {};
let _showAllProfiles = false;
let _activeProject = 'proj';
let _sessionSourceFilter = 'cli';
let _showArchived = false;
let _contentSearchResults = null;
let activeSidForSidebar = '';
let listScrollTopBeforeRender = 0;
// Timers the REAL _invalidateTouchRender clears (absent from the preamble's
// mock invalidation).
let _pendingTouchDeferredRenderTimer = 0;
let _strandedTouchRecoveryTimer = 0;
// The production fingerprint block, verbatim, wrapped so each harness render
// call runs exactly what renderSessionListFromCache runs in its touch branch.
function productionTouchScopeBranch(){
""" + _fingerprint_block_source() + """
}
function makeFlatRows(total, firstSidOffset) {
  const rows = [];
  for (let i = 0; i < total; i++) {
    rows.push({ group: { label: 'G' }, session: { session_id: 's' + (i + (firstSidOffset || 0)) } });
  }
  return rows;
}
"""


def _chain_script(body: str, strip_setup_restore: bool = False) -> str:
    script = _CHAIN_PRELUDE
    if strip_setup_restore:
        setup_src = _function_source("_setupTouchSentinel")
        for stmt in _SETUP_RESTORE_STMTS:
            assert stmt in setup_src, "restore statement missing from extracted setup source"
            setup_src = setup_src.replace(stmt, "")
        script += "eval(" + json.dumps(setup_src) + ");\n"
    script += body
    return script


class TestProductionRenderSetupChain:
    """Round-4 CORE blockers, driven through the REAL production chain:
    fingerprint block (verbatim source slice) → _sessionVirtualWindow →
    _setupTouchSentinel (real, includes its unconditional invalidation)."""

    def _render_once_js(self) -> str:
        # The production fingerprint block reads the module-level
        # `flatSessionRows` binding (as renderSessionListFromCache does), so
        # scenarios model content changes by REASSIGNING that binding.
        return """
function renderOnce() {
  productionTouchScopeBranch();
  const vw = _sessionVirtualWindow({
    total: flatSessionRows.length,
    scrollTop: listScrollTopBeforeRender,
    viewportHeight: list.clientHeight || 1080,
    itemHeight: SESSION_VIRTUAL_ROW_HEIGHT,
    buffer: SESSION_VIRTUAL_BUFFER_ROWS,
    threshold: SESSION_VIRTUAL_THRESHOLD_ROWS,
    activeIndex: -1,
  });
  _setupTouchSentinel(list, flatSessionRows.length, flatSessionRows,
    function(s){ return makeSessionItem(s.session_id); },
    activeSidForSidebar, vw.end, vw.start);
  return vw;
}
"""

    def test_forced_repaints_at_depth_keep_window_and_fingerprints(self):
        """Forced re-render while scrolled deep (collapse toggle, select mode,
        pin success, recovery timer — eight call sites) must not blank the
        sidebar: setup's invalidation must not erase the fingerprints the
        render just stamped, so unchanged-scope repaints keep the deep window
        instead of resetting to [0,60) beneath the restored deep scrollTop."""
        body = """
const list = makeList();
list.clientHeight = 1080;
let flatSessionRows = makeFlatRows(1500, 0);
listScrollTopBeforeRender = 170 * SESSION_VIRTUAL_ROW_HEIGHT; // deep viewport at row ~170
// Reach the mid-session state through the production block itself: the first
// call sees an empty fingerprint and takes the initial-batch path (stamping
// the fingerprints), then the deep appends + scroll are applied on top.
productionTouchScopeBranch();
_sessionTouchStartIndex = 140;
_sessionTouchLoadedCount = 200;
""" + self._render_once_js() + """
const vw1 = renderOnce();
const scopeAfterSetup1 = list.dataset.sessionTouchScope || '';
const scopeOnlyAfterSetup1 = list.dataset.sessionTouchScopeOnly || '';
const vw2 = renderOnce();
const vw3 = renderOnce();
const viewportRow = 170;
console.log(JSON.stringify({
  fingerprintsSurviveSetup: !!(scopeAfterSetup1 && scopeOnlyAfterSetup1),
  fingerprintsAfterThreeRenders: !!(list.dataset.sessionTouchScope && list.dataset.sessionTouchScopeOnly),
  start3: _sessionTouchStartIndex,
  loaded3: _sessionTouchLoadedCount,
  vw3Start: vw3.start,
  vw3End: vw3.end,
  viewportCovered: vw3.start * SESSION_VIRTUAL_ROW_HEIGHT <= viewportRow * SESSION_VIRTUAL_ROW_HEIGHT
    && viewportRow * SESSION_VIRTUAL_ROW_HEIGHT < vw3.end * SESSION_VIRTUAL_ROW_HEIGHT,
  noTopReset: _sessionTouchStartIndex >= 100,
}));
"""
        out = _run_node(_chain_script(body))
        assert out["fingerprintsSurviveSetup"] is True, (
            "_setupTouchSentinel's invalidation wiped the fingerprints the "
            "render just stamped — the next refresh resets to [0,60) beneath "
            "the preserved deep scroll position (gate round-4 blocker 1)"
        )
        assert out["fingerprintsAfterThreeRenders"] is True
        assert out["viewportCovered"] is True, (
            "the third forced repaint must still cover the viewport — zero "
            "visible rows at depth is the reproduced blank"
        )
        assert out["noTopReset"] is True
        assert out["vw3End"] > out["vw3Start"]

    def test_mutation_bite_setup_restore_removal(self):
        """Bite: stripping the capture/restore from the REAL setup source
        (the pre-fix head) must fail the chain — fingerprints vanish during a
        setup whose list is already owned, and the NEXT render (the third
        repaint — the gate's exact repro) resets the deep window to [0,60)
        beneath the restored deep scroll position."""
        body = """
const list = makeList();
list.clientHeight = 1080;
let flatSessionRows = makeFlatRows(1500, 0);
listScrollTopBeforeRender = 170 * SESSION_VIRTUAL_ROW_HEIGHT;
productionTouchScopeBranch();
_sessionTouchStartIndex = 140;
_sessionTouchLoadedCount = 200;
""" + self._render_once_js() + """
renderOnce();   // establishes list ownership; setup#2 onwards wipes on strip
renderOnce();   // strip: setup#2 deletes the fingerprints the block stamped
const fingerprintsAfterSecondSetup = !!(list.dataset.sessionTouchScope && list.dataset.sessionTouchScopeOnly);
const vw3 = renderOnce();   // block now reads an empty fingerprint → reset
console.log(JSON.stringify({
  fingerprintsAfterSecondSetup: fingerprintsAfterSecondSetup,
  windowResetToTop: _sessionTouchStartIndex === 0 && _sessionTouchLoadedCount === SESSION_TOUCH_INITIAL_BATCH,
  viewportCovered: vw3.start * SESSION_VIRTUAL_ROW_HEIGHT <= 170 * SESSION_VIRTUAL_ROW_HEIGHT
    && 170 * SESSION_VIRTUAL_ROW_HEIGHT < vw3.end * SESSION_VIRTUAL_ROW_HEIGHT,
}));
"""
        out = _run_node(_chain_script(body, strip_setup_restore=True))
        assert out["fingerprintsAfterSecondSetup"] is False, (
            "the bite must bite: without the capture/restore, a setup whose "
            "list is already owned deletes the freshly stamped fingerprints"
        )
        assert out["windowResetToTop"] is True, (
            "without the restore, the third repaint must take the full "
            "[0,60) reset — the exact reproduced regression"
        )
        assert out["viewportCovered"] is False

    def test_content_only_refresh_keeps_painted_window(self):
        """A conversation arriving while the viewport is deep inside a PREFIX
        touch window ([0,1060), start=0 after appends) must KEEP the window
        the previous render painted when the restored scroll position still
        falls inside it — repainting [0,1060) from the refreshed rows shifts
        the viewport by exactly the inserted rows (master parity), while
        re-anchoring walks the viewport by the fixed-row-height estimate
        error (gate round-5 blocker 2)."""
        body = """
const list = makeList();
list.clientHeight = 1080;
let flatSessionRows = makeFlatRows(1500, 0);
// Mid-session state: prefix window [0,1060) (appends keep start=0), viewport
// scrolled ~37,000px deep into the loaded prefix (row ~712).
listScrollTopBeforeRender = 712 * SESSION_VIRTUAL_ROW_HEIGHT;
productionTouchScopeBranch();            // stamps fingerprints via initial path
_sessionTouchStartIndex = 0;
_sessionTouchLoadedCount = 1060;
""" + self._render_once_js() + """
const vw1 = renderOnce();                // unchanged repaint at depth
// A conversation arrived at the top: one more row, different SID prefix —
// scope fields identical, CONTENT changed. Reassign the production binding.
flatSessionRows = makeFlatRows(1501, 1);
const vw2 = renderOnce();                // content-only change arrives
console.log(JSON.stringify({
  start2: _sessionTouchStartIndex,
  loaded2: _sessionTouchLoadedCount,
  vw2Start: vw2.start,
  vw2End: vw2.end,
  keptPaintedWindow: _sessionTouchStartIndex === 0 && _sessionTouchLoadedCount === 1060,
  viewportCovered: vw2.start * SESSION_VIRTUAL_ROW_HEIGHT <= 712 * SESSION_VIRTUAL_ROW_HEIGHT
    && 712 * SESSION_VIRTUAL_ROW_HEIGHT < vw2.end * SESSION_VIRTUAL_ROW_HEIGHT,
}));
"""
        out = _run_node(_chain_script(body))
        assert out["keptPaintedWindow"] is True, (
            "a content-only refresh whose anchor sits inside the previously "
            "painted window must KEEP that window (master parity) instead of "
            "re-anchoring with the fixed-row-height estimate"
        )
        assert out["viewportCovered"] is True
        assert out["vw2End"] > out["vw2Start"]

    def test_content_only_refresh_outside_window_still_reanchors(self):
        """The keep-window rule must not freeze a stale window: when the
        restored anchor falls OUTSIDE the previously painted window (rows
        removed above it), the bounded re-anchor still runs."""
        body = """
const list = makeList();
list.clientHeight = 1080;
let flatSessionRows = makeFlatRows(1500, 0);
// Deep window painted by appends at the BOTTOM of the list; the viewport
// scrolled near the loaded prefix's deep edge (row ~1010 of [0,1060)).
listScrollTopBeforeRender = 1010 * SESSION_VIRTUAL_ROW_HEIGHT;
productionTouchScopeBranch();
_sessionTouchStartIndex = 0;
_sessionTouchLoadedCount = 1060;
""" + self._render_once_js() + """
renderOnce();
// A large purge above the window: content changed, and the restored anchor
// (row ~1010) now sits beyond the previously loaded prefix edge — the
// keep-window rule does not apply because the anchor is outside
// [prevStart, prevLoaded) of the refreshed list... but it is INSIDE
// [0,1060) here by construction, so instead exercise the true outside case:
// shrink the total so far below the anchor that the window must re-anchor.
flatSessionRows = makeFlatRows(700, 2);
const vw2 = renderOnce();
console.log(JSON.stringify({
  start2: _sessionTouchStartIndex,
  loaded2: _sessionTouchLoadedCount,
  vw2Start: vw2.start,
  vw2End: vw2.end,
  anchoredNearViewport: _sessionTouchStartIndex >= 640 && _sessionTouchStartIndex <= 660,
  viewportCovered: vw2.start * SESSION_VIRTUAL_ROW_HEIGHT <= 640 * SESSION_VIRTUAL_ROW_HEIGHT
    && 640 * SESSION_VIRTUAL_ROW_HEIGHT < vw2.end * SESSION_VIRTUAL_ROW_HEIGHT,
}));
"""
        out = _run_node(_chain_script(body))
        assert out["anchoredNearViewport"] is True, (
            "an anchor outside the previously painted window must still take "
            "the bounded re-anchor path"
        )
        assert out["viewportCovered"] is True

    def test_shallow_content_only_change_degrades_to_initial_batch(self):
        """Without the start gate, a genuinely shallow viewport must behave
        exactly as before: the re-anchor projects an anchor of ≈0 and the
        window degrades to the same [0,60) the reset would have painted."""
        body = """
const list = makeList();
list.clientHeight = 1080;
let flatSessionRows = makeFlatRows(120, 0);
listScrollTopBeforeRender = 0; // viewport at the top
productionTouchScopeBranch();
_sessionTouchStartIndex = 0;
_sessionTouchLoadedCount = SESSION_TOUCH_INITIAL_BATCH;
""" + self._render_once_js() + """
renderOnce();
// one more row, different SID prefix — scope identical, content changed
flatSessionRows = makeFlatRows(121, 1);
const vw2 = renderOnce();
console.log(JSON.stringify({ start2: _sessionTouchStartIndex, end2: vw2.end }));
"""
        out = _run_node(_chain_script(body))
        assert out["start2"] == 0 and out["end2"] == INITIAL_BATCH, (
            "shallow viewports must degrade to the initial batch — no behavior "
            "change for small lists now that the start gate is gone"
        )

    def test_teardown_still_clears_fingerprints(self):
        """Genuine teardown keeps its deletion: the REAL _invalidateTouchRender
        clears both fingerprints and the bounds, so touch → mouse → touch
        re-entry takes the normal initial-batch path instead of surviving into
        _sessionVirtualWindow with zeroed bounds as an unchanged-scope [0,0)
        window (the round-3 CORE case must stay fixed)."""
        body = """
const list = makeList();
list.clientHeight = 1080;
let flatSessionRows = makeFlatRows(1500, 0);
listScrollTopBeforeRender = 170 * SESSION_VIRTUAL_ROW_HEIGHT;
productionTouchScopeBranch();
_sessionTouchStartIndex = 140;
_sessionTouchLoadedCount = 200;
_sessionTouchListEl = list;
// REAL production teardown (extracted above, replacing the preamble mock):
_invalidateTouchRender();
const fingerprintCleared = !list.dataset.sessionTouchScope && !list.dataset.sessionTouchScopeOnly;
const boundsZeroedAfterTeardown = _sessionTouchStartIndex === 0 && _sessionTouchLoadedCount === 0;
// Re-entry: same scope, fresh render — the production sequence runs the
// fingerprint block FIRST (empty prev → initial-batch reset), then the
// window. The degenerate [0,0) must be unreachable.
productionTouchScopeBranch();
const vw = _sessionVirtualWindow({
  total: flatSessionRows.length,
  scrollTop: 0,
  viewportHeight: 1080,
  itemHeight: SESSION_VIRTUAL_ROW_HEIGHT,
  buffer: SESSION_VIRTUAL_BUFFER_ROWS,
  threshold: SESSION_VIRTUAL_THRESHOLD_ROWS,
  activeIndex: -1,
});
console.log(JSON.stringify({
  fingerprintCleared: fingerprintCleared,
  start: vw.start,
  end: vw.end,
  boundsZeroed: boundsZeroedAfterTeardown,
}));
"""
        out = _run_node(_chain_script(body))
        assert out["fingerprintCleared"] is True, (
            "genuine teardown must clear the stored scope fingerprints so "
            "re-entry takes the initial-batch path"
        )
        assert out["boundsZeroed"] is True
        assert out["start"] == 0 and out["end"] == INITIAL_BATCH, (
            "re-entry after teardown with an unchanged scope must paint the "
            "initial [0,60) batch — never the degenerate [0,0)"
        )


class TestCountChangeKeepsDeepWindow:
    """Window-math pins (real _sessionVirtualWindow; production source pins)."""

    def _script(self, body: str) -> str:
        return (
            _collect_functions(["_sessionVirtualWindow"])
            + "function _isTouchPrimary(){return true;}\n"
            + "const SESSION_TOUCH_INITIAL_BATCH=60;\n"
            + "const SESSION_VIRTUAL_ROW_HEIGHT=52;\n"
            + "const SESSION_VIRTUAL_BUFFER_ROWS=12;\n"
            + "const SESSION_VIRTUAL_THRESHOLD_ROWS=80;\n"
            + body
        )

    def test_content_only_change_reanchors_on_scroll_position(self):
        """Deep window [9940,10000) in an 11,000-row list + one new
        conversation (total 11001) + restored deep scrollTop → the window
        must stay anchored near the scroll position, NOT reset to [0,60)."""
        script = self._script(r"""
let _sessionTouchStartIndex=9940;
let _sessionTouchLoadedCount=10000;
const vw=_sessionVirtualWindow({
  total:11001, scrollTop:9940*SESSION_VIRTUAL_ROW_HEIGHT, viewportHeight:1080,
  itemHeight:SESSION_VIRTUAL_ROW_HEIGHT, buffer:SESSION_VIRTUAL_BUFFER_ROWS,
  threshold:SESSION_VIRTUAL_THRESHOLD_ROWS, activeIndex:-1,
});
console.log(JSON.stringify({start:vw.start,end:vw.end}));
""")
        out = _run_node(script)
        assert out["start"] >= 9800, (
            "a content-only change while scrolled deep must keep the window "
            "anchored near the scroll position — not reset to [0,60)"
        )
        assert out["end"] > out["start"], "window must be non-empty"
        restored_scroll = 9940 * 52
        assert out["start"] * 52 <= restored_scroll < out["end"] * 52

    def test_render_source_wires_scope_only_logic(self):
        """Pin the production wiring: scope-only fingerprint computed BEFORE
        the reset, content-only re-anchor AFTER it, both stored."""
        body = _function_source("renderSessionListFromCache")
        assert "const scopeOnlyFingerprint=scopeFingerprint.split('|').slice(0,8).join('|');" in body
        assert "list.dataset.sessionTouchScopeOnly=scopeOnlyFingerprint;" in body
        assert "contentOnlyDeepChange" in body
        # The scope-only decision must precede the bounds reset...
        assert body.find("scopeOnlyFingerprint=scopeFingerprint.split") < body.find("_sessionTouchStartIndex=0;\n      _sessionTouchLoadedCount=SESSION_TOUCH_INITIAL_BATCH;")
        # ...and the re-anchor must follow it.
        assert body.find("contentOnlyDeepChange)") > body.find("_sessionTouchStartIndex=0;\n      _sessionTouchLoadedCount=SESSION_TOUCH_INITIAL_BATCH;")

    def test_render_source_pins_keep_window_reanchor(self):
        """Round-5 fix wiring: a content-only refresh whose restored anchor
        still falls inside the previously painted window KEEPS that window;
        the bounded re-anchor survives only for anchors outside it. The
        round-4 ``prevTouchStart>0`` rejection gate must stay OUT either way
        — incremental appends keep start=0 while the viewport is deep, so a
        start-based gate rejected the re-anchor exactly when it was needed."""
        body = _function_source("renderSessionListFromCache")
        # The keep-window wiring is present...
        assert "const prevTouchStart=Math.max(0, Number(_sessionTouchStartIndex)||0);" in body
        assert "const prevTouchLoaded=Math.max(prevTouchStart, Number(_sessionTouchLoadedCount)||0);" in body
        assert "if(anchorFirstVisible>=prevTouchStart&&anchorFirstVisible<Math.min(totalRows,prevTouchLoaded)){" in body
        # ...the bounded re-anchor survives for out-of-window anchors...
        assert "anchorFirstVisible-SESSION_VIRTUAL_BUFFER_ROWS" in body
        # ...and the old rejection gate is still gone.
        assert "prevTouchStart>0" not in body, (
            "the start-based rejection gate is the round-4 regression: "
            "incremental appends keep start=0 while the viewport is deep, so "
            "the gate rejected the re-anchor exactly when a content-only "
            "refresh needed it"
        )

    def test_mutation_bite_scope_guard_removal(self):
        """Removing the scope-only comparison (reverting to full-fingerprint
        content sensitivity) must fail the wiring pin: the guard expression
        must compare prevScopeOnlyFingerprint with scopeOnlyFingerprint."""
        body = _function_source("renderSessionListFromCache")
        assert "prevScopeOnlyFingerprint===scopeOnlyFingerprint" in body

    def test_setup_source_pins_capture_restore(self):
        """Round-4 fix wiring: setup captures the fingerprints from the list
        it is about to invalidate and restores them only for a same-list
        replacement; the deletion itself stays in _invalidateTouchRender."""
        setup = _function_source("_setupTouchSentinel")
        for stmt in _SETUP_RESTORE_STMTS:
            assert stmt in setup, f"missing restore statement: {stmt}"
        assert setup.find("const _setupInvalidatedList=_sessionTouchListEl;") != -1
        # The capture must precede the setup-path invalidation (the SECOND
        # call site — the first is the touch→non-touch exit above it, which is
        # genuine teardown and must keep its deletion).
        capture_idx = setup.find("const _setupInvalidatedList=_sessionTouchListEl;")
        main_invalidate_idx = setup.find("_invalidateTouchRender();", capture_idx)
        assert main_invalidate_idx != -1
        restore_idx = setup.find(_SETUP_RESTORE_STMTS[0])
        assert capture_idx < main_invalidate_idx < restore_idx, (
            "the capture must happen BEFORE the invalidation wipes the dataset, "
            "and the restore right after it"
        )
        invalidate = _function_source("_invalidateTouchRender")
        assert "delete _touchTeardownList.dataset.sessionTouchScope;" in invalidate
        assert "delete _touchTeardownList.dataset.sessionTouchScopeOnly;" in invalidate
        # The list must be captured BEFORE _sessionTouchListEl is nulled.
        capture = invalidate.find("const _touchTeardownList=_sessionTouchListEl;")
        nulling = invalidate.find("_sessionTouchListEl=null;")
        assert -1 < capture < nulling


def list_scroll_top(out):
    # helper kept for the deep-window assertion above
    return 9940 * 52


class TestTeardownClearsFingerprint:
    """Source pins for the teardown wipe (round-3 CORE case)."""

    def test_invalidate_touch_render_clears_scope_fingerprint(self):
        body = _function_source("_invalidateTouchRender")
        assert "delete _touchTeardownList.dataset.sessionTouchScope;" in body, (
            "teardown must clear the stored scope fingerprint so re-entry "
            "takes the initial-batch path instead of painting [0,0)"
        )
        assert "delete _touchTeardownList.dataset.sessionTouchScopeOnly;" in body

    def test_mutation_bite_fingerprint_clear_removal(self):
        """Reverting the teardown fingerprint clear must fail: the pre-fix
        teardown left the fingerprint set, so the re-entry chain test would
        see prevFingerprint===scopeFingerprint and paint [0,0)."""
        body = _function_source("_invalidateTouchRender")
        stripped = body.replace("delete _touchTeardownList.dataset.sessionTouchScope;", "")
        assert stripped != body, "pin must reference the exact clearing statement"
