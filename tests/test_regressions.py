"""
Regression tests -- one test per bug that was introduced and fixed.
These tests exist specifically to prevent those bugs from silently returning.

Each test is tagged with the sprint/commit where the bug was found and fixed.
"""
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request
import urllib.parse
REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()

from tests._pytest_port import BASE

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status

def get_raw(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read(), r.headers.get("Content-Type",""), r.status

def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code

def make_session(created_list):
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid


def _make_session_visible(sid):
    from api.models import Session
    from tests.conftest import TEST_WORKSPACE

    session = Session(
        session_id=sid,
        title="regression-test-delete-R8",
        workspace=str(TEST_WORKSPACE),
        model="test",
        created_at=time.time(),
        updated_at=time.time(),
        profile="default",
        messages=[{"role": "user", "content": "visible row", "timestamp": time.time()}],
        tool_calls=[],
    )
    session.save(touch_updated_at=False)


def _make_auth_json_with_credential_pool(
    provider_id: str, pool_entries: list[dict], tmp_dir: pathlib.Path
) -> pathlib.Path:
    """Write an auth.json with only credential_pool entries for provider_id."""
    store = {"providers": {}, "credential_pool": {provider_id: pool_entries}}
    auth_path = tmp_dir / "auth.json"
    auth_path.write_text(json.dumps(store), encoding="utf-8")
    return auth_path


# ── R1: uuid not imported in server.py (Sprint 10 split regression) ──────────

def test_chat_start_returns_stream_id(cleanup_test_sessions):
    """R1: chat/start must return stream_id -- catches missing uuid import.
    When uuid was missing, this returned 500 (NameError).
    """
    sid = make_session(cleanup_test_sessions)
    data, status = post("/api/chat/start", {
        "session_id": sid,
        "message": "ping",
        "model": "openai/gpt-5.4-mini",
    })
    # Must return 200 with a stream_id -- not 500
    assert status == 200, f"chat/start failed with {status}: {data}"
    assert "stream_id" in data, "stream_id missing from chat/start response"
    assert len(data["stream_id"]) > 8, "stream_id looks invalid"
    post("/api/session/delete", {"session_id": sid})
    cleanup_test_sessions.clear()


# ── R2: AIAgent not imported in api/streaming.py (Sprint 10 split regression) ─

def test_chat_stream_opens_successfully(cleanup_test_sessions):
    """R2: After chat/start, GET /api/chat/stream must return 200 (SSE opens).
    When AIAgent was missing, the thread crashed immediately, popped STREAMS,
    and the SSE GET returned 404.
    """
    sid = make_session(cleanup_test_sessions)
    data, status = post("/api/chat/start", {
        "session_id": sid,
        "message": "say: hello",
        "model": "openai/gpt-5.4-mini",
    })
    assert status == 200, f"chat/start failed: {data}"
    stream_id = data["stream_id"]

    # Open the SSE stream -- must return 200, not 404
    # We only check headers (don't read the full stream body)
    req = urllib.request.Request(BASE + f"/api/chat/stream?stream_id={stream_id}")
    try:
        r = urllib.request.urlopen(req, timeout=3)
        assert r.status == 200, f"SSE stream returned {r.status} (expected 200)"
        ct = r.headers.get("Content-Type", "")
        assert "text/event-stream" in ct, f"Wrong Content-Type: {ct}"
        r.close()
    except urllib.error.HTTPError as e:
        assert False, f"SSE stream returned {e.code} -- AIAgent may not be imported"
    except Exception:
        pass  # timeout or connection close after brief read is fine

    post("/api/session/delete", {"session_id": sid})
    cleanup_test_sessions.clear()


# ── R3: Session.__init__ missing tool_calls param (Sprint 10 split regression) ─

def test_session_with_tool_calls_in_json_loads_ok(cleanup_test_sessions):
    """R3: Sessions that have tool_calls in their JSON must load without 500.
    When tool_calls=None was missing from Session.__init__, loading such sessions
    threw TypeError: unexpected keyword argument.
    """
    sid = make_session(cleanup_test_sessions)

    # Manually inject tool_calls into the session's JSON file
    from tests._pytest_port import TEST_STATE_DIR
    sessions_dir = pathlib.Path(os.environ.get("HERMES_WEBUI_TEST_STATE_DIR", str(TEST_STATE_DIR))) / "sessions"
    session_file = sessions_dir / f"{sid}.json"
    if session_file.exists():
        d = json.loads(session_file.read_text())
        d["tool_calls"] = [
            {"name": "terminal", "snippet": "test output", "tid": "test_tid_001", "assistant_msg_idx": 1}
        ]
        session_file.write_text(json.dumps(d))

    # Loading the session must return 200, not 500
    data, status = get(f"/api/session?session_id={urllib.parse.quote(sid)}")
    assert status == 200, f"Session with tool_calls returned {status}: {data}"
    assert data["session"]["session_id"] == sid

    post("/api/session/delete", {"session_id": sid})
    cleanup_test_sessions.clear()


# ── R4: approval check not imported in streaming.py (Sprint 10 split regression) ─

def test_streaming_py_imports_has_pending(cleanup_test_sessions):
    """R4: api/streaming.py must import an approval-check function.
    When missing, the approval check mid-stream caused NameError.
    """
    src = (REPO_ROOT / "api/streaming.py").read_text()
    assert "has_blocking_approval" in src, "has_blocking_approval not found in api/streaming.py"
    assert "import" in src and "has_blocking_approval" in src, \
        "has_blocking_approval must be imported in api/streaming.py"


def test_aiagent_imported_in_streaming(cleanup_test_sessions):
    """R2b: api/streaming.py must resolve AIAgent through the runtime guard.
    When missing, the streaming thread crashed immediately after being spawned.
    """
    src = (REPO_ROOT / "api/streaming.py").read_text()
    assert "get_ai_agent_class" in src, "guarded AIAgent resolver not referenced in api/streaming.py"
    assert "from api.agent_runtime import" in src and "get_ai_agent_class" in src, \
        "AIAgent must be resolved through api.agent_runtime in api/streaming.py"


# ── R5: SSE loop did not break on cancel event (Sprint 10 bug) ───────────────

def test_cancel_nonexistent_stream_returns_not_cancelled(cleanup_test_sessions):
    """R5a: Cancel endpoint works and returns cancelled:false for unknown stream."""
    data, status = get("/api/chat/cancel?stream_id=nonexistent_test_xyz")
    assert status == 200
    assert data["ok"] is True
    assert data["cancelled"] is False


def test_server_py_sse_loop_breaks_on_cancel(cleanup_test_sessions):
    """R5b: SSE loop must include 'cancel' in the break condition.
    When missing, the connection hung after the cancel event was processed.
    Sprint 11: logic moved from server.py to api/routes.py -- check both.
    """
    import re
    # Check server.py first, then api/routes.py (Sprint 11 extracted routes)
    src = (REPO_ROOT / "server.py").read_text()
    routes_src = (REPO_ROOT / "api" / "routes.py").read_text() if (REPO_ROOT / "api" / "routes.py").exists() else ""
    combined = src + routes_src
    # #6527: the break condition may be an inline tuple ("stream_end", ...) OR
    # the named SSE_RELAY_CLOSE_EVENTS frozenset. Accept either shape and pin
    # the BEHAVIOR (cancel closes the relay) against the resolved close set,
    # not the source syntax.
    m = re.search(
        r"if event in (?:SSE_RELAY_CLOSE_EVENTS|\([^)]*cancel[^)]*\)):\s*break",
        combined,
    )
    assert m, "SSE break/close condition not found in server.py or api/routes.py"
    from api.run_journal import SSE_RELAY_CLOSE_EVENTS
    assert "cancel" in SSE_RELAY_CLOSE_EVENTS, \
        f"'cancel' missing from SSE relay close set: {SSE_RELAY_CLOSE_EVENTS}"
    assert "apperror" in SSE_RELAY_CLOSE_EVENTS, \
        f"'apperror' missing from SSE relay close set: {SSE_RELAY_CLOSE_EVENTS}"


# ── R6: Test cron isolation (Sprint 10) ──────────────────────────────────────

def test_real_jobs_json_not_polluted_by_tests(cleanup_test_sessions):
    """R6: Test runs must not write to the real ~/.hermes/cron/jobs.json.
    When HERMES_HOME isolation was missing, every test run added test-job-* entries.
    """
    real_jobs_path = pathlib.Path.home() / ".hermes" / "cron" / "jobs.json"
    if not real_jobs_path.exists():
        return  # no jobs file at all -- fine

    jobs = json.loads(real_jobs_path.read_text())
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs", [])

    test_jobs = [j for j in jobs if j.get("name", "").startswith("test-job-")]
    assert len(test_jobs) == 0, \
        f"Real jobs.json contains {len(test_jobs)} test-job-* entries: " \
        f"{[j['name'] for j in test_jobs]}"


# ── General: api modules all importable ──────────────────────────────────────

def test_all_api_modules_importable(cleanup_test_sessions):
    """All api/ modules must be importable without NameError or ImportError.
    Catches missing imports introduced during future module splits.
    """
    import ast, pathlib
    api_dir = REPO_ROOT / "api"
    for module_file in api_dir.glob("*.py"):
        src = module_file.read_text()
        try:
            ast.parse(src)
        except SyntaxError as e:
            assert False, f"{module_file.name} has syntax error: {e}"


def test_server_py_importable(cleanup_test_sessions):
    """server.py must parse without syntax errors after any split."""
    import ast, pathlib
    src = (REPO_ROOT / "server.py").read_text()
    try:
        ast.parse(src)
    except SyntaxError as e:
        assert False, f"server.py has syntax error: {e}"

# ── R7: Cross-session busy state bleed ───────────────────────────────────────

def test_loadSession_resets_busy_state_for_idle_session(cleanup_test_sessions):
    """R7: sessions.js loadSession for a non-inflight session must reset S.busy to false.
    When missing, switching from a busy session to an idle one left the Send button
    disabled, showed the wrong activity bar, and pointed Cancel at the wrong stream.
    """
    src = (REPO_ROOT / "static/sessions.js").read_text()
    # The fix adds explicit S.busy=false in the non-inflight else branch
    assert "S.busy=false;" in src,         "sessions.js loadSession must set S.busy=false when loading a non-inflight session"
    # btnSend state must be refreshed via updateSendBtn
    assert "updateSendBtn()" in src,         "sessions.js loadSession must call updateSendBtn for non-inflight sessions"


def test_done_handler_guards_setbusy_with_inflight_check(cleanup_test_sessions):
    """R7b: messages.js done/error handlers must not call setBusy(false) if the
    currently viewed session is itself still in-flight.
    When missing, finishing session A while viewing in-flight session B would
    disable B's Send button.
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    # The fix wraps setBusy(false) in an active-pane ownership guard. Newer
    # implementations may centralize the guard in a helper rather than repeat the
    # raw INFLIGHT expression at every terminal event site.
    assert (
        "INFLIGHT[S.session.session_id]" in src
        or "function _setActivePaneIdleIfOwner" in src
    ), "messages.js must guard setBusy(false) for the current session"


def test_refresh_handler_does_not_drop_tool_messages_needed_by_todos(cleanup_test_sessions):
    """Todo panel state must survive session reload/refresh.
    The UI can hide tool-role messages from the visible transcript, but it must not
    destroy the raw session messages because loadTodos reconstructs state from the
    latest todo tool output.
    """
    sessions_src = (REPO_ROOT / "static/sessions.js").read_text()
    ui_src = (REPO_ROOT / "static/ui.js").read_text()
    panels_src = (REPO_ROOT / "static/panels.js").read_text()

    assert "data.session.messages=(data.session.messages||[]).filter(" not in sessions_src, \
        "sessions.js must not overwrite raw session.messages when filtering transcript display"
    assert "S.messages = (data.session.messages || []).filter(" not in ui_src, \
        "ui.js refreshSession must not rebuild S.messages by discarding tool messages from the raw session payload"
    assert "const sourceMessages = (S.session && Array.isArray(S.session.messages) && S.session.messages.length) ? S.session.messages : S.messages;" in panels_src, \
        "loadTodos must prefer raw S.session.messages so todo state survives reloads"


def test_cancel_button_not_cleared_across_sessions(cleanup_test_sessions):
    """R7c: The Cancel button and activeStreamId must only be cleared when the
    done/error event belongs to the currently viewed session.
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    # Both clear operations must be inside the activeSid === S.session guard
    # We check for the pattern added by the fix
    assert "S.session.session_id===activeSid" in src,         "messages.js must guard activeStreamId/Cancel clearing with session identity check"

# ── R8: Session delete does not invalidate index (ghost sessions) ─────────────

def test_deleted_session_does_not_appear_in_list(cleanup_test_sessions):
    """R8: After deleting a session, it must not appear in /api/sessions.
    When _index.json was not invalidated on delete, the session reappeared
    in the list even after the JSON file was removed.
    """
    # Create a session with a title so it shows in the list
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    post("/api/session/rename", {"session_id": sid, "title": "regression-test-delete-R8"})
    _make_session_visible(sid)
    post("/api/session/rename", {"session_id": sid, "title": "regression-test-delete-R8"})

    # Verify it appears
    sessions, _ = get("/api/sessions")
    ids_before = [s["session_id"] for s in sessions["sessions"]]
    assert sid in ids_before, "Session must appear in list before delete"

    # Delete it
    result, status = post("/api/session/delete", {"session_id": sid})
    assert status == 200 and result.get("ok") is True

    # Verify it no longer appears -- even after a second fetch (index rebuild)
    sessions2, _ = get("/api/sessions")
    ids_after = [s["session_id"] for s in sessions2["sessions"]]
    assert sid not in ids_after,         f"Deleted session {sid} still appears in list -- index not invalidated on delete"


def test_server_delete_prunes_session_index(cleanup_test_sessions):
    """session/delete should prune the deleted row without discarding the index."""
    src = (REPO_ROOT / "server.py").read_text()
    routes_src = (REPO_ROOT / "api" / "routes.py").read_text() if (REPO_ROOT / "api" / "routes.py").exists() else ""
    # Find the delete handler in either file
    for label, text in [("server.py", src), ("api/routes.py", routes_src)]:
        # Accept both single-quote and double-quote style (formatting varies by contributor)
        delete_idx = max(
            text.find("if parsed.path == '/api/session/delete':"),
            text.find('if parsed.path == "/api/session/delete":'),
        )
        if delete_idx >= 0:
            delete_block = text[delete_idx:delete_idx+2400]
            assert "prune_session_from_index(sid)" in delete_block, \
                f"{label} session/delete must prune SESSION_INDEX_FILE"
            return
    assert False, "session/delete handler not found in server.py or api/routes.py"


def test_server_delete_removes_session_bak_snapshot(cleanup_test_sessions):
    """session/delete must remove sidecar backups so deleted sessions stay deleted."""
    routes_src = (REPO_ROOT / "api" / "routes.py").read_text()
    delete_idx = max(
        routes_src.find("if parsed.path == '/api/session/delete':"),
        routes_src.find('if parsed.path == "/api/session/delete":'),
    )
    assert delete_idx >= 0, "session/delete handler not found in api/routes.py"
    delete_block = routes_src[delete_idx:delete_idx+2400]
    assert "with_suffix('.json.bak').unlink" in delete_block or 'with_suffix(".json.bak").unlink' in delete_block, \
        "session/delete must unlink <sid>.json.bak to avoid later orphan-backup recovery"

# ── R9: Token/tool SSE events write to wrong session after switch ─────────────

def test_token_handler_guards_session_id(cleanup_test_sessions):
    """R9a: The SSE token event handler must check activeSid before writing to DOM.
    When missing, tokens from session A would render into session B's message area
    if the user switched sessions mid-stream.
    Sprint 12: handler moved into _wireSSE(source), so search source.addEventListener.
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    # Sprint 12 refactored es.addEventListener -> source.addEventListener inside _wireSSE()
    token_idx = src.find("source.addEventListener('token'")
    if token_idx < 0:
        token_idx = src.find("es.addEventListener('token'")
    assert token_idx >= 0, "token event handler not found"
    token_block = src[token_idx:token_idx+300]
    assert "activeSid" in token_block, \
        "token handler must check activeSid before writing to DOM"
    assert "S.session.session_id!==activeSid" in token_block or \
           "S.session.session_id===activeSid" in token_block, \
    "token handler must compare current session to activeSid"


def test_tool_handler_guards_session_id(cleanup_test_sessions):
    """R9b: The SSE tool event handler must check activeSid before writing to DOM.
    When missing, tool cards from session A would render into session B's message area.
    Sprint 12: handler moved into _wireSSE(source), so search source.addEventListener.
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    tool_idx = src.find("source.addEventListener('tool'")
    if tool_idx < 0:
        tool_idx = src.find("es.addEventListener('tool'")
    assert tool_idx >= 0, "tool event handler not found"
    tool_block = src[tool_idx:tool_idx+400]
    assert "activeSid" in tool_block, \
        "tool handler must check activeSid before writing to DOM"


# ── R10: respondApproval uses wrong session_id after switch (multi-session) ─

def test_respond_approval_uses_approval_session_id(cleanup_test_sessions):
    """R10: respondApproval must use the session_id of the session that triggered
    the approval, not S.session.session_id (which may be a different session
    if the user switched while approval was pending).
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    # Click ownership is captured from the visible approval card, then passed
    # immutably into respondApproval rather than re-read after an await.
    capture_idx = src.find("function _captureApprovalResponseOwner(")
    assert capture_idx >= 0, "approval response owner capture helper not found"
    capture_body = src[capture_idx:capture_idx+500]
    assert "const sid = _approvalSessionId" in capture_body
    assert "const approvalId = _approvalCurrentId" in capture_body
    idx = src.find("async function respondApproval(")
    assert idx >= 0, "respondApproval not found"
    fn_body = src[idx:idx+500]
    assert "options.owner || _captureApprovalResponseOwner()" in fn_body
    assert "const {sid, approvalId} = owner" in fn_body


# ── R11: Tool progress must not use shared status chrome ──────────────────

def test_tool_status_only_shown_for_current_session(cleanup_test_sessions):
    """R11: Tool progress should not drive the global status bar or composer
    status. Live tool cards in the current conversation are the authoritative
    progress UI, which avoids cross-session status leakage entirely.
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    # Sprint 12: handler moved into _wireSSE(source)
    tool_idx = src.find("source.addEventListener('tool'")
    if tool_idx < 0:
        tool_idx = src.find("es.addEventListener('tool'")
    assert tool_idx >= 0
    tool_block = src[tool_idx:tool_idx+400]
    assert "setStatus(" not in tool_block, \
        "tool handler should not use the global activity/status bar"
    assert "setComposerStatus(" not in tool_block, \
        "tool handler should not use composer status for tool progress"

# ── R12: Live tool cards lost on switch-away and switch-back ──────────────

def test_loadSession_inflight_restores_live_tool_cards(cleanup_test_sessions):
    """R12: When switching back to an in-flight session, live tool cards in
    #liveToolCards must be restored from S.toolCalls.
    When missing, tool cards disappeared on switch-away even though the session
    was still processing.
    """
    src = (REPO_ROOT / "static/sessions.js").read_text()
    # INFLIGHT branch must call appendLiveToolCard
    # Anchor on the Phase-2 INFLIGHT restore branch (the later occurrence); #3899
    # added an earlier if(INFLIGHT[sid]){ idle-reset block, so .find() would
    # grab the wrong one. (rfind = the substantive restore branch.)
    inflight_idx = src.rfind("if(INFLIGHT[sid]){")
    assert inflight_idx >= 0, "INFLIGHT branch not found in loadSession"
    inflight_block = src[inflight_idx:inflight_idx+4200]
    assert "appendLiveToolCard" in inflight_block,         "loadSession INFLIGHT branch must restore live tool cards via appendLiveToolCard"
    assert "clearLiveToolCards" in inflight_block,         "loadSession INFLIGHT branch must clear old live cards before restoring"

# ── R13: renderMessages() called before S.busy=false in done handler ────────

def test_done_handler_sets_busy_false_before_renderMessages(cleanup_test_sessions):
    """R13: In the done handler, S.busy must be set to false BEFORE renderMessages()
    is called for the active session. The !S.busy guard in renderMessages() controls
    whether settled tool cards are rendered. When S.busy=true during renderMessages(),
    tool cards are skipped entirely after a response completes.
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    # Sprint 12: handler moved into _wireSSE(source)
    done_idx = src.find("source.addEventListener('done'")
    if done_idx < 0:
        done_idx = src.find("es.addEventListener('done'")
    assert done_idx >= 0
    stream_end_idx = src.find("source.addEventListener('stream_end'", done_idx)
    assert stream_end_idx >= 0, "stream_end listener after done handler not found"
    done_block = src[done_idx:stream_end_idx]
    # S.busy=false must appear before the terminal render call within the done handler.
    busy_pos = done_block.find("S.busy=false;")
    render_pos = done_block.find("renderMessages(")
    assert busy_pos >= 0, "done handler must set S.busy=false before renderMessages()"
    assert render_pos >= 0, "done handler must call renderMessages after settling state"
    assert busy_pos < render_pos,         f"S.busy=false (pos {busy_pos}) must come before renderMessages (pos {render_pos})"


# ── R14: send() uses stale modelSelect.value instead of session model ────────

def test_send_uses_session_model_as_authoritative_source(cleanup_test_sessions):
    """R14: send() must use S.session.model as the authoritative model, not just
    $('modelSelect').value. When a session was created with a model not in the
    current dropdown list, the select value would be stale after switching sessions,
    causing the wrong model to be sent.
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    # The model field in the chat/start payload must prefer S.session.model.
    # PR #1591 (May 2026) added optimistic `upsertActiveSessionForLocalTurn`
    # comments that mention `/api/chat/start` BEFORE the actual POST call, so
    # `src.find("/api/chat/start")` may land on a comment occurrence rather
    # than the `api('/api/chat/start',{...})` POST. Match the call signature
    # explicitly to land on the payload block.
    chat_start_idx = src.find("api('/api/chat/start'")
    assert chat_start_idx >= 0, "could not find /api/chat/start POST in messages.js"
    payload_block = src[chat_start_idx:chat_start_idx+400]
    assert "S.session.model" in payload_block, \
        "send() must use S.session.model in the chat/start payload"


# ── R15: newSession does not clear live tool cards ────────────────────────────

def test_newSession_clears_live_tool_cards(cleanup_test_sessions):
    """R15: newSession() must call clearLiveToolCards() so live cards from a
    previous in-flight session don't persist when starting a fresh conversation.
    """
    src = (REPO_ROOT / "static/sessions.js").read_text()
    new_sess_idx = src.find("async function newSession(")
    assert new_sess_idx >= 0
    # Find end of newSession (next async function)
    next_fn = src.find("async function ", new_sess_idx + 10)
    new_sess_body = src[new_sess_idx:next_fn]
    assert "clearLiveToolCards" in new_sess_body,         "newSession() must call clearLiveToolCards() to clear stale live cards"


def test_newSession_resets_busy_state_for_fresh_chat(cleanup_test_sessions):
    """R15b: newSession() must reset the viewed chat to idle state.
    Without this, starting a second chat while another session is streaming leaves
    S.busy=true, so the first send in the new chat gets incorrectly queued.
    """
    src = (REPO_ROOT / "static/sessions.js").read_text()
    new_sess_idx = src.find("async function newSession(")
    assert new_sess_idx >= 0
    next_fn = src.find("async function ", new_sess_idx + 10)
    new_sess_body = src[new_sess_idx:next_fn]
    assert "S.busy=false;" in new_sess_body, \
        "newSession() must clear S.busy so a fresh chat is immediately sendable"
    assert "S.activeStreamId=null;" in new_sess_body, \
        "newSession() must clear the active stream id for the newly viewed chat"
    assert "updateQueueBadge(S.session.session_id);" in new_sess_body, \
        "newSession() must refresh the badge for the new session rather than leaving the old session's queue badge visible"


def test_session_scoped_message_queue_frontend_wiring(cleanup_test_sessions):
    """R15bb: queued follow-ups must stay attached to their originating session.
    The frontend should use a session-keyed queue store and drain only the active
    session's queued messages when that session becomes idle.
    """
    ui_src = (REPO_ROOT / "static/ui.js").read_text()
    messages_src = (REPO_ROOT / "static/messages.js").read_text()
    sessions_src = (REPO_ROOT / "static/sessions.js").read_text()
    assert "const SESSION_QUEUES" in ui_src
    assert "function queueSessionMessage" in ui_src
    assert "function shiftQueuedSessionMessage" in ui_src
    # _queueDrainSid tracks which session's queue to drain even after session switches
    assert "_queueDrainSid" in ui_src
    assert "shiftQueuedSessionMessage(sid)" in ui_src
    assert "queueSessionMessage(S.session.session_id" in messages_src
    assert "updateQueueBadge(S.session.session_id);" in messages_src
    assert "updateQueueBadge(sid);" in sessions_src


def test_queue_card_cross_session_clear_called_before_draft_save(cleanup_test_sessions):
    """R15c: switching away from one session to another should clear the old
    session's queue card before the async draft-save await, so stale DOM cannot
    survive into the destination session.
    """
    src = (REPO_ROOT / "static/sessions.js").read_text()
    block_pattern = re.compile(
        r"if \(currentSid && currentSid !== sid\) \{\s*"
        r"if\(typeof window\._clearPendingSelections==='function'\) window\._clearPendingSelections\(\);\s*"
        r"if\(typeof _clearQueueCardDisplay==='function'\) _clearQueueCardDisplay\(currentSid\);\s*"
        r"await _saveComposerDraftNow\(currentSid",
        re.S,
    )
    assert block_pattern.search(src), (
        "cross-session loadSession path must clear queue card display via"
        " _clearQueueCardDisplay(currentSid) before awaiting _saveComposerDraftNow"
    )


def test_queue_card_cross_session_helper_used_only_for_session_change(cleanup_test_sessions):
    """R15c: _clearQueueCardDisplay(sid) must only fire on cross-session switches,
    not on same-session navigation/force-reload code paths.
    """
    src = (REPO_ROOT / "static/sessions.js").read_text()
    load_start = src.find("async function loadSession(sid){")
    assert load_start >= 0
    load_end = src.find("  // Sync context usage indicator from session data", load_start)
    load_body = src[load_start:load_end]
    cross_start = load_body.find("if (currentSid && currentSid !== sid) {")
    cross_end = load_body.find("if (currentSid !== sid || forceReload) {", cross_start)
    assert cross_start >= 0 and cross_end >= 0
    assert "_clearQueueCardDisplay(currentSid);" in load_body[cross_start:cross_end], (
        "queue-card clear helper must be inside the cross-session branch"
    )
    same_session_idx = load_body.find(
        "if(currentSid===sid && !forceReload && (!_loadingSessionId || _loadingSessionId===sid)){"
    )
    assert same_session_idx >= 0, (
        "same-session no-op guard must still exist (now a block that first clears "
        "a stale unread dot before returning) and must precede the cross-session branch"
    )
    assert same_session_idx < cross_start


def test_queue_card_clear_helper_tracks_render_epoch(cleanup_test_sessions):
    """R15d: the delayed queue clear must only wipe the chips from the render it
    was asked to dismiss, not a later render for the same shared queue DOM.
    """
    src = (REPO_ROOT / "static/ui.js").read_text()
    assert "let _queueRenderEpoch=0;" in src
    assert "if(sid) delete _queueRenderKeys[sid];" in src
    assert "inner.setAttribute('data-queue-render-sid',sid);" in src
    assert "inner.setAttribute('data-queue-render-epoch',String(++_queueRenderEpoch));" in src
    assert "const _epoch=_chips.getAttribute('data-queue-render-epoch')||'';" in src
    assert "(_chips.getAttribute('data-queue-render-sid')||'')===_sid" in src
    assert "(_chips.getAttribute('data-queue-render-epoch')||'')===_epoch" in src


def test_chat_start_persists_pending_turn_metadata_for_reload_recovery(cleanup_test_sessions):
    """R15c: chat/start must expose enough pending-turn metadata for a reload to
    rebuild the in-flight conversation instead of showing a blank session.
    """
    routes_src = (REPO_ROOT / "api/routes.py").read_text()
    assert 's.active_stream_id = stream_id' in routes_src
    assert 's.pending_user_message = msg' in routes_src
    assert 's.pending_attachments = attachments' in routes_src
    assert '"active_stream_id": getattr(s, "active_stream_id", None)' in routes_src
    assert '"pending_user_message": getattr(s, "pending_user_message", None)' in routes_src


def test_session_detail_uses_runtime_streaming_state(cleanup_test_sessions):
    """GET /api/session must agree with /api/sessions on live stream ownership."""
    routes_src = (REPO_ROOT / "api/routes.py").read_text()
    session_route = routes_src.split('def _handle_session_get(', 1)[1].split(
        'def handle_get(', 1
    )[0]
    assert "active_stream_ids = _active_stream_ids()" in session_route
    assert "s.compact(" in session_route
    assert "include_runtime=True" in session_route
    assert "active_stream_ids=active_stream_ids" in session_route


def test_reload_path_restores_pending_message_and_reattaches_live_stream(cleanup_test_sessions):
    """R15d: the frontend reload path must show the pending user turn and
    reattach to the live SSE stream after loadSession().
    """
    sessions_src = (REPO_ROOT / "static/sessions.js").read_text()
    ui_src = (REPO_ROOT / "static/ui.js").read_text()
    messages_src = (REPO_ROOT / "static/messages.js").read_text()
    assert 'getPendingSessionMessage' in ui_src
    assert 'pending_user_message' in ui_src
    assert 'function attachLiveStream' in messages_src
    assert 'const pendingMsg=typeof getPendingSessionMessage' in sessions_src
    # `activeStreamId` declaration was widened const → let (#5248 race-guard
    # re-reads it after the awaited message load). Accept either keyword via a
    # prefix anchor that omits const/let.
    assert ('activeStreamId=data.session.active_stream_id||null;' in sessions_src or
            'activeStreamId=S.session.active_stream_id||null;' in sessions_src)
    assert 'attachLiveStream(sid, activeStreamId' in sessions_src
    assert 'if (S.activeStreamId && S.activeStreamId === streamId) return;' in ui_src
    active_branch_start = sessions_src.index("if(activeStreamId){\n      S.busy=true;")
    active_branch_end = sessions_src.index("}else{\n      S.busy=false;", active_branch_start)
    active_branch = sessions_src[active_branch_start:active_branch_end]
    render_pos = active_branch.index("renderMessages(")
    shell_pos = active_branch.index("ensureLiveWorklogShell")
    assert render_pos < shell_pos, (
        "Reloading an active stream must recreate the live worklog shell after "
        "renderMessages() rebuilds msgInner; otherwise the stream stays invisible "
        "until a session switch triggers another restore path."
    )
    assert "else appendThinking();" in active_branch, (
        "Non-simplified tool-calling must keep the legacy fallback."
    )


# ── R16: Switching away/back must preserve live partial assistant output ─────


def test_live_stream_tokens_persist_partial_assistant_for_session_switch(cleanup_test_sessions):
    """R16: in-flight assistant text must be mirrored into INFLIGHT session state,
    and the live stream must rebind to the rebuilt DOM after switching away and back.
    Without this, partial assistant output disappears until the final done payload lands.
    """
    messages_src = (REPO_ROOT / "static/messages.js").read_text()
    ui_src = (REPO_ROOT / "static/ui.js").read_text()

    # #3455: the persisted partial assistant content is now the think-split content
    # (inline <think> moved to m.reasoning), so the push uses content:split.content
    # where split=_splitThinkFromContent(assistantText, ...). The invariant — partial
    # assistant text is mirrored into INFLIGHT state — is unchanged.
    assert "content:split.content" in messages_src, \
        "messages.js must persist the (think-split) partial assistant text into INFLIGHT state"
    assert "_splitThinkFromContent(assistantText" in messages_src, \
        "the persisted partial must be derived from the live assistantText"
    assert "_live:true" in messages_src, \
        "messages.js must mark the persisted in-flight assistant row so renderMessages can re-anchor it"
    assert "syncInflightAssistantMessage();" in messages_src, \
        "token handler must update INFLIGHT state before checking the active session"
    token_match = re.search(r"source\.addEventListener\('token',e=>\{(.*?)\n\s*\}\);", messages_src, re.S)
    assert token_match, "token listener not found"
    token_fn = token_match.group(1)
    assert token_fn.find("assistantText+=d.text") < token_fn.find("if(!S.session||S.session.session_id!==activeSid) return;"), (
        "token events must update the active stream's local state before DOM-only active-session guards"
    )
    assert token_fn.find("syncInflightAssistantMessage();") < token_fn.find("if(!S.session||S.session.session_id!==activeSid) return;"), (
        "token events must persist INFLIGHT state even while another session is selected"
    )
    assert "assistantRow&&!assistantRow.isConnected" in messages_src, \
        "live stream must drop stale detached assistant DOM references after session switches"
    assert "data-live-assistant" in ui_src, \
        "renderMessages must preserve a live-assistant DOM anchor when rebuilding the thread"
    assert "snapshotLiveTurnHtmlForSession(activeSid)" in messages_src, \
        "live turn DOM snapshots should preserve the interleaved timeline across session switches"
    assert "restoreLiveTurnHtmlForSession(sid)" in (REPO_ROOT / "static/sessions.js").read_text(), \
        "loadSession should restore the live turn snapshot before replaying flat tool cards"


def test_inflight_session_state_tracks_live_tool_cards_per_session(cleanup_test_sessions):
    """R16b: live tool cards must be stored on the in-flight session, not only in the
    global S.toolCalls array, so switching chats does not lose or misattach them.
    """
    messages_src = (REPO_ROOT / "static/messages.js").read_text()
    sessions_src = (REPO_ROOT / "static/sessions.js").read_text()

    assert "inflight.toolCalls.push(tc)" in messages_src, \
        "tool SSE handler must persist live tool calls onto the in-flight session"
    assert "S.toolCalls=(INFLIGHT[sid].toolCalls||[]);" in sessions_src, \
        "loadSession() must restore live tool calls from the in-flight session state"


def test_loadSession_inflight_sets_busy_before_renderMessages(cleanup_test_sessions):
    """R16c: loading an in-flight session must mark it busy before renderMessages().
    Otherwise renderMessages() treats S.toolCalls as settled history cards and the
    same tool call appears once inline and once in the live tool host after a
    session switch.
    """
    src = (REPO_ROOT / "static/sessions.js").read_text()
    # Anchor on the Phase-2 INFLIGHT restore branch (the later occurrence); #3899
    # added an earlier if(INFLIGHT[sid]){ idle-reset block, so .find() would
    # grab the wrong one. (rfind = the substantive restore branch.)
    inflight_idx = src.rfind("if(INFLIGHT[sid]){")
    assert inflight_idx >= 0, "INFLIGHT branch not found in loadSession"
    inflight_block = src[inflight_idx:inflight_idx+4200]
    busy_pos = inflight_block.find("S.busy=")
    # #3326 added an optional {preserveScroll} arg to the INFLIGHT-branch render
    # call, so match the call form rather than the bare `renderMessages();`.
    render_pos = inflight_block.find("renderMessages(")
    assert busy_pos >= 0, "loadSession INFLIGHT branch must set S.busy"
    assert render_pos >= 0, "loadSession INFLIGHT branch must call renderMessages()"
    assert busy_pos < render_pos, \
        "loadSession must set S.busy before renderMessages() to avoid duplicate tool cards"


def test_loadSession_inflight_merges_tail_with_persisted_transcript(cleanup_test_sessions):
    src = (REPO_ROOT / "static/sessions.js").read_text()
    # Anchor on the Phase-2 INFLIGHT restore branch (the later occurrence); #3899
    # added an earlier if(INFLIGHT[sid]){ idle-reset block, so .find() would
    # grab the wrong one. (rfind = the substantive restore branch.)
    inflight_idx = src.rfind("if(INFLIGHT[sid]){")
    assert inflight_idx >= 0, "INFLIGHT branch not found in loadSession"
    inflight_block = src[inflight_idx:inflight_idx+1200]

    assert "await _ensureMessagesLoaded(sid" in inflight_block, (
        "returning to an active stream should load the persisted transcript before adding the live tail"
    )
    assert "_mergeInflightTailMessages(S.messages,inflightMessages)" in inflight_block, (
        "INFLIGHT messages should be merged as a tail, not replace the full transcript"
    )
    assert "function _mergeInflightTailMessages" in src, (
        "sessions.js should centralize INFLIGHT tail merge logic for regression coverage"
    )


def test_renderMessages_preserves_loading_placeholder_for_session_switch(cleanup_test_sessions):
    """R16d: renderMessages should not repaint transcript during session-load window.

    During loadSession(sid) after the loading metadata call, S.messages is
    intentionally empty until _ensureMessagesLoaded(sid) settles. A concurrent
    renderMessages() must keep the existing 'Loading conversation...' placeholder
    instead of clearing #msgInner to an empty transcript.
    """
    ui_src = (REPO_ROOT / "static/ui.js").read_text()
    fn_start = ui_src.find("function renderMessages")
    assert fn_start >= 0, "renderMessages() not found in ui.js"
    fn_body = ui_src[fn_start:fn_start + 1400]

    compact = re.sub(r"\s+", "", fn_body)
    assert (
        "if(_loadingSessionId===sid&&msgCount===0&&inner)return;" in compact
    ), (
        "renderMessages() must return early when loadSession is active for"
        " the current sid and S.messages is still empty."
    )

    # Guard must live before render-window reset and message-filter pass.
    reset_pos = compact.find("if(sid!==_messageRenderWindowSid)_resetMessageRenderWindow(sid);")
    guard_pos = compact.find("if(_loadingSessionId===sid&&msgCount===0&&inner)return;")
    assert (
        0 <= guard_pos < reset_pos
    ), "Session-load empty-state guard must run before render-window/state resets."


def test_browser_session_url_accepts_api_session_id_param(cleanup_test_sessions):
    """External links using ?session_id=... should open that session in the browser.

    The API endpoint uses `session_id`, while the browser URL historically used
    `session`/`/session/<id>`. Auth/cookie bridges and external callers can
    legitimately produce `/?session_id=<sid>`; ignoring it falls back to stale
    localStorage and renders the wrong or empty conversation.
    """
    src = (REPO_ROOT / "static/sessions.js").read_text()
    start = src.find("function _sessionIdFromLocation")
    assert start >= 0, "session URL parser not found"
    end = src.find("function _sessionUrlForSid", start)
    assert end > start, "session URL parser block end not found"
    block = src[start:end]
    assert "qs.get('session')" in block or 'qs.get("session")' in block
    assert "qs.get('session_id')" in block or 'qs.get("session_id")' in block


def test_inflight_merge_dedupes_uploaded_user_message(cleanup_test_sessions):
    """Uploaded-file turns render optimistically before the server stores the
    final pending text with an `[Attached files: ...]` suffix.  The INFLIGHT
    merge must treat those as the same user turn instead of rendering both.
    """
    src = (REPO_ROOT / "static/sessions.js").read_text()
    assert "function _normalizeUserTranscriptText" in src, (
        "sessions.js should normalize user transcript text before deduping user turns"
    )
    assert "_stripAttachedFilesMarker(_stripForcedSkillEnvelope(text))" in src, (
        "user transcript normalization should still remove the server-side attached-files suffix"
    )
    assert "_normalizeUserTranscriptText(aText)===_normalizeUserTranscriptText(bText)" in src, (
        "INFLIGHT user-message comparison should dedupe optimistic upload text against final pending text"
    )
    assert "role==='user'" in src, (
        "attached-files normalization should be limited to user turns"
    )
    pending_idx = src.find("function _mergePendingSessionMessage")
    assert pending_idx >= 0, "pending session merge helper not found"
    pending_block = src[pending_idx:pending_idx+500]
    assert "_hasCurrentTailUserDuplicate(currentTurnMessages,pendingMsg)" in pending_block, (
        "pending-user merge should dedupe only against the current active-turn user row"
    )
    assert "messages.some(" not in pending_block, (
        "pending-user merge must not scan historical user rows by normalized content"
    )


def test_loadSession_inflight_sets_active_stream_before_replaying_live_tool_cards(cleanup_test_sessions):
    """#1715: returning to an active chat must replay persisted tool cards.

    appendLiveToolCard() intentionally no-ops unless S.activeStreamId is already
    set for the viewed streaming session. If loadSession() restores S.toolCalls
    and replays them before assigning S.activeStreamId, the compact Activity
    counter drops the previously-seen tools after a focus change.
    """
    src = (REPO_ROOT / "static/sessions.js").read_text()
    # Anchor on the Phase-2 INFLIGHT restore branch (the later occurrence); #3899
    # added an earlier if(INFLIGHT[sid]){ idle-reset block, so .find() would
    # grab the wrong one. (rfind = the substantive restore branch.)
    inflight_idx = src.rfind("if(INFLIGHT[sid]){")
    assert inflight_idx >= 0, "INFLIGHT branch not found in loadSession"
    inflight_block = src[inflight_idx:inflight_idx+4200]
    active_pos = inflight_block.find("S.activeStreamId=activeStreamId;")
    replay_pos = inflight_block.find("const replayPersistedLiveToolCards=(opts)=>{")
    attach_pos = inflight_block.find("attachLiveStream(sid, activeStreamId")
    assert active_pos >= 0, "loadSession INFLIGHT branch must restore S.activeStreamId"
    assert replay_pos >= 0, "loadSession INFLIGHT branch must replay persisted live tool cards"
    assert active_pos < replay_pos, \
        "S.activeStreamId must be restored before replaying persisted tools"
    assert attach_pos < 0 or active_pos < attach_pos, \
        "S.activeStreamId should also be restored before SSE reattach can deliver more tool events"


def test_streaming_bridge_accepts_current_tool_progress_callback_signature(cleanup_test_sessions):
    """R17: api/streaming.py must accept the current Hermes agent callback contract.
    The agent now calls tool_progress_callback(event_type, name, preview, args, **kwargs).
    If the WebUI bridge only accepts (name, preview, args), live tool updates silently vanish.
    """
    src = (REPO_ROOT / "api/streaming.py").read_text()
    assert "def on_tool(*cb_args, **cb_kwargs):" in src, \
        "streaming.py must accept variable callback args for tool progress events"
    assert "reasoning_callback=on_reasoning" in src, \
        "streaming.py must wire the agent's reasoning callback into the SSE bridge"
    assert "put('tool_complete'" in src or 'put("tool_complete"' in src, \
        "streaming.py must emit live tool completion SSE events"


def test_streaming_reads_reasoning_effort_from_config_dict(cleanup_test_sessions):
    """R17b: WebUI must read agent.reasoning_effort from the dict returned by get_config().

    `get_config()` returns a plain dict (not a wrapper exposing `.cfg`).  The
    pre-fix line `_cfg.cfg.get('agent', {})` raised AttributeError that the
    surrounding try/except swallowed, so `_reasoning_config` was always None
    regardless of what `/reasoning <level>` had been set to.  This static
    source assertion pins the fix because the runtime symptom is silent.
    """
    src = (REPO_ROOT / "api/streaming.py").read_text()
    assert "_cfg.cfg" not in src, \
        "get_config() returns a dict; accessing _cfg.cfg drops reasoning_config to None"
    assert "_cfg.get('agent', {})" in src or '_cfg.get("agent", {})' in src, \
        "streaming.py must read agent.reasoning_effort via the config dict"


def test_streaming_agent_cache_signature_includes_reasoning_config(cleanup_test_sessions):
    """R17c: changing reasoning effort mid-session must rebuild the cached per-session agent.

    Without `_reasoning_config` participating in `_sig_blob`, the cache key
    matches the old entry and the operator's `/reasoning xhigh` change has
    no effect on the live session.
    """
    src = (REPO_ROOT / "api/streaming.py").read_text()
    start = src.find("_sig_blob = _json.dumps")
    end = src.find("_agent_sig", start)
    assert start >= 0 and end > start, "agent cache signature block not found"
    sig_block = src[start:end]
    assert "_reasoning_config" in sig_block, \
        "agent cache signature must include reasoning_config so xhigh/medium changes take effect"


def test_messages_js_supports_live_reasoning_and_tool_completion(cleanup_test_sessions):
    """R18: messages.js must render live reasoning and react to tool completion events.
    Without these handlers, the operator only sees generic Thinking… or nothing
    until the final done snapshot redraws the whole turn.
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    # reasoningText is initialised at closure scope in attachLiveStream.
    # On initial connect it defaults to ''; on reconnect it restores from
    # INFLIGHT so the already-rendered content survives the session switch.
    assert ("let reasoningText=''" in src
            or "let reasoningText = _lastLiveAssistant" in src
            or "let reasoningText=_lastLiveReasoning" in src), \
        "messages.js must track streamed reasoning text separately from assistant text"
    assert ("let liveReasoningText=''" in src
            or "let liveReasoningText = reasoningText" in src
            or "let liveReasoningText=_lastLiveReasoning" in src), \
        "messages.js must track the currently active reasoning segment separately from cumulative reasoning"
    assert "source.addEventListener('reasoning'" in src or 'source.addEventListener("reasoning"' in src, \
        "messages.js must listen for live reasoning SSE events"
    assert "liveReasoningText += text" in src, \
        "live reasoning SSE events must update the active Worklog Thinking Card text"
    assert "const liveThinkingText=_liveThinkingText();" in src, \
        "live reasoning SSE events must compute the current segment's Worklog Thinking Card text once"
    assert "const anchorReasoningFallback={};" in src, \
        "live reasoning SSE events must capture the active anchor id for fallback"
    assert "if(!_upsertAnchorReasoning(liveThinkingText, anchorReasoningFallback))" in src, \
        "live reasoning SSE events must prefer the anchor renderer before falling back"
    assert "_updateLiveThinkingCard(liveThinkingText,{" in src and "...anchorReasoningFallback" in src, \
        "live reasoning SSE events must carry anchor identity into the fallback renderer"
    assert "anchorRenderFallback:true" in src and "sessionId:activeSid" in src and "streamId" in src, \
        "live reasoning SSE events must keep the current segment's Worklog Thinking Card as fallback"
    assert "source.addEventListener('tool_complete'" in src or 'source.addEventListener("tool_complete"' in src, \
        "messages.js must listen for live tool completion SSE events"
    assert "function _parseStreamState()" in src, \
        "messages.js must parse live stream state into reasoning + visible answer"


def test_messages_js_supports_interim_assistant_events(cleanup_test_sessions):
    """R18b: messages.js must render live interim assistant commentary when
    `interim_assistant` SSE events arrive.

    AIAgent emits completed mid-turn commentary through an interim callback.
    Without a dedicated SSE handler, Codex-style interim status text disappears
    from the live answer and users only see the final response after tool calls.
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    assert "source.addEventListener('interim_assistant'" in src or 'source.addEventListener("interim_assistant"' in src, \
        "messages.js must listen for interim_assistant SSE events"
    assert "function _resetAssistantSegment()" in src, \
        "messages.js should share live-segment reset logic between interim assistant updates and tool events"
    assert "_resetAssistantSegment();" in src, \
        "messages.js should apply segment reset when tool or interim assistant events require it"


def test_ui_js_can_upgrade_thinking_spinner_into_live_reasoning_card(cleanup_test_sessions):
    """R19: ui.js keeps the thinking helpers available while simplified mode
    renders provider reasoning as a Worklog Thinking Card.
    """
    src = (REPO_ROOT / "static/ui.js").read_text()
    assert "function _thinkingMarkup(text='')" in src or 'function _thinkingMarkup(text="")' in src, \
        "ui.js must centralize thinking row markup so it can switch between spinner and live text"
    assert ("function updateThinking(text='', options){appendThinking(text, options);}" in src
            or 'function updateThinking(text="", options){appendThinking(text, options);}' in src), \
        "ui.js must expose an updateThinking helper that preserves live Thinking placement metadata"
    assert "function finalizeThinkingCard()" in src, \
        "ui.js must expose a helper to finalize one live thinking card before starting another"


def test_ui_js_keeps_split_thinking_cards_and_assistant_header(cleanup_test_sessions):
    """R19b: settled render should keep distinct thinking cards for split assistant
    turns inside a single assistant turn container, preserving one assistant header
    for the whole response while keeping multiple thinking cards distinct.
    """
    src = (REPO_ROOT / "static" / "ui.js").read_text()
    assert "pendingTurnThinking" not in src, \
        "renderMessages must not merge distinct thinking blocks into one settled card"
    assert "_createAssistantTurn(" in src, \
        "renderMessages must build a shared assistant turn wrapper instead of separate top-level rows"
    assert "assistant-segment" in src, \
        "settled assistant turns must preserve per-message segments for multiple thinking/tool/result blocks"


def test_ui_js_keeps_reasoning_only_assistant_messages_visible(cleanup_test_sessions):
    """R19c: assistant messages that only contain reasoning must still survive
    rerenders, otherwise prior thinking cards disappear on the next turn.
    """
    src = (REPO_ROOT / "static" / "ui.js").read_text()
    assert "function _messageHasReasoningPayload(m)" in src, \
        "ui.js must detect reasoning-only assistant messages"
    assert "hasTc||hasTu||_messageHasReasoningPayload(m)" in src.replace(' ', ''), \
        "renderMessages visibility filter must preserve reasoning-only assistant messages"


def test_ui_js_does_not_hide_anchor_segments_that_contain_thinking(cleanup_test_sessions):
    """R19c2/R19c3: reasoning-only metadata must remain preserved as a
    collapsed Worklog Thinking Card.
    """
    src = (REPO_ROOT / "static" / "ui.js").read_text()
    compact = src.replace(' ', '').replace('\n', '')
    assert "assistantThinking.set(rawIdx,thinkingText)" in compact, \
        "renderMessages must preserve reasoning text before hiding empty anchor segments"
    helper_start = src.find("function _worklogReasoningTextFromMessage")
    helper_end = src.find("function _thinkingCardHtml", helper_start)
    assert helper_start != -1 and helper_end != -1
    helper = src[helper_start:helper_end]
    assert "_assistantReasoningPayloadText(m)" in helper and "_stripVisibleAssistantEchoFromThinking" in helper, \
        "provider reasoning metadata should feed the Worklog Thinking Card after exact duplicate suppression"
    assert "data-worklog-thinking-card" in src, \
        "Thinking Cards should be explicit Worklog items, not tool cards"
    assert "_thinkingActivityNode(thinkingText, false, thinkingDisclosureKey)" in src, \
        "settled reasoning should render as a collapsed Worklog Thinking Card"


def test_messages_js_live_assistant_segment_reuses_live_turn_wrapper(cleanup_test_sessions):
    """R19d: live streaming must reuse the existing live assistant turn wrapper created
    by appendThinking(), otherwise the header gets recreated when answer tokens start.
    """
    src = (REPO_ROOT / "static" / "messages.js").read_text()
    assert "function ensureAssistantRow(force=false)" in src or 'function ensureAssistantRow(force = false)' in src, \
        "ensureAssistantRow should manage the live assistant content segment"
    assert "let turn=$('liveAssistantTurn');" in src, \
        "ensureAssistantRow must bind to the existing live assistant turn wrapper"
    assert "appendThinking();" in src, \
        "ensureAssistantRow should create the live turn via appendThinking() when needed"
    assert "assistantRow.className='assistant-segment';" in src or 'assistantRow.className = \'assistant-segment\';' in src, \
        "live answer content should be appended as a segment inside the live turn wrapper"
    assert "if(!force&&!assistantRow){" in src.replace(' ', ''), \
        "ensureAssistantRow must still avoid creating the live answer segment when no display text exists yet"
    token_start = src.find("source.addEventListener('token'")
    interim_start = src.find("source.addEventListener('interim_assistant'", token_start)
    assert token_start >= 0 and interim_start > token_start
    token_body = src[token_start:interim_start]
    compact_token_body = token_body.replace(" ", "").replace("\n", "")
    assert "if(assistantRow){ensureAssistantRow();_scheduleRender();}" in compact_token_body, \
        "token handler should skip the per-token full-text parse after the live answer segment exists"
    assert "constparsed=_parseStreamState();if(String((parsed&&parsed.displayText)||'').trim())ensureAssistantRow();_scheduleRender(parsed);" in compact_token_body, \
        "token handler must only create the live answer segment once visible answer text starts"


def test_messages_js_stream_perf_cleanup_lifecycle(cleanup_test_sessions):
    """#5455 review: throttled snapshot timers and incremental anchor caches tear down at terminal events."""
    src = (REPO_ROOT / "static/messages.js").read_text()
    assert "function _cancelThrottledSnapshotTimer()" in src
    assert "clearTimeout(_snapshotLiveTurnTimer)" in src
    assert "function _clearAnchorProseIncrementalNode()" in src
    assert "window.__anchorProseIncrementalNode===_anchorProseIncrementalNode" in src
    assert "_anchorProseSmdCache.clear();" in src
    fallback_start = src.find("function _finalizeStreamEndFallback")
    recovery_start = src.find("async function _runStreamEndRecovery", fallback_start)
    assert fallback_start >= 0 and recovery_start > fallback_start
    fallback_body = src[fallback_start:recovery_start]
    assert "_cancelThrottledSnapshotTimer();" in fallback_body
    assert "_clearAnchorProseIncrementalNode();" in fallback_body
    done_start = src.find("source.addEventListener('done'")
    stream_end_start = src.find("source.addEventListener('stream_end'", done_start)
    assert done_start >= 0 and stream_end_start > done_start
    done_body = src[done_start:stream_end_start]
    assert "_cancelThrottledSnapshotTimer();" in done_body
    assert "_clearAnchorProseIncrementalNode();" in done_body

    # #5466 Codex gate: the snapshot/anchor cleanup must run on EVERY terminal
    # path, not just fallback/done/stream_end — otherwise a timer/cache/global
    # survives the turn on apperror, cancel, stream-error, and the settled-session
    # recovery path (the PR's own stated teardown invariant).
    def _terminal_body(anchor, end_marker):
        a = src.find(anchor)
        assert a >= 0, f"missing terminal handler: {anchor}"
        b = src.find(end_marker, a)
        assert b > a, f"could not bound terminal handler: {anchor}"
        return src[a:b]

    apperror_body = _terminal_body("source.addEventListener('apperror'", "_streamFadeCleanupReduceMotionListener();")
    assert "_cancelThrottledSnapshotTimer();" in apperror_body and "_clearAnchorProseIncrementalNode();" in apperror_body, \
        "apperror terminal handler must tear down the snapshot timer + anchor prose cache"
    cancel_body = _terminal_body("source.addEventListener('cancel'", "_streamFadeCleanupReduceMotionListener();")
    assert "_cancelThrottledSnapshotTimer();" in cancel_body and "_clearAnchorProseIncrementalNode();" in cancel_body, \
        "cancel terminal handler must tear down the snapshot timer + anchor prose cache"
    stream_error_body = _terminal_body("function _handleStreamError(source)", "_streamFadeCleanupReduceMotionListener();")
    assert "_cancelThrottledSnapshotTimer();" in stream_error_body and "_clearAnchorProseIncrementalNode();" in stream_error_body, \
        "_handleStreamError must tear down the snapshot timer + anchor prose cache"
    restore_body = _terminal_body("async function _restoreSettledSession(source", "_cancelAnimationFramePendingStreamRender();")
    assert "_cancelThrottledSnapshotTimer();" in restore_body and "_clearAnchorProseIncrementalNode();" in restore_body, \
        "_restoreSettledSession terminal recovery must tear down the snapshot timer + anchor prose cache"


def test_messages_js_finalizes_thinking_card_before_tool_card(cleanup_test_sessions):
    """R19e: later reasoning after a tool call must render in a fresh Worklog
    Thinking Card without discarding durable reasoning.
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    tool_start = src.find("source.addEventListener('tool'")
    tool_complete_start = src.find("source.addEventListener('tool_complete'", tool_start + 1)
    assert tool_start >= 0 and tool_complete_start > tool_start
    body = src[tool_start:tool_complete_start]
    assert "finalizeThinkingCard()" in body, \
        "tool handler must finalize the current live thinking card before appending a tool card"
    assert "liveReasoningText='';" in body or 'liveReasoningText = "";' in body, \
        "tool handler must reset the active reasoning segment before post-tool reasoning arrives"
    assert "reasoningText=''" not in body and 'reasoningText = ""' not in body, \
        "tool handler must not discard durable reasoning already assigned to the Worklog"


# ── R17: Stack traces must not leak to clients in 500 responses ────────────

def test_500_response_has_no_trace_field():
    """R16: HTTP 500 responses must not include a 'trace' field.
    Leaking tracebacks exposes file paths, module names, and potentially
    secret values from local variables.
    """
    # POST to /api/chat/start with missing required fields to trigger an error
    data, status = post("/api/chat/start", {})
    # Should be an error response (4xx or 5xx)
    assert "trace" not in data, \
        "Server must not leak stack traces to clients"

def test_upload_error_has_no_trace_field():
    """R16b: Upload 500 responses must not include a 'trace' field."""
    # Send a POST to /api/upload with invalid content to trigger the error handler
    req = urllib.request.Request(
        BASE + "/api/upload",
        data=b"not-multipart-data",
        headers={"Content-Type": "text/plain", "Content-Length": "18"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
            code = r.status
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        code = e.code
    assert code >= 400, "Invalid upload should return an error status"
    assert "trace" not in body, \
        "Upload errors must not leak stack traces to clients"
    assert "error" in body, "Error responses must include an 'error' key"


# ── #248: /skills slash command ───────────────────────────────────────────────

def test_skills_slash_command_defined():
    """#248: /skills slash command must be wired up.

    Pre-Task 2 (slash-command-parity batch 1) this checked for the
    hardcoded ``name:'skills'`` entry in the COMMANDS array. The COMMANDS
    array is now sourced from hermes-agent's ``COMMAND_REGISTRY`` at boot
    via ``GET /api/commands``, so the literal string is gone. The handler
    must still exist and be registered, otherwise ``/skills`` would fall
    through to \"not yet supported\".
    """
    src = (REPO_ROOT / "static/commands.js").read_text()

    # 1. cmdSkills function must be defined
    assert "async function cmdSkills" in src or "function cmdSkills" in src, \
        "cmdSkills function missing from commands.js"

    # 2. HANDLERS.skills must be registered to dispatch /skills to cmdSkills
    assert "HANDLERS.skills" in src, \
        "HANDLERS.skills registration missing from commands.js"


def test_skills_dropdown_options_have_distinct_per_option_descriptions():
    """/skills' dropdown must surface the write-approval subcommands (pending,
    approve, reject, diff, approval, mode) the same way /goal and /reasoning
    surface their own static subArgs -- otherwise they're only usable by typing
    them from memory, with no discovery path in the chat box's autocomplete.

    It must ALSO show what each subcommand actually does, not the same generic
    /skills description six times over -- the pre-existing bug every other
    static-subArgs command (e.g. /goal) still has, deliberately left alone here
    as separate, larger follow-up work (fixing it for all of them touches the
    shared getSlashAutocompleteMatches() dispatch, not just this one command).

    Real execution via node, not a source-string check: proves the actual
    getSlashAutocompleteMatches() output, and that /goal's existing behavior
    (all its options sharing the parent desc) is unchanged.
    """
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    src = (REPO_ROOT / "static/commands.js").read_text()
    skills_entry = _js_block(src, "{name:'skills',", "\n  {name:'use',")
    goal_entry = _js_block(src, "{name:'goal',", "\n  {name:'queue',")
    get_options_fn = _js_block(src, "function _getSlashSubArgOptions(spec){", "\nfunction _activeSlashCommandOffset")
    offset_fn = _js_block(src, "function _activeSlashCommandOffset(text){", "\nfunction _parseSlashAutocomplete")
    parse_fn = _js_block(src, "function _parseSlashAutocomplete(text){", "\nasync function getSlashAutocompleteMatches")
    autocomplete_fn = _js_block(src, "async function getSlashAutocompleteMatches(text){", "\nfunction _findComposerPathToken")

    harness = textwrap.dedent(
        """
        function t(k){ return k; }
        function getMatchingCommands(){ return []; }
        function cmdSkills(){}
        function cmdGoal(){}
        const COMMANDS = [
          %(skills_entry)s
          %(goal_entry)s
        ];

        %(offset_fn)s
        %(parse_fn)s
        %(get_options_fn)s
        %(autocomplete_fn)s

        (async () => {
          const skillsMatches = await getSlashAutocompleteMatches('/skills ');
          const goalMatches = await getSlashAutocompleteMatches('/goal ');

          const skillsDescByValue = Object.fromEntries(skillsMatches.map(m => [m.value, m.desc]));
          const skillsDescSet = new Set(skillsMatches.map(m => m.desc));
          const goalDescSet = new Set(goalMatches.map(m => m.desc));

          console.log(JSON.stringify({
            skillsCount: skillsMatches.length,
            skillsAllDistinct: skillsDescSet.size === skillsMatches.length,
            skillsPendingDesc: skillsDescByValue['pending'] || null,
            skillsApproveDesc: skillsDescByValue['approve'] || null,
            skillsDescEqualsParent: skillsMatches.some(m => m.desc === 'cmd_skills'),
            goalCount: goalMatches.length,
            goalAllShareParentDesc: goalDescSet.size === 1 && goalMatches[0].desc === 'cmd_goal',
          }));
        })();
        """
    ) % {
        "skills_entry": skills_entry,
        "goal_entry": goal_entry,
        "offset_fn": offset_fn,
        "parse_fn": parse_fn,
        "get_options_fn": get_options_fn,
        "autocomplete_fn": autocomplete_fn,
    }

    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip())
    assert out["skillsCount"] == 6, "/skills must offer all six write-approval subcommands"
    assert out["skillsAllDistinct"] is True, \
        "each /skills subcommand must show its OWN description, not a repeated generic one"
    assert out["skillsDescEqualsParent"] is False, \
        "/skills options must not fall back to the parent command's own description"
    assert out["skillsPendingDesc"] and out["skillsPendingDesc"] != out["skillsApproveDesc"], \
        "pending and approve must have distinct, real descriptions"
    # Unchanged commands (bare-string subArgs) keep sharing the parent desc -- this is the
    # documented pre-existing limitation, not something this change was meant to fix.
    assert out["goalCount"] == 4
    assert out["goalAllShareParentDesc"] is True, \
        "/goal's bare-string subArgs must keep its existing (shared-desc) behavior unchanged"


def test_memory_dropdown_lists_write_approval_subcommands():
    """/memory has no local COMMANDS entry (unlike /skills), so its dropdown must come
    from SLASH_SUBARG_SOURCES -- the same fallback map /model and /personality already
    use for commands with no local handler function. Without this, /memory pending etc.
    are only usable by typing them from memory (no pun intended), with zero discovery
    path in the chat box's autocomplete -- exactly the gap /skills had before its own
    subArgs were added.

    Real execution via node: proves the actual getSlashAutocompleteMatches() output for
    a SLASH_SUBARG_SOURCES-sourced command, not a source-string check.
    """
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    src = (REPO_ROOT / "static/commands.js").read_text()
    subarg_sources = _js_block(src, "const SLASH_SUBARG_SOURCES={", "\n};") + "\n};"
    get_options_fn = _js_block(src, "function _getSlashSubArgOptions(spec){", "\nfunction _activeSlashCommandOffset")
    offset_fn = _js_block(src, "function _activeSlashCommandOffset(text){", "\nfunction _parseSlashAutocomplete")
    parse_fn = _js_block(src, "function _parseSlashAutocomplete(text){", "\nasync function getSlashAutocompleteMatches")
    autocomplete_fn = _js_block(src, "async function getSlashAutocompleteMatches(text){", "\nfunction _findComposerPathToken")

    harness = textwrap.dedent(
        """
        function t(k){ return k; }
        const COMMANDS = [];

        %(subarg_sources)s
        %(offset_fn)s
        %(parse_fn)s
        %(get_options_fn)s
        %(autocomplete_fn)s

        (async () => {
          const memoryMatches = await getSlashAutocompleteMatches('/memory ');
          const descByValue = Object.fromEntries(memoryMatches.map(m => [m.value, m.desc]));
          const descSet = new Set(memoryMatches.map(m => m.desc));

          console.log(JSON.stringify({
            values: memoryMatches.map(m => m.value).sort(),
            allDistinct: descSet.size === memoryMatches.length,
            pendingDesc: descByValue['pending'] || null,
            approveDesc: descByValue['approve'] || null,
          }));
        })();
        """
    ) % {
        "subarg_sources": subarg_sources,
        "offset_fn": offset_fn,
        "parse_fn": parse_fn,
        "get_options_fn": get_options_fn,
        "autocomplete_fn": autocomplete_fn,
    }

    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip())
    assert out["values"] == ["approval", "approve", "mode", "pending", "reject"], (
        "/memory's dropdown must offer its five write-approval subcommands "
        "(no 'diff' -- memory entries are reviewed inline)")
    assert out["allDistinct"] is True, "each /memory subcommand must show its OWN description"
    assert out["pendingDesc"] and out["pendingDesc"] != out["approveDesc"]


def _js_block(source: str, start_marker: str, end_marker: str) -> str:
    """Slice a JS source string between two exact markers (inclusive of start)."""
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_memory_command_routed_through_webui_agent_command_allowlist():
    """/memory has no client-side handler at all in commands.js (unlike /skills'
    local search), so it must be reachable via messages.js'
    _AGENT_COMMANDS_RUN_ON_WEBUI allowlist -- otherwise it falls straight through
    to plain chat text with no clue anything went wrong (/skills at least had a
    visibly wrong search result pointing at the bug; /memory had nothing)."""
    src = (REPO_ROOT / "static/messages.js").read_text()
    allowlist_line = _js_block(
        src, "const _AGENT_COMMANDS_RUN_ON_WEBUI", "\n\n") or ""
    assert "'memory'" in allowlist_line, \
        "/memory must be in _AGENT_COMMANDS_RUN_ON_WEBUI so it reaches executeAgentCommand()"
    # And commands.js must NOT have grown a competing local /memory handler --
    # if it ever does, that handler (like cmdSkills) becomes solely responsible
    # for reaching the write-approval store, same as this PR found for /skills.
    commands_src = (REPO_ROOT / "static/commands.js").read_text()
    assert "name:'memory'" not in commands_src, (
        "a local /memory COMMANDS entry appeared -- it must dispatch write-approval "
        "subcommands itself (like cmdSkills does) or /memory will silently stop "
        "reaching handle_pending_subcommand()")


def test_skills_write_approval_response_targets_owner_session_not_current():
    """A `/skills approve <id>` reply landing after the user has switched sessions
    must NOT be appended to whichever session happens to be open when the async
    /api/commands/exec call resolves -- it must be withheld with a visible warning, the same
    owner-session guard the delayed steer paths use (_steerOwnerIsCurrent). Real execution via a
    node harness, not a mock of the guard itself: proves the actual message array is left
    untouched and a warning is emitted.
    """
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    src = (REPO_ROOT / "static/commands.js").read_text()
    cmd_skills_fn = _js_block(src, "function cmdSkills(args){", "\nasync function cmdUse")
    steer_owner_fn = _js_block(src, "function _steerOwnerIsCurrent(ownerSid){", "\nfunction _steerOwnerStreamIsCurrent")
    subcommands_decl = src[src.index("const SKILLS_AGENT_SUBCOMMANDS="):src.index("\n\nfunction cmdSkills")]

    harness = textwrap.dedent(
        """
        %(subcommands_decl)s
        %(steer_owner_fn)s

        let resolveTransport;
        function _runAgentCommandTransport(text){
          return new Promise((resolve) => { resolveTransport = resolve; });
        }

        const S = { session: { session_id: 'sid-A' }, messages: [] };
        let renderCount = 0;
        let toastCount = 0;
        function renderMessages(){ renderCount++; }
        function showToast(){ toastCount++; }

        %(cmd_skills_fn)s

        const returned = cmdSkills('approve abc123');

        // User switches sessions before the /api/commands/exec response lands --
        // loadSession() swaps in a fresh session object AND a fresh messages array.
        S.session = { session_id: 'sid-B' };
        S.messages = [];

        resolveTransport('Approved 1 skill write(s).');

        // Let the microtask queue drain so the async IIFE's .then chain runs.
        setTimeout(() => {
          console.log(JSON.stringify({
            returnedTrueSynchronously: returned === true,
            newSessionMessages: S.messages.length,
            renderCalledAfterSwitch: renderCount,
            warningShownAfterSwitch: toastCount,
          }));
        }, 20);
        """
    ) % {
        "subcommands_decl": subcommands_decl,
        "steer_owner_fn": steer_owner_fn,
        "cmd_skills_fn": cmd_skills_fn,
    }

    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip())
    assert out["returnedTrueSynchronously"] is True, \
        "cmdSkills must return true synchronously so the caller doesn't treat it as a fallthrough"
    assert out["newSessionMessages"] == 0, (
        "the response for sid-A's command was appended to sid-B's (the now-current "
        "session's) messages array -- it must be dropped instead")
    assert out["renderCalledAfterSwitch"] == 0, \
        "renderMessages() must not run for a response whose owner session is no longer current"
    assert out["warningShownAfterSwitch"] == 1, \
        "a withheld response must produce a visible non-sensitive warning"

def _run_webui_agent_command_scenarios():
    """Execute the REAL awaited WebUI agent-command block from static/messages.js (the
    `_AGENT_COMMANDS_RUN_ON_WEBUI` path that /memory, /credits, /reload-mcp ... use) in node,
    once per ownership scenario, and return each scenario's observable end state.

    The block is sliced verbatim out of send() (from the `sessions`/`resume` branch to the
    Plugin branch), so this drives production code, not a copy of the guard."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    src = (REPO_ROOT / "static/messages.js").read_text()
    block = _js_block(
        src,
        "if(_parsedCmd.name==='sessions' || _parsedCmd.name==='resume'){",
        "if(_agentCmd&&_agentCmd.category==='Plugin'){",
    )
    allowlist_start = src.index("const _AGENT_COMMANDS_RUN_ON_WEBUI")
    allowlist_end = src.index("]);", allowlist_start) + 3
    allowlist_decl = src[allowlist_start:allowlist_end]

    harness = textwrap.dedent(
        """
        %(allowlist_decl)s
        function cliOnlyCommandResponse(){ return 'cli-only'; }

        function makeEnv(initialSession){
          const env = {
            S: { session: initialSession, activeProfile: 'default', messages: [] },
            composer: { value: '/memory pending' },
            draftRevision: 0,
            renders: 0,
            toasts: 0,
            stashes: [],
            clears: [],
            executeCalls: 0,
            transcripts: {},
            pending: {},
          };
          return env;
        }

        async function runScenario(name, opts){
          const env = makeEnv(opts.initialSession === undefined ? { session_id: 'sid-A' } : opts.initialSession);
          const S = env.S;
          const $ = () => env.composer;
          const autoResize = () => {};
          const hideCmdDropdown = () => {};
          const renderMessages = () => { env.renders++; };
          const showToast = () => { env.toasts++; };
          const _stashApprovalTransportFailure = (profile, sid, text, files) => {
            env.stashes.push({ profile, sid, text, files });
            return true;
          };
          const _clearComposerDraft = (sid, text, files) => { env.clears.push({ sid, text, files }); };
          const _composerDraftRevision = () => env.draftRevision;
          const renderSessionList = async () => {};
          const newSession = async () => { S.session = { session_id: 'sid-NEW' }; S.messages = []; };
          const getAgentCommandMetadata = () => new Promise((res) => {
            env.pending.metadata = () => res({ name: 'memory' });
            if (opts.metadataImmediate) env.pending.metadata();
          });
          const executeAgentCommand = () => new Promise((res, rej) => {
            env.executeCalls++;
            env.pending.execute = () => opts.executeReject
              ? rej(new Error('offline'))
              : res('memory result');
          });
          const text = '/memory pending';
          const _parsedCmd = { name: 'memory', args: 'pending' };

          const run = async () => {
            %(block)s
            return 'fell-through';
          };

          const done = run();
          await new Promise((r) => setTimeout(r, 5));
          const snapshotBefore = { composer: env.composer.value };
          await opts.during(env, S);
          if (env.pending.metadata && !opts.metadataImmediate) env.pending.metadata();
          await new Promise((r) => setTimeout(r, 5));
          if (env.pending.execute) env.pending.execute();
          const result = await done;
          return {
            name, result, snapshotBefore,
            composer: env.composer.value,
            executeCalls: env.executeCalls,
            currentMessages: S.messages.map((m) => m.role + ':' + m.content),
            renders: env.renders,
            warnings: env.toasts,
            stashes: env.stashes,
            clears: env.clears,
            currentSid: S.session && S.session.session_id,
          };
        }

        (async () => {
          const out = {};

          // 1. Positive control: no switch -> reply lands, composer cleared.
          out.noSwitch = await runScenario('noSwitch', {
            metadataImmediate: true,
            during: async () => {},
          });

          // 2. Switch WHILE the metadata lookup is in flight, then land B's draft.
          out.switchDuringMetadata = await runScenario('switchDuringMetadata', {
            during: async (env, S) => {
              S.session = { session_id: 'sid-B' }; S.messages = [];
              env.composer.value = 'draft typed in B';
            },
          });

          // 3. Switch WHILE the command itself is in flight.
          out.switchDuringCommand = await runScenario('switchDuringCommand', {
            metadataImmediate: true,
            during: async (env, S) => {
              S.session = { session_id: 'sid-B' }; S.messages = [];
              env.composer.value = 'draft typed in B';
            },
          });

          // 4. Profile switch while the command is in flight (same session id).
          out.profileSwitchDuringCommand = await runScenario('profileSwitchDuringCommand', {
            metadataImmediate: true,
            during: async (env, S) => {
              S.activeProfile = 'other-profile';
              env.composer.value = 'draft typed under other profile';
            },
          });

          // 5. A transport failure after switching must preserve the originating
          // command as a restorable draft instead of falsely claiming it was saved.
          out.failureAfterSwitch = await runScenario('failureAfterSwitch', {
            metadataImmediate: true,
            executeReject: true,
            during: async (env, S) => {
              S.session = { session_id: 'sid-B' }; S.messages = [];
              env.composer.value = 'draft typed in B';
            },
          });

          // 6. New input in the SAME session while metadata is loading belongs to
          // a new draft and must not be erased when the earlier command resumes.
          out.sameOwnerNewerDraft = await runScenario('sameOwnerNewerDraft', {
            during: async (env) => {
              env.composer.value = 'newer draft';
              env.draftRevision++;
            },
          });

          // 7. Revision, not only text equality, guards the clear. The user may
          // edit and then return to the same visible text before metadata resolves.
          out.sameTextNewerRevision = await runScenario('sameTextNewerRevision', {
            during: async (env) => {
              env.composer.value = '/memory pending';
              env.draftRevision++;
            },
          });

          // 8. No session yet: newSession() creates one; reply must land in THAT session.
          out.noSessionYet = await runScenario('noSessionYet', {
            initialSession: null,
            metadataImmediate: true,
            during: async () => {},
          });

          console.log(JSON.stringify(out));
        })().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
        """
    ) % {"allowlist_decl": allowlist_decl, "block": block}

    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_webui_agent_command_positive_control_delivers_and_clears_composer():
    out = _run_webui_agent_command_scenarios()["noSwitch"]
    assert out["currentMessages"] == ["user:/memory pending", "assistant:memory result"]
    assert out["composer"] == ""
    assert out["executeCalls"] == 1
    assert out["warnings"] == 0
    assert out["clears"] == [{"sid": "sid-A", "text": "/memory pending", "files": []}]

