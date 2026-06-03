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


def test_live_activity_group_has_observable_baseline_events():
    assert "function _ensureLiveActivityBaseline(group)" in UI_JS
    assert "Run started" in UI_JS
    assert "Observable activity will appear here as the agent works." in UI_JS
    assert "Model: ${modelLabel}" in UI_JS
    assert "_ensureLiveActivityBaseline(group);" in UI_JS


def test_empty_thinking_placeholder_becomes_status_row_not_raw_thinking_card():
    assert "data-activity-event-id=\"thinking-placeholder\"" in UI_JS
    assert "Starting agent" in UI_JS
    assert "Creating the stream and sending your message…" in UI_JS
    assert "Waiting for first model token" in UI_JS
    assert "Stream connected; no model output has arrived yet." in UI_JS
    assert "Waiting on model" in UI_JS
    assert "Tool finished; waiting for the model to continue." in UI_JS
    assert "Waiting on tool result" in UI_JS
    assert "The tool is still running; the response will continue after it completes." in UI_JS
    assert "_thinkingActivityNode(thinkingText, false)" in UI_JS


def test_stream_start_refreshes_waiting_status_after_stream_id_arrives():
    active_idx = MESSAGES_JS.find("S.activeStreamId = streamId;")
    assert active_idx != -1
    refresh_idx = MESSAGES_JS.find("appendThinking('',{pending:true})", active_idx)
    attach_idx = MESSAGES_JS.find("attachLiveStream(activeSid, streamId, uploadedNames);", active_idx)
    assert refresh_idx != -1
    assert attach_idx != -1
    assert refresh_idx < attach_idx


def test_tool_events_update_activity_timeline_and_summary():
    assert "Tool finished: ${toolName}" in UI_JS
    assert "Running tool: ${toolName}" in UI_JS
    assert "No recent activity for ${_formatActiveElapsedTimer(idleAge)}" in UI_JS
    assert "Activity · Running" in UI_JS
    assert "Working for ${label}" in UI_JS


def test_activity_status_rows_have_quiet_metadata_styling():
    assert ".agent-activity-status{" in STYLE_CSS
    assert "grid-template-columns:18px minmax(0,1fr) auto" in STYLE_CSS
    assert ".agent-activity-status-detail" in STYLE_CSS
    assert ".agent-activity-status-time" in STYLE_CSS
    assert ".agent-activity-status-error .agent-activity-status-label{color:var(--error);}" in STYLE_CSS
