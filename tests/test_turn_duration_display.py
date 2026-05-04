"""Regression tests for per-turn response duration in WebUI.

The WebUI should expose how long an agent turn took, using backend timing so
reload/reconnect does not lose the measurement.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_streaming_done_payload_includes_backend_turn_duration():
    assert "duration_seconds" in STREAMING_PY, (
        "api/streaming.py should include a backend-measured duration_seconds "
        "field in the done usage payload."
    )
    assert "pending_started_at" in STREAMING_PY and "time.time()" in STREAMING_PY, (
        "Turn duration should be measured from the persisted pending_started_at "
        "start time, not only from browser-local state."
    )
    assert "recovered/legacy flows" in STREAMING_PY, (
        "The missing-start fallback should be documented so it is not mistaken "
        "for the primary timing path."
    )
    assert "_turnDuration" in STREAMING_PY, (
        "The measured duration should be persisted on the assistant message so "
        "it survives reload after the SSE stream settles."
    )


def test_done_handler_persists_duration_on_last_assistant_message():
    assert "d.usage.duration_seconds" in MESSAGES_JS, (
        "static/messages.js should read duration_seconds from the done usage payload."
    )
    assert "lastAsst._turnDuration" in MESSAGES_JS, (
        "The done handler should attach the duration to the last assistant message "
        "so renderMessages() can display it after the live stream settles."
    )


def test_ui_formats_and_renders_turn_duration_in_footer_and_activity_summary():
    assert "function _formatTurnDuration" in UI_JS, (
        "ui.js should centralize duration formatting for footer and compact activity display."
    )
    assert "msg-duration-inline" in UI_JS and "Done in" in UI_JS, (
        "Expanded/non-activity display should show a subtle footer chip like 'Done in 42s'."
    )
    assert "tool-call-group-duration" in UI_JS, (
        "Compact tool activity summary should have a dedicated duration span at the end of the line."
    )
    assert "data-turn-duration" in UI_JS, (
        "Activity groups need a stable data-turn-duration hook so settled duration can update the summary."
    )
    assert "compactActivityForMessage" in UI_JS, (
        "When compact activity is present, duration should live on the Activity row "
        "instead of being duplicated in the assistant footer."
    )
    assert ".msg-duration-inline" in CSS and ".tool-call-group-duration" in CSS, (
        "Duration UI should have explicit CSS hooks for the footer chip and compact activity summary."
    )
