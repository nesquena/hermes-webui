"""Regression coverage for failed-send snapshot, draft restore, and retry affordance (#5472).

Behavioral browserless tests: verify that generic /api/chat/start failure restores the
composer draft and marks the user turn retryable; that retry routes through send(); that
404 and conflict branches are unaffected; that stream errors mark the matching active turn
retryable; that background errors stay in trackBackgroundError; and that i18n strings and
the ui.js retry button rendering are in place.
"""
from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS       = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS     = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


def _function_body(src: str, name: str) -> str:
    for prefix in (f"async function {name}(", f"function {name}("):
        start = src.find(prefix)
        if start >= 0:
            break
    assert start >= 0, f"{name} not found in source"
    brace = src.find("{", start)
    depth = 0
    i = brace
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace: i + 1]
        i += 1
    raise AssertionError(f"unclosed function: {name}")


# ─── Module-level snapshot variable ─────────────────────────────────────────

def test_failed_send_snapshot_declared_before_send():
    """_failedSendSnapshot must be declared at module scope (before send()) to persist
    across the async gap between send() clearing the composer and the start-request settling."""
    clean = _strip_js_comments(MESSAGES_JS)
    assert "_failedSendSnapshot = null" in clean, (
        "_failedSendSnapshot module-level variable not found in messages.js"
    )
    snapshot_decl_idx = clean.index("_failedSendSnapshot = null")
    send_fn_idx = clean.index("async function send(")
    assert snapshot_decl_idx < send_fn_idx, (
        "_failedSendSnapshot must be declared before the send() function"
    )


# ─── Snapshot created before composer clear ──────────────────────────────────

def test_snapshot_created_before_composer_clear():
    """The send snapshot must be written before the main composer clear (the one paired with
    _clearComposerDraft) so the user text is captured.
    Expected head behavior: snapshot holds {sid, text} at the moment of send."""
    body = _function_body(MESSAGES_JS, "send")
    clean = _strip_js_comments(body)
    assert "_failedSendSnapshot = {sid" in clean, (
        "_failedSendSnapshot assignment not found in send()"
    )
    snapshot_idx = clean.index("_failedSendSnapshot = {sid")
    # Use _clearComposerDraft as the landmark for the main send-path composer clear,
    # not bare $('msg').value='' which also appears in early-return agent-command blocks.
    clear_draft_idx = clean.index("_clearComposerDraft(activeSid)")
    assert snapshot_idx < clear_draft_idx, (
        "snapshot must be assigned before the main composer clear (_clearComposerDraft) "
        "so the user text is captured before the textarea is wiped "
        "(base: no snapshot; head: snapshot precedes clear)"
    )


def test_snapshot_carries_session_id_and_user_text():
    """Snapshot must carry activeSid so retryFailedSend can reject stale-session retries,
    and the user text so the draft can be restored without round-tripping the server."""
    body = _function_body(MESSAGES_JS, "send")
    idx = body.find("_failedSendSnapshot = {sid")
    assert idx >= 0, "_failedSendSnapshot assignment not found in send()"
    snippet = body[idx: idx + 140]
    assert "activeSid" in snippet, f"snapshot must include activeSid, got: {snippet!r}"
    assert "text" in snippet, f"snapshot must include user text, got: {snippet!r}"


# ─── Snapshot cleared on successful done ─────────────────────────────────────

def test_snapshot_survives_start_success_until_done():
    """Snapshot must survive /api/chat/start success so early stream errors can mark retry."""
    send_body = _strip_js_comments(_function_body(MESSAGES_JS, "send"))
    start_api_idx = send_body.index("await api('/api/chat/start'")
    success_idx = send_body.index("postStartData = startData", start_api_idx)
    catch_idx = send_body.index("}catch(e){", success_idx)
    post_start_window = send_body[success_idx:catch_idx]
    assert "_failedSendSnapshot = null" not in post_start_window, (
        "_failedSendSnapshot must not be cleared between /api/chat/start success "
        "and attachLiveStream, otherwise terminal stream errors cannot mark retry"
    )


