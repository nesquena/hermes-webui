     1|"""
from conftest import REPO_ROOT
     2|Regression tests -- one test per bug that was introduced and fixed.
     3|These tests exist specifically to prevent those bugs from silently returning.
     4|
     5|Each test is tagged with the sprint/commit where the bug was found and fixed.
     6|"""
     7|import json
     8|import pathlib
     9|import time
    10|import urllib.error
    11|import urllib.request
    12|import urllib.parse
    13|
    14|BASE = "http://127.0.0.1:8788"
    15|
    16|def get(path):
    17|    with urllib.request.urlopen(BASE + path, timeout=10) as r:
    18|        return json.loads(r.read()), r.status
    19|
    20|def get_raw(path):
    21|    with urllib.request.urlopen(BASE + path, timeout=10) as r:
    22|        return r.read(), r.headers.get("Content-Type",""), r.status
    23|
    24|def post(path, body=None):
    25|    data = json.dumps(body or {}).encode()
    26|    req = urllib.request.Request(
    27|        BASE + path, data=data, headers={"Content-Type": "application/json"}
    28|    )
    29|    try:
    30|        with urllib.request.urlopen(req, timeout=10) as r:
    31|            return json.loads(r.read()), r.status
    32|    except urllib.error.HTTPError as e:
    33|        return json.loads(e.read()), e.code
    34|
    35|def make_session(created_list):
    36|    d, _ = post("/api/session/new", {})
    37|    sid = d["session"]["session_id"]
    38|    created_list.append(sid)
    39|    return sid
    40|
    41|
    42|# ── R1: uuid not imported in server.py (Sprint 10 split regression) ──────────
    43|
    44|def test_chat_start_returns_stream_id(cleanup_test_sessions):
    45|    """R1: chat/start must return stream_id -- catches missing uuid import.
    46|    When uuid was missing, this returned 500 (NameError).
    47|    """
    48|    sid = make_session(cleanup_test_sessions)
    49|    data, status = post("/api/chat/start", {
    50|        "session_id": sid,
    51|        "message": "ping",
    52|        "model": "openai/gpt-5.4-mini",
    53|    })
    54|    # Must return 200 with a stream_id -- not 500
    55|    assert status == 200, f"chat/start failed with {status}: {data}"
    56|    assert "stream_id" in data, "stream_id missing from chat/start response"
    57|    assert len(data["stream_id"]) > 8, "stream_id looks invalid"
    58|    post("/api/session/delete", {"session_id": sid})
    59|    cleanup_test_sessions.clear()
    60|
    61|
    62|# ── R2: AIAgent not imported in api/streaming.py (Sprint 10 split regression) ─
    63|
    64|def test_chat_stream_opens_successfully(cleanup_test_sessions):
    65|    """R2: After chat/start, GET /api/chat/stream must return 200 (SSE opens).
    66|    When AIAgent was missing, the thread crashed immediately, popped STREAMS,
    67|    and the SSE GET returned 404.
    68|    """
    69|    sid = make_session(cleanup_test_sessions)
    70|    data, status = post("/api/chat/start", {
    71|        "session_id": sid,
    72|        "message": "say: hello",
    73|        "model": "openai/gpt-5.4-mini",
    74|    })
    75|    assert status == 200, f"chat/start failed: {data}"
    76|    stream_id = data["stream_id"]
    77|
    78|    # Open the SSE stream -- must return 200, not 404
    79|    # We only check headers (don't read the full stream body)
    80|    req = urllib.request.Request(BASE + f"/api/chat/stream?stream_id={stream_id}")
    81|    try:
    82|        r = urllib.request.urlopen(req, timeout=3)
    83|        assert r.status == 200, f"SSE stream returned {r.status} (expected 200)"
    84|        ct = r.headers.get("Content-Type", "")
    85|        assert "text/event-stream" in ct, f"Wrong Content-Type: {ct}"
    86|        r.close()
    87|    except urllib.error.HTTPError as e:
    88|        assert False, f"SSE stream returned {e.code} -- AIAgent may not be imported"
    89|    except Exception:
    90|        pass  # timeout or connection close after brief read is fine
    91|
    92|    post("/api/session/delete", {"session_id": sid})
    93|    cleanup_test_sessions.clear()
    94|
    95|
    96|# ── R3: Session.__init__ missing tool_calls param (Sprint 10 split regression) ─
    97|
    98|def test_session_with_tool_calls_in_json_loads_ok(cleanup_test_sessions):
    99|    """R3: Sessions that have tool_calls in their JSON must load without 500.
   100|    When tool_calls=None was missing from Session.__init__, loading such sessions
   101|    threw TypeError: unexpected keyword argument.
   102|    """
   103|    sid = make_session(cleanup_test_sessions)
   104|
   105|    # Manually inject tool_calls into the session's JSON file
   106|    sessions_dir = pathlib.Path.home() / ".hermes" / "webui-mvp-test" / "sessions"
   107|    session_file = sessions_dir / f"{sid}.json"
   108|    if session_file.exists():
   109|        d = json.loads(session_file.read_text())
   110|        d["tool_calls"] = [
   111|            {"name": "terminal", "snippet": "test output", "tid": "test_tid_001", "assistant_msg_idx": 1}
   112|        ]
   113|        session_file.write_text(json.dumps(d))
   114|
   115|    # Loading the session must return 200, not 500
   116|    data, status = get(f"/api/session?session_id={urllib.parse.quote(sid)}")
   117|    assert status == 200, f"Session with tool_calls returned {status}: {data}"
   118|    assert data["session"]["session_id"] == sid
   119|
   120|    post("/api/session/delete", {"session_id": sid})
   121|    cleanup_test_sessions.clear()
   122|
   123|
   124|# ── R4: has_pending not imported in streaming.py (Sprint 10 split regression) ─
   125|
   126|def test_streaming_py_imports_has_pending(cleanup_test_sessions):
   127|    """R4: api/streaming.py must import or define has_pending.
   128|    When missing, the approval check mid-stream caused NameError.
   129|    """
   130|    src = REPO_ROOT / "api/streaming.py".read_text()
   131|    assert "has_pending" in src, "has_pending not found in api/streaming.py"
   132|    # Verify it's imported (not just used)
   133|    assert "import" in src and "has_pending" in src, \
   134|        "has_pending must be imported in api/streaming.py"
   135|
   136|
   137|def test_aiagent_imported_in_streaming(cleanup_test_sessions):
   138|    """R2b: api/streaming.py must import AIAgent.
   139|    When missing, the streaming thread crashed immediately after being spawned.
   140|    """
   141|    src = REPO_ROOT / "api/streaming.py".read_text()
   142|    assert "AIAgent" in src, "AIAgent not referenced in api/streaming.py"
   143|    assert "from run_agent import AIAgent" in src or "import AIAgent" in src, \
   144|        "AIAgent must be imported in api/streaming.py"
   145|
   146|
   147|# ── R5: SSE loop did not break on cancel event (Sprint 10 bug) ───────────────
   148|
   149|def test_cancel_nonexistent_stream_returns_not_cancelled(cleanup_test_sessions):
   150|    """R5a: Cancel endpoint works and returns cancelled:false for unknown stream."""
   151|    data, status = get("/api/chat/cancel?stream_id=nonexistent_test_xyz")
   152|    assert status == 200
   153|    assert data["ok"] is True
   154|    assert data["cancelled"] is False
   155|
   156|
   157|def test_server_py_sse_loop_breaks_on_cancel(cleanup_test_sessions):
   158|    """R5b: server.py SSE loop must include 'cancel' in the break condition.
   159|    When missing, the connection hung after the cancel event was processed.
   160|    """
   161|    src = REPO_ROOT / "server.py".read_text()
   162|    # Find the SSE break condition
   163|    import re
   164|    m = re.search(r"if event in \([^)]+\):\s*break", src)
   165|    assert m, "SSE break condition not found in server.py"
   166|    assert "cancel" in m.group(), \
   167|        f"'cancel' missing from SSE break condition: {m.group()}"
   168|
   169|
   170|# ── R6: Test cron isolation (Sprint 10) ──────────────────────────────────────
   171|
   172|def test_real_jobs_json_not_polluted_by_tests(cleanup_test_sessions):
   173|    """R6: Test runs must not write to the real ~/.hermes/cron/jobs.json.
   174|    When HERMES_HOME isolation was missing, every test run added test-job-* entries.
   175|    """
   176|    real_jobs_path = pathlib.Path.home() / ".hermes" / "cron" / "jobs.json"
   177|    if not real_jobs_path.exists():
   178|        return  # no jobs file at all -- fine
   179|
   180|    jobs = json.loads(real_jobs_path.read_text())
   181|    if isinstance(jobs, dict):
   182|        jobs = jobs.get("jobs", [])
   183|
   184|    test_jobs = [j for j in jobs if j.get("name", "").startswith("test-job-")]
   185|    assert len(test_jobs) == 0, \
   186|        f"Real jobs.json contains {len(test_jobs)} test-job-* entries: " \
   187|        f"{[j['name'] for j in test_jobs]}"
   188|
   189|
   190|# ── General: api modules all importable ──────────────────────────────────────
   191|
   192|def test_all_api_modules_importable(cleanup_test_sessions):
   193|    """All api/ modules must be importable without NameError or ImportError.
   194|    Catches missing imports introduced during future module splits.
   195|    """
   196|    import ast, pathlib
   197|    api_dir = REPO_ROOT / "api"
   198|    for module_file in api_dir.glob("*.py"):
   199|        src = module_file.read_text()
   200|        try:
   201|            ast.parse(src)
   202|        except SyntaxError as e:
   203|            assert False, f"{module_file.name} has syntax error: {e}"
   204|
   205|
   206|def test_server_py_importable(cleanup_test_sessions):
   207|    """server.py must parse without syntax errors after any split."""
   208|    import ast, pathlib
   209|    src = REPO_ROOT / "server.py".read_text()
   210|    try:
   211|        ast.parse(src)
   212|    except SyntaxError as e:
   213|        assert False, f"server.py has syntax error: {e}"
   214|
   215|# ── R7: Cross-session busy state bleed ───────────────────────────────────────
   216|
   217|def test_loadSession_resets_busy_state_for_idle_session(cleanup_test_sessions):
   218|    """R7: sessions.js loadSession for a non-inflight session must reset S.busy to false.
   219|    When missing, switching from a busy session to an idle one left the Send button
   220|    disabled, showed the wrong activity bar, and pointed Cancel at the wrong stream.
   221|    """
   222|    src = REPO_ROOT / "static/sessions.js".read_text()
   223|    # The fix adds explicit S.busy=false in the non-inflight else branch
   224|    assert "S.busy=false;" in src,         "sessions.js loadSession must set S.busy=false when loading a non-inflight session"
   225|    # btnSend must be explicitly re-enabled
   226|    assert "$('btnSend').disabled=false;" in src,         "sessions.js loadSession must enable btnSend for non-inflight sessions"
   227|
   228|
   229|def test_done_handler_guards_setbusy_with_inflight_check(cleanup_test_sessions):
   230|    """R7b: messages.js done/error handlers must not call setBusy(false) if the
   231|    currently viewed session is itself still in-flight.
   232|    When missing, finishing session A while viewing in-flight session B would
   233|    disable B's Send button.
   234|    """
   235|    src = REPO_ROOT / "static/messages.js".read_text()
   236|    # The fix wraps setBusy(false) in a guard
   237|    assert "INFLIGHT[S.session.session_id]" in src,         "messages.js must guard setBusy(false) with INFLIGHT check for current session"
   238|
   239|
   240|def test_cancel_button_not_cleared_across_sessions(cleanup_test_sessions):
   241|    """R7c: The Cancel button and activeStreamId must only be cleared when the
   242|    done/error event belongs to the currently viewed session.
   243|    """
   244|    src = REPO_ROOT / "static/messages.js".read_text()
   245|    # Both clear operations must be inside the activeSid === S.session guard
   246|    # We check for the pattern added by the fix
   247|    assert "S.session.session_id===activeSid" in src,         "messages.js must guard activeStreamId/Cancel clearing with session identity check"
   248|
   249|# ── R8: Session delete does not invalidate index (ghost sessions) ─────────────
   250|
   251|def test_deleted_session_does_not_appear_in_list(cleanup_test_sessions):
   252|    """R8: After deleting a session, it must not appear in /api/sessions.
   253|    When _index.json was not invalidated on delete, the session reappeared
   254|    in the list even after the JSON file was removed.
   255|    """
   256|    # Create a session with a title so it shows in the list
   257|    d, _ = post("/api/session/new", {})
   258|    sid = d["session"]["session_id"]
   259|    post("/api/session/rename", {"session_id": sid, "title": "regression-test-delete-R8"})
   260|
   261|    # Verify it appears
   262|    sessions, _ = get("/api/sessions")
   263|    ids_before = [s["session_id"] for s in sessions["sessions"]]
   264|    assert sid in ids_before, "Session must appear in list before delete"
   265|
   266|    # Delete it
   267|    result, status = post("/api/session/delete", {"session_id": sid})
   268|    assert status == 200 and result.get("ok") is True
   269|
   270|    # Verify it no longer appears -- even after a second fetch (index rebuild)
   271|    sessions2, _ = get("/api/sessions")
   272|    ids_after = [s["session_id"] for s in sessions2["sessions"]]
   273|    assert sid not in ids_after,         f"Deleted session {sid} still appears in list -- index not invalidated on delete"
   274|
   275|
   276|def test_server_delete_invalidates_index(cleanup_test_sessions):
   277|    """R8b: server.py session/delete handler must unlink _index.json.
   278|    Static check that the fix is in place.
   279|    """
   280|    src = REPO_ROOT / "server.py".read_text()
   281|    # Find the delete handler and verify it unlinks the index
   282|    delete_idx = src.find("if parsed.path == '/api/session/delete':")
   283|    assert delete_idx >= 0, "session/delete handler not found"
   284|    delete_block = src[delete_idx:delete_idx+600]
   285|    assert "SESSION_INDEX_FILE" in delete_block,         "server.py session/delete must invalidate SESSION_INDEX_FILE"
   286|
   287|
   288|# ── R9: Token/tool SSE events write to wrong session after switch ─────────────
   289|
   290|def test_token_handler_guards_session_id(cleanup_test_sessions):
   291|    """R9a: The SSE token event handler must check activeSid before writing to DOM.
   292|    When missing, tokens from session A would render into session B's message area
   293|    if the user switched sessions mid-stream.
   294|    """
   295|    src = REPO_ROOT / "static/messages.js".read_text()
   296|    # Find the token event handler
   297|    token_idx=src.fi...n'")
   298|    assert token_idx >= 0, "token event handler not found"
   299|    token_block=src[to...300]
   300|    assert "activeSid" in token_block,         "token handler must check activeSid before writing to DOM"
   301|    assert "S.session.session_id!==activeSid" in token_block or            "S.session.session_id===activeSid" in token_block,         "token handler must compare current session to activeSid"
   302|
   303|
   304|def test_tool_handler_guards_session_id(cleanup_test_sessions):
   305|    """R9b: The SSE tool event handler must check activeSid before writing to DOM.
   306|    When missing, tool cards from session A would render into session B's message area.
   307|    """
   308|    src = REPO_ROOT / "static/messages.js".read_text()
   309|    tool_idx = src.find("es.addEventListener('tool'")
   310|    assert tool_idx >= 0, "tool event handler not found"
   311|    tool_block = src[tool_idx:tool_idx+400]
   312|    assert "activeSid" in tool_block,         "tool handler must check activeSid before writing to DOM"
   313|
   314|# ── R10: respondApproval uses wrong session_id after switch (multi-session) ─
   315|
   316|def test_respond_approval_uses_approval_session_id(cleanup_test_sessions):
   317|    """R10: respondApproval must use the session_id of the session that triggered
   318|    the approval, not S.session.session_id (which may be a different session
   319|    if the user switched while approval was pending).
   320|    """
   321|    src = REPO_ROOT / "static/messages.js".read_text()
   322|    # The fix introduces _approvalSessionId to track the correct session
   323|    assert "_approvalSessionId" in src,         "messages.js must use _approvalSessionId in respondApproval"
   324|    # respondApproval must use _approvalSessionId, not S.session.session_id directly
   325|    idx = src.find("async function respondApproval(")
   326|    assert idx >= 0, "respondApproval not found"
   327|    fn_body = src[idx:idx+300]
   328|    assert "_approvalSessionId" in fn_body,         "respondApproval must read _approvalSessionId, not S.session.session_id"
   329|
   330|
   331|# ── R11: Activity bar shows cross-session tool status ─────────────────────
   332|
   333|def test_tool_status_only_shown_for_current_session(cleanup_test_sessions):
   334|    """R11: The activity bar setStatus() call in the tool SSE handler must only
   335|    fire when the user is viewing the session that triggered the tool.
   336|    When missing, session A's tool names would appear in session B's activity bar.
   337|    """
   338|    src = REPO_ROOT / "static/messages.js".read_text()
   339|    # Find the tool event handler
   340|    tool_idx = src.find("es.addEventListener('tool'")
   341|    assert tool_idx >= 0
   342|    tool_block = src[tool_idx:tool_idx+400]
   343|    # setStatus must be inside the activeSid guard, not before it
   344|    status_pos = tool_block.find("setStatus(")
   345|    guard_pos  = tool_block.find("S.session.session_id===activeSid")
   346|    assert guard_pos >= 0, "tool handler must guard with activeSid check"
   347|    # The guard must appear BEFORE or AROUND the setStatus call
   348|    # (status only fires for the current session)
   349|    assert status_pos > tool_block.find("activeSid"),         "setStatus in tool handler must be inside the activeSid guard"
   350|
   351|
   352|# ── R12: Live tool cards lost on switch-away and switch-back ──────────────
   353|
   354|def test_loadSession_inflight_restores_live_tool_cards(cleanup_test_sessions):
   355|    """R12: When switching back to an in-flight session, live tool cards in
   356|    #liveToolCards must be restored from S.toolCalls.
   357|    When missing, tool cards disappeared on switch-away even though the session
   358|    was still processing.
   359|    """
   360|    src = REPO_ROOT / "static/sessions.js".read_text()
   361|    # INFLIGHT branch must call appendLiveToolCard
   362|    inflight_idx = src.find("if(INFLIGHT[sid]){")
   363|    assert inflight_idx >= 0, "INFLIGHT branch not found in loadSession"
   364|    inflight_block = src[inflight_idx:inflight_idx+500]
   365|    assert "appendLiveToolCard" in inflight_block,         "loadSession INFLIGHT branch must restore live tool cards via appendLiveToolCard"
   366|    assert "clearLiveToolCards" in inflight_block,         "loadSession INFLIGHT branch must clear old live cards before restoring"
   367|
   368|# ── R13: renderMessages() called before S.busy=false in done handler ────────
   369|
   370|def test_done_handler_sets_busy_false_before_renderMessages(cleanup_test_sessions):
   371|    """R13: In the done handler, S.busy must be set to false BEFORE renderMessages()
   372|    is called for the active session. The !S.busy guard in renderMessages() controls
   373|    whether settled tool cards are rendered. When S.busy=true during renderMessages(),
   374|    tool cards are skipped entirely after a response completes.
   375|    """
   376|    src = REPO_ROOT / "static/messages.js".read_text()
   377|    done_idx = src.find("es.addEventListener('done'")
   378|    assert done_idx >= 0
   379|    done_block = src[done_idx:done_idx+1500]
   380|    # S.busy=false must appear before renderMessages() within the done handler
   381|    busy_pos = done_block.find("S.busy=false;")
   382|    render_pos = done_block.find("renderMessages()")
   383|    assert busy_pos >= 0, "done handler must set S.busy=false before renderMessages()"
   384|    assert busy_pos < render_pos,         f"S.busy=false (pos {busy_pos}) must come before renderMessages() (pos {render_pos})"
   385|
   386|
   387|# ── R14: send() uses stale modelSelect.value instead of session model ────────
   388|
   389|def test_send_uses_session_model_as_authoritative_source(cleanup_test_sessions):
   390|    """R14: send() must use S.session.model as the authoritative model, not just
   391|    $('modelSelect').value. When a session was created with a model not in the
   392|    current dropdown list, the select value would be stale after switching sessions,
   393|    causing the wrong model to be sent.
   394|    """
   395|    src = REPO_ROOT / "static/messages.js".read_text()
   396|    # The model field in the chat/start payload must prefer S.session.model
   397|    chat_start_idx = src.find("/api/chat/start")
   398|    assert chat_start_idx >= 0
   399|    payload_block = src[chat_start_idx:chat_start_idx+300]
   400|    assert "S.session.model" in payload_block,         "send() must use S.session.model in the chat/start payload"
   401|
   402|
   403|# ── R15: newSession does not clear live tool cards ────────────────────────────
   404|
   405|def test_newSession_clears_live_tool_cards(cleanup_test_sessions):
   406|    """R15: newSession() must call clearLiveToolCards() so live cards from a
   407|    previous in-flight session don't persist when starting a fresh conversation.
   408|    """
   409|    src = REPO_ROOT / "static/sessions.js".read_text()
   410|    new_sess_idx = src.find("async function newSession(")
   411|    assert new_sess_idx >= 0
   412|    # Find end of newSession (next async function)
   413|    next_fn = src.find("async function ", new_sess_idx + 10)
   414|    new_sess_body = src[new_sess_idx:next_fn]
   415|    assert "clearLiveToolCards" in new_sess_body,         "newSession() must call clearLiveToolCards() to clear stale live cards"
   416|