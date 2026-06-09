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