def test_snapshot_cleared_after_successful_done():
    """Successful done must clear the snapshot so stale retry state cannot leak."""
    source_idx = MESSAGES_JS.index("source.addEventListener('done'")
    clear_idx = MESSAGES_JS.index("_failedSendSnapshot&&_failedSendSnapshot.sid===activeSid", source_idx)
    owner_clear_idx = MESSAGES_JS.index("_clearOwnerInflightState()", source_idx)
    assert owner_clear_idx < clear_idx, (
        "successful done should clear owner inflight state before clearing the failed-send snapshot"
    )


# ─── Generic start failure marks user turn retryable ─────────────────────────

def test_generic_start_failure_marks_last_user_turn_retryable():
    """The generic /api/chat/start catch must mark the last user turn with _send_failed_retry.
    Base: no marker exists after failure.  Head: last user turn gets _send_failed_retry=true."""
    body = _function_body(MESSAGES_JS, "send")
    clean = _strip_js_comments(body)
    # Conflict branch comes before generic error path
    conflict_idx = clean.index("conflictActiveStream")
    retry_marker_idx = clean.index("_send_failed_retry=true", conflict_idx)
    retry_text_idx = clean.index("_send_failed_text=_failedSendSnapshot.text||''", retry_marker_idx)
    render_idx = clean.index("renderMessages()", retry_marker_idx)
    assert retry_marker_idx < render_idx, (
        "_send_failed_retry must be set on the user turn before renderMessages() so the "
        "retry button is visible in the first render after failure"
    )
    assert retry_marker_idx < retry_text_idx < render_idx, (
        "failed user turn must keep its own raw retry text before renderMessages()"
    )


def test_generic_failure_retry_marker_guarded_by_snapshot():
    """The retry marker must only be applied when _failedSendSnapshot matches the active session,
    not for every failure regardless of state."""
    body = _function_body(MESSAGES_JS, "send")
    clean = _strip_js_comments(body)
    conflict_idx = clean.index("conflictActiveStream")
    retry_marker_idx = clean.index("_send_failed_retry=true", conflict_idx)
    guard_window = clean[conflict_idx:retry_marker_idx]
    assert "_failedSendSnapshot" in guard_window, (
        "retry marker must be guarded by _failedSendSnapshot check"
    )
    assert "activeSid" in guard_window, (
        "retry marker guard must verify session id matches the snapshot"
    )


# ─── Draft restored after generic failure ─────────────────────────────────────

def test_draft_restored_into_composer_after_generic_failure():
    """After generic start failure, the original text must be written back into #msg.
    Base: composer stays empty after send() clears it.  Head: snapshot text restored."""
    body = _function_body(MESSAGES_JS, "send")
    clean = _strip_js_comments(body)
    conflict_idx = clean.index("conflictActiveStream")
    setbusy_idx = clean.index("setBusy(false)", conflict_idx)
    restore_idx = clean.index("$('msg').value=_snapText", setbusy_idx)
    assert restore_idx > setbusy_idx, (
        "draft must be restored after setBusy(false) — ensures composer is writable again"
    )


def test_draft_restored_status_overrides_error_status():
    """Composer status after failed send with snapshot match must indicate draft was restored,
    not repeat the raw error string (the error remains visible in the chat bubble)."""
    body = _function_body(MESSAGES_JS, "send")
    clean = _strip_js_comments(body)
    conflict_idx = clean.index("conflictActiveStream")
    setbusy_idx = clean.index("setBusy(false)", conflict_idx)
    draft_status_idx = clean.index("send_draft_restored", setbusy_idx)
    assert draft_status_idx > setbusy_idx, (
        "'send_draft_restored' status string must appear in the generic failure path after setBusy(false)"
    )


# ─── Negative space: 404 branch untouched ────────────────────────────────────

