"""
Regression tests -- one test per bug that was introduced and fixed.
These tests exist specifically to prevent those bugs from silently returning.

Each test is tagged with the sprint/commit where the bug was found and fixed.
"""
import json
import os
import pathlib
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
    sessions_dir = pathlib.Path(os.environ.get("HERMES_WEBUI_TEST_STATE_DIR", str(pathlib.Path.home() / ".hermes" / "webui-mvp-test"))) / "sessions"
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


# ── R4: has_pending not imported in streaming.py (Sprint 10 split regression) ─

def test_streaming_py_imports_has_pending(cleanup_test_sessions):
    """R4: api/streaming.py must import or define has_pending.
    When missing, the approval check mid-stream caused NameError.
    """
    src = (REPO_ROOT / "api/streaming.py").read_text()
    assert "has_pending" in src, "has_pending not found in api/streaming.py"
    # Verify it's imported (not just used)
    assert "import" in src and "has_pending" in src, \
        "has_pending must be imported in api/streaming.py"


def test_aiagent_imported_in_streaming(cleanup_test_sessions):
    """R2b: api/streaming.py must import AIAgent.
    When missing, the streaming thread crashed immediately after being spawned.
    """
    src = (REPO_ROOT / "api/streaming.py").read_text()
    assert "AIAgent" in src, "AIAgent not referenced in api/streaming.py"
    assert "from run_agent import AIAgent" in src or "import AIAgent" in src, \
        "AIAgent must be imported in api/streaming.py"


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
    m = re.search(r"if event in \([^)]+\):\s*break", combined)
    assert m, "SSE break condition not found in server.py or api/routes.py"
    assert "cancel" in m.group(), \
        f"'cancel' missing from SSE break condition: {m.group()}"


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
    # The fix wraps setBusy(false) in a guard
    assert "INFLIGHT[S.session.session_id]" in src,         "messages.js must guard setBusy(false) with INFLIGHT check for current session"


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


def test_server_delete_invalidates_index(cleanup_test_sessions):
    """R8b: session/delete handler must unlink _index.json.
    Static check that the fix is in place.
    Sprint 11: handler moved from server.py to api/routes.py -- check both.
    """
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
            # Use 1200 chars to accommodate any validation/guard code added
            # before the SESSION_INDEX_FILE.unlink() call (e.g. session_id
            # character checks, path traversal guards).
            delete_block = text[delete_idx:delete_idx+1200]
            assert "SESSION_INDEX_FILE" in delete_block, \
                f"{label} session/delete must invalidate SESSION_INDEX_FILE"
            return
    assert False, "session/delete handler not found in server.py or api/routes.py"

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
    # The fix introduces _approvalSessionId to track the correct session
    assert "_approvalSessionId" in src,         "messages.js must use _approvalSessionId in respondApproval"
    # respondApproval must use _approvalSessionId, not S.session.session_id directly
    idx = src.find("async function respondApproval(")
    assert idx >= 0, "respondApproval not found"
    fn_body = src[idx:idx+300]
    assert "_approvalSessionId" in fn_body,         "respondApproval must read _approvalSessionId, not S.session.session_id"


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
    inflight_idx = src.find("if(INFLIGHT[sid]){")
    assert inflight_idx >= 0, "INFLIGHT branch not found in loadSession"
    inflight_block = src[inflight_idx:inflight_idx+500]
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
    done_block = src[done_idx:done_idx+2500]
    # S.busy=false must appear before renderMessages() within the done handler
    busy_pos = done_block.find("S.busy=false;")
    render_pos = done_block.find("renderMessages()")
    assert busy_pos >= 0, "done handler must set S.busy=false before renderMessages()"
    assert busy_pos < render_pos,         f"S.busy=false (pos {busy_pos}) must come before renderMessages() (pos {render_pos})"


# ── R14: send() uses stale modelSelect.value instead of session model ────────

