"""Regression coverage for live Activity timeline UX.

The live Activity disclosure should surface observable run telemetry instead of a
blank Thinking placeholder while preserving the quiet tool/thinking metadata
family.
"""

import pathlib
import shutil
import subprocess


REPO = pathlib.Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_source(src, name):
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1
    brace = src.find("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError(f"function {name} did not close")


def test_run_activity_group_has_observable_baseline_events():
    assert "function _ensureLiveActivityBaseline(group)" in UI_JS
    assert "function ensureRunActivityGroup(inner, opts)" in UI_JS
    assert "data-run-activity-group" in UI_JS
    assert "Run started" in UI_JS
    assert "Observable activity will appear here as the agent works." in UI_JS
    assert "Model: ${modelLabel}" in UI_JS
    assert "_ensureLiveActivityBaseline(group);" in UI_JS
    assert "ensureActivityGroup(inner, opts)" in UI_JS


def test_per_segment_tool_activity_does_not_include_run_metadata_rows():
    activity_fn = UI_JS.split("function ensureActivityGroup(inner, opts)", 1)[1].split("function ensureRunActivityGroup", 1)[0]
    tool_fn = UI_JS.split("function appendLiveToolCard(tc)", 1)[1].split("function clearLiveToolCards", 1)[0]
    assert "_ensureLiveActivityBaseline" not in activity_fn
    assert "_appendActivityEvent(group" not in tool_fn
    assert "Tool finished: ${toolName}" not in UI_JS
    assert "Running tool: ${toolName}" not in UI_JS
    assert "_thinkingActivityNode(thinkingText, false)" in UI_JS


def test_tool_activity_uses_tool_cards_and_run_activity_owns_timer():
    assert "buildToolCard(tc)" in UI_JS
    assert "tool-card-duration" in UI_JS
    assert "Activity · Running" in UI_JS
    assert "Working for ${label}" in UI_JS
    assert "_isActivityTimerGroup(group)" in UI_JS
    assert "opts.turnDuration" in UI_JS
    assert "data-turn-duration" in UI_JS
    assert "durationText?`Done in ${durationText}`" in UI_JS
    assert "return !!(group&&group.getAttribute('data-run-activity-group')==='1');" in UI_JS
    live_summary_fn = UI_JS.split("function _syncToolCallGroupSummary(group)", 1)[1].split("function _activityProgressLabelForToolName", 1)[0]
    assert "group.removeAttribute('data-active-turn-elapsed');" in live_summary_fn
    assert "durationEl.textContent='';" in live_summary_fn


def test_settled_activity_render_keeps_tools_bound_to_progress_bursts():
    render_fn = UI_JS.split("if(!S.busy){", 1)[1].split("// Render per-turn duration", 1)[0]
    assert "_assistantAnchorForActivity" in render_fn
    assert "const byActivity = new Map();" in render_fn
    assert "tc.activityBurstId" in render_fn
    assert "`burst:${burstId}`" in render_fn
    assert "ensureActivityGroup(anchorParent,{collapsed:true,anchor:insertAfterNode,activityKey,burstId})" in render_fn
    assert "group.setAttribute('data-turn-duration'" not in render_fn


def test_reattach_normalizes_live_activity_group_placement_by_burst_anchor():
    assert "function normalizeLiveActivityGroupPlacement(turn)" in UI_JS
    assert "normalizeLiveActivityGroupPlacement(restored)" in UI_JS
    activity_fn = UI_JS.split("function ensureActivityGroup(inner, opts)", 1)[1].split("function normalizeLiveActivityGroupPlacement", 1)[0]
    assert "anchor.insertAdjacentElement('afterend',group);" in activity_fn
    normalize_fn = UI_JS.split("function normalizeLiveActivityGroupPlacement(turn)", 1)[1].split("function ensureRunActivityGroup", 1)[0]
    assert '.tool-call-group[data-live-tool-call-group="1"][data-activity-burst-id]' in normalize_fn
    assert '[data-live-assistant="1"][data-activity-burst-id="${CSS.escape(burstId)}"]' in normalize_fn


def test_done_handler_preserves_live_tool_burst_metadata_for_settled_render():
    assert "function _mergeSettledToolCallsWithLiveMetadata(rawCalls)" in MESSAGES_JS
    assert "activityBurstId" in MESSAGES_JS
    assert "S.toolCalls=_mergeSettledToolCallsWithLiveMetadata(d.session.tool_calls);" in MESSAGES_JS
    assert "S.toolCalls=_mergeSettledToolCallsWithLiveMetadata(session.tool_calls||[]);" in MESSAGES_JS


def test_message_tool_metadata_path_keeps_live_burst_metadata_available():
    assert "S._settledLiveToolMetadata=S.toolCalls.map" in MESSAGES_JS
    assert "S.toolCalls=hasMessageToolMetadata?[]:S.toolCalls.map" in MESSAGES_JS
    render_fn = UI_JS.split("const derived=[];", 1)[1].split("if(derived.length) S.toolCalls=derived;", 1)[0]
    assert "S._settledLiveToolMetadata" in render_fn
    assert "liveToolMetadata" in render_fn
    assert "copyLiveToolMetadata" in render_fn
    assert "activityBurstId" in render_fn


def test_message_tool_metadata_empty_assistant_tools_reuse_previous_visible_anchor():
    assert "function _assistantToolAnchorIdxForMessage(messages, rawIdx)" in UI_JS
    render_fn = UI_JS.split("const derived=[];", 1)[1].split("if(derived.length) S.toolCalls=derived;", 1)[0]
    assert "const assistantToolAnchorIdx=_assistantToolAnchorIdxForMessage(S.messages,rawIdx);" in render_fn
    assert "assistant_msg_idx:assistantToolAnchorIdx" in render_fn

    assert NODE, "node not on PATH"
    has_visible_fn = _function_source(UI_JS, "_assistantMessageHasVisibleContent")
    anchor_fn = _function_source(UI_JS, "_assistantToolAnchorIdxForMessage")
    script = f"""
const assert = require('assert');
{has_visible_fn}
{anchor_fn}
const messages = [
  {{role:'assistant', content:'visible progress'}},
  {{role:'assistant', content:'', tool_calls:[{{id:'call-1'}}]}},
  {{role:'assistant', content:'', tool_calls:[{{id:'call-2'}}]}},
  {{role:'assistant', content:'next progress', tool_calls:[{{id:'call-3'}}]}},
  {{role:'assistant', content:[{{type:'tool_use', id:'call-4', name:'read_file'}}]}},
];
assert.strictEqual(_assistantToolAnchorIdxForMessage(messages, 1), 0);
assert.strictEqual(_assistantToolAnchorIdxForMessage(messages, 2), 0);
assert.strictEqual(_assistantToolAnchorIdxForMessage(messages, 3), 3);
assert.strictEqual(_assistantToolAnchorIdxForMessage(messages, 4), 3);
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_settled_tool_metadata_merge_replaces_null_activity_metadata():
    assert NODE, "node not on PATH"
    fn = _function_source(MESSAGES_JS, "_mergeSettledToolCallsWithLiveMetadata")
    script = f"""
const assert = require('assert');
const S = {{
  toolCalls: [{{tid:'tool-1', name:'read_file', activityBurstId:2, duration:1.25, started_at:123}}]
}};
{fn}
const merged = _mergeSettledToolCallsWithLiveMetadata([
  {{tid:'tool-1', name:'read_file', activityBurstId:null, duration:null, started_at:null}}
]);
assert.strictEqual(merged[0].activityBurstId, 2);
assert.strictEqual(merged[0].duration, 1.25);
assert.strictEqual(merged[0].started_at, 123);
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_settled_activity_render_treats_burst_zero_as_unanchored_activity():
    render_fn = UI_JS.split("if(!S.busy){", 1)[1].split("// Render per-turn duration", 1)[0]
    assert "String(burstId)!=='0'" in render_fn
    assert "if(aIdx<assistantIdxs[0]) return null;" in render_fn
    assert "String(tc.activityBurstId)!=='0'" in render_fn


def test_record_activity_boundary_updates_segment_burst_id_to_post_increment():
    """recordActivityBoundary must re-stamp the current assistantRow DOM element with
    the post-increment burst id so that subsequent tool events (which read the same
    _currentActivityBurstId) find the matching [data-activity-burst-id] anchor.

    Without this update the segment keeps id=N while tools get id=N+1, causing
    appendLiveToolCard to miss the anchor and Activity groups to pile up after all
    text instead of interleaving with their source segments.
    """
    boundary_fn = MESSAGES_JS.split("function recordActivityBoundary()", 1)[1].split("function ensureAssistantRow", 1)[0]
    # Must update the DOM attribute after incrementing the counter
    assert "assistantRow.setAttribute('data-activity-burst-id',String(_currentActivityBurstId))" in boundary_fn
    # The update must be guarded so it only fires when assistantRow exists
    assert "if(assistantRow) assistantRow.setAttribute" in boundary_fn


def test_record_activity_boundary_does_not_create_empty_duplicate_burst():
    boundary_fn = MESSAGES_JS.split("function recordActivityBoundary()", 1)[1].split("function ensureAssistantRow", 1)[0]
    assert "const lastTextEnd=inflight.activityBurstAnchors.reduce" in boundary_fn
    assert "if(textEnd<=lastTextEnd)" in boundary_fn
    assert "_currentActivityBurstId+=1;" in boundary_fn
    assert boundary_fn.find("if(textEnd<=lastTextEnd)") < boundary_fn.find("_currentActivityBurstId+=1;")


def test_inactive_interim_assistant_still_records_activity_boundary():
    """A session can receive SSE events while the pane is being switched away.

    Token/tool state is still persisted for reattach in that window, so interim
    progress boundaries must also be persisted before the inactive-pane return.
    Otherwise later tool calls keep an activityBurstId with no text anchor and
    Activity groups pile up at the tail after switching back.
    """
    wire_fn = MESSAGES_JS.split("function _wireSSE(source)", 1)[1].split("source.addEventListener('reasoning'", 1)[0]
    inactive_returns = [
        idx for idx in range(len(wire_fn))
        if wire_fn.startswith("if(!S.session||S.session.session_id!==activeSid){", idx)
    ]
    assert len(inactive_returns) >= 2
    for idx in inactive_returns[:2]:
        branch = wire_fn[idx:wire_fn.find("}", idx) + 1]
        assert "recordActivityBoundary();" in branch
        assert "_resetAssistantSegment();" in branch


def test_reattach_segment_start_aligns_with_last_burst_anchor():
    """Simulate the reattach segmentStart initializer with multiple anchors.

    The initializer must clamp to the actual assistantText length and ignore
    stale anchors past the end of the text, otherwise displayText slicing in
    _doRender would produce empty output for the tail segment.
    """
    assert NODE, "node not on PATH"
    body = MESSAGES_JS.split("function attachLiveStream(", 1)[1]
    seg_block_start = body.find("let segmentStart=(()=>{")
    assert seg_block_start != -1, "expected reconnect-aware segmentStart IIFE"
    seg_block_end = body.find("})();", seg_block_start) + len("})();")
    initializer = body[seg_block_start:seg_block_end] + ";"
    # Wrap as a callable with explicit reconnecting + INFLIGHT/activeSid stand-ins.
    script = f"""
const assert = require('assert');
function computeStart(reconnecting, inflight, assistantText) {{
  const INFLIGHT = {{ 'sid': inflight }};
  const activeSid = 'sid';
  {initializer}
  return segmentStart;
}}
// No anchors -> 0
assert.strictEqual(computeStart(true, {{activityBurstAnchors:[]}}, 'hello world'), 0);
// Single anchor inside text length -> anchor textEnd
assert.strictEqual(computeStart(true, {{activityBurstAnchors:[{{id:1,textEnd:5}}]}}, 'hello world'), 5);
// Multiple anchors -> picks max textEnd within text length
assert.strictEqual(computeStart(true, {{activityBurstAnchors:[
  {{id:1,textEnd:5}}, {{id:2,textEnd:11}}, {{id:3,textEnd:7}}
]}}, 'hello world'), 11);
// Anchor textEnd past assistantText length -> ignored
assert.strictEqual(computeStart(true, {{activityBurstAnchors:[
  {{id:1,textEnd:5}}, {{id:2,textEnd:99}}
]}}, 'hello world'), 5);
// Not reconnecting -> always 0
assert.strictEqual(computeStart(false, {{activityBurstAnchors:[
  {{id:1,textEnd:5}}, {{id:2,textEnd:11}}
]}}, 'hello world'), 0);
// Missing inflight entry -> 0
assert.strictEqual(computeStart(true, undefined, 'hello'), 0);
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_activity_status_rows_have_quiet_metadata_styling():
    assert ".agent-activity-status{" in STYLE_CSS
    assert "grid-template-columns:18px minmax(0,1fr) auto" in STYLE_CSS
    assert ".agent-activity-status-detail" in STYLE_CSS
    assert ".agent-activity-status-time" in STYLE_CSS
    assert ".agent-activity-status-error .agent-activity-status-label{color:var(--error);}" in STYLE_CSS
