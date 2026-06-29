"""Regression tests for #1694 terminal stream cleanup ownership.

Terminal SSE events for one session must not mutate another currently viewed
active pane. The owning session's persisted/runtime stream marker can be cleared,
but global pane state such as ``clearInflight()``, approval/clarify polling, and
``setBusy(false)`` must be gated to the session that owns the active pane/card.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _body_from_brace(src: str, brace: int, label: str) -> str:
    assert brace >= 0, f"body opening brace not found for: {label}"
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"body did not close for: {label}"
    return src[brace + 1 : i - 1]


def _brace_body_after(src: str, marker: str) -> str:
    start = src.find(marker)
    assert start >= 0, f"marker not found: {marker}"
    brace = src.find("{", start)
    return _body_from_brace(src, brace, marker)


def _event_body(event_name: str) -> str:
    return _brace_body_after(MESSAGES_JS, f"source.addEventListener('{event_name}'")


def _function_body(name: str) -> str:
    marker = f"function {name}("
    start = MESSAGES_JS.find(marker)
    assert start >= 0, f"function not found: {name}"
    signature_end = MESSAGES_JS.find("){", start)
    assert signature_end >= 0, f"function body not found: {name}"
    return _body_from_brace(MESSAGES_JS, signature_end + 1, name)


def test_terminal_handlers_use_session_owned_cleanup_helpers():
    """Patch #1694 should centralize terminal cleanup behind owner-aware helpers."""
    attach_body = _function_body("attachLiveStream")
    assert "function _clearOwnerInflightState()" in attach_body
    owner_helper = _function_body("_clearOwnerInflightState")
    assert "delete INFLIGHT[activeSid]" in owner_helper
    assert "clearInflightState(activeSid)" in owner_helper
    assert "_clearActivePaneInflightIfOwner();" in owner_helper
    assert "function _clearActivePaneInflightIfOwner()" in attach_body
    assert "function _clearApprovalForOwner()" in attach_body
    assert "function _clearClarifyForOwner(" in attach_body
    assert "function _setActivePaneIdleIfOwner(" in attach_body


def test_done_event_does_not_clear_active_pane_for_background_session():
    """A background done event may clear its owner marker, not the active pane."""
    body = _event_body("done")
    assert "_clearOwnerInflightState();" in body
    assert "clearInflight();clearInflightState(activeSid)" not in body
    assert "delete INFLIGHT[activeSid];\n      clearInflight();" not in body
    assert "renderSessionList();setBusy(false)" not in body
    assert "_setActivePaneIdleIfOwner" in body


def test_error_and_cancel_events_do_not_blanket_stop_active_pane_polling():
    """Background app errors/cancels must not stop another pane's prompt polling."""
    for event_name in ("apperror", "cancel"):
        body = _event_body(event_name)
        assert "_clearOwnerInflightState();" in body, event_name
        assert "_clearApprovalForOwner" in body, event_name
        assert "_clearClarifyForOwner" in body, event_name
        assert "stopApprovalPolling();stopClarifyPolling();" not in body, event_name
        assert "clearInflight();clearInflightState(activeSid)" not in body, event_name


def test_reconnect_settled_and_error_paths_keep_cleanup_session_scoped():
    """Reconnect terminal cleanup paths should follow the same owner model."""
    restore_body = _function_body("_restoreSettledSession")
    error_body = _function_body("_handleStreamError")
    combined = restore_body + "\n" + error_body
    assert combined.count("_clearOwnerInflightState();") >= 2
    assert "delete INFLIGHT[activeSid];clearInflight();clearInflightState(activeSid)" not in combined
    assert "stopApprovalPolling();stopClarifyPolling();" not in combined
    assert "renderSessionList();setBusy(false)" not in combined
    assert "_setActivePaneIdleIfOwner" in combined