def test_404_branch_returns_before_retry_marker():
    """The 404 self-heal branch must return early, before retry marker assignment.
    This is the negative-space proof: 404 stale-session self-heal must NOT show a retry action."""
    body = _function_body(MESSAGES_JS, "send")
    clean = _strip_js_comments(body)
    e404_idx = clean.index("e&&e.status===404")
    e404_return_idx = clean.index("return;", e404_idx)
    retry_marker_idx = clean.index("_send_failed_retry=true")
    assert e404_return_idx < retry_marker_idx, (
        "404 branch must return before _send_failed_retry is assigned — "
        "404 self-heal resets dead session state and must NOT create a retry affordance"
    )


# ─── Negative space: conflict branch queues but does not mark retry ───────────

def test_conflict_branch_returns_after_queue_even_if_reload_fails():
    """The active-stream conflict branch must be terminal after queueing the turn.
    A loadSession failure must not fall through to the generic retry marker path."""
    body = _function_body(MESSAGES_JS, "send")
    clean = _strip_js_comments(body)
    conflict_idx = clean.index("conflictActiveStream")
    queue_idx = clean.index("queueSessionMessage", conflict_idx)
    catch_idx = clean.index("catch(_)", queue_idx)
    retry_marker_idx = clean.index("_send_failed_retry=true", conflict_idx)
    conflict_window = clean[conflict_idx:retry_marker_idx]
    catch_offset = catch_idx - conflict_idx
    assert queue_idx < catch_idx, "conflict branch must queue before the reload catch"
    assert "_failedSendSnapshot = null" in conflict_window[:catch_offset], (
        "conflict branch must clear the failed-send snapshot after queueing the turn"
    )
    assert "return;" in conflict_window[catch_offset:], (
        "conflict branch catch must return before the generic retry marker path"
    )
    assert "_failedSendSnapshot = null" in conflict_window[catch_offset:], (
        "conflict branch catch must clear the failed-send snapshot before returning"
    )


# ─── Active terminal stream error marks matching turn ────────────────────────

def test_stream_error_marks_active_turn_when_snapshot_matches():
    """_handleStreamError must set _send_failed_retry on the last user turn when the
    active session matches the failed-send snapshot and no assistant content arrived.
    Base: terminal error records connection lost only.  Head: matching user turn also marked."""
    marker = "function _handleStreamError("
    start = MESSAGES_JS.find(marker)
    assert start >= 0, "_handleStreamError not found"
    brace = MESSAGES_JS.find("{", start)
    depth = 0
    i = brace
    handler_body = ""
    while i < len(MESSAGES_JS):
        if MESSAGES_JS[i] == "{":
            depth += 1
        elif MESSAGES_JS[i] == "}":
            depth -= 1
            if depth == 0:
                handler_body = MESSAGES_JS[brace: i + 1]
                break
        i += 1
    assert handler_body, "_handleStreamError body extraction failed"
    clean = _strip_js_comments(handler_body)
    assert "_failedSendSnapshot" in clean, (
        "_handleStreamError must reference _failedSendSnapshot to check for a matching snapshot"
    )
    assert "_send_failed_retry=true" in clean, (
        "_handleStreamError must set _send_failed_retry=true on the matching user turn"
    )
    assert "_send_failed_text=_failedSendSnapshot.text||''" in clean, (
        "_handleStreamError must copy raw snapshot text onto the marked user turn"
    )
    assert "!assistantText" in clean, (
        "_handleStreamError must only mark retry when no assistant content was received (!assistantText)"
    )


