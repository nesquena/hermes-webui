"""Regression coverage for #1694 session-owned runtime invariants.

These source-level tests protect the existing vanilla-JS runtime boundary:
stream transports are keyed by stream_id/session_id, while the active pane is only
one projection. Background terminal events must update session/sidebar metadata
without tearing down the currently viewed pane's runtime state.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    idx = src.find(f"function {name}")
    if idx == -1:
        idx = src.find(f"async function {name}")
    assert idx != -1, f"{name} not found"
    brace = src.find("{", idx)
    depth = 0
    for pos in range(brace, len(src)):
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[idx : pos + 1]
    raise AssertionError(f"{name} body did not terminate")


def _event_handler(src: str, event_name: str) -> str:
    marker = f"source.addEventListener('{event_name}'"
    idx = src.find(marker)
    assert idx != -1, f"{event_name} handler not found"
    next_handler = src.find("source.addEventListener(", idx + len(marker))
    return src[idx:next_handler if next_handler != -1 else len(src)]


class TestSessionOwnedRuntimeInvariants:
    def test_sidebar_cancel_uses_row_stream_id_not_active_pane_stream(self):
        boot = read("static/boot.js")
        body = _function_body(boot, "cancelSessionStream")
        assert "session&&session.active_stream_id" in body, (
            "Sidebar row cancellation must target the row-owned active_stream_id, "
            "not the currently viewed pane's S.activeStreamId."
        )
        assert "S.activeStreamId" not in body[: body.index("if(S.session&&S.session.session_id===sid)")], (
            "cancelSessionStream must not read or clear active-pane stream state until "
            "it has proved the row session is the active pane."
        )

    def test_done_event_does_not_clear_unrelated_active_pane_busy_state(self):
        messages = read("static/messages.js")
        done = _event_handler(messages, "done")
        unconditional = "_queueDrainSid=activeSid;renderSessionList();setBusy(false);setStatus('');"
        assert unconditional not in done, (
            "A background session's done event must not unconditionally call setBusy(false); "
            "that can idle an unrelated active pane that is still running."
        )
        assert "if(isActiveSession||!S.session||!INFLIGHT[S.session.session_id])" in done.replace(" ", ""), (
            "The done handler should only idle composer state when the completed stream "
            "belongs to the active pane, or when no other active-pane inflight runtime exists."
        )

    def test_server_session_finalize_does_not_idle_unrelated_active_pane(self):
        messages = read("static/messages.js")
        finalize = _function_body(messages, "_restoreSettledSession")
        assert "_queueDrainSid=activeSid;renderSessionList();setBusy(false);setComposerStatus('');" not in finalize, (
            "The fallback server-finalize path must not idle the active pane for a "
            "background session completion."
        )
        assert "if(isActiveSession||!S.session||!INFLIGHT[S.session.session_id])" in finalize.replace(" ", ""), (
            "The fallback server-finalize path should use the same active-pane guard as the live done event."
        )

    def test_approval_and_clarify_pollers_are_stopped_by_owner_session(self):
        messages = read("static/messages.js")
        assert "let _approvalPollingSessionId = null" in messages
        assert "let _clarifyPollingSessionId = null" in messages
        assert "function stopApprovalPollingForSession" in messages
        assert "function stopClarifyPollingForSession" in messages

        approval_stop = _function_body(messages, "stopApprovalPollingForSession")
        clarify_stop = _function_body(messages, "stopClarifyPollingForSession")
        assert "_approvalPollingSessionId!==sid" in approval_stop.replace(" ", ""), (
            "A terminal event for session A must not stop approval polling that now belongs to session B."
        )
        assert "_clarifyPollingSessionId!==sid" in clarify_stop.replace(" ", ""), (
            "A terminal event for session A must not stop clarify polling that now belongs to session B."
        )

        done = _event_handler(messages, "done")
        assert "stopApprovalPollingForSession(activeSid)" in done
        assert "stopClarifyPollingForSession(activeSid)" in done
        assert "stopApprovalPolling();\n      stopClarifyPolling();" not in done, (
            "The done handler must not blindly stop whatever approval/clarify poller "
            "the active pane currently owns."
        )

    def test_live_stream_transport_and_inflight_state_remain_session_keyed(self):
        messages = read("static/messages.js")
        close_live = _function_body(messages, "closeLiveStream")
        attach_start = messages.index("function attachLiveStream")
        attach_live = messages[attach_start:messages.index("function _isActiveSession", attach_start)]
        assert "constlive=LIVE_STREAMS[sessionId]" in close_live.replace(" ", ""), (
            "LIVE_STREAMS must remain keyed by the owning session_id."
        )
        assert "constexistingLive=LIVE_STREAMS[activeSid]" in attach_live.replace(" ", ""), (
            "attachLiveStream should reuse the session-owned live transport for the same stream."
        )
        assert re.search(r"INFLIGHT\[activeSid\].*messages", attach_live, re.DOTALL), (
            "The browser-side inflight projection must remain keyed by the owning session_id."
        )
