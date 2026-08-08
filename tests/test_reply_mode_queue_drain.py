"""Queued /ask and /plan turns must retain reply mode through queue drain."""

from pathlib import Path

ROOT = Path(__file__).parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_busy_queue_payload_includes_reply_mode():
    """Busy-session queue entries must carry ask/plan mode for drain."""
    assert "mode:window._pendingChatMode||undefined" in MESSAGES_JS


def test_send_consumes_queued_reply_mode_before_ask_plan_parse():
    """Drain-stamped mode must survive stripped queue text (no /ask prefix)."""
    assert "window._queuedChatMode" in MESSAGES_JS
    assert "_drainReplyMode=window._queuedChatMode" in MESSAGES_JS


def test_queue_drain_restores_reply_mode_before_send():
    """setBusy(false) drain must stamp mode before calling send()."""
    idx = UI_JS.find("function setBusy(v)")
    assert idx >= 0
    block = UI_JS[idx:idx + 2500]
    assert "window._queuedChatMode=next.mode||null" in block
    assert "send();" in block