def test_stream_error_background_path_uses_trackBackgroundError_not_retry():
    """Background-session terminal errors must continue routing through trackBackgroundError
    and must NOT set _send_failed_retry or touch the active composer."""
    marker = "function _handleStreamError("
    start = MESSAGES_JS.find(marker)
    brace = MESSAGES_JS.find("{", start)
    depth = 0
    i = brace
    handler_body = ""
    while i < len(MESSAGES_JS):
        if MESSAGES_JS[i] == "{":
            depth += 1
        elif MESSAGES_JS[i] == "}":
            depth -= 1
            if depth == 0:
                handler_body = MESSAGES_JS[brace: i + 1]
                break
        i += 1
    clean = _strip_js_comments(handler_body)
    assert "trackBackgroundError" in clean, (
        "background branch of _handleStreamError must call trackBackgroundError"
    )
    # The retry marker is in the active-session (first) branch;
    # trackBackgroundError is in the else branch — so retry marker comes first
    retry_idx = clean.index("_send_failed_retry=true")
    bg_idx = clean.index("trackBackgroundError")
    assert retry_idx < bg_idx, (
        "retry marking must be inside the active-session branch; "
        "trackBackgroundError confirms the else/background branch is separate"
    )


# ─── retryFailedSend function ─────────────────────────────────────────────────

def test_retry_failed_send_is_defined():
    """retryFailedSend must be a top-level function in messages.js (callable from onclick)."""
    assert "async function retryFailedSend(" in MESSAGES_JS, (
        "async function retryFailedSend not found in messages.js"
    )


def test_retry_failed_send_guards_on_busy_and_session():
    """retryFailedSend must early-return when S.busy, no session, or no snapshot — prevents
    double-send and stale-snapshot retry."""
    body = _function_body(MESSAGES_JS, "retryFailedSend")
    clean = _strip_js_comments(body)
    assert "S.busy" in clean, "retryFailedSend must guard on S.busy"
    assert "S.session" in clean, "retryFailedSend must guard on S.session presence"
    assert "_failedSendSnapshot" in clean, "retryFailedSend must guard on _failedSendSnapshot"
    # All guards must come before the truncate call
    busy_idx = clean.index("S.busy")
    truncate_idx = clean.index("/api/session/truncate")
    assert busy_idx < truncate_idx, "busy guard must run before truncate"


def test_retry_failed_send_verifies_session_match():
    """retryFailedSend must reject the retry when snapshot.sid differs from the current session."""
    body = _function_body(MESSAGES_JS, "retryFailedSend")
    clean = _strip_js_comments(body)
    assert "_failedSendSnapshot.sid" in clean and "session_id" in clean, (
        "retryFailedSend must compare snapshot.sid to current S.session.session_id"
    )


def test_retry_failed_send_uses_snapshot_text():
    """retryFailedSend must use the clicked failed turn's raw text, not the mutable snapshot."""
    body = _function_body(MESSAGES_JS, "retryFailedSend")
    clean = _strip_js_comments(body)
    assert "const retryText=m._send_failed_text||''" in clean, (
        "retryFailedSend must read the raw retry text from the clicked failed user turn"
    )
    assert "_failedSendSnapshot.text" not in clean, (
        "retryFailedSend must not read retry text from the mutable global snapshot"
    )


def test_retry_failed_send_truncates_then_sends():
    """retryFailedSend must truncate the failed turn from the server before calling send().
    This removes the failed user message, any assistant _error content, and any _recovered
    server turns from the model context for the next request."""
    body = _function_body(MESSAGES_JS, "retryFailedSend")
    clean = _strip_js_comments(body)
    assert "/api/session/truncate" in clean, (
        "retryFailedSend must call /api/session/truncate to remove the failed turn"
    )
    truncate_idx = clean.index("/api/session/truncate")
    send_idx = clean.index("send()")
    assert truncate_idx < send_idx, (
        "truncate must run before send() — ensures _error and _recovered turns are "
        "excluded from the next model request"
    )


