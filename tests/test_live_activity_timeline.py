"""Regression coverage for live Activity timeline UX.

The live Activity disclosure should surface observable run telemetry instead of a
blank Thinking placeholder while preserving the quiet tool/thinking metadata
family.
"""

import pathlib


REPO = pathlib.Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


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
    assert "No recent activity for ${_formatActiveElapsedTimer(idleAge)}" in UI_JS
    assert "Activity · Running" in UI_JS
    assert "Working for ${label}" in UI_JS
    assert "_isActivityTimerGroup(group)" in UI_JS
    assert "opts.turnDuration" in UI_JS
    assert "data-turn-duration" in UI_JS
    assert "durationText?`Done in ${durationText}`" in UI_JS


def test_settled_activity_render_keeps_tools_bound_to_progress_bursts():
    render_fn = UI_JS.split("if(!S.busy){", 1)[1].split("// Render per-turn duration", 1)[0]
    assert "_assistantAnchorForActivity" in render_fn
    assert "const byActivity = new Map();" in render_fn
    assert "tc.activityBurstId" in render_fn
    assert "`burst:${burstId}`" in render_fn
    assert "ensureActivityGroup(anchorParent,{collapsed:true,anchor:insertAfterNode,activityKey,burstId})" in render_fn
    assert "group.setAttribute('data-turn-duration'" not in render_fn


def test_done_handler_preserves_live_tool_burst_metadata_for_settled_render():
    assert "function _mergeSettledToolCallsWithLiveMetadata(rawCalls)" in MESSAGES_JS
    assert "activityBurstId" in MESSAGES_JS
    assert "S.toolCalls=_mergeSettledToolCallsWithLiveMetadata(d.session.tool_calls);" in MESSAGES_JS
    assert "S.toolCalls=_mergeSettledToolCallsWithLiveMetadata(session.tool_calls||[]);" in MESSAGES_JS


def test_activity_status_rows_have_quiet_metadata_styling():
    assert ".agent-activity-status{" in STYLE_CSS
    assert "grid-template-columns:18px minmax(0,1fr) auto" in STYLE_CSS
    assert ".agent-activity-status-detail" in STYLE_CSS
    assert ".agent-activity-status-time" in STYLE_CSS
    assert ".agent-activity-status-error .agent-activity-status-label{color:var(--error);}" in STYLE_CSS