def test_webui_agent_command_switch_during_metadata_lookup_touches_nothing():
    """Switching sessions while the command-metadata lookup is in flight must not run the
    command, append anything to the newly selected conversation, or clear ITS unsent draft."""
    out = _run_webui_agent_command_scenarios()["switchDuringMetadata"]
    assert out["currentSid"] == "sid-B"
    assert out["currentMessages"] == [], (
        "the command's transcript entries landed in the newly selected conversation")
    assert out["composer"] == "draft typed in B", (
        "the newly selected conversation's unsent composer draft was erased")
    assert out["executeCalls"] == 0, "command executed for an abandoned request"
    assert out["warnings"] == 0
    assert out["clears"] == []


def test_webui_agent_command_switch_during_command_never_touches_new_conversation():
    """Switching sessions while `/memory ...` is executing must drop the reply instead of
    appending it to (and clearing the composer of) the newly selected conversation."""
    out = _run_webui_agent_command_scenarios()["switchDuringCommand"]
    assert out["currentSid"] == "sid-B"
    assert out["currentMessages"] == [], (
        "reply for sid-A was appended to sid-B's transcript")
    assert out["composer"] == "draft typed in B", (
        "sid-B's unsent composer draft was erased by sid-A's command finishing")
    assert out["renders"] == 0, "renderMessages() must not run once ownership has changed"
    assert out["warnings"] == 1, "a withheld completion must produce a visible warning"
    assert out["clears"] == [{"sid": "sid-A", "text": "/memory pending", "files": []}]