def test_retry_failed_send_computes_absolute_keep_count_before_full_load():
    """retryFailedSend must compute the server keep_count before _ensureAllMessagesLoaded()
    resets _oldestIdx, then use a local index for the refreshed S.messages array."""
    body = _function_body(MESSAGES_JS, "retryFailedSend")
    clean = _strip_js_comments(body)
    absolute_idx = clean.index("const absoluteKeepCount=")
    ensure_idx = clean.index("_ensureAllMessagesLoaded")
    local_idx = clean.index("const localKeepCount=")
    truncate_idx = clean.index("/api/session/truncate")
    slice_idx = clean.index("S.messages=S.messages.slice(0,localKeepCount)")
    assert absolute_idx < ensure_idx < local_idx < truncate_idx < slice_idx, (
        "retryFailedSend must compute server keep_count before full-load, then slice "
        "with localKeepCount after full-load"
    )


def test_retry_failed_send_revalidates_loaded_message_marker():
    """After full-load replaces S.messages, retryFailedSend must re-read the current message
    instead of mutating a detached pre-load object."""
    body = _function_body(MESSAGES_JS, "retryFailedSend")
    clean = _strip_js_comments(body)
    assert "const current=S.messages[localKeepCount]" in clean, (
        "retryFailedSend must re-read the target message after _ensureAllMessagesLoaded()"
    )
    assert "current._send_failed_retry" in clean, (
        "retryFailedSend must verify the refreshed message is still retry-marked"
    )
    assert "delete m._send_failed_retry" not in clean, (
        "retryFailedSend must not mutate the detached pre-load message object"
    )


def test_retry_failed_send_clears_snapshot():
    """retryFailedSend must clear _failedSendSnapshot before calling send() so a concurrent
    retry cannot be triggered for the same failed turn."""
    body = _function_body(MESSAGES_JS, "retryFailedSend")
    clean = _strip_js_comments(body)
    # _failedSendSnapshot=null should appear between truncate and send()
    truncate_idx = clean.index("/api/session/truncate")
    send_idx = clean.index("send()")
    between = clean[truncate_idx:send_idx]
    assert "_failedSendSnapshot=null" in between, (
        "retryFailedSend must set _failedSendSnapshot=null before calling send()"
    )


# ─── ui.js retry button rendering ────────────────────────────────────────────

def test_ui_renders_failed_send_retry_button():
    """ui.js must declare failedSendRetryBtn for user turns with _send_failed_retry set."""
    assert "failedSendRetryBtn" in UI_JS, "failedSendRetryBtn not found in ui.js"
    assert "_send_failed_retry" in UI_JS, "m._send_failed_retry check not found in ui.js"
    assert "retryFailedSend(this)" in UI_JS, "retryFailedSend onclick not found in ui.js"


def test_ui_failed_send_retry_btn_uses_msg_action_btn_class():
    """failedSendRetryBtn must use msg-action-btn class for visual consistency with existing buttons."""
    idx = UI_JS.index("failedSendRetryBtn")
    snippet = UI_JS[idx: idx + 300]
    assert "msg-action-btn" in snippet, (
        "failedSendRetryBtn must use the msg-action-btn class"
    )


def test_ui_failed_send_retry_btn_in_footer_html():
    """failedSendRetryBtn must be interpolated into the msg-foot HTML so it appears in the DOM."""
    assert "${failedSendRetryBtn}" in UI_JS, (
        "failedSendRetryBtn must be included in the footer HTML template string"
    )


def test_ui_edit_and_regenerate_still_present():
    """editMessage and regenerateResponse must not be removed — retry is additive."""
    assert "editMessage(this)" in UI_JS, "editMessage must still be present in ui.js"
    assert "regenerateResponse(this)" in UI_JS, "regenerateResponse must still be present in ui.js"


# ─── i18n strings ─────────────────────────────────────────────────────────────

def test_i18n_send_failed_retry_key_exists():
    """send_failed_retry i18n key must be present for the retry button title."""
    assert "send_failed_retry:" in I18N_JS, (
        "send_failed_retry key not found in i18n.js"
    )


def test_i18n_send_draft_restored_key_exists():
    """send_draft_restored i18n key must be present for the composer status after draft restore."""
    assert "send_draft_restored:" in I18N_JS, (
        "send_draft_restored key not found in i18n.js"
    )
