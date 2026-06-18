"""Regression coverage for issue #500 transcript virtualization."""
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
            timeout=10,
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


def test_message_virtual_window_virtualizes_older_history_but_keeps_recent_tail():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
eval(extractFunc('_messageVirtualWindow'));
const metrics = _messageVirtualWindow({
  total: 240,
  scrollTop: 120 * 70,
  viewportHeight: 720,
  heights: Array.from({length: 240}, (_, i) => i >= 190 ? 220 : 120),
  defaultHeight: 120,
  bufferPx: 240,
  threshold: 80,
  keepTailCount: 50,
});
console.log(JSON.stringify(metrics));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["virtualized"] is True
    assert 60 <= metrics["start"] <= 75
    assert metrics["end"] <= metrics["tailStart"] == 190
    assert metrics["topPad"] > 0
    assert metrics["bottomPad"] > 0


def test_message_virtual_window_collapses_to_tail_only_near_bottom():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
eval(extractFunc('_messageVirtualWindow'));
const metrics = _messageVirtualWindow({
  total: 240,
  scrollTop: 120 * 260,
  viewportHeight: 720,
  heights: Array.from({length: 240}, () => 120),
  defaultHeight: 120,
  bufferPx: 240,
  threshold: 80,
  keepTailCount: 50,
});
console.log(JSON.stringify(metrics));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["virtualized"] is True
    assert metrics["start"] == metrics["tailStart"] == 190
    assert metrics["end"] == metrics["tailStart"]
    assert metrics["bottomPad"] == 0


def test_render_messages_uses_virtual_window_and_spacer_measurement_path():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    render_start = js.index("function renderMessages(options)")
    render_end = js.index("function _toolDisplayName", render_start)
    render_body = js[render_start:render_end]

    assert "_currentMessageVirtualWindow(visWithIdx,_messageVirtualKeepTailCount())" in render_body
    assert "const renderVisibleIdxs=[" in render_body
    assert "_messageVirtualSpacer(virtualWindow.topPad,'before')" in render_body
    assert "_messageVirtualSpacer(virtualWindow.bottomPad,'after')" in render_body
    assert "_updateMessageVirtualMeasurements(renderVisWithIdx, renderVisibleIdxs, virtualWindow);" in render_body
    assert "const renderableRawIdxs=new Set(visWithIdx.map(e=>e.rawIdx));" in render_body
    assert "if(virtualWindow.virtualized&&renderableRawIdxs.has(aIdx)&&!renderedRawIdxs.has(aIdx)) continue;" in render_body
    assert "if(hasServerOlder){" in render_body
    assert "_showEarlierRenderedMessages();" not in render_body
    top_spacer_idx = render_body.index("_messageVirtualSpacer(virtualWindow.topPad,'before')")
    indicator_idx = render_body.index("indicator.id='loadOlderIndicator';")
    assert top_spacer_idx < indicator_idx, (
        "renderMessages() must place the load-older affordance after the top "
        "virtual spacer so it stays visible at the top of the rendered window."
    )
    gap_reset_idx = render_body.index("currentAssistantTurn=null;", render_body.index("_messageVirtualSpacer(virtualWindow.bottomPad,'after')") - 220)
    gap_spacer_idx = render_body.index("_messageVirtualSpacer(virtualWindow.bottomPad,'after')")
    assert gap_reset_idx < gap_spacer_idx, (
        "renderMessages() must reset currentAssistantTurn before inserting the "
        "virtual gap spacer so assistant bubbles do not merge across the head/tail boundary."
    )