def test_webui_agent_command_clears_origin_composer_before_awaiting_command():
    """The originating composer is cleared BEFORE the command await (so a session switch
    mid-command can never leave the sent text behind, nor clear a different draft after)."""
    src = (REPO_ROOT / "static/messages.js").read_text()
    block = _js_block(
        src,
        "if(_AGENT_COMMANDS_RUN_ON_WEBUI.has(_agentCmdName)){",
        "if(_agentCmd&&_agentCmd.category==='Plugin'){",
    )
    clear_at = block.index("$('msg').value=''")
    await_at = block.index("await executeAgentCommand(")
    assert clear_at < await_at, "composer must be cleared before the command await"
    tail = block[await_at:]
    assert "$('msg').value" not in tail, (
        "composer is touched again after the command await -- it may belong to another conversation now")


def test_webui_agent_command_profile_switch_during_command_drops_reply():
    out = _run_webui_agent_command_scenarios()["profileSwitchDuringCommand"]
    assert "assistant:memory result" not in out["currentMessages"], (
        "a reply produced under another profile was delivered after the profile changed")
    assert out["composer"] == "draft typed under other profile"
    assert out["warnings"] == 1
    assert out["clears"] == [{"sid": "sid-A", "text": "/memory pending", "files": []}]


def test_webui_agent_command_failure_after_switch_stashes_originating_draft():
    out = _run_webui_agent_command_scenarios()["failureAfterSwitch"]
    assert out["currentSid"] == "sid-B"
    assert out["currentMessages"] == []
    assert out["composer"] == "draft typed in B"
    assert out["warnings"] == 1
    assert out["stashes"] == [{
        "profile": "default",
        "sid": "sid-A",
        "text": "/memory pending",
        "files": [],
    }]


