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

    assert "_currentMessageVirtualWindow(visWithIdx,renderWindowSize)" in render_body
    assert "const renderVisibleIdxs=[" in render_body
    assert "_messageVirtualSpacer(virtualWindow.topPad,'before')" in render_body
    assert "_messageVirtualSpacer(virtualWindow.bottomPad,'after')" in render_body
    assert "_updateMessageVirtualMeasurements(renderVisWithIdx, renderVisibleIdxs, virtualWindow);" in render_body
    assert "_showEarlierRenderedMessages();" not in render_body


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
  getBoundingClientRect(){ return {height: 60}; },
  nextElementSibling: nextMessage,
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


def test_tool_rows_do_not_carry_message_measurement_hook():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    build_start = js.index("function buildToolCard(tc){")
    build_end = js.index("function _colorDiffLines", build_start)
    build_body = js[build_start:build_end]

    assert "row.dataset.msgIdx" not in build_body
    assert "querySelectorAll(`[data-msg-idx=" not in js
