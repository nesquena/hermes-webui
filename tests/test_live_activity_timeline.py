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


def test_activity_status_rows_have_quiet_metadata_styling():
    assert ".agent-activity-status{" in STYLE_CSS
    assert "grid-template-columns:18px minmax(0,1fr) auto" in STYLE_CSS
    assert ".agent-activity-status-detail" in STYLE_CSS
    assert ".agent-activity-status-time" in STYLE_CSS
    assert ".agent-activity-status-error .agent-activity-status-label{color:var(--error);}" in STYLE_CSS