def test_webui_sessionless_agent_transport_omits_command_id():
    """Legacy sessionless commands must not send half of the persistence owner pair."""
    source = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
    assert "const commandId=ownerSid?" in source
    assert "...(commandId?{command_id:commandId}:{})" in source


def _run_webui_plugin_command_scenario(*, reject=False):
    """Run the real awaited plugin-command branch while ownership changes."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")
    src = (REPO_ROOT / "static/messages.js").read_text()
    block = _js_block(
        src,
        "if(_agentCmd&&_agentCmd.category==='Plugin'){",
        "if(_agentCmdName==='moa'){",
    )
    harness = textwrap.dedent(
        """
        (async () => {
          const env = { composer: {value:'/plugin run'}, clears:[], stashes:[], warnings:0, renders:0 };
          const S = {session:{session_id:'sid-A'},activeProfile:'default',messages:[],pendingFiles:[]};
          const text='/plugin run';
          const _agentCmd={name:'plugin',category:'Plugin'};
          const _cmdOwner={sid:'sid-A',profile:'default'};
          const _cmdOwnerIsCurrent=()=>((S.session&&S.session.session_id)||null)===_cmdOwner.sid
            &&(S.activeProfile||'default')===_cmdOwner.profile;
          let _cmdDraftRevision=0;
          const _composerDraftRevision=()=>0;
          const $=()=>env.composer;
          const autoResize=()=>{};
          const hideCmdDropdown=()=>{};
          const renderMessages=()=>{env.renders++;};
          const renderSessionList=async()=>{};
          const newSession=async()=>{};
          const showToast=()=>{env.warnings++;};
          const _clearComposerDraft=(sid,value,files)=>env.clears.push({sid,value,files});
          const _stashApprovalTransportFailure=(profile,sid,value,files)=>{
            env.stashes.push({profile,sid,value,files}); return true;
          };
          let settle;
          const executeAgentPluginCommand=()=>new Promise((resolve,rejectFn)=>{
            settle=()=>%(reject)s ? rejectFn(new Error('offline')) : resolve('plugin result');
          });
          const run=async()=>{
            %(block)s
            return 'fell-through';
          };
          const done=run();
          await new Promise((r)=>setTimeout(r,5));
          S.session={session_id:'sid-B'};
          S.messages=[];
          env.composer.value='draft typed in B';
          settle();
          await done;
          console.log(JSON.stringify({
            messages:S.messages.map((m)=>m.role+':'+m.content),
            composer:env.composer.value,
            clears:env.clears,
            stashes:env.stashes,
            warnings:env.warnings,
            renders:env.renders,
          }));
        })().catch((e)=>{console.error(e&&e.stack||e);process.exit(1);});
        """
    ) % {"block": block, "reject": "true" if reject else "false"}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_webui_plugin_command_switch_never_writes_to_new_conversation():
    out = _run_webui_plugin_command_scenario()
    assert out["messages"] == []
    assert out["composer"] == "draft typed in B"
    assert out["warnings"] == 1
    assert out["renders"] == 0
    assert out["clears"] == [{"sid": "sid-A", "value": "/plugin run", "files": []}]


def test_webui_plugin_command_failure_after_switch_stashes_originating_draft():
    out = _run_webui_plugin_command_scenario(reject=True)
    assert out["messages"] == []
    assert out["composer"] == "draft typed in B"
    assert out["stashes"] == [{
        "profile": "default",
        "sid": "sid-A",
        "value": "/plugin run",
        "files": [],
    }]


def test_transport_failure_draft_survives_reload_with_profile_alias_and_expires():
    """The bounded per-tab fallback restores only its owner and prunes stale data."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")
    src = (REPO_ROOT / "static/sessions.js").read_text()
    profile_matcher = _js_block(
        src,
        "function _profileMatchesActiveProfile(profile, activeProfile){",
        "function _sessionEventProfilesMatch",
    )
    helpers = _js_block(
        src,
        "const _APPROVAL_TRANSPORT_FAILURE_KEY=",
        "function _restoreApprovalCommandDraft",
    )
    script = textwrap.dedent(
        """
        const store=new Map();
        const sessionStorage={
          getItem:(key)=>store.has(key)?store.get(key):null,
          setItem:(key,value)=>store.set(key,String(value)),
          removeItem:(key)=>store.delete(key),
        };
        const composer={value:''};
        const _profilesCache={profiles:[{name:'renamed-root',is_default:true}]};
        const _cronProfileNameIsRootAlias=(name)=>name==='default'||_profilesCache.profiles.some(p=>p.name===name&&p.is_default);
        const S={activeProfile:'renamed-root',activeProfileIsDefault:true,pendingFiles:[]};
        const $=()=>composer;
        const autoResize=()=>{};
        const renderTray=()=>{};
        const saved=[];
        const _saveComposerDraftNow=(...args)=>saved.push(args);
        %(profile_matcher)s
        %(helpers)s
        const kept=_stashApprovalTransportFailure('default','sid-A','/memory pending',[]);
        _restoreApprovalTransportFailureForSession({session_id:'sid-A'});
        const restored={kept,text:composer.value,saved,remaining:_readApprovalTransportFailures()};
        composer.value='';
        S.activeProfile='default';
        _stashApprovalTransportFailure('renamed-root','sid-reverse','reverse alias',[]);
        _restoreApprovalTransportFailureForSession({session_id:'sid-reverse'});
        const reverseRestored={text:composer.value,remaining:_readApprovalTransportFailures()};
        S.activeProfile='renamed-root';
        composer.value='';
        _stashApprovalTransportFailure('default','sid-B','secret',[]);
        _clearApprovalTransportFailuresForSession('renamed-root','sid-B');
        const cleared=_readApprovalTransportFailures();
        sessionStorage.setItem(_APPROVAL_TRANSPORT_FAILURE_KEY,JSON.stringify([{
          profile:'default',sid:'old',text:'old secret',files:[],
          created_at:Date.now()-_APPROVAL_TRANSPORT_FAILURE_TTL_MS-1,
        }]));
        const expired=_readApprovalTransportFailures();
        console.log(JSON.stringify({restored,reverseRestored,cleared,expired,raw:sessionStorage.getItem(_APPROVAL_TRANSPORT_FAILURE_KEY)}));
        """
    ) % {"profile_matcher": profile_matcher, "helpers": helpers}
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert out["restored"]["kept"] is True
    assert out["restored"]["text"] == "/memory pending"
    assert out["restored"]["remaining"] == []
    assert out["reverseRestored"]["text"] == "reverse alias"
    assert out["reverseRestored"]["remaining"] == []
    assert out["cleared"] == []
    assert out["expired"] == []
    assert out["raw"] is None


