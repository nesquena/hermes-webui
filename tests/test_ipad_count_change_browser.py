"""#6426 bounded-time Chromium regression: deep-scrolled touch sidebar stays
visible across forced repaints and content-only refreshes.

The gate-certifier reproduced both round-4 blanking cases in Chromium with
touch at iPad sizes and asked for a browser-level count-change case ("the
Node geometry test can't see it"). This drives the REAL production functions
(the verbatim fingerprint block from ``renderSessionListFromCache``, the real
``_sessionVirtualWindow``, ``_setupTouchSentinel`` and
``_invalidateTouchRender``) against REAL layout in Chromium:

1. **Forced re-render while scrolled deep** (collapse toggle, select mode,
   pin success, the recovery timer — eight call sites): three consecutive
   repaints at a deep scroll position must each leave a real session row
   visible in the viewport. The round-4 regression blanked on the third.
2. **Content-only refresh with a prefix touch window** (a conversation
   arrives while the viewport sits ~37,000px deep inside a ``[0,1060)``
   window): the refreshed window must re-anchor on the restored scroll
   position. The round-4 regression reset to ``[0,60)`` under the restored
   scrollTop — zero visible rows for ~1.24s.

Skips (does not fail) when Playwright or its Chromium is not installed,
matching the repo's other browser oracles.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_count_change_and_forced_render_keep_deep_viewport():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    from tests.test_ipad_regate_blank_scenarios import (
        _fingerprint_block_source,
        _function_source,
    )

    functions = "\n".join(_function_source(name) for name in (
        "_sessionVirtualWindow", "_touchIntervalState",
        "_invalidateTouchRender", "_ensureTouchSentinelObserver",
        "_setupTouchSentinel",
    ))
    fingerprint_block = _fingerprint_block_source()

    page_js = """
// ── production constants (static/sessions.js:4727-4735) ──
const SESSION_TOUCH_BATCH_SIZE = 40;
const SESSION_TOUCH_INITIAL_BATCH = 60;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 12;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
// ── production render-scope state the fingerprint block reads/writes ──
let q = '';
let _groupCollapsed = {};
let _showAllProfiles = false;
let _activeProject = 'proj';
let _sessionSourceFilter = 'cli';
let _showArchived = false;
let _contentSearchResults = null;
let activeSidForSidebar = '';
let listScrollTopBeforeRender = 0;
let _sessionTouchGen = 1;
let _sessionTouchStartIndex = 0;
let _sessionTouchLoadedCount = 0;
let _sessionTouchTotalCount = 0;
let _sessionTouchListEl = null;
let _touchRenderState = null;
let _touchScrollOwner = null;
let _touchSentinelObserver = null;
let _touchBatchPending = false;
let _touchBatchToken = 0;
let _touchContinuousBatchOwner = null;
let _touchContinuousBatchScheduled = false;
let _pendingTouchDeferredRenderTimer = 0;
let _strandedTouchRecoveryTimer = 0;
function _isTouchPrimary() { return true; }
function t(key) { return key; }
function _scheduleContinuousBatch() { /* suppressed: append chain, not this court */ }
function _appendTouchBatch() { /* suppressed */ }
function _prependTouchBatch() { /* suppressed */ }
// ── REAL production functions (extracted verbatim from sessions.js) ──
""" + functions + """
// ── REAL production fingerprint block, verbatim ──
function productionTouchScopeBranch(){
""" + fingerprint_block + """
}

const list = document.getElementById('list');
list.clientHeight; // force layout
let flatSessionRows = [];

function makeRows(total, firstSidOffset) {
  const rows = [];
  for (let i = 0; i < total; i++) {
    rows.push({ group: { label: 'G' }, session: { session_id: 's' + (i + (firstSidOffset || 0)) } });
  }
  return rows;
}
function renderOneSession(s) {
  const el = document.createElement('div');
  el.className = 'session-item';
  el.dataset.sid = s.session_id;
  el.style.height = SESSION_VIRTUAL_ROW_HEIGHT + 'px';
  el.textContent = 'Conversation ' + s.session_id;
  return el;
}
function paintWindow(vw) {
  list.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'session-date-group';
  const body = document.createElement('div');
  body.className = 'session-date-body';
  if (vw.topPad > 0) {
    const sp = document.createElement('div');
    sp.style.height = vw.topPad + 'px';
    body.appendChild(sp);
  }
  for (let i = vw.start; i < vw.end; i++) body.appendChild(renderOneSession(flatSessionRows[i].session));
  if (vw.bottomPad > 0) {
    const sp = document.createElement('div');
    sp.style.height = vw.bottomPad + 'px';
    body.appendChild(sp);
  }
  wrap.appendChild(body);
  list.appendChild(wrap);
  if (listScrollTopBeforeRender > 0) list.scrollTop = listScrollTopBeforeRender;
}
function renderOnce() {
  productionTouchScopeBranch();
  const vw = _sessionVirtualWindow({
    total: flatSessionRows.length,
    scrollTop: listScrollTopBeforeRender,
    viewportHeight: list.clientHeight || 520,
    itemHeight: SESSION_VIRTUAL_ROW_HEIGHT,
    buffer: SESSION_VIRTUAL_BUFFER_ROWS,
    threshold: SESSION_VIRTUAL_THRESHOLD_ROWS,
    activeIndex: -1,
  });
  paintWindow(vw);
  _setupTouchSentinel(list, flatSessionRows.length, flatSessionRows, renderOneSession, activeSidForSidebar, vw.end, vw.start);
  return vw;
}
function viewportRowVisible() {
  const lr = list.getBoundingClientRect();
  const rows = list.querySelectorAll('.session-item[data-sid]');
  for (const r of rows) {
    const rr = r.getBoundingClientRect();
    if (rr.bottom > lr.top + 1 && rr.top < lr.bottom - 1) return true;
  }
  return false;
}

