"""Regression coverage for live Activity timeline UX.

The live Activity disclosure should surface observable run telemetry instead of a
blank Thinking placeholder while preserving the quiet tool/thinking metadata
family.
"""

import pathlib


REPO = pathlib.Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_live_activity_group_has_observable_baseline_events():
    assert "function _ensureLiveActivityBaseline(group)" in UI_JS
    assert "Run started" in UI_JS
    assert "Observable activity will appear here as the agent works." in UI_JS
    assert "Model: ${modelLabel}" in UI_JS
    assert "_ensureLiveActivityBaseline(group);" in UI_JS


def test_empty_thinking_placeholder_becomes_status_row_not_raw_thinking_card():
    assert "data-activity-event-id=\"thinking-placeholder\"" in UI_JS
    assert "Waiting on model" in UI_JS
    assert "No tool activity has been reported yet." in UI_JS
    assert "Waiting on tool result" in UI_JS
    assert "_thinkingActivityNode(thinkingText, false)" in UI_JS


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
