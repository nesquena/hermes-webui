"""Regression coverage for issue #1617: TPS belongs on message headers.

Product decision:
- show live TPS in the assistant message header while streaming when real TPS is available;
- persist/show the final TPS at the end of the turn;
- do not show placeholder or estimated TPS when unavailable.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_tps_renders_in_message_header_not_global_titlebar():
    assert "msg-tps-inline" in UI_JS, "assistant message headers need a TPS chip hook"
    assert "msg-tps-inline" in CSS, "TPS header chip needs an explicit CSS hook"
    assert "_assistantRoleHtml(tsTitle='', tpsText='')" in UI_JS, (
        "assistant role/header rendering should accept the per-message TPS text"
    )
    assert "_formatTurnTps" in UI_JS, "TPS formatting should be centralized"
    assert "_turnTps" in UI_JS, "settled assistant messages should render final TPS from message metadata"
    assert "tpsStat" not in MESSAGES_JS, "live TPS must not target the removed/global titlebar chip"


def test_live_metering_updates_only_real_tps_and_never_placeholders():
    listener_start = MESSAGES_JS.find("source.addEventListener('metering'")
    assert listener_start != -1, "messages.js should listen for metering SSE events"
    listener_end = MESSAGES_JS.find("source.addEventListener('apperror'", listener_start)
    assert listener_end != -1, "apperror listener should follow metering listener"
    listener = MESSAGES_JS[listener_start:listener_end]
    assert "_setLiveAssistantTps" in listener, "live metering should update the live assistant header"
    assert "tps_available" in listener and "estimated" in listener, (
        "live TPS display must check availability and reject estimated readings"
    )
    assert "0.0 t/s" not in listener, "unavailable TPS should render nothing, not a 0.0 placeholder"
    assert "'—'" not in listener and '"—"' not in listener, "unavailable TPS should render nothing, not a dash"
    assert "high" not in listener.lower() and "low" not in listener.lower(), (
        "message-header TPS should not carry global HIGH/LOW titlebar semantics"
    )


def test_done_payload_persists_final_tps_when_exact_usage_available():
    assert "usage['tps']" in STREAMING_PY, "done usage payload should include final exact TPS when available"
    assert "output_tokens" in STREAMING_PY and "duration_seconds" in STREAMING_PY, (
        "final TPS should be based on exact completion tokens over measured turn duration"
    )
    assert "d.usage.tps" in MESSAGES_JS, "done handler should read final TPS from the usage payload"
    assert "lastAsst._turnTps" in MESSAGES_JS, "done handler should persist final TPS on the last assistant message"


def test_backend_marks_streaming_metering_availability_explicitly():
    assert "tps_available" in STREAMING_PY, "metering SSE payloads must explicitly say whether TPS is displayable"
    assert "estimated" in STREAMING_PY, "metering SSE payloads must explicitly distinguish estimated readings"
    assert "record_token(stream_id, len(STREAM_PARTIAL_TEXT[stream_id]))" not in STREAMING_PY, (
        "live TPS must not be derived from streamed character count / byte-size estimates"
    )