// ══ Scenario 1: three forced repaints while scrolled deep (row ~170 of 1500) ══
flatSessionRows = makeRows(1500, 0);
listScrollTopBeforeRender = 170 * SESSION_VIRTUAL_ROW_HEIGHT;
productionTouchScopeBranch();          // initial paint stamps fingerprints
_sessionTouchStartIndex = 140;         // deep window after appends + scroll
_sessionTouchLoadedCount = 200;
renderOnce();
const fingerprintsAfterSetup1 = !!(list.dataset.sessionTouchScope && list.dataset.sessionTouchScopeOnly);
renderOnce();                          // forced repaint (unchanged scope)
const visibleAfterSecond = viewportRowVisible();
renderOnce();                          // forced repaint #3 — the repro case
const scenarioA = {
  fingerprintsAfterSetup1: fingerprintsAfterSetup1,
  fingerprintsAfterThirdRender: !!(list.dataset.sessionTouchScope && list.dataset.sessionTouchScopeOnly),
  visibleAfterSecond: visibleAfterSecond,
  visibleAfterThird: viewportRowVisible(),
  start3: _sessionTouchStartIndex,
};

// ══ Scenario 2: content-only refresh deep inside a prefix window ══
flatSessionRows = makeRows(1500, 0);
listScrollTopBeforeRender = 712 * SESSION_VIRTUAL_ROW_HEIGHT; // ~37,024px
productionTouchScopeBranch();
_sessionTouchStartIndex = 0;
_sessionTouchLoadedCount = 1060;       // prefix window after appends
renderOnce();
const visibleBeforeRefresh = viewportRowVisible();
flatSessionRows = makeRows(1501, 1);   // a conversation arrived: content-only
const vw2 = renderOnce();
const scenarioB = {
  visibleBeforeRefresh: visibleBeforeRefresh,
  visibleAfterRefresh: viewportRowVisible(),
  start2: _sessionTouchStartIndex,
  end2: vw2.end,
  reanchoredNearViewport: _sessionTouchStartIndex >= 680 && _sessionTouchStartIndex <= 712,
  keptPaintedWindow: _sessionTouchStartIndex === 0 && _sessionTouchLoadedCount === 1060,
};

return { scenarioA: scenarioA, scenarioB: scenarioB };
"""

    with sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium is not installed")
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment-dependent
            pytest.skip(f"Playwright Chromium failed to launch: {exc}")
        try:
            page = browser.new_page(
                viewport={"width": 820, "height": 1180}, has_touch=True
            )
            page.set_content(
                '<div id="list" style="position:relative;width:300px;height:520px;'
                'overflow-y:auto;overflow-anchor:none;-webkit-overflow-scrolling:touch">'
                "</div>"
            )
            out = page.evaluate("() => {\n" + page_js + "\n}")
        finally:
            browser.close()

    a = out["scenarioA"]
    assert a["fingerprintsAfterSetup1"] is True, (
        "setup's invalidation wiped the fingerprints the render just stamped"
    )
    assert a["fingerprintsAfterThirdRender"] is True
    assert a["visibleAfterSecond"] is True
    assert a["visibleAfterThird"] is True, (
        "the third forced repaint at depth left zero visible rows — the "
        "reproduced round-4 blank (real Chromium layout, touch primary)"
    )
    assert a["start3"] >= 100, "the deep window must not reset to [0,60)"

    b = out["scenarioB"]
    assert b["visibleBeforeRefresh"] is True
    assert b["visibleAfterRefresh"] is True, (
        "a content-only refresh deep inside a prefix window left zero visible "
        "rows — the reproduced round-4 blank (real Chromium layout)"
    )
    assert b["keptPaintedWindow"] is True, (
        "a content-only refresh whose anchor sits inside the previously "
        "painted window must KEEP that window (master parity: the viewport "
        "shifts by exactly the inserted rows, not by the row-height "
        "estimate error — gate round-5 blocker 2)"
    )
