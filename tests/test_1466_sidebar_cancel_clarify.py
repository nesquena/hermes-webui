"""Regression coverage for issue #1466 sidebar cancel ownership.

The active pane is only a projection; running state belongs to the session that
owns the stream. Cancelling a running session from the sidebar context menu must
address that session's stream id and must only clear approval/clarify UI owned by
that session.
"""
from pathlib import Path

ROOT = Path(__file__).parent.parent
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str, window: int = 1800) -> str:
    idx = src.find(f"function {name}(")
    assert idx >= 0, f"{name} not found"
    return src[idx : idx + window]


class TestSidebarCancelAction:
    def test_running_sidebar_sessions_get_stop_action(self):
        """Running sessions need a context-menu cancel action even when not active pane."""
        body = _function_body(SESSIONS_JS, "_openSessionActionMenu", 3200)
        assert "session.active_stream_id" in body, (
            "sidebar action menu must detect per-session active_stream_id instead of S.activeStreamId"
        )
        assert "cancelSessionStream(session)" in body, (
            "running sidebar sessions must expose a stop action that cancels that session"
        )
        assert body.find("cancelSessionStream(session)") < body.find("deleteSession(session.session_id)"), (
            "stop action should appear before destructive delete action"
        )

    def test_cancel_session_stream_uses_session_owned_stream_id(self):
        """Cancel-from-sidebar must call /api/chat/cancel with the row's stream id."""
        body = _function_body(BOOT_JS, "cancelSessionStream")
        assert "session&&session.active_stream_id" in body or "session && session.active_stream_id" in body
        assert "stream_id=${encodeURIComponent(streamId)}" in body
        assert "S.activeStreamId" not in body.split("const streamId", 1)[1].split("fetch", 1)[0], (
            "sidebar cancel must not derive the stream id from the active pane global"
        )

    def test_cancel_session_stream_clears_only_owned_clarify_and_approval_cards(self):
        """Cancelling A from sidebar must not blanket-clear B's clarify/approval cards."""
        body = _function_body(BOOT_JS, "cancelSessionStream")
        assert "_clarifySessionId===sid" in body, (
            "clarify card cleanup must be gated to the cancelled session id"
        )
        assert "_approvalSessionId===sid" in body, (
            "approval card cleanup must be gated to the cancelled session id"
        )
        assert "hideClarifyCard(true" in body
        assert "hideApprovalCard(true" in body