def test_send_uses_session_model_as_authoritative_source(cleanup_test_sessions):
    """R14: send() must use S.session.model as the authoritative model, not just
    $('modelSelect').value. When a session was created with a model not in the
    current dropdown list, the select value would be stale after switching sessions,
    causing the wrong model to be sent.
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    # The model field in the chat/start payload must prefer S.session.model
    chat_start_idx = src.find("/api/chat/start")
    assert chat_start_idx >= 0
    payload_block = src[chat_start_idx:chat_start_idx+300]
    assert "S.session.model" in payload_block,         "send() must use S.session.model in the chat/start payload"


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
    assert "const sid=S.session&&S.session.session_id;" in ui_src
    assert "const next=sid?shiftQueuedSessionMessage(sid):null;" in ui_src
    assert "queueSessionMessage(S.session.session_id" in messages_src
    assert "updateQueueBadge(S.session.session_id);" in messages_src
    assert "updateQueueBadge(sid);" in sessions_src


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
    assert 'const activeStreamId=data.session.active_stream_id||null;' in sessions_src
    assert 'attachLiveStream(sid, activeStreamId' in sessions_src
    assert 'if (S.activeStreamId && S.activeStreamId === streamId) return;' in ui_src


# ── R16: Switching away/back must preserve live partial assistant output ─────


def test_live_stream_tokens_persist_partial_assistant_for_session_switch(cleanup_test_sessions):
    """R16: in-flight assistant text must be mirrored into INFLIGHT session state,
    and the live stream must rebind to the rebuilt DOM after switching away and back.
    Without this, partial assistant output disappears until the final done payload lands.
    """
    messages_src = (REPO_ROOT / "static/messages.js").read_text()
    ui_src = (REPO_ROOT / "static/ui.js").read_text()

    assert "content:assistantText" in messages_src, \
        "messages.js must persist the partial assistant text into INFLIGHT state"
    assert "_live:true" in messages_src, \
        "messages.js must mark the persisted in-flight assistant row so renderMessages can re-anchor it"
    assert "syncInflightAssistantMessage();" in messages_src, \
        "token handler must update INFLIGHT state before checking the active session"
    assert "assistantRow&&!assistantRow.isConnected" in messages_src, \
        "live stream must drop stale detached assistant DOM references after session switches"
    assert "data-live-assistant" in ui_src, \
        "renderMessages must preserve a live-assistant DOM anchor when rebuilding the thread"


def test_inflight_session_state_tracks_live_tool_cards_per_session(cleanup_test_sessions):
    """R16b: live tool cards must be stored on the in-flight session, not only in the
    global S.toolCalls array, so switching chats does not lose or misattach them.
    """
    messages_src = (REPO_ROOT / "static/messages.js").read_text()
    sessions_src = (REPO_ROOT / "static/sessions.js").read_text()

    assert "INFLIGHT[activeSid].toolCalls.push(tc);" in messages_src, \
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
    inflight_idx = src.find("if(INFLIGHT[sid]){")
    assert inflight_idx >= 0, "INFLIGHT branch not found in loadSession"
    inflight_block = src[inflight_idx:inflight_idx+700]
    busy_pos = inflight_block.find("S.busy=true;")
    render_pos = inflight_block.find("renderMessages();appendThinking();")
    assert busy_pos >= 0, "loadSession INFLIGHT branch must set S.busy=true"
    assert render_pos >= 0, "loadSession INFLIGHT branch must call renderMessages()"
    assert busy_pos < render_pos, \
        "loadSession must set S.busy=true before renderMessages() to avoid duplicate tool cards"


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


def test_messages_js_supports_live_reasoning_and_tool_completion(cleanup_test_sessions):
    """R18: messages.js must render live reasoning and react to tool completion events.
    Without these handlers, the operator only sees generic Thinking… or nothing
    until the final done snapshot redraws the whole turn.
    """
    src = (REPO_ROOT / "static/messages.js").read_text()
    assert "let reasoningText=''" in src, \
        "messages.js must track streamed reasoning text separately from assistant text"
    assert "let liveReasoningText=''" in src or 'let liveReasoningText = ""' in src, \
        "messages.js must track the currently active reasoning segment separately from cumulative reasoning"
    assert "source.addEventListener('reasoning'" in src or 'source.addEventListener("reasoning"' in src, \
        "messages.js must listen for live reasoning SSE events"
    assert "source.addEventListener('tool_complete'" in src or 'source.addEventListener("tool_complete"' in src, \
        "messages.js must listen for live tool completion SSE events"
    assert "function _parseStreamState()" in src, \
        "messages.js must parse live stream state into reasoning + visible answer"


def test_ui_js_can_upgrade_thinking_spinner_into_live_reasoning_card(cleanup_test_sessions):
    """R19: ui.js must be able to replace the placeholder thinking spinner with
    streamed reasoning text while a turn is in progress.
    """
    src = (REPO_ROOT / "static/ui.js").read_text()
    assert "function _thinkingMarkup(text='')" in src or 'function _thinkingMarkup(text="")' in src, \
        "ui.js must centralize thinking row markup so it can switch between spinner and live text"
    assert "function updateThinking(text=''){appendThinking(text);}" in src or 'function updateThinking(text=""){appendThinking(text);}' in src, \
        "ui.js must expose an updateThinking helper for live reasoning rendering"
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
    """R19c2: assistant anchor segments that contain a thinking card must remain
    visible; only truly empty tool-call anchor segments should be hidden.
    """
    src = (REPO_ROOT / "static" / "ui.js").read_text()
    compact = src.replace(' ', '').replace('\n', '')
    assert "}elseif(!thinkingText){" in compact, \
        "renderMessages must only hide assistant anchor segments when they have no thinking content"


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
    assert "if(String((parsed&&parsed.displayText)||'').trim()||assistantRow) ensureAssistantRow();" in src, \
        "token handler must only create the live answer segment once visible answer text starts"


def test_messages_js_finalizes_thinking_card_before_tool_card(cleanup_test_sessions):
    """R19e: later reasoning after a tool call must render in a fresh card."""
    src = (REPO_ROOT / "static/messages.js").read_text()
    assert "finalizeThinkingCard" in src, \
        "tool handler must finalize the current live thinking card before appending a tool card"
    assert "liveReasoningText='';" in src or 'liveReasoningText = "";' in src, \
        "tool handler must reset the active reasoning segment before post-tool reasoning arrives"


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