def test_webui_agent_command_preserves_newer_same_session_draft():
    out = _run_webui_agent_command_scenarios()["sameOwnerNewerDraft"]
    assert out["currentSid"] == "sid-A"
    assert out["executeCalls"] == 1
    assert out["currentMessages"] == ["user:/memory pending", "assistant:memory result"]
    assert out["composer"] == "newer draft"
    assert out["clears"] == []


def test_webui_agent_command_uses_revision_when_newer_draft_has_same_text():
    out = _run_webui_agent_command_scenarios()["sameTextNewerRevision"]
    assert out["executeCalls"] == 1
    assert out["composer"] == "/memory pending"
    assert out["clears"] == []


def test_webui_agent_command_creates_session_and_delivers_to_it():
    out = _run_webui_agent_command_scenarios()["noSessionYet"]
    assert out["currentSid"] == "sid-NEW"
    assert out["currentMessages"] == ["user:/memory pending", "assistant:memory result"]
    assert out["warnings"] == 0
    assert out["clears"] == [{"sid": "sid-NEW", "text": "/memory pending", "files": []}]


def test_webui_agent_commands_use_server_persisted_session_transcript():
    """Command transport must bind a stable id + owner session, while the server
    persists and deduplicates the transcript under the per-session lock."""
    commands = (REPO_ROOT / "static/commands.js").read_text()
    routes = (REPO_ROOT / "api/routes.py").read_text()
    sessions = (REPO_ROOT / "static/sessions.js").read_text()
    assert "session_id:ownerSid,...(commandId?{command_id:commandId}:{})" in commands
    assert 'with _get_session_agent_lock(sid):' in routes
    assert 'message.get("_webui_command_id") == command_id' in routes
    assert '"_webui_command_id": command_id' in routes
    assert '"_webui_command_pending": True' in routes
    assert 'Could not save command before execution' in routes
    assert 'session.save()' in routes
    assert "hermes-webui-approval-command-results" not in sessions


