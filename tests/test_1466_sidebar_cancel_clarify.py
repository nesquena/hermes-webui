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

    def test_cli_session_helper_identifies_cli_origin(self):
        """CLI sessions should be treated as external-only for destructive action gating."""
        body = _function_body(SESSIONS_JS, "_isCliSession", 900)
        assert "function _isCliSession(session) {" in body
        assert "session.session_source === 'cli'" in body
        assert "session.raw_source" in body
        assert "session.source_tag" in body
        assert "session.source" in body
        assert "session.source_label" in body
        assert "if (_isMessagingSession(session)) return false;" in body
        assert "return session.is_cli_session === true;" in body

    def test_cli_sessions_hide_duplicate_and_delete_in_action_menu(self):
        """Session action menu should hide duplicate/delete for CLI-origin sessions."""
        body = _function_body(SESSIONS_JS, "_openSessionActionMenu", 3600)
        assert "const isCliSession = _isCliSession(session);" in body
        assert "const isExternalSession = isMessagingSession || isCliSession;" in body
        assert "if(!isExternalSession)" in body
        # duplicate/delete should both be gated by the same external-session check
        first = body.find("_appendSessionDuplicateAction")
        second = body.find("t('session_delete')")
        assert first > 0 and second > 0, "menu actions should still include duplicate/delete nodes"
        assert first < second, "duplicate action should render before delete action"
