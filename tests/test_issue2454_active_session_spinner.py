"""Regression coverage for #2454 active-session stale sidebar spinner.

The backend can already reconcile stale stream state and return `/api/sessions`
rows with `is_streaming: false`, `active_stream_id: null`, and
`pending_user_message: null`. The remaining bug is frontend-local: the current
open session can keep `S.busy = true`, so `_isSessionLocallyStreaming()` still
makes the sidebar row render as streaming even after the server says idle.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start != -1, f"missing {signature}"
    brace = src.find("{", start)
    assert brace != -1, f"missing opening brace for {signature}"
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : i]
    raise AssertionError(f"could not extract function body for {signature}")


def test_active_session_idle_reconcile_clears_stale_busy_and_inflight_state():
    body = _function_body(SESSIONS_SRC, "function _reconcileActiveSessionIdleStateFromList(")

    assert "serverRows" in body, "reconcile must inspect raw /api/sessions rows before optimistic merging"
    assert "S.session.session_id" in body, "reconcile must target the currently active session"
    assert "_sendInProgress" in body, "cleanup must not interrupt a send that has not received stream_id yet"
    assert "!serverRow.is_streaming" in body, "server idle metadata must gate the cleanup"
    assert "!serverRow.active_stream_id" in body, "active stream id must be absent before cleanup"
    assert "!serverRow.pending_user_message" in body, "pending user text must be absent before cleanup"
    assert "S.busy=false" in body, "stale local busy state must be cleared"
    assert "S.activeStreamId=null" in body, "stale active stream id must be cleared"
    assert "delete INFLIGHT[sid]" in body, "stale active-session inflight cache must be purged"
    assert "clearInflightState(sid)" in body, "persisted inflight cache must be cleared too"
    assert "updateSendBtn()" in body, "composer controls must reflect the idle state after cleanup"


def test_session_list_payload_reconciles_active_idle_state_before_optimistic_merge_and_render():
    body = _function_body(SESSIONS_SRC, "function _applySessionListPayload(")

    reconcile_pos = body.find("_reconcileActiveSessionIdleStateFromList(sessData.sessions||[])")
    merge_pos = body.find("_allSessions = _mergeOptimisticFirstTurnSessions")
    render_pos = body.find("renderSessionListFromCache()")

    assert reconcile_pos != -1, "active-session idle reconciliation must run for refreshed rows"
    assert merge_pos != -1, "session rows must still be applied from /api/sessions"
    assert render_pos != -1, "payload application must still render from cache"
    assert reconcile_pos < merge_pos < render_pos, (
        "local S.busy/INFLIGHT state must be reconciled against raw server rows "
        "before optimistic merging can re-label a stale active session as streaming"
    )