def test_skills_write_approval_response_delivered_when_no_session_existed():
    """A `/skills pending` reply must still be delivered when there was NO active
    session at invocation time (e.g. right after deleting the last session) --
    _steerOwnerIsCurrent(null) is always false (correct for steer, which always
    needs a real session/stream), but a null ownerSid here means there was nothing
    to have switched away FROM, so there is no real owner mismatch to guard against.
    The prior fix (owner-session guard) over-applied that check and silently
    dropped every response sent with no session, both for reserved subcommands and
    for the plain local search branch. Real execution via node, not a mock of the
    guard: proves the message is genuinely appended and rendered.
    """
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        import pytest
        pytest.skip("node not available")

    src = (REPO_ROOT / "static/commands.js").read_text()
    cmd_skills_fn = _js_block(src, "function cmdSkills(args){", "\nasync function cmdUse")
    steer_owner_fn = _js_block(src, "function _steerOwnerIsCurrent(ownerSid){", "\nfunction _steerOwnerStreamIsCurrent")
    subcommands_decl = src[src.index("const SKILLS_AGENT_SUBCOMMANDS="):src.index("\n\nfunction cmdSkills")]

    harness = textwrap.dedent(
        """
        %(subcommands_decl)s
        %(steer_owner_fn)s

        let resolveTransport;
        function _runAgentCommandTransport(text){
          return new Promise((resolve) => { resolveTransport = resolve; });
        }

        // No active session at invocation -- e.g. the last session was just deleted.
        const S = { session: null, messages: [] };
        let renderCount = 0;
        let toastCount = 0;
        function renderMessages(){ renderCount++; }
        function showToast(){ toastCount++; }

        %(cmd_skills_fn)s

        const returned = cmdSkills('pending');
        resolveTransport('No pending skill writes.');

        setTimeout(() => {
          console.log(JSON.stringify({
            returnedTrueSynchronously: returned === true,
            messageCount: S.messages.length,
            lastMessageContent: S.messages.length ? S.messages[S.messages.length - 1].content : null,
            renderCalled: renderCount,
            warningShown: toastCount,
          }));
        }, 20);
        """
    ) % {
        "subcommands_decl": subcommands_decl,
        "steer_owner_fn": steer_owner_fn,
        "cmd_skills_fn": cmd_skills_fn,
    }

    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip())
    assert out["returnedTrueSynchronously"] is True
    assert out["messageCount"] == 1, (
        "the response must be delivered when there was no session to have switched "
        "away from -- it must not be silently dropped")
    assert out["lastMessageContent"] == "No pending skill writes."
    assert out["renderCalled"] == 1
    assert out["warningShown"] == 0


