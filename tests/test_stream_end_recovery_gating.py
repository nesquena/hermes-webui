"""Regression coverage for stream-end recovery ordering.

#3877-style recovery relies on one subtle path:
when `stream_end` arrives while the active live assistant row is still
present, cleanup should be deferred briefly to allow pending final SSE updates to
settle, then performed through the shared terminal recovery helper.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _event_block(event_name: str) -> str:
    marker = f"source.addEventListener('{event_name}'"
    start = MESSAGES_JS.find(marker)
    assert start >= 0, f"missing {event_name} listener"
    brace = MESSAGES_JS.find("{", start)
    assert brace >= 0, f"missing {event_name} listener body"
    depth = 0
    i = brace
    while i < len(MESSAGES_JS):
        if MESSAGES_JS[i] == "{":
            depth += 1
        elif MESSAGES_JS[i] == "}":
            depth -= 1
            if depth == 0:
                return MESSAGES_JS[brace : i + 1]
        i += 1
    raise AssertionError(f"unclosed {event_name} listener body")


def _function_body(name: str) -> str:
    marker = f"async function {name}("
    start = MESSAGES_JS.find(marker)
    if start < 0:
        marker = f"function {name}("
        start = MESSAGES_JS.find(marker)
    assert start >= 0, f"missing function: {name}"
    brace = MESSAGES_JS.find("{", start)
    assert brace >= 0, f"missing {name} body"
    depth = 0
    i = brace
    while i < len(MESSAGES_JS):
        if MESSAGES_JS[i] == "{":
            depth += 1
        elif MESSAGES_JS[i] == "}":
            depth -= 1
            if depth == 0:
                return MESSAGES_JS[brace : i + 1]
        i += 1
    raise AssertionError(f"unclosed function body: {name}")


def test_stream_end_defers_settlement_when_live_assistant_still_present():
    body = _event_block("stream_end")
    assert "if(S.activeStreamId===streamId && _liveStreamEndScenePresent())" in body, (
        "stream_end should defer terminal cleanup while active live scene content is still present"
    )
    assert "_scheduleStreamEndRecovery(source);" in body, (
        "stream_end should schedule the deferred recovery timer before returning"
    )
    assert "_scheduleStreamEndRecovery(source)" in body, (
        "stream_end must delegate deferred cleanup to helper"
    )


def test_stream_end_fallback_does_not_finalize_when_session_is_still_active():
    body = _event_block("stream_end")
    assert "const status=await _restoreSettledSession(source,{status:true,allowUnmarkedShorterTerminalSnapshot:true});" in body
    assert "if(status==='active'&&S.activeStreamId===streamId)" in body
    assert "_scheduleStreamEndRecovery(source,200);" in body
    assert "_finalizeStreamEndFallback(source);" in body


def test_stream_end_recovery_helper_retries_while_session_is_still_active():
    fn = _function_body("_runStreamEndRecovery")
    assert "if(_streamFinalized || _terminalStateReached || !_pendingStreamEndRecovery)" in fn
    assert "_restoreSettledSession(source,{status:true,allowUnmarkedShorterTerminalSnapshot:true})" in fn
    assert "if(status==='active'&&_streamEndRecoveryAttempts<10)" in fn
    assert "_scheduleStreamEndRecovery(source,200);" in fn
    assert "_finalizeStreamEndFallback(source);" in fn


def test_stream_end_fallback_helper_clears_owner_state_before_closing():
    fn = _function_body("_finalizeStreamEndFallback")
    assert "_terminalStateReached=true;" in fn
    assert "_streamFinalized=true;" in fn
    assert "_clearOwnerInflightState();" in fn
    assert "_clearApprovalForOwner();" in fn
    assert "_clearClarifyForOwner('terminal');" in fn
    assert "renderMessages({preserveScroll:true});" in fn
    assert "_setActivePaneIdleIfOwner();" in fn
    assert "_closeSource(source)" in fn


def test_stream_end_live_scene_detection_includes_empty_text_activity():
    fn = _function_body("_liveStreamEndScenePresent")
    assert "if(assistantText||assistantRow) return true;" in fn
    assert "liveReasoningText||reasoningText" in fn
    assert "inflight.toolCalls.length" in fn
    assert "data-live-worklog-shell" in fn
    assert "data-thinking-active" in fn


def test_restore_settled_session_can_report_active_pending_status():
    fn = _function_body("_restoreSettledSession")
    assert "async function _restoreSettledSession(source, options=null)" in MESSAGES_JS
    assert "arguments[1]" not in fn
    assert "const returnStatus=!!(options&&options.status);" in fn
    assert "return returnStatus?'active':false;" in fn
    assert "return returnStatus?'restored':true;" in fn


def test_stream_end_recovery_state_is_cleared_on_done_and_terminal_events():
    assert "_clearStreamEndRecovery();" in _event_block("done")
    assert "_clearStreamEndRecovery();" in _event_block("stream_end")
    assert "_clearStreamEndRecovery();" in _event_block("cancel")
    assert "_clearStreamEndRecovery();" in _event_block("apperror")