def test_stream_end_without_done_restores_settled_session_before_closing():
    """If a journal/replay emits stream_end without done, the UI must settle from /api/session.

    A close-only stream_end handler leaves live Thinking/inflight DOM around and
    never replaces the pane with the persisted transcript when done is missing.
    """
    body = _event_body("stream_end")
    restore_idx = body.find("_restoreSettledSession(source,{status:true})")
    if restore_idx == -1:
        restore_idx = body.find("_restoreSettledSession(source)")
    close_idx = body.find("_closeSource(source)", restore_idx)
    if close_idx == -1:
        close_idx = body.find("_finalizeStreamEndFallback(source)", restore_idx)
    finalized_idx = body.find("_streamFinalized=true", restore_idx)
    if finalized_idx == -1:
        finalized_idx = body.find("_finalizeStreamEndFallback(source)", restore_idx)
    assert restore_idx != -1, "stream_end handler must restore settled session when done is absent"
    assert close_idx != -1, "stream_end handler must still close the owning EventSource"
    assert restore_idx < close_idx, "restore must be attempted before closing the stream"
    assert finalized_idx != -1, "stream_end terminal path must suppress trailing rAF/render work"


def test_settled_restore_and_error_close_only_the_event_source_owner():
    """Late stale-source async cleanup must not close a newer reconnect source."""
    restore_body = _function_body("_restoreSettledSession")
    error_body = _function_body("_handleStreamError")
    event_body = _event_body("error")
    assert "async function _restoreSettledSession(source, options=null)" in MESSAGES_JS
    assert "function _handleStreamError(source)" in MESSAGES_JS
    assert "_closeSource(source);" in restore_body
    assert "_closeSource(source);" in error_body
    assert "_restoreSettledSession(source, {preserveVisibleOnShorterTerminalSnapshot:true})" in event_body
    assert "_handleStreamError(source)" in event_body
    assert "_restoreSettledSession())" not in event_body
    assert "_handleStreamError();" not in event_body

def test_done_handler_is_idempotent_for_replay_or_duplicate_done_events():
    """Duplicate/replayed done events must not replay completion sound or duplicate render."""
    body = _event_body("done")
    first_stmt = body.strip().splitlines()[0].strip()
    assert "_streamFinalized" in first_stmt and "return" in first_stmt, (
        "done handler must return early when the stream was already finalized"
    )
    guard_idx = body.find("if(_streamFinalized) return;")
    sound_idx = body.find("playNotificationSound();")
    assert sound_idx != -1, "done handler should still play completion sound once"
    assert guard_idx != -1 and guard_idx < sound_idx, (
        "completion sound must be behind the duplicate-done finalization guard"
    )


def test_attention_events_use_distinct_sound_from_completion():
    approval_body = _event_body("approval")
    clarify_body = _event_body("clarify")
    done_body = _event_body("done")
    attention_body = _function_body("playAttentionSound")

    assert "playAttentionSound(_attentionSoundKey(activeSid,'approval',1));" in approval_body
    assert "playAttentionSound(_attentionSoundKey(activeSid,'clarify',1));" in clarify_body
    for body in (approval_body, clarify_body):
        assert "playNotificationSound();" not in body
    assert "playNotificationSound();" in done_body
    assert "playAttentionSound();" not in done_body
    assert "osc.type='sine'" in attention_body
    assert "window._lastAttentionSoundAt" in attention_body
    assert "nowMs-window._lastAttentionSoundAt<900" in attention_body
    assert "window._attentionSoundSeenKeys" in attention_body
    assert "seen.has(dedupeKey)" in attention_body
    assert "seen.set(dedupeKey,nowMs)" in attention_body
    assert "osc.frequency.setValueAtTime(880,ctx.currentTime);" in attention_body
    assert "osc.frequency.setValueAtTime(660,ctx.currentTime+0.075);" in attention_body
    assert "gain.gain.setValueAtTime(0.24,ctx.currentTime);" in attention_body
    assert "osc.stop(ctx.currentTime+0.24);" in attention_body
    assert "Notification sound failed" not in attention_body


def test_attach_live_stream_registers_one_source_per_session_stream():
    """Reconnect/compaction paths must not stack same-stream EventSources.

    The stream channel broadcasts each token to every subscriber. If the browser
    opens four live EventSources for the same run, one assistant paragraph is
    appended four times even though the run journal contains it once.
    """
    close_body = _function_body("closeLiveStream")
    attach_body = _function_body("attachLiveStream")
    wire_body = _function_body("_wireSSE")
    error_body = _event_body("error")

    assert "const LIVE_STREAMS={};" in MESSAGES_JS
    assert "LIVE_STREAMS[activeSid]={streamId,source};" in wire_body
    assert "existingLive.source.close();" in wire_body
    assert "if(source&&live.source!==source) return;" in close_body
    assert "existingLive&&existingLive.streamId===streamId" in attach_body
    assert "_closeSource(source);" in error_body