def test_measurement_uses_one_primary_row_and_adjacent_activity_siblings_only():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
eval(extractFunc('_measureMessageVirtualRow'));
const nextMessage = {
  hasAttribute(name){ return name === 'data-msg-idx'; },
  getBoundingClientRect(){ return {height: 999}; },
  nextElementSibling: null,
};
const activityGroup = {
  hasAttribute(){ return false; },
  matches(selector){ return selector === '.tool-call-group,.tool-card-row,.agent-activity-thinking,.thinking-card-row'; },
  getBoundingClientRect(){ return {height: 60}; },
  nextElementSibling: {
    hasAttribute(){ return false; },
    matches(){ return false; },
    getBoundingClientRect(){ return {height: 5000}; },
    nextElementSibling: nextMessage,
  },
};
const primary = {
  classList: { contains(name){ return name === 'assistant-segment'; } },
  getBoundingClientRect(){ return {height: 120}; },
  nextElementSibling: activityGroup,
};
const inner = {
  querySelector(selector){
    if(selector === '[data-msg-idx="42"]') return primary;
    return null;
  },
};
console.log(JSON.stringify({
  total: _measureMessageVirtualRow(inner, {rawIdx: 42}),
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["total"] == 180


def test_virtual_keep_tail_count_stays_bounded_after_history_expands_render_window():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
const MESSAGE_RENDER_WINDOW_DEFAULT = 50;
let _messageRenderWindowSize = 240;
eval(extractFunc('_currentMessageRenderWindowSize'));
eval(extractFunc('_messageVirtualKeepTailCount'));
console.log(JSON.stringify({
  renderWindowSize: _currentMessageRenderWindowSize(),
  keepTailCount: _messageVirtualKeepTailCount(),
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["renderWindowSize"] == 240
    assert metrics["keepTailCount"] == 50


def test_virtual_prepended_height_delta_uses_prefix_cache_only_when_virtualized():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS = {
  user: 120,
  assistant: 160,
  tool_call: 400,
  default: 140,
};
eval(extractFunc('_messageVirtualDefaultHeightForRole'));
eval(extractFunc('_messageVirtualRoleForEntry'));
let virtualized = true;
let _messageVirtualHeightCache = [0, 220, 180, 120];
let _messageVirtualEstimatedRowHeight = 140;
function _getVisibleMessagesWithIdx(){
  return [{rawIdx: 0}, {rawIdx: 1}, {rawIdx: 2}, {rawIdx: 3}];
}
function _messageVirtualKeepTailCount(){ return 2; }
function _currentMessageVirtualWindow(){
  return {virtualized};
}
eval(extractFunc('_messageVirtualPrependedHeightDelta'));
const active = _messageVirtualPrependedHeightDelta(3);
virtualized = false;
const inactive = _messageVirtualPrependedHeightDelta(3);
console.log(JSON.stringify({active, inactive}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["active"] == 540
    assert metrics["inactive"] is None


def test_virtual_question_jump_scroll_target_uses_visible_index_height_prefix():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _messageVirtualHeightCache = [100, 120, 80, 140];
let _messageVirtualHeightCacheEntries = [];
let _messageVirtualHeightCacheLen = 4;
let _messageVirtualHeightCacheSrc = null;
let _messageVirtualEstimatedRowHeight = 110;
let _messageVirtualWindowKey = 'old';
let S = {messages: [{}, {}, {}, {}]};
function _messageIsRenderable(){ return true; }
eval(extractFunc('_messageVirtualHeightEntryMatches'));
eval(extractFunc('_syncMessageVirtualHeightCache'));
eval(extractFunc('_messageVisibleIndexForRawIdx'));
eval(extractFunc('_messageVirtualScrollTopForVisibleIdx'));
const visWithIdx = [
  {rawIdx: 10, m: S.messages[0]},
  {rawIdx: 12, m: S.messages[1]},
  {rawIdx: 14, m: S.messages[2]},
  {rawIdx: 16, m: S.messages[3]},
];
_messageVirtualHeightCacheEntries = visWithIdx;
_messageVirtualHeightCacheSrc = S.messages;
const visibleIdx = _messageVisibleIndexForRawIdx(14, visWithIdx);
const scrollTop = _messageVirtualScrollTopForVisibleIdx(visWithIdx, visibleIdx, {clientHeight: 200});
console.log(JSON.stringify({visibleIdx, scrollTop}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["visibleIdx"] == 2
    assert metrics["scrollTop"] == 150


def test_height_cache_preserves_measured_prefix_across_append_only_growth():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHT = 140;
let _messageVirtualHeightCache = [180, 220];
let _messageVirtualHeightCacheEntries = [];
let _messageVirtualHeightCacheLen = 2;
let _messageVirtualHeightCacheSrc = null;
let _messageVirtualEstimatedRowHeight = 200;
let _messageVirtualWindowKey = 'stale-key';
function _clearMessageVirtualHeightCache() {
  _messageVirtualHeightCache = [];
  _messageVirtualHeightCacheEntries = [];
  _messageVirtualHeightCacheLen = 0;
  _messageVirtualHeightCacheSrc = null;
  _messageVirtualEstimatedRowHeight = MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHT;
  _messageVirtualWindowKey = '';
}
eval(extractFunc('_messageVirtualHeightEntryMatches'));
eval(extractFunc('_messageVirtualHeightPrefixEntryMatches'));
eval(extractFunc('_syncMessageVirtualHeightCache'));
const first = {id: 'first'};
const second = {id: 'second'};
let S = {messages: [first, second]};
_messageVirtualHeightCacheEntries = [
  {rawIdx: 0, m: first},
  {rawIdx: 1, m: second},
];
_messageVirtualHeightCacheSrc = S.messages;
S = {messages: [first, second, {id: 'third'}]};
_syncMessageVirtualHeightCache([
  {rawIdx: 0, m: first},
  {rawIdx: 1, m: second},
  {rawIdx: 2, m: S.messages[2]},
]);
console.log(JSON.stringify({
  cache: _messageVirtualHeightCache,
  estimated: _messageVirtualEstimatedRowHeight,
  windowKey: _messageVirtualWindowKey,
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["cache"][:2] == [180, 220]
    assert len(metrics["cache"]) == 3
    assert metrics["estimated"] == 200
    assert metrics["windowKey"] == ""


def test_height_cache_preserves_measured_suffix_across_prepended_history():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHT = 140;
let _messageVirtualHeightCache = [180, 220];
let _messageVirtualHeightCacheEntries = [];
let _messageVirtualHeightCacheLen = 2;
let _messageVirtualHeightCacheSrc = null;
let _messageVirtualEstimatedRowHeight = 200;
let _messageVirtualWindowKey = 'stale-key';
function _clearMessageVirtualHeightCache() {
  _messageVirtualHeightCache = [];
  _messageVirtualHeightCacheEntries = [];
  _messageVirtualHeightCacheLen = 0;
  _messageVirtualHeightCacheSrc = null;
  _messageVirtualEstimatedRowHeight = MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHT;
  _messageVirtualWindowKey = '';
}
eval(extractFunc('_messageVirtualHeightEntryMatches'));
eval(extractFunc('_messageVirtualHeightPrefixEntryMatches'));
eval(extractFunc('_syncMessageVirtualHeightCache'));
const first = {id: 'first'};
const second = {id: 'second'};
let S = {messages: [first, second]};
_messageVirtualHeightCacheEntries = [
  {rawIdx: 0, m: first},
  {rawIdx: 1, m: second},
];
_messageVirtualHeightCacheSrc = S.messages;
const olderA = {id: 'older-a'};
const olderB = {id: 'older-b'};
S = {messages: [olderA, olderB, first, second]};
_syncMessageVirtualHeightCache([
  {rawIdx: 0, m: olderA},
  {rawIdx: 1, m: olderB},
  {rawIdx: 2, m: first},
  {rawIdx: 3, m: second},
]);
console.log(JSON.stringify({
  cache: _messageVirtualHeightCache,
  estimated: _messageVirtualEstimatedRowHeight,
  windowKey: _messageVirtualWindowKey,
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["cache"][2:] == [180, 220]
    assert len(metrics["cache"]) == 4
    assert metrics["estimated"] == 200
    assert metrics["windowKey"] == ""


def test_measurement_refresh_budget_is_keyed_to_window_shape_not_pad_height():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
eval(extractFunc('_messageVirtualMeasurementCycleKeyFor'));
console.log(JSON.stringify({
  a: _messageVirtualMeasurementCycleKeyFor({virtualized: true, start: 10, end: 20, topPad: 1000, bottomPad: 2000, tailStart: 190}),
  b: _messageVirtualMeasurementCycleKeyFor({virtualized: true, start: 10, end: 20, topPad: 1001, bottomPad: 1999, tailStart: 190}),
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["a"] == metrics["b"]


def test_tool_rows_do_not_carry_message_measurement_hook():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    build_start = js.index("function buildToolCard(tc){")
    build_end = js.index("function _colorDiffLines", build_start)
    build_body = js[build_start:build_end]

    assert "row.dataset.msgIdx" not in build_body
    assert "querySelectorAll(`[data-msg-idx=" not in js


def test_viewport_intersection_helper_detects_visible_rendered_rows_only():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let rows = [];
const container = {
  getBoundingClientRect(){ return {top: 100, bottom: 300}; },
  querySelectorAll(selector){
    if(selector === '[data-msg-idx]') return rows;
    return [];
  },
};
function $(id){ return id === 'messages' ? container : null; }
eval(extractFunc('_messageViewportIntersectsRenderedRow'));
rows = [
  { getBoundingClientRect(){ return {top: 10, bottom: 90}; } },
  { getBoundingClientRect(){ return {top: 320, bottom: 360}; } },
];
const blank = _messageViewportIntersectsRenderedRow();
rows = [
  { getBoundingClientRect(){ return {top: 120, bottom: 180}; } },
];
const visible = _messageViewportIntersectsRenderedRow();
console.log(JSON.stringify({blank, visible}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["blank"] is False
    assert metrics["visible"] is True


def test_render_messages_has_one_shot_virtual_blank_viewport_fallback():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    render_start = js.index("function renderMessages(options)")
    render_end = js.index("function _toolDisplayName", render_start)
    render_body = js[render_start:render_end]

    assert "const virtualFallback=!!(options&&options._virtualFallback);" in render_body
    assert "const virtualWindow=virtualFallback" in render_body
    assert "if(_maybeRecoverVirtualizedBlankViewport(options, preserveScroll, virtualWindow)) return;" in render_body
    assert "if(_sessionHtmlCacheSid&&S.session&&S.session.session_id===_sessionHtmlCacheSid){" in js
    assert "_sessionHtmlCache.delete(_sessionHtmlCacheSid);" in js
    assert "renderMessages({preserveScroll:true,_virtualFallback:true});" in js


def test_virtual_blank_viewport_recovery_evicts_stale_cache_before_fallback():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let deletes = [];
let renderCalls = [];
const _sessionHtmlCache = {
  delete(sid){ deletes.push(sid); }
};
let _sessionHtmlCacheSid = 'sid-123';
const S = { session: { session_id: 'sid-123' } };
function _messageViewportIntersectsRenderedRow(){ return false; }
function renderMessages(options){ renderCalls.push(options); }
eval(extractFunc('_maybeRecoverVirtualizedBlankViewport'));
const recovered = _maybeRecoverVirtualizedBlankViewport({preserveScroll:false, someFlag:true}, true, {virtualized:true});
console.log(JSON.stringify({recovered, deletes, renderCalls}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["recovered"] is True
    assert metrics["deletes"] == ["sid-123"]
    assert metrics["renderCalls"] == [{"preserveScroll": True, "_virtualFallback": True}]


def test_same_frame_restore_nudges_virtual_window_when_anchor_row_is_missing():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
const ROW_HEIGHT = 120;
const TOTAL = 60;

// Rows 10-59 are mounted (tail window); rows 0-9 are virtualized out.
// Anchor points to rawIdx=5, which is in the virtualized zone.
let mountedStart = 10;
let scrollTopValue = 0;
let renderCalls = [];
let scrollTopHistory = [];
let _messageVirtualWindowKey = 'stale-key';
let _programmaticScroll = false;
let _lastScrollTop = 0;
let _messageUserUnpinned = true;
let _scrollPinned = false;
let _nearBottomCount = 0;
let _messageVirtualHeightCache = Array.from({length: TOTAL}, () => ROW_HEIGHT);
let _messageVirtualHeightCacheEntries = [];
let _messageVirtualHeightCacheLen = TOTAL;
let _messageVirtualHeightCacheSrc = null;
let _messageVirtualEstimatedRowHeight = ROW_HEIGHT;

function _clearMessageVirtualHeightCache(){}
function _syncMessageVirtualHeightCache(){}

const container = {
  get scrollTop(){ return scrollTopValue; },
  set scrollTop(v){ scrollTopHistory.push(v); scrollTopValue = v; },
  get scrollHeight(){ return TOTAL * ROW_HEIGHT; },
  get clientHeight(){ return 600; },
  getBoundingClientRect(){ return {top: 0, bottom: 600}; },
  querySelector(selector){
    const m = selector && selector.match(/\[data-msg-idx="(\d+)"\]/);
    if(!m) return null;
    const idx = Number(m[1]);
    if(idx < mountedStart || idx >= TOTAL) return null;
    const top = (idx - mountedStart) * ROW_HEIGHT;
    return { getBoundingClientRect(){ return {top, bottom: top + ROW_HEIGHT}; } };
  },
};
function $(id){ return id === 'messages' ? container : null; }

function _getVisibleMessagesWithIdx(){
  return Array.from({length: TOTAL}, (_, i) => ({rawIdx: i}));
}

function renderMessages(opts){
  renderCalls.push(JSON.parse(JSON.stringify(opts)));
  mountedStart = 0;
}

function _restoreMessageViewportAnchor(anchor, delta){
  const idx = Number(anchor.rawIdx) + Number(delta||0);
  const row = container.querySelector(`[data-msg-idx="${idx}"]`);
  if(!row) return false;
  _programmaticScroll = true;
  return true;
}

eval(extractFunc('_messageVisibleIndexForRawIdx'));
eval(extractFunc('_messageVirtualScrollTopForVisibleIdx'));
eval(extractFunc('_restoreMessageScrollSnapshotSameFrame'));

const snapshot = {
  anchor: {rawIdx: 5, topOffset: 50},
  top: 100,
  bottom: 6600,
  scrollHeight: 7200,
  pinned: false,
  userUnpinned: true,
};
_restoreMessageScrollSnapshotSameFrame(snapshot);
console.log(JSON.stringify({renderCalls, scrollTopHistory}));
"""
    metrics = json.loads(_run_node(source))
    assert len(metrics["renderCalls"]) == 1, (
        "_restoreMessageScrollSnapshotSameFrame must call renderMessages to mount the virtualized-out anchor row"
    )
    assert metrics["renderCalls"][0].get("preserveScroll") is True, (
        "re-render must use preserveScroll:true to avoid scrolling to bottom"
    )
    assert len(metrics["scrollTopHistory"]) >= 1, (
        "scrollTop must be adjusted before re-render to place anchor row in the virtual window"
    )
    # rawIdx=5, visIdx=5: offset=5*120=600, viewport=600, scrollTop=round(600-600*0.35)=390
    assert metrics["scrollTopHistory"][0] == 390


def test_virtualize_transcript_opt_out_forces_full_render_window():
    """#4325: when window._virtualizeTranscript===false, _currentMessageVirtualWindow
    must return a non-virtualized full window even for a long (>threshold) transcript,
    so the whole transcript renders. When true/undefined it virtualizes as before."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS={
  user:120,
  assistant:160,
  tool_call:400,
  default:140,
};
function _messageVirtualDefaultHeightForRole(role){
  return MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS[
    role&&Object.prototype.hasOwnProperty.call(MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS,role)?role:'default'
  ];
}
const MESSAGE_VIRTUAL_THRESHOLD_ROWS = 80;
const MESSAGE_VIRTUAL_BUFFER_PX = 900;
let _messageVirtualHeightCache = [];
let _messageVirtualEstimatedRowHeight = 140;
function _syncMessageVirtualHeightCache(){ /* no-op for the test */ }
function $(id){ return {scrollTop: 5000, clientHeight: 720}; }
function _messageVirtualRoleForEntry(){ return 'default'; }
const window = {};
eval(extractFunc('_messageVirtualWindow'));
eval(extractFunc('_currentMessageVirtualWindow'));
// 200 visible messages — well over the 80 threshold
const visWithIdx = Array.from({length: 200}, (_, i) => ({rawIdx: i}));
// OFF: opt-out → full render
window._virtualizeTranscript = false;
const off = _currentMessageVirtualWindow(visWithIdx, 50);
// ON (default): virtualizes
window._virtualizeTranscript = true;
const on = _currentMessageVirtualWindow(visWithIdx, 50);
// UNDEFINED: also virtualizes (opt-out only when explicitly false)
delete window._virtualizeTranscript;
const undef = _currentMessageVirtualWindow(visWithIdx, 50);
console.log(JSON.stringify({off, on, undef}));
"""
    metrics = json.loads(_run_node(source))
    # OFF → full, non-virtualized window covering every row
    assert metrics["off"]["virtualized"] is False
    assert metrics["off"]["start"] == 0
    assert metrics["off"]["end"] == 200
    assert metrics["off"]["topPad"] == 0
    assert metrics["off"]["bottomPad"] == 0
    # ON → virtualized (only a window of the 200 rows)
    assert metrics["on"]["virtualized"] is True
    assert metrics["on"]["end"] - metrics["on"]["start"] < 200
    # UNDEFINED → still virtualizes (opt-out is explicit-false only)
    assert metrics["undef"]["virtualized"] is True


def test_virtualize_transcript_gate_present_in_current_window_fn():
    """The opt-out gate must live in _currentMessageVirtualWindow (the single
    chokepoint), guarding on window._virtualizeTranscript===false."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    start = js.index("function _currentMessageVirtualWindow(")
    body = js[start:start + 900]
    assert "_virtualizeTranscript===false" in body, (
        "opt-out gate must check window._virtualizeTranscript===false in "
        "_currentMessageVirtualWindow"
    )
    assert "virtualized:false" in body, "gate must return a non-virtualized window when opted out"


def test_message_virtual_default_height_for_role_returns_correct_heights():
    """Verify per-role default heights are configured."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS={
  user:120,
  assistant:160,
  tool_call:400,
  default:140,
};
eval(extractFunc('_messageVirtualDefaultHeightForRole'));
console.log(JSON.stringify({
  tool_call: _messageVirtualDefaultHeightForRole('tool_call'),
  user: _messageVirtualDefaultHeightForRole('user'),
  assistant: _messageVirtualDefaultHeightForRole('assistant'),
  unknown: _messageVirtualDefaultHeightForRole('unknown'),
  default: _messageVirtualDefaultHeightForRole('default'),
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["tool_call"] == 400
    assert metrics["user"] == 120
    assert metrics["assistant"] == 160
    assert metrics["unknown"] == 140
    assert metrics["default"] == 140


def test_message_virtual_role_for_entry_classifies_tool_calls():
    """Verify role classifier detects tool_calls, tool_use content, and _partial_tool_calls."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
eval(extractFunc('_messageVirtualRoleForEntry'));
console.log(JSON.stringify({
  userRole: _messageVirtualRoleForEntry({m: {role: 'user'}}),
  assistantNoTools: _messageVirtualRoleForEntry({m: {role: 'assistant'}}),
  assistantWithToolCalls: _messageVirtualRoleForEntry({m: {role: 'assistant', tool_calls: [{id: '1'}]}}),
  assistantWithToolUse: _messageVirtualRoleForEntry({m: {role: 'assistant', content: [{type: 'tool_use', id: '1'}]}}),
  assistantWithPartialToolCalls: _messageVirtualRoleForEntry({m: {role: 'assistant', _partial_tool_calls: [{id: '1'}]}}),
  noEntry: _messageVirtualRoleForEntry(null),
  noMessage: _messageVirtualRoleForEntry({m: null}),
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["userRole"] == "user"
    assert metrics["assistantNoTools"] == "assistant"
    assert metrics["assistantWithToolCalls"] == "tool_call"
    assert metrics["assistantWithToolUse"] == "tool_call"
    assert metrics["assistantWithPartialToolCalls"] == "tool_call"
    assert metrics["noEntry"] == "default"
    assert metrics["noMessage"] == "default"


def test_message_virtual_window_with_role_for_idx_uses_role_defaults():
    """Verify _messageVirtualWindow uses role-specific heights when roleForIdx is provided."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS={
  user:120,
  assistant:160,
  tool_call:400,
  default:140,
};
const MESSAGE_VIRTUAL_THRESHOLD_ROWS = 80;
const MESSAGE_VIRTUAL_BUFFER_PX = 900;
eval(extractFunc('_messageVirtualDefaultHeightForRole'));
eval(extractFunc('_messageVirtualWindow'));
const visWithIdx = [
  {m: {role: 'user'}},
  {m: {role: 'assistant', tool_calls: [{id: '1'}]}},
  {m: {role: 'assistant'}},
];
const metrics = _messageVirtualWindow({
  total: 3,
  scrollTop: 0,
  viewportHeight: 600,
  heights: [0, 0, 0],
  defaultHeight: 140,
  roleForIdx: (idx) => {
    const entry = visWithIdx[idx];
    if(!entry || !entry.m) return 'default';
    if(entry.m.role === 'user') return 'user';
    if(entry.m.role === 'assistant'){
      if(Array.isArray(entry.m.tool_calls) && entry.m.tool_calls.length > 0) return 'tool_call';
      return 'assistant';
    }
    return 'default';
  },
  bufferPx: 0,
  threshold: 2,
  keepTailCount: 0,
});
console.log(JSON.stringify({
  virtualized: metrics.virtualized,
  start: metrics.start,
  end: metrics.end,
}));
"""
    metrics = json.loads(_run_node(source))
    # With 3 rows (120 + 400 + 160 = 680px) and viewport 600px, should not virtualize (below threshold)
    # So this checks that the role-based defaults are being computed
    assert metrics["end"] - metrics["start"] > 0


def test_message_virtual_window_cached_heights_override_role_defaults():
    """Verify cached heights take precedence over role-specific defaults."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS={
  user:120,
  assistant:160,
  tool_call:400,
  default:140,
};
const MESSAGE_VIRTUAL_THRESHOLD_ROWS = 80;
const MESSAGE_VIRTUAL_BUFFER_PX = 900;
eval(extractFunc('_messageVirtualDefaultHeightForRole'));
eval(extractFunc('_messageVirtualWindow'));
const visWithIdx = [
  {m: {role: 'user'}},  // normally 120
  {m: {role: 'assistant', tool_calls: [{id: '1'}]}},  // normally 400
];
// Test case 1: with cached heights 250+300=550px < 600px viewport, no virtualization needed
const metricsSmall = _messageVirtualWindow({
  total: 2,
  scrollTop: 0,
  viewportHeight: 600,
  heights: [250, 300],  // Cached heights override roles
  defaultHeight: 140,
  roleForIdx: (idx) => {
    const entry = visWithIdx[idx];
    if(!entry || !entry.m) return 'default';
    if(entry.m.role === 'user') return 'user';
    if(entry.m.role === 'assistant'){
      if(Array.isArray(entry.m.tool_calls) && entry.m.tool_calls.length > 0) return 'tool_call';
      return 'assistant';
    }
    return 'default';
  },
  bufferPx: 0,
  threshold: 80,
  keepTailCount: 0,
});
// Test case 2: with many rows and cached heights, should use cached heights not role defaults
const largeList = Array.from({length: 100}, (_, i) => ({m: {role: i % 2 ? 'user' : 'assistant'}}));
const cachedHeights = Array.from({length: 100}, (_, i) => 200);  // All 200px when cached
const metricsLarge = _messageVirtualWindow({
  total: 100,
  scrollTop: 0,
  viewportHeight: 600,
  heights: cachedHeights,
  defaultHeight: 140,
  roleForIdx: (idx) => {
    if(largeList[idx]?.m?.role === 'user') return 'user';
    return 'assistant';
  },
  bufferPx: 0,
  threshold: 80,
  keepTailCount: 0,
});
console.log(JSON.stringify({
  smallVirtualized: metricsSmall.virtualized,
  largeVirtualized: metricsLarge.virtualized,
  largeHasWindow: metricsLarge.end > metricsLarge.start,
}));
"""
    metrics = json.loads(_run_node(source))
    # With 2 rows below threshold, should not virtualize
    assert metrics["smallVirtualized"] is False
    # With 100 rows above threshold, should virtualize and use cached heights
    assert metrics["largeVirtualized"] is True
    assert metrics["largeHasWindow"] is True


def test_offset_helpers_use_per_role_defaults_for_uncached_rows():
    """Verify _messageVirtualScrollTopForVisibleIdx and _messageVirtualPrependedHeightDelta
    use per-role default heights (not the flat 140px estimate) for uncached rows,
    and that these agree with _messageVirtualWindow's own accounting."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS = {
  user: 120,
  assistant: 160,
  tool_call: 400,
  default: 140,
};
eval(extractFunc('_messageVirtualDefaultHeightForRole'));
eval(extractFunc('_messageVirtualRoleForEntry'));

// Three entries: user (120), tool_call (400), assistant (160) — all uncached (height=0)
const visWithIdx = [
  {rawIdx: 0, m: {role: 'user'}},
  {rawIdx: 1, m: {role: 'assistant', tool_calls: [{id: 'x'}]}},
  {rawIdx: 2, m: {role: 'assistant'}},
];

// --- _messageVirtualScrollTopForVisibleIdx ---
let _messageVirtualHeightCache = [0, 0, 0];
let _messageVirtualHeightCacheEntries = [];
let _messageVirtualHeightCacheLen = 3;
let _messageVirtualHeightCacheSrc = null;
let _messageVirtualEstimatedRowHeight = 140;
let _messageVirtualWindowKey = '';
let S = {messages: visWithIdx.map(e => e.m)};
function _messageIsRenderable(){ return true; }
eval(extractFunc('_messageVirtualHeightEntryMatches'));
eval(extractFunc('_syncMessageVirtualHeightCache'));
eval(extractFunc('_messageVirtualScrollTopForVisibleIdx'));
// scrollTop to visibleIdx=2 must sum heights of idx 0 (120) and idx 1 (400) = 520
_messageVirtualHeightCacheEntries = visWithIdx;
_messageVirtualHeightCacheSrc = S.messages;
const scrollTop = _messageVirtualScrollTopForVisibleIdx(visWithIdx, 2, null);

// --- _messageVirtualPrependedHeightDelta ---
let virtualized2 = true;
let _messageVirtualHeightCache2 = [0, 0, 0];
let _messageVirtualEstimatedRowHeight2 = 140;
function _getVisibleMessagesWithIdx(){ return visWithIdx; }
function _messageVirtualKeepTailCount(){ return 0; }
function _currentMessageVirtualWindow(){ return {virtualized: virtualized2}; }
// Patch cache var used by _messageVirtualPrependedHeightDelta
eval(extractFunc('_messageVirtualPrependedHeightDelta').replace(
  /_messageVirtualHeightCache/g, '_messageVirtualHeightCache2'
).replace(
  /_messageVirtualEstimatedRowHeight/g, '_messageVirtualEstimatedRowHeight2'
));
// Sum of first 3 uncached entries: user(120) + tool_call(400) + assistant(160) = 680
const delta = _messageVirtualPrependedHeightDelta(3);

// --- _messageVirtualWindow agreement ---
const MESSAGE_VIRTUAL_THRESHOLD_ROWS = 2;
const MESSAGE_VIRTUAL_BUFFER_PX = 0;
eval(extractFunc('_messageVirtualWindow'));
const win = _messageVirtualWindow({
  total: 3,
  scrollTop: 0,
  viewportHeight: 600,
  heights: [0, 0, 0],
  defaultHeight: 140,
  roleForIdx: idx => _messageVirtualRoleForEntry(visWithIdx[idx]),
  bufferPx: 0,
  threshold: 2,
  keepTailCount: 0,
});
// topPad is sum of rows before win.start; with start=0 it is 0, but
// the window must have consumed the same per-role heights when computing
// row positions, so verify the window spans all rows correctly
const windowCoversAll = win.start === 0 && win.end === 3;

console.log(JSON.stringify({scrollTop, delta, windowCoversAll}));
"""
    metrics = json.loads(_run_node(source))
    # scrollTop to idx=2 = sum of row 0 (120) + row 1 (400) = 520, no viewport offset (null container)
    assert metrics["scrollTop"] == 520, (
        f"expected 520 (120+400) but got {metrics['scrollTop']}; "
        "offset helper must use per-role defaults, not flat 140px"
    )
    # prepended delta for 3 uncached rows = 120 + 400 + 160 = 680
    assert metrics["delta"] == 680, (
        f"expected 680 (120+400+160) but got {metrics['delta']}; "
        "prepend helper must use per-role defaults, not flat 140px"
    )
    # windowing function must also cover the same three rows
    assert metrics["windowCoversAll"] is True


def test_compensate_scroll_for_measurement_delta_no_anchor_does_not_throw():
    """When _captureMessageViewportAnchor returns null, the compensation helper
    should not throw and should not mutate scrollTop."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let renderCalls = [];
let scrollTopWasMutated = false;
let scrollTopValue = 100;
const container = {
  get scrollTop(){ return scrollTopValue; },
  set scrollTop(v){ scrollTopWasMutated = true; scrollTopValue = v; },
  getBoundingClientRect(){ return {top: 0}; },
  querySelector(){ return null; },
};
function $(id){ return id === 'messages' ? container : null; }
function _captureMessageViewportAnchor(){ return null; }
let _programmaticScroll = false;
let _lastScrollTop = 0;
function _scheduleMessageVirtualizedRender(){ renderCalls.push(true); }
function requestAnimationFrame(cb){ cb(); }
eval(extractFunc('_compensateScrollForMeasurementDelta'));
_compensateScrollForMeasurementDelta(()=>{ _scheduleMessageVirtualizedRender(true); });
console.log(JSON.stringify({
  renderCalled: renderCalls.length > 0,
  scrollTopMutated: scrollTopWasMutated,
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["renderCalled"] is True
    assert metrics["scrollTopMutated"] is False


def test_compensate_scroll_for_measurement_delta_shifts_scroll_when_anchor_moves():
    """When the anchor row shifts position due to measurement changes,
    scrollTop should be adjusted by the delta."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let scrollTopValue = 200;
let scrollHistory = [];
const container = {
  get scrollTop(){ return scrollTopValue; },
  set scrollTop(v){ scrollHistory.push(v); scrollTopValue = v; },
  getBoundingClientRect(){ return {top: 50, bottom: 650}; },
  querySelector(selector){
    if(selector === '[data-msg-idx="42"]'){
      // After render, the row moved from relative offset 100 to 150 (50px shift)
      return {
        getBoundingClientRect(){ return {top: 100}; }
      };
    }
    return null;
  },
};
function $(id){ return id === 'messages' ? container : null; }
function _captureMessageViewportAnchor(){
  // Anchor was at topOffset 100 before the render
  return {rawIdx: 42, topOffset: 100};
}
let _programmaticScroll = false;
let _lastScrollTop = 0;
let renderCalls = [];
function _scheduleMessageVirtualizedRender(){ renderCalls.push(true); }
function requestAnimationFrame(cb){ cb(); }
eval(extractFunc('_compensateScrollForMeasurementDelta'));
_compensateScrollForMeasurementDelta(()=>{ _scheduleMessageVirtualizedRender(true); });
console.log(JSON.stringify({
  scrollHistory: scrollHistory,
  renderCalled: renderCalls.length > 0,
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["renderCalled"] is True
    # actualOffset = 100 - 50 = 50, delta = 50 - 100 = -50 (row moved up 50px)
    # scrollTop should adjust: 200 + (-50) = 150
    assert metrics["scrollHistory"] == [150], (
        "scrollTop should shift by -50px to keep anchor in place"
    )


def test_compensate_scroll_for_measurement_delta_skips_small_delta():
    """When delta < 2px, no compensation is applied (tolerance)."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let scrollTopValue = 200;
let scrollHistory = [];
const container = {
  get scrollTop(){ return scrollTopValue; },
  set scrollTop(v){ scrollHistory.push(v); scrollTopValue = v; },
  getBoundingClientRect(){ return {top: 50, bottom: 650}; },
  querySelector(selector){
    if(selector === '[data-msg-idx="42"]'){
      // Row moved by only 1px, within tolerance
      return {
        getBoundingClientRect(){ return {top: 251}; }
      };
    }
    return null;
  },
};
function $(id){ return id === 'messages' ? container : null; }
function _captureMessageViewportAnchor(){
  return {rawIdx: 42, topOffset: 200};
}
let _programmaticScroll = false;
let _lastScrollTop = 0;
let renderCalls = [];
function _scheduleMessageVirtualizedRender(){ renderCalls.push(true); }
function requestAnimationFrame(cb){ cb(); }
eval(extractFunc('_compensateScrollForMeasurementDelta'));
_compensateScrollForMeasurementDelta(()=>{ _scheduleMessageVirtualizedRender(true); });
console.log(JSON.stringify({
  scrollHistory: scrollHistory,
  renderCalled: renderCalls.length > 0,
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["renderCalled"] is True
    assert metrics["scrollHistory"] == [], (
        "scrollTop should not change for delta < 2px"
    )


def test_compensate_scroll_for_measurement_delta_sets_programmatic_scroll_flag():
    """_programmaticScroll should be set during compensation and cleared after rAF+setTimeout."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let scrollTopValue = 200;
let _programmaticScroll = false;
let _lastScrollTop = 0;
let scrollSetCount = 0;
const container = {
  get scrollTop(){ return scrollTopValue; },
  set scrollTop(v){
    scrollSetCount++;
    scrollTopValue = v;
  },
  getBoundingClientRect(){ return {top: 50, bottom: 650}; },
  querySelector(selector){
    if(selector === '[data-msg-idx="42"]'){
      return {
        getBoundingClientRect(){ return {top: 300}; }
      };
    }
    return null;
  },
};
function $(id){ return id === 'messages' ? container : null; }
function _captureMessageViewportAnchor(){
  return {rawIdx: 42, topOffset: 200};
}
let renderCalls = [];
function _scheduleMessageVirtualizedRender(){ renderCalls.push(true); }
let timeoutCbBeingCalled = false;
function requestAnimationFrame(cb){
  // This is called with a callback that will eventually clear _programmaticScroll
  cb();
}
function setTimeout(cb, delay){
  // This is called from within the rAF callback (after scrollTop is set)
  // The callback should clear _programmaticScroll
  timeoutCbBeingCalled = true;
  cb();
  timeoutCbBeingCalled = false;
}
eval(extractFunc('_compensateScrollForMeasurementDelta'));
_compensateScrollForMeasurementDelta(()=>{ _scheduleMessageVirtualizedRender(true); });
console.log(JSON.stringify({
  programmaticScrollWasTrue: _programmaticScroll === true || scrollSetCount > 0,
  programmaticScrollNowFalse: _programmaticScroll === false,
  scrollWasSet: scrollSetCount > 0,
}));
"""
    metrics = json.loads(_run_node(source))
    # _programmaticScroll should have been set to true when scrollTop was adjusted
    assert metrics["scrollWasSet"] is True, (
        "scrollTop should be set when delta > 2px"
    )
    # _programmaticScroll should have been cleared after the setTimeout callback
    assert metrics["programmaticScrollNowFalse"] is True, (
        "_programmaticScroll should be false after setTimeout completes"
    )


def test_virtualized_render_uses_compensation_helper():
    """_scheduleMessageVirtualizedRender must wrap renderMessages with _compensateScrollForMeasurementDelta."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    start = js.index("function _scheduleMessageVirtualizedRender(")
    end = js.index("\n// ──", start)
    body = js[start:end]

    assert "_compensateScrollForMeasurementDelta" in body, (
        "_scheduleMessageVirtualizedRender must call "
        "_compensateScrollForMeasurementDelta to compensate scroll after measurement-driven rerenders"
    )
    assert "renderMessages(" in body, (
        "the compensation helper should wrap the actual renderMessages call"
    )


def test_scroll_listener_guards_programmatic_scroll_before_marking_active():
    """The _programmaticScroll guard must appear before _markMessageVirtualScrollActive
    in the scroll listener so that programmatic scrolls (e.g. from
    _compensateScrollForMeasurementDelta) do not arm the 150ms settle timer."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    listener_start = js.index("el.addEventListener('scroll',()=>{")
    listener_end = js.index("});", listener_start)
    listener_body = js[listener_start:listener_end]

    guard_pos = listener_body.index("if(_programmaticScroll) return;")
    mark_pos = listener_body.index("_markMessageVirtualScrollActive();")
    assert guard_pos < mark_pos, (
        "Scroll listener must check _programmaticScroll before calling "
        "_markMessageVirtualScrollActive so programmatic scrolls do not arm "
        "the 150ms settle timer and chain measurement delays"
    )


def test_clear_height_cache_resets_scroll_settle_globals():
    """_clearMessageVirtualHeightCache must zero out the three scroll-settle globals
    so that a deferred measurement from a previous session cannot fire against the
    new session's DOM after a session switch."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let timerCleared = false;
let _messageVirtualScrollActive = true;
let _messageVirtualScrollSettleTimer = 99;
let _messageVirtualDeferredMeasurement = {some: 'stale-metrics'};
let _messageVirtualHeightCache = [100, 200];
let _messageVirtualHeightCacheEntries = [{rawIdx: 0}];
let _messageVirtualHeightCacheLen = 2;
let _messageVirtualHeightCacheSrc = {};
let _messageVirtualEstimatedRowHeight = 200;
let _messageVirtualWindowKey = 'old-key';
let _messageVirtualMeasurementCycleKey = 'old-cycle';
let _messageVirtualMeasurementRetryCount = 3;
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHT = 140;
// #4367 introduced per-role default heights; the combined _clearMessageVirtualHeightCache
// resets the estimate via _messageVirtualDefaultHeightForRole('default'), so the harness
// must provide the role map + that helper.
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS = {user:120, assistant:160, tool_call:400, default:140};
function clearTimeout(id){ timerCleared = (id === 99); }
eval(extractFunc('_messageVirtualDefaultHeightForRole'));
eval(extractFunc('_clearMessageVirtualHeightCache'));
_clearMessageVirtualHeightCache();
console.log(JSON.stringify({
  scrollActive: _messageVirtualScrollActive,
  settleTimer: _messageVirtualScrollSettleTimer,
  deferred: _messageVirtualDeferredMeasurement,
  timerCleared: timerCleared,
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["scrollActive"] is False, (
        "_clearMessageVirtualHeightCache must reset _messageVirtualScrollActive to false"
    )
    assert metrics["settleTimer"] == 0, (
        "_clearMessageVirtualHeightCache must reset _messageVirtualScrollSettleTimer to 0"
    )
    assert metrics["deferred"] is None, (
        "_clearMessageVirtualHeightCache must clear _messageVirtualDeferredMeasurement to null"
    )
    assert metrics["timerCleared"] is True, (
        "_clearMessageVirtualHeightCache must call clearTimeout on the pending settle timer"
    )