def test_reload_recovery_persists_durable_inflight_state(cleanup_test_sessions):
    """Reload recovery must persist a durable per-session inflight snapshot.
    Without these helpers, loadSession() references loadInflightState() but a full
    browser reload has no saved state to hydrate, so recovery silently no-ops.
    """
    ui_src = (REPO_ROOT / "static/ui.js").read_text()
    messages_src = (REPO_ROOT / "static/messages.js").read_text()
    sessions_src = (REPO_ROOT / "static/sessions.js").read_text()

    assert "const INFLIGHT_STATE_KEY = 'hermes-webui-inflight-state'" in ui_src
    assert "function saveInflightState(sid, state)" in ui_src
    assert "function loadInflightState(sid, streamId)" in ui_src
    assert "function clearInflightState(sid)" in ui_src
    assert "saveInflightState(activeSid" in messages_src, \
        "messages.js must persist live stream snapshots while a turn is in flight"
    assert "clearInflightState(activeSid)" in messages_src, \
        "messages.js must clear durable inflight snapshots when the run ends/errors/cancels"
    assert "const stored=loadInflightState(sid, activeStreamId);" in sessions_src, \
        "loadSession() must hydrate in-flight state from durable browser storage on reload"


# ── R18: OAuth onboarding must recognize credential_pool-only auth ───────────

