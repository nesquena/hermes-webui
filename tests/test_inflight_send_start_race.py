"""Regression coverage for send/start optimistic INFLIGHT races."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 1
    i = brace + 1
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[brace + 1 : i - 1]


def test_send_preserves_optimistic_messages_across_chat_start_await():
    """send() must not dereference INFLIGHT[activeSid] after await without a fallback."""
    body = _function_body(MESSAGES_JS, "send")
    setup_idx = body.index("optimisticMessages=[...S.messages];")
    inflight_idx = body.index("INFLIGHT[activeSid]={messages:optimisticMessages")
    await_idx = body.index("const startData=await api('/api/chat/start'")
    save_idx = body.index("saveInflightState(activeSid,{streamId", await_idx)

    assert setup_idx < inflight_idx < await_idx < save_idx
    post_await = body[await_idx:save_idx]
    assert "if(!INFLIGHT[activeSid])" in post_await, (
        "send() should recreate the INFLIGHT entry if a session-list refresh pruned it"
    )
    assert "messages:INFLIGHT[activeSid].messages" not in body[save_idx : save_idx + 220], (
        "saveInflightState() should use a guarded local/current inflight object, not a blind nested read"
    )


def test_stale_inflight_purge_preserves_current_send_before_stream_id_exists():
    """Sidebar cleanup must not delete the active send before /api/chat/start responds."""
    body = _function_body(SESSIONS_JS, "_purgeStaleInflightEntries")

    assert "_sendInProgress" in body and "_sendInProgressSid" in body, (
        "_purgeStaleInflightEntries() should skip the current send while start is in progress"
    )
    skip_idx = body.index("_sendInProgress")
    delete_idx = body.index("delete INFLIGHT[sid];")
    assert skip_idx < delete_idx, "the current-send skip must run before any purge deletion"


def test_send_clears_stale_busy_state_before_queue_branch():
    """A stale client-only busy flag must not divert a new user turn into the invisible queue."""
    body = _function_body(MESSAGES_JS, "send")

    assert "_clearStaleBusyStateBeforeSend" in body, (
        "send() should reconcile client-only stale busy state before deciding busy/queue mode"
    )
    reconcile_idx = body.index("_clearStaleBusyStateBeforeSend")
    busy_branch_idx = body.index("if(S.busy||compressionRunning)")
    chat_start_idx = body.index("api('/api/chat/start'")
    assert reconcile_idx < busy_branch_idx < chat_start_idx, (
        "stale busy reconciliation must run before the queue branch and before /api/chat/start"
    )


def test_pre_start_optimistic_ui_helpers_cannot_block_chat_start():
    """Optional optimistic UI helpers must not strand a local bubble before /api/chat/start."""
    body = _function_body(MESSAGES_JS, "send")
    helper_body = _function_body(MESSAGES_JS, "_runOptionalPreStartUiStep")

    optimistic_idx = body.index("S.messages.push(userMsg);renderMessages();appendThinking('',{pending:true});setBusy(true);")
    chat_start_idx = body.index("api('/api/chat/start'")
    pre_start = body[optimistic_idx:chat_start_idx]

    assert "try" in helper_body and "catch" in helper_body, (
        "optional pre-start UI helper wrapper must catch errors before /api/chat/start"
    )
    assert "setStatus(`UI warning before send:" not in helper_body, (
        "non-fatal pre-start UI helper failures should stay in the console; visible status flashes "
        "look like real send errors even though /api/chat/start continues"
    )
    assert "_runOptionalPreStartUiStep" in pre_start, (
        "send() should wrap optimistic sidebar/title/polling helpers before /api/chat/start"
    )
    assert "upsertActiveSessionForLocalTurn" in pre_start and "applySessionTitleUpdate" in pre_start


def test_pre_start_optimistic_block_cannot_prevent_chat_start():
    """Any pre-start UI/storage exception must still fall through to /api/chat/start."""
    body = _function_body(MESSAGES_JS, "send")
    optimistic_idx = body.index("S.messages.push(userMsg);renderMessages();appendThinking('',{pending:true});setBusy(true);")
    chat_start_idx = body.index("api('/api/chat/start'")
    pre_start = body[optimistic_idx:chat_start_idx]

    assert "}catch(preStartError){" in pre_start, (
        "The whole optimistic pre-start block needs a catch, not only individual optional helpers"
    )
    assert "continuing to /api/chat/start" in pre_start, (
        "The recovery path should document that chat/start must still execute"
    )
    assert pre_start.rindex("}catch(preStartError){") < chat_start_idx, (
        "pre-start catch must be before the /api/chat/start call"
    )


def test_server_absent_optimistic_first_turn_rows_are_not_kept_forever():
    """A local first-turn sidebar row must expire when /api/chat/start never persisted it."""
    body = _function_body(SESSIONS_JS, "_mergeOptimisticFirstTurnSessions")

    assert "_shouldKeepLocalOnlyOptimisticSessionRow(local)" in body, (
        "server-absent optimistic rows need an explicit keep/drop gate"
    )
    keep_idx = body.index("if(_shouldKeepLocalOnlyOptimisticSessionRow(local))")
    append_idx = body.index("merged.push({...local,is_streaming:true});")
    drop_idx = body.index("_dropStaleOptimisticSessionRow(sid);", append_idx)
    assert keep_idx < append_idx < drop_idx, (
        "local optimistic rows may only be appended inside the explicit keep gate"
    )
    drop_body = _function_body(SESSIONS_JS, "_dropStaleOptimisticSessionRow")
    assert "clearInflightState(sid)" in drop_body, (
        "dropping a phantom row should also clear persisted browser recovery state"
    )


def test_server_idle_row_wins_over_stale_optimistic_count():
    """If the server says the row is idle, stale local message_count/title must not win."""
    body = _function_body(SESSIONS_JS, "_mergeOptimisticFirstTurnSessions")

    assert "const keepLocalOptimistic=" in body
    assert "message_count:keepLocalOptimistic?Math.max(localCount,fetchedCount):fetchedCount" in body, (
        "stale optimistic message_count must not override a confirmed idle server row"
    )
    assert "title:keepLocalOptimistic?(local.title||fetched.title):fetched.title" in body, (
        "stale optimistic provisional title must not override a confirmed idle server row"
    )
