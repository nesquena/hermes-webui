"""Regression coverage for #2454 active-session stale sidebar spinner.

The backend can already reconcile stale stream state and return `/api/sessions`
rows with `is_streaming: false`, `active_stream_id: null`, and
`pending_user_message: null`. The remaining bug is frontend-local: the current
open session can keep `S.busy = true`, so `_isSessionLocallyStreaming()` still
makes the sidebar row render as streaming even after the server says idle.
"""

from pathlib import Path
import json
import subprocess

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
    helper_body = _function_body(SESSIONS_SRC, "function _isServerIdleSessionRow(")
    body = _function_body(SESSIONS_SRC, "function _reconcileActiveSessionIdleStateFromList(")

    assert "serverRows" in body, "reconcile must inspect raw /api/sessions rows before optimistic merging"
    assert "S.session.session_id" in body, "reconcile must target the currently active session"
    assert "_sendInProgress" in body, "cleanup must not interrupt a send that has not received stream_id yet"
    assert "_isServerIdleSessionRow(serverRow)" in body, "server idle metadata must gate the cleanup"
    assert "!s.is_streaming" in helper_body, "server idle helper must require is_streaming=false"
    assert "!s.active_stream_id" in helper_body, "server idle helper must require no active stream id"
    assert "!s.pending_user_message" in helper_body, "server idle helper must require no pending user text"
    assert "S.busy=false" in body, "stale local busy state must be cleared"
    assert "S.activeStreamId=null" in body, "stale active stream id must be cleared"
    assert "delete INFLIGHT[sid]" in body, "stale active-session inflight cache must be purged"
    assert "clearInflightState(sid)" in body, "persisted inflight cache must be cleared too"
    assert "_sessionStreamingById.set(sid, false)" in body, "observed active streaming state must be reset"
    assert "_forgetObservedStreamingSession(sid)" in body, "persisted observed streaming marker must be cleared"
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


def test_optimistic_merge_does_not_resurrect_server_idle_session_from_stale_cache():
    helper_body = _function_body(SESSIONS_SRC, "function _isServerIdleSessionRow(")
    local_body = _function_body(SESSIONS_SRC, "function _isSessionLocallyStreaming(")
    optimistic_body = _function_body(SESSIONS_SRC, "function _isOptimisticFirstTurnSessionRow(")
    merge_body = _function_body(SESSIONS_SRC, "function _mergeOptimisticFirstTurnSessions(")

    assert "fetchedIsServerIdle" in merge_body, "merge must detect server-idle fetched rows"
    assert "active_stream_id:fetchedIsServerIdle?null" in merge_body
    assert "pending_user_message:fetchedIsServerIdle?null" in merge_body
    assert "pending_started_at:fetchedIsServerIdle?null" in merge_body
    assert "is_streaming:fetchedIsServerIdle?false" in merge_body

    script = f"""
let S = {{ session: null, busy: false }};
let _sessionStreamingById = new Map();
let _allSessions = [{{
  session_id: 'dogfood-session',
  title: 'Dogfood Docs Routing Review',
  message_count: 50,
  last_message_at: 1000,
  updated_at: 1000,
  active_stream_id: 'stale-stream',
  pending_user_message: 'stale pending text',
  pending_started_at: 900,
  is_streaming: true,
}}];
function _isServerIdleSessionRow(s) {{{helper_body}}}
function _isSessionLocallyStreaming(s) {{{local_body}}}
function _isOptimisticFirstTurnSessionRow(s) {{{optimistic_body}}}
function _mergeOptimisticFirstTurnSessions(fetchedSessions) {{{merge_body}}}
const merged = _mergeOptimisticFirstTurnSessions([{{
  session_id: 'dogfood-session',
  title: 'Dogfood Docs Routing Review',
  message_count: 50,
  last_message_at: 1100,
  updated_at: 1100,
  active_stream_id: null,
  pending_user_message: null,
  pending_started_at: null,
  is_streaming: false,
}}]);
console.log(JSON.stringify(merged[0]));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    row = json.loads(result.stdout)

    assert row["session_id"] == "dogfood-session"
    assert row["message_count"] == 50
    assert row["active_stream_id"] is None
    assert row["pending_user_message"] is None
    assert row["pending_started_at"] is None
    assert row["is_streaming"] is False
