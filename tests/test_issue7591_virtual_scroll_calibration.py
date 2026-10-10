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
  _recordMessageVirtualRoleMeasurement('tool_call', 150);
  early.push(_messageVirtualCalibratedRoleHeight('tool_call'));
}
const frozenSamples = [];
for (let i = 0; i < 40; i++) {
  _recordMessageVirtualRoleMeasurement('tool_call', 150);
}
const frozen = _messageVirtualCalibratedRoleHeight('tool_call');
// A later outlier must not move a frozen role mean.
_recordMessageVirtualRoleMeasurement('tool_call', 4000);
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
for (let i = 0; i < 25; i++) entries.push({ role: 'tool_call', height: 160 });
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
eval(extractFunc('_recordMessageVirtualRoleMeasurement'));
eval(extractFunc('_resetMessageVirtualRoleCalibration'));
eval(extractFunc('_messageVirtualCalibratedRoleHeight'));
eval(extractFunc('_clearMessageVirtualHeightCache'));
for (let i = 0; i < 25; i++) _recordMessageVirtualRoleMeasurement('tool_call', 150);
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