def test_provider_oauth_authenticated_accepts_credential_pool_entries(
    cleanup_test_sessions, tmp_path
):
    """R18a: pool-only OAuth auth.json should count as authenticated.

    Hermes runtime resolves Codex credentials from credential_pool; onboarding
    must not insist on stale or duplicated providers[provider_id] entries.
    """
    _make_auth_json_with_credential_pool(
        "openai-codex",
        [
            {
                "id": "pool1",
                "label": "device_code",
                "source": "device_code",
                "auth_type": "oauth",
                "access_token": "***",
                "refresh_token": "***",
                "base_url": "https://chatgpt.com/backend-api/codex",
            }
        ],
        tmp_path,
    )

    from api.onboarding import _provider_oauth_authenticated

    assert _provider_oauth_authenticated("openai-codex", tmp_path) is True


def test_provider_oauth_authenticated_rejects_flag_only_credential_pool_entries(
    cleanup_test_sessions, tmp_path
):
    """R18a2: metadata flags alone must not count as usable OAuth auth."""
    _make_auth_json_with_credential_pool(
        "openai-codex",
        [
            {
                "id": "pool1",
                "label": "device_code",
                "source": "device_code",
                "auth_type": "oauth",
                "has_access_token": True,
                "has_refresh_token": True,
                "base_url": "https://chatgpt.com/backend-api/codex",
            }
        ],
        tmp_path,
    )

    from api.onboarding import _provider_oauth_authenticated

    assert _provider_oauth_authenticated("openai-codex", tmp_path) is False


def test_status_from_runtime_marks_openai_codex_ready_from_credential_pool(
    cleanup_test_sessions, tmp_path
):
    """R18b: provider_ready should be true when auth lives only in credential_pool."""
    _make_auth_json_with_credential_pool(
        "openai-codex",
        [
            {
                "id": "pool1",
                "label": "device_code",
                "source": "device_code",
                "auth_type": "oauth",
                "access_token": "***",
                "refresh_token": "***",
                "base_url": "https://chatgpt.com/backend-api/codex",
            }
        ],
        tmp_path,
    )

    from api.onboarding import _status_from_runtime
    import api.onboarding as _ob

    orig_home = _ob._get_active_hermes_home
    orig_found = _ob._HERMES_FOUND
    _ob._get_active_hermes_home = lambda: tmp_path
    _ob._HERMES_FOUND = True
    try:
        result = _status_from_runtime(
            {"model": {"provider": "openai-codex", "default": "codex-mini-latest"}},
            True,
        )
    finally:
        _ob._get_active_hermes_home = orig_home
        _ob._HERMES_FOUND = orig_found

    assert result["provider_configured"] is True
    assert result["provider_ready"] is True
    assert result["setup_state"] == "ready"
