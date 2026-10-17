"""Regression coverage for issue #7591: virtualized transcript scroll oscillation.

Two compounding causes measured in the issue:
  1. `rowHeightFor` ignores the measured row heights the render loop already
     collects, so pads are built from flat per-role constants that ran 3.6x the
     real height (34% short on a different transcript per #7283).
  2. `_maybeRecoverVirtualizedBlankViewport` recovers a blank viewport by
     re-rendering the WHOLE transcript, which collapses scrollHeight, lets the
     browser clamp scrollTop into the tail, and re-derives the window from the
     clamped position — the measured windowed<->full-render oscillation.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _extract_func_script(js: str) -> str:
    return f"""
const src = {js!r};
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
"""


def _calibration_block(js: str) -> str:
    """Constants + calibration helpers as they appear in ui.js."""
    start = js.index("const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS")
    end = js.index("const MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS")
    return js[start:end]


def test_virtual_window_seeds_unmeasured_rows_from_calibrated_role_heights():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _calibration_block(js) + """
eval(extractFunc('_messageVirtualWindow'));
const base = {
  total: 240,
  scrollTop: 120 * 70,
  viewportHeight: 720,
  heights: [],
  defaultHeight: 140,
  bufferPx: 240,
  threshold: 80,
  keepTailCount: 50,
  roleForIdx: () => 'tool_call',
};
const flat = _messageVirtualWindow(base);
const calibrated = _messageVirtualWindow(Object.assign({}, base, {
  defaultHeightForRole: (role) => (role === 'tool_call' ? 150 : 140),
}));
console.log(JSON.stringify({ flat: flat.topPad, calibrated: calibrated.topPad }));
"""
    metrics = json.loads(_run_node(source))
    # Flat constant for tool_call rows is 400px; the calibrated mean is 150px, so
    # the pad must be built from 150px rows instead of 400px rows.
    assert metrics["flat"] == 8000
    assert metrics["calibrated"] == 8100


def test_role_calibration_blends_samples_with_flat_prior_then_freezes():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _calibration_block(js) + """
eval(extractFunc('_recordMessageVirtualRoleMeasurement'));
eval(extractFunc('_messageVirtualCalibratedRoleHeight'));
const flatTool = MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS.tool_call;
const before = _messageVirtualCalibratedRoleHeight('tool_call');
const early = [];
for (let i = 0; i < 3; i++) {
  _recordMessageVirtualRoleMeasurement('tool_call', i, 150);
  early.push(_messageVirtualCalibratedRoleHeight('tool_call'));
}
for (let i = 0; i < 40; i++) {
  // Distinct rows: the freeze counts rows, not repeated measure passes.
  _recordMessageVirtualRoleMeasurement('tool_call', i, 150);
}
const frozen = _messageVirtualCalibratedRoleHeight('tool_call');
// A later outlier must not move a frozen role mean.
_recordMessageVirtualRoleMeasurement('tool_call', 999, 4000);
const afterOutlier = _messageVirtualCalibratedRoleHeight('tool_call');
// Roles never measured still fall back to the flat constant (factor ~1 here).
const untouched = _messageVirtualCalibratedRoleHeight('assistant');
console.log(JSON.stringify({
  flatTool, before, early, frozen, afterOutlier, untouched,
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["before"] == metrics["flatTool"]
    # Shrinkage: samples pull the estimate down from the 400px flat constant.
    assert all(v < metrics["flatTool"] for v in metrics["early"])
    assert metrics["early"] == sorted(metrics["early"], reverse=True)
    assert metrics["frozen"] == 150
    assert metrics["afterOutlier"] == 150
    assert metrics["untouched"] == MESSAGE_VIRTUAL_ASSISTANT_HEIGHT


MESSAGE_VIRTUAL_ASSISTANT_HEIGHT = 160


def test_measurements_feed_role_calibration():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _calibration_block(js) + """
let _messageVirtualHeightCache = [];
let _messageVirtualEstimatedRowHeight = 140;
let refreshed = 0;
let settled = 0;
function $(id){ return id === 'msgInner' ? { } : null; }
function _measureMessageVirtualRow(inner, entry){ return entry.height; }
function _messageVirtualRoleForEntry(entry){ return entry.role; }
function _scheduleMessageVirtualMeasurementRefresh(){ refreshed++; }
function _markMessageVirtualMeasurementsSettled(){ settled++; }
eval(extractFunc('_recordMessageVirtualRoleMeasurement'));
eval(extractFunc('_messageVirtualCalibratedRoleHeight'));
eval(extractFunc('_updateMessageVirtualMeasurements'));

const entries = [];
for (let i = 0; i < 25; i++) entries.push({ role: 'tool_call', height: 160, rawIdx: i });
const idxs = entries.map((_, i) => i);
// First pass: heights recorded and cache filled, so a refresh is scheduled.
_updateMessageVirtualMeasurements(entries, idxs, { virtualized: true });
const toolAfterFirst = _messageVirtualCalibratedRoleHeight('tool_call');
// Second pass with identical heights: nothing changed -> settle.
_updateMessageVirtualMeasurements(entries, idxs, { virtualized: true });
console.log(JSON.stringify({
  toolAfterFirst,
  estimated: _messageVirtualEstimatedRowHeight,
  refreshed, settled,
  cacheFilled: _messageVirtualHeightCache.filter((h) => h === 160).length,
}));
"""
    metrics = json.loads(_run_node(source))
    # 25 samples of 160px tool_call rows: measured mean wins over the 400px
    # flat constant, and the running mean of the render loop is published too.
    assert metrics["toolAfterFirst"] == 160
    assert metrics["estimated"] == 160
    assert metrics["cacheFilled"] == 25
    assert metrics["refreshed"] >= 1
    assert metrics["settled"] >= 1


def test_calibration_resets_with_the_per_session_height_cache():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _calibration_block(js) + """
const MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS = 2;
let _messageVirtualMeasurementCycleKey = '';
let _messageVirtualMeasurementRetryCount = 0;
let _messageVirtualScrollActive = false;
let _messageVirtualScrollSettleTimer = 0;
let _messageVirtualDeferredMeasurement = null;
let _messageVirtualWindowKey = 'x';
function clearTimeout(){}
function _clearUserRowIntrinsicHeightCache(){}
function _resetMessageVirtualMeasurementBurst(){}
eval(extractFunc('_recordMessageVirtualRoleMeasurement'));
eval(extractFunc('_resetMessageVirtualRoleCalibration'));
eval(extractFunc('_messageVirtualCalibratedRoleHeight'));
eval(extractFunc('_clearMessageVirtualHeightCache'));
for (let i = 0; i < 25; i++) _recordMessageVirtualRoleMeasurement('tool_call', i, 150);
const before = _messageVirtualCalibratedRoleHeight('tool_call');
_clearMessageVirtualHeightCache();
const after = _messageVirtualCalibratedRoleHeight('tool_call');
console.log(JSON.stringify({ before, after }));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["before"] == 150
    assert metrics["after"] == MESSAGE_VIRTUAL_TOOL_HEIGHT


MESSAGE_VIRTUAL_TOOL_HEIGHT = 400


def test_blank_viewport_recovery_clamps_to_rendered_edge_instead_of_full_render():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let deletes = [];
let renderCalls = [];
const _sessionHtmlCache = { delete(sid){ deletes.push(sid); } };
let _sessionHtmlCacheSid = 'sid-123';
const S = { session: { session_id: 'sid-123' } };
let _messageVirtualWindowKey = 'stale';
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _lastScrollTop = 0;
let _messageVirtualBlankClampAttempts = 0;
// Module state the extracted clamp / compensation wrapper touch.
let _blankViewportRecoveryToken = 0;
let _blankViewportRecoveryApplied = 0;
const performance = { now: () => 1000 };
function _freshProgrammaticScrollActive(){ return false; }
function _deferClearProgrammaticScroll(){}
function _messageViewportIntersectsRenderedRow(){ return false; }
function renderMessages(options){ renderCalls.push(options); }

// Rendered rows occupy document y=[4000, 6000]; the viewport sits entirely
// above them inside the (over-estimated) top spacer.
const container = {
  scrollTop: 800,
  clientHeight: 600,
  scrollHeight: 20000,
  getBoundingClientRect(){ return { top: 0, bottom: 600 }; },
  querySelectorAll(){ return [
    { getBoundingClientRect(){ return { top: 4000, bottom: 4500 }; } },
    { getBoundingClientRect(){ return { top: 4500, bottom: 6000 }; } },
  ]; },
};
function $(id){ return id === 'messages' ? container : null; }
eval(extractFunc('_clampVirtualizedBlankViewportToRenderedEdge'));
eval(extractFunc('_maybeRecoverVirtualizedBlankViewport'));

const first = _maybeRecoverVirtualizedBlankViewport({}, true, { virtualized: true });
const scrollTopAfterFirst = container.scrollTop;
const deletesAfterFirst = deletes.slice();
// A second blank detection after the clamp escalates to the old full render.
const second = _maybeRecoverVirtualizedBlankViewport({}, true, { virtualized: true });
console.log(JSON.stringify({
  first, deletesAfterFirst, renderCalls, scrollTopAfterFirst,
  second, key: _messageVirtualWindowKey,
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["first"] is True
    assert metrics["deletesAfterFirst"] == ["sid-123"]
    # Windowed refresh only — no full-render fallback (that collapses scrollHeight).
    assert metrics["renderCalls"][0] == {"preserveScroll": True}
    # Clamped down toward the nearest rendered edge (renderedTop - 0.5 * viewport).
    # Rendered rows sit at viewport y=4000 with scrollTop=800, i.e. document y=4800.
    assert metrics["scrollTopAfterFirst"] == 4500
    # Escalation is bounded: a still-blank viewport falls back to the full render.
    assert metrics["second"] is True
    assert metrics["renderCalls"][1] == {
        "preserveScroll": True,
        "_virtualFallback": True,
    }


def test_blank_viewport_recovery_keeps_full_render_without_rendered_rows():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let deletes = [];
let renderCalls = [];
const _sessionHtmlCache = { delete(sid){ deletes.push(sid); } };
let _sessionHtmlCacheSid = 'sid-123';
const S = { session: { session_id: 'sid-123' } };
let _messageVirtualWindowKey = 'stale';
let _messageVirtualBlankClampAttempts = 0;
// Module state the extracted clamp / compensation wrapper touch.
let _blankViewportRecoveryToken = 0;
let _blankViewportRecoveryApplied = 0;
function _messageViewportIntersectsRenderedRow(){ return false; }
function renderMessages(options){ renderCalls.push(options); }
const container = {
  scrollTop: 0,
  clientHeight: 600,
  scrollHeight: 5000,
  getBoundingClientRect(){ return { top: 0, bottom: 600 }; },
  querySelectorAll(){ return []; },
};
function $(id){ return id === 'messages' ? container : null; }
eval(extractFunc('_clampVirtualizedBlankViewportToRenderedEdge'));
eval(extractFunc('_maybeRecoverVirtualizedBlankViewport'));
const recovered = _maybeRecoverVirtualizedBlankViewport({}, true, { virtualized: true });
console.log(JSON.stringify({ recovered, renderCalls }));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["recovered"] is True
    assert metrics["renderCalls"] == [
        {"preserveScroll": True, "_virtualFallback": True}
    ]


def test_blank_viewport_recovery_keeps_full_render_during_programmatic_write():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let deletes = [];
let renderCalls = [];
const _sessionHtmlCache = { delete(sid){ deletes.push(sid); } };
let _sessionHtmlCacheSid = 'sid-123';
const S = { session: { session_id: 'sid-123' } };
let _messageVirtualWindowKey = 'stale';
let _messageVirtualBlankClampAttempts = 0;
// Module state the extracted clamp / compensation wrapper touch.
let _blankViewportRecoveryToken = 0;
let _blankViewportRecoveryApplied = 0;
function _messageViewportIntersectsRenderedRow(){ return false; }
function _freshProgrammaticScrollActive(){ return true; }
function renderMessages(options){ renderCalls.push(options); }
const container = {
  scrollTop: 800,
  clientHeight: 600,
  scrollHeight: 20000,
  getBoundingClientRect(){ return { top: 0, bottom: 600 }; },
  querySelectorAll(){ return [
    { getBoundingClientRect(){ return { top: 4000, bottom: 6000 }; } },
  ]; },
};
function $(id){ return id === 'messages' ? container : null; }
eval(extractFunc('_clampVirtualizedBlankViewportToRenderedEdge'));
eval(extractFunc('_maybeRecoverVirtualizedBlankViewport'));
const recovered = _maybeRecoverVirtualizedBlankViewport({}, true, { virtualized: true });
console.log(JSON.stringify({ recovered, renderCalls, scrollTop: container.scrollTop }));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["recovered"] is True
    assert metrics["renderCalls"] == [
        {"preserveScroll": True, "_virtualFallback": True}
    ]
    # Cannot clamp against an in-flight programmatic write.
    assert metrics["scrollTop"] == 800


def test_blank_viewport_recovery_clamps_across_the_head_tail_gap():
    """The render window is head rows + retained tail with a bottomPad gap in
    between; a viewport parked in that gap is neither above the union top nor
    below the union bottom, so the clamp must work per row, not per block."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let deletes = [];
let renderCalls = [];
const _sessionHtmlCache = { delete(sid){ deletes.push(sid); } };
let _sessionHtmlCacheSid = 'sid-123';
const S = { session: { session_id: 'sid-123' } };
let _messageVirtualWindowKey = 'stale';
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _lastScrollTop = 0;
let _messageVirtualBlankClampAttempts = 0;
// Module state the extracted clamp / compensation wrapper touch.
let _blankViewportRecoveryToken = 0;
let _blankViewportRecoveryApplied = 0;
let _messageRenderScrollRestoreDepth = 0;
const performance = { now: () => 1000 };
function _freshProgrammaticScrollActive(){ return false; }
function _deferClearProgrammaticScroll(){}
function _messageViewportIntersectsRenderedRow(){ return false; }
function renderMessages(options){ renderCalls.push(options); }

// Head block ends at document y=800, tail block starts at document y=2700;
// the viewport (1000..1600) sits in the gap between them.
const container = {
  scrollTop: 1000,
  clientHeight: 600,
  scrollHeight: 4000,
  getBoundingClientRect(){ return { top: 0, bottom: 600 }; },
  querySelectorAll(){ return [
    { getBoundingClientRect(){ return { top: -500, bottom: -200 }; } },
    { getBoundingClientRect(){ return { top: 1700, bottom: 2000 }; } },
  ]; },
};
function $(id){ return id === 'messages' ? container : null; }
eval(extractFunc('_clampVirtualizedBlankViewportToRenderedEdge'));
eval(extractFunc('_maybeRecoverVirtualizedBlankViewport'));
const recovered = _maybeRecoverVirtualizedBlankViewport({}, true, { virtualized: true });
console.log(JSON.stringify({ recovered, scrollTop: container.scrollTop, renderCalls }));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["recovered"] is True
    # Nearest edge is the head block's bottom (800) -> 800 - 0.5 * 600.
    assert metrics["scrollTop"] == 500
    assert metrics["renderCalls"] == [{"preserveScroll": True}]


# Shared harness for the two COMPOSED render-path regressions below. It wires the
# real ownership capture + real clamp + real recovery together and reproduces the
# one production behaviour that matters: _scrollAfterMessageRender re-arms
# _programmaticScroll with a fresh timestamp immediately before recovery runs
# (static/ui.js _restoreMessageScrollSnapshot), so by the time recovery sees the
# latch it can no longer tell that write apart from a foreign in-flight one.
# Ownership must therefore be captured BEFORE the restore, at the call site.
_COMPOSED_RECOVERY_HARNESS = """
let deletes = [];
let renderCalls = [];
let restoreCalls = 0;
const _sessionHtmlCache = { delete(sid){ deletes.push(sid); } };
let _sessionHtmlCacheSid = 'sid-123';
const S = { session: { session_id: 'sid-123' } };
let _messageVirtualWindowKey = 'stale';
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _lastScrollTop = 0;
let _messageVirtualBlankClampAttempts = 0;
// Module state the extracted clamp / compensation wrapper touch.
let _blankViewportRecoveryToken = 0;
let _blankViewportRecoveryApplied = 0;
let _messageJumpScrollOwner = null;
const performance = { now: () => 1000 };
function _freshProgrammaticScrollActive(){
  if(!_programmaticScroll) return false;
  const age = performance.now() - _programmaticScrollSetAt;
  return Number.isFinite(age) && age >= 0 && age <= 150;
}
function _deferClearProgrammaticScroll(){}
function _messageViewportIntersectsRenderedRow(){ return false; }
function renderMessages(options){ renderCalls.push(options); }
// Stand-in for _scrollAfterMessageRender: re-arms the latch exactly the way
// _restoreMessageScrollSnapshot does for a numeric restore.
function _scrollAfterMessageRender(){
  restoreCalls++;
  _programmaticScroll = true;
  _programmaticScrollSetAt = performance.now();
}
const container = {
  scrollTop: 800,
  clientHeight: 600,
  scrollHeight: 20000,
  getBoundingClientRect(){ return { top: 0, bottom: 600 }; },
  querySelectorAll(){ return [
    { getBoundingClientRect(){ return { top: 4000, bottom: 4500 }; } },
    { getBoundingClientRect(){ return { top: 4500, bottom: 6000 }; } },
  ]; },
};
function $(id){ return id === 'messages' ? container : null; }
eval(extractFunc('_captureMessageRecoveryOwnership'));
eval(extractFunc('_clampVirtualizedBlankViewportToRenderedEdge'));
eval(extractFunc('_maybeRecoverVirtualizedBlankViewport'));
"""


def test_render_own_scroll_restore_does_not_disable_the_blank_viewport_clamp():
    """Composed render path: when the ONLY fresh programmatic write is the one
    this render made itself, the clamp must still run. Ownership is captured
    before the restore, so the restore's own latch is not mistaken for a foreign
    in-flight scroll (the maintainer's rejected depth-counter 'always-false
    freshness stub' cannot express this case)."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _COMPOSED_RECOVERY_HARNESS + """
// No write in flight when the render starts -> ownership says "ours".
const ownership = _captureMessageRecoveryOwnership();
_scrollAfterMessageRender(true, null);
const recovered = _maybeRecoverVirtualizedBlankViewport({}, true, { virtualized: true }, ownership);
console.log(JSON.stringify({
  ownership, recovered, restoreCalls, scrollTop: container.scrollTop, renderCalls,
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["restoreCalls"] == 1
    # The restore really did arm the latch before recovery ran.
    assert metrics["ownership"] == {"programmatic": False, "jumpOwner": False}
    assert metrics["recovered"] is True
    # Bounded clamp, not the full-render fallback.
    assert metrics["renderCalls"] == [{"preserveScroll": True}]
    assert metrics["scrollTop"] == 4500


def test_foreign_programmatic_write_in_flight_falls_back_to_full_render():
    """The same composed path, but a FOREIGN scroll write was already settling
    when the render started: recovery must not fight it, so the clamp is skipped
    and the full render escalates. This is the live contract the dead
    depth-counter guard failed to express."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _COMPOSED_RECOVERY_HARNESS + """
// A foreign write was stamped just before the render started.
_programmaticScroll = true;
_programmaticScrollSetAt = performance.now();
const ownership = _captureMessageRecoveryOwnership();
_scrollAfterMessageRender(true, null);
const recovered = _maybeRecoverVirtualizedBlankViewport({}, true, { virtualized: true }, ownership);
console.log(JSON.stringify({
  ownership, recovered, scrollTop: container.scrollTop, renderCalls,
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["ownership"]["programmatic"] is True
    assert metrics["recovered"] is True
    assert metrics["renderCalls"] == [
        {"preserveScroll": True, "_virtualFallback": True}
    ]
    assert metrics["scrollTop"] == 800


def test_blank_viewport_recovery_skips_rows_without_client_rects():
    """assistant-segment-worklog-source / -anchor keep data-msg-idx but are
    display:none, so their rects are zero. Recovery must skip them before
    comparing edges, or it scrolls toward an invisible row and stays blank."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let deletes = [];
let renderCalls = [];
const _sessionHtmlCache = { delete(sid){ deletes.push(sid); } };
let _sessionHtmlCacheSid = 'sid-123';
const S = { session: { session_id: 'sid-123' } };
let _messageVirtualWindowKey = 'stale';
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _lastScrollTop = 0;
let _messageVirtualBlankClampAttempts = 0;
// Module state the extracted clamp / compensation wrapper touch.
let _blankViewportRecoveryToken = 0;
let _blankViewportRecoveryApplied = 0;
let _messageJumpScrollOwner = null;
const performance = { now: () => 1000 };
function _freshProgrammaticScrollActive(){ return false; }
function _deferClearProgrammaticScroll(){}
function _messageViewportIntersectsRenderedRow(){ return false; }
function renderMessages(options){ renderCalls.push(options); }
// Viewport (1000..1600) sits in the head/tail gap. The hidden segment row is
// NEARER (viewport y=950) than the real rendered row (viewport y=1700).
const container = {
  scrollTop: 1000,
  clientHeight: 600,
  scrollHeight: 4000,
  getBoundingClientRect(){ return { top: 0, bottom: 600 }; },
  querySelectorAll(){ return [
    { getClientRects(){ return []; },
      getBoundingClientRect(){ return { top: 950, bottom: 950 }; } },
    { getClientRects(){ return [{ width: 100, height: 300 }]; },
      getBoundingClientRect(){ return { top: 1700, bottom: 2000 }; } },
  ]; },
};
function $(id){ return id === 'messages' ? container : null; }
eval(extractFunc('_captureMessageRecoveryOwnership'));
eval(extractFunc('_clampVirtualizedBlankViewportToRenderedEdge'));
eval(extractFunc('_maybeRecoverVirtualizedBlankViewport'));
const recovered = _maybeRecoverVirtualizedBlankViewport({}, true, { virtualized: true }, null);
console.log(JSON.stringify({ recovered, scrollTop: container.scrollTop, renderCalls }));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["recovered"] is True
    # Rects are lifted from viewport space into document space (rowTop = rect.top
    # + scrollTop), so the visible row's top edge is 1700+1000=2700 and the
    # clamp lands at 2700 - 0.5*600. The hidden row (zero rect, doc y=1950) sits
    # NEARER to the viewport bottom and would win at 1950-300=1650 if it were not
    # skipped — that is the regression this pins.
    assert metrics["scrollTop"] == 2400
    assert metrics["renderCalls"] == [{"preserveScroll": True}]


def test_measurement_compensation_preserves_the_blank_viewport_recovery():
    """The clamp writes scrollTop from INSIDE a render that
    _compensateScrollForMeasurementDelta wraps. Without a recovery flag the
    wrapper reads its own deliberate jump as a measurement delta and writes
    scrollTopBefore + delta over it, putting the reader back in the blank gap."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let deletes = [];
const _sessionHtmlCache = { delete(sid){ deletes.push(sid); } };
let _sessionHtmlCacheSid = 'sid-123';
const S = { session: { session_id: 'sid-123' } };
let _messageVirtualWindowKey = 'stale';
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _lastScrollTop = 0;
let _messageVirtualBlankClampAttempts = 0;
// Module state the extracted clamp / compensation wrapper touch.
let _blankViewportRecoveryToken = 0;
let _blankViewportRecoveryApplied = 0;
let _messageJumpScrollOwner = null;
const performance = { now: () => 1000 };
function _freshProgrammaticScrollActive(){ return false; }
function _deferClearProgrammaticScroll(){}
function _messageViewportIntersectsRenderedRow(){ return false; }
function renderMessages(){}
// Rows live at fixed DOCUMENT positions; their rects move with scrollTop, so
// the anchor delta the wrapper computes reflects the clamp's own jump.
const ROWS = [
  { docTop: 4800, docBottom: 5300 },
  { docTop: 5300, docBottom: 6800 },
];
const container = {
  scrollTop: 800,
  clientHeight: 600,
  scrollHeight: 20000,
  classList: { add(){}, remove(){} },
  getBoundingClientRect(){ return { top: 0, bottom: 600 }; },
  querySelectorAll(sel){
    if(String(sel).indexOf('spacer')>=0) return [];
    if(String(sel).indexOf('msg-idx')>=0){
      return ROWS.map(r => ({
        getClientRects(){ return [{ width: 100, height: 100 }]; },
        getBoundingClientRect(){
          return { top: r.docTop - container.scrollTop, bottom: r.docBottom - container.scrollTop };
        },
      }));
    }
    return [];
  },
  querySelector(sel){
    const m = /data-msg-idx="([0-9]+)"/.exec(String(sel));
    if(!m) return null;
    const r = ROWS[Number(m[1])];
    if(!r) return null;
    return {
      getBoundingClientRect(){
        return { top: r.docTop - container.scrollTop, bottom: r.docBottom - container.scrollTop };
      },
    };
  },
};
function $(id){ return id === 'messages' ? container : null; }
// Faithful stand-in for the real capture: the first row whose rect extends past
// the container top, with its offset relative to the container.
function _captureMessageViewportAnchor(){
  const containerRect = container.getBoundingClientRect();
  for (let i = 0; i < ROWS.length; i++) {
    const r = ROWS[i];
    const top = r.docTop - container.scrollTop;
    const bottom = r.docBottom - container.scrollTop;
    if (bottom > containerRect.top + 1) {
      return {
        rawIdx: i, sessionIdx: i, key: '', topOffset: top - containerRect.top,
        topPadBefore: 0, scrollHeightAtCapture: container.scrollHeight, inputGeneration: 0,
      };
    }
  }
  return null;
}
eval(extractFunc('_captureMessageRecoveryOwnership'));
eval(extractFunc('_clampVirtualizedBlankViewportToRenderedEdge'));
eval(extractFunc('_maybeRecoverVirtualizedBlankViewport'));
eval(extractFunc('_compensateScrollForMeasurementDelta'));

const scrollTopBefore = container.scrollTop;
_compensateScrollForMeasurementDelta(() => {
  _maybeRecoverVirtualizedBlankViewport({}, true, { virtualized: true }, null);
});
// The clamp settled on the nearest rendered edge...
const recoveredEdge = 4500;
console.log(JSON.stringify({
  scrollTopBefore,
  recoveredEdge,
  afterFirstRender: container.scrollTop,
  // A LATER render, unrelated to the recovery, must still compensate normally:
  // the row above the anchor genuinely grew by 120px during that render.
  afterSecondRender: (function(){
    _compensateScrollForMeasurementDelta(() => { ROWS[0].docTop += 120; });
    return container.scrollTop;
  })(),
  // The one-render stamp must not leak: after a render that throws, the next
  // render still compensates.
  afterThrowingRender: (function(){
    try { _compensateScrollForMeasurementDelta(() => { throw new Error('boom'); }); }
    catch(_){}
    const before = container.scrollTop;
    _compensateScrollForMeasurementDelta(() => { ROWS[0].docTop += 90; });
    return container.scrollTop !== before;
  })(),
}));
"""
    metrics = json.loads(_run_node(source))
    # The wrapper must not overwrite the deliberate recovery.
    assert metrics["afterFirstRender"] == metrics["recoveredEdge"]
    # Compensation is intact for renders that did not recover.
    assert metrics["afterSecondRender"] != metrics["afterFirstRender"]


def test_role_calibration_samples_shift_keys_when_history_is_prepended():
    """rawIdx is positional over S.messages, so prepending older history shifts
    every key. The height cache is shifted for exactly this reason
    (_syncMessageVirtualHeightCache); the per-role samples must shift with it or
    the same rows get counted twice toward the freeze."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _calibration_block(js) + """
eval(extractFunc('_shiftMessageVirtualRoleSamples'));
eval(extractFunc('_recordMessageVirtualRoleMeasurement'));
eval(extractFunc('_messageVirtualCalibratedRoleHeight'));
// Ten distinct tool rows measured at their original rawIdx.
for (let i = 0; i < 10; i++) _recordMessageVirtualRoleMeasurement('tool_call', i, 150);
// Two older rows are prepended, so rawIdx 0..9 become 2..11 and the two NEW
// rows occupy rawIdx 0 and 1 (mirrors nextHeights[prependedCount+i]).
_shiftMessageVirtualRoleSamples(
  [{ rawIdx: 0 }, { rawIdx: 1 }, { rawIdx: 2 }, { rawIdx: 3 }, { rawIdx: 4 },
   { rawIdx: 5 }, { rawIdx: 6 }, { rawIdx: 7 }, { rawIdx: 8 }, { rawIdx: 9 }],
  [{ rawIdx: 0 }, { rawIdx: 1 }, { rawIdx: 2 }, { rawIdx: 3 }, { rawIdx: 4 },
   { rawIdx: 5 }, { rawIdx: 6 }, { rawIdx: 7 }, { rawIdx: 8 }, { rawIdx: 9 },
   { rawIdx: 10 }, { rawIdx: 11 }],
  2,
);
// Re-measuring the SAME ten rows at their NEW indices must be a no-op: the
// sample count stays 10, so ten more distinct rows are still needed to freeze.
for (let i = 2; i < 12; i++) _recordMessageVirtualRoleMeasurement('tool_call', i, 150);
const afterRemeasure = _messageVirtualCalibratedRoleHeight('tool_call');
const rows = _messageVirtualRoleHeightSamples.tool_call.rows;
console.log(JSON.stringify({
  afterRemeasure,
  distinctRows: rows.size,
  keys: Array.from(rows.keys()).sort((a, b) => a - b),
}));
"""
    metrics = json.loads(_run_node(source))
    # 10 distinct samples of 150 shrunk toward the 400px prior, not 20 samples
    # of the same ten rows (which would have frozen the mean at 150).
    assert metrics["distinctRows"] == 10
    assert metrics["keys"] == list(range(2, 12))
    # Still the 5-prior shrinkage over exactly TEN samples: (10*150 + 5*400)/15
    # = 233. Had the shift failed and the re-measure added a twelfth row it would
    # be (12*150 + 5*400)/17 = 223.
    assert metrics["afterRemeasure"] == 233


def test_internal_measurement_render_is_deferred_while_a_jump_owns_the_viewport():
    """jumpToTurnQuestion smooth-scrolls to an answer row the render window does
    not contain yet. A queued measurement render replaces that smooth-scroll
    target with its own anchor restore, stranding the answer above the viewport.
    The measurement chain must be deferred for the duration of the jump."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _calibration_block(js) + """
let scheduled = [];
const MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS = 12;
let _messageVirtualMeasurementCycleKey = '';
let _messageVirtualMeasurementSeenKeys = [];
let _messageVirtualMeasurementBurstActive = false;
let _messageVirtualScrollActive = false;
let _messageVirtualScrollSettleTimer = 0;
let _messageVirtualDeferredMeasurement = null;
let _messageJumpScrollOwner = { generation: 7, container: {}, sessionId: 'sid-123' };
let _resetBurstCalls = 0;
function requestAnimationFrame(fn){ fn(); }
function _resetMessageVirtualMeasurementBurst(){ _resetBurstCalls++; }
eval(extractFunc('_messageVirtualMeasurementCycleKeyFor'));
eval(extractFunc('_messageJumpScrollOwnsViewport'));
eval(extractFunc('_scheduleMessageVirtualMeasurementRefresh'));
// Stub the render scheduler: the whole point is that NOTHING is scheduled while
// the jump owns the viewport.
function _scheduleMessageVirtualizedRender(force, request){ scheduled.push(request); }
const metrics = { virtualized: true, start: 0, end: 50, tailStart: 0 };
_scheduleMessageVirtualMeasurementRefresh(metrics);
console.log(JSON.stringify({
  scheduled,
  deferredHeld: _messageVirtualDeferredMeasurement === metrics,
  burstCalls: _resetBurstCalls,
}));
"""
    metrics = json.loads(_run_node(source))
    # Nothing scheduled while the jump owns the viewport; the refresh is held.
    assert metrics["scheduled"] == []
    assert metrics["deferredHeld"] is True


def test_deferred_measurement_resumes_after_jump_settlement():
    """Deferral must not strand the measurement chain: when the jump finishes
    (or is cancelled), the held refresh is scheduled."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _calibration_block(js) + """
let scheduled = [];
const MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS = 12;
let _messageVirtualMeasurementCycleKey = '';
let _messageVirtualMeasurementSeenKeys = [];
let _messageVirtualMeasurementBurstActive = false;
let _messageVirtualScrollActive = false;
// The jump settle timer is a DIFFERENT module variable from the measurement
// settle timer above (_messageJumpScrollSettleTimer); both settle timers exist.
let _messageVirtualScrollSettleTimer = 0;
let _messageJumpScrollSettleTimer = 0;
let _messageVirtualDeferredMeasurement = null;
let _messageJumpScrollOwner = { generation: 7, container: {}, sessionId: 'sid-123' };
let _messageJumpScrollGeneration = 7;
function requestAnimationFrame(fn){ fn(); }
function _resetMessageVirtualMeasurementBurst(){ _messageVirtualMeasurementSeenKeys = []; }
function clearTimeout(){}
const _scrollPinned = true;
const _messageUserUnpinned = false;
const _nearBottomCount = 2;
const _lastScrollTop = 0;
const _lastMessageClientHeight = 600;
const _newMessageCueVisible = false;
function _syncScrollToBottomCue(){}
function _updateSessionStartJumpButton(){}
function _flushDeferredActiveSessionExternalRefresh(){}
eval(extractFunc('_messageJumpSessionId'));
eval(extractFunc('_messageVirtualMeasurementCycleKeyFor'));
eval(extractFunc('_messageJumpScrollOwnsViewport'));
eval(extractFunc('_resumeDeferredMessageVirtualMeasurement'));
eval(extractFunc('_scheduleMessageVirtualMeasurementRefresh'));
eval(extractFunc('_finishMessageJumpScroll'));
eval(extractFunc('_cancelMessageJumpScroll'));
// Stub the render scheduler so a flushed refresh is observable.
function _scheduleMessageVirtualizedRender(force, request){ scheduled.push(request); }

const metrics = { virtualized: true, start: 0, end: 50, tailStart: 0 };
// Held by the active jump owner.
_scheduleMessageVirtualMeasurementRefresh(metrics);
const scheduledDuringJump = scheduled.length;
// Settlement clears the owner and must flush the held refresh.
_messageJumpScrollOwner.container = { scrollHeight: 20000, clientHeight: 600, scrollTop: 4500 };
_finishMessageJumpScroll(7);
// And a cancelled jump must not strand it either.
_scheduleMessageVirtualMeasurementRefresh(metrics);
_cancelMessageJumpScroll();
console.log(JSON.stringify({
  scheduledDuringJump,
  scheduledTotal: scheduled.length,
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["scheduledDuringJump"] == 0
    # One flush from settlement + one from the cancel path.
    assert metrics["scheduledTotal"] == 2


def test_role_calibration_freeze_requires_distinct_measured_rows():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _calibration_block(js) + """
eval(extractFunc('_recordMessageVirtualRoleMeasurement'));
eval(extractFunc('_messageVirtualCalibratedRoleHeight'));
// The measure pass re-measures the SAME mounted row on every render: repeating
// one row must never freeze the role's estimate.
for (let i = 0; i < 40; i++) _recordMessageVirtualRoleMeasurement('tool_call', 0, 150);
const repeatedOneRow = _messageVirtualCalibratedRoleHeight('tool_call');
_recordMessageVirtualRoleMeasurement('tool_call', 1, 200);
const afterNewRow = _messageVirtualCalibratedRoleHeight('tool_call');
console.log(JSON.stringify({ repeatedOneRow, afterNewRow }));
"""
    metrics = json.loads(_run_node(source))
    # Still the 5-prior shrinkage over ONE sample: (150 + 5*400)/6 = 358.
    assert metrics["repeatedOneRow"] == 358
    # A second distinct row moves the estimate: (150+200+5*400)/7 = 336.
    assert metrics["afterNewRow"] == 336
