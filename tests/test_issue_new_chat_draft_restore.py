"""Regression coverage for New Chat draft-session restoration.

The bug: New Chat -> type draft -> open history -> New Chat created a fresh
empty session instead of returning to the empty session that owns the draft.
"""
from pathlib import Path


ROOT = Path(__file__).parents[1]
SESSIONS_JS = ROOT.joinpath("static", "sessions.js").read_text(encoding="utf-8")
BOOT_JS = ROOT.joinpath("static", "boot.js").read_text(encoding="utf-8")


def _btn_new_chat_handler() -> str:
    start = BOOT_JS.find("$('btnNewChat').onclick=async()=>{")
    end = BOOT_JS.find("$('btnDownload').onclick", start)
    assert start != -1 and end != -1, "btnNewChat handler block not found"
    return BOOT_JS[start:end]


def test_new_session_remembers_regular_empty_session_id():
    start = SESSIONS_JS.find("async function newSession(")
    end = SESSIONS_JS.find("async function loadSession(", start)
    assert start != -1 and end != -1, "newSession block not found"
    body = SESSIONS_JS[start:end]
    assign_idx = body.find("S.session=data.session")
    remember_idx = body.find("_rememberNewChatDraftSession(S.session)")
    assert assign_idx != -1, "newSession must assign S.session from the POST response"
    assert remember_idx > assign_idx, "newSession must remember the created empty session id"
    assert "if(!(options&&options.worktree)) _rememberNewChatDraftSession(S.session);" in body, (
        "worktree-backed new sessions must not become New Chat draft candidates"
    )


def test_new_chat_button_restores_remembered_draft_before_creating_session():
    body = _btn_new_chat_handler()
    restore_idx = body.find("_restoreRememberedNewChatDraftSession")
    new_idx = body.find("await newSession()")
    assert restore_idx != -1, "New Chat button must try the remembered draft session first"
    assert new_idx != -1, "New Chat button must still fall back to creating a session"
    assert restore_idx < new_idx, "draft-session restore must happen before newSession fallback"
    assert "return;" in body[restore_idx:new_idx], (
        "successful draft-session restore must return instead of also creating a new session"
    )


def test_restore_helper_validates_candidate_with_session_metadata():
    assert "const NEW_CHAT_DRAFT_SESSION_KEY = 'hermes-new-chat-draft-session';" in SESSIONS_JS
    assert "async function _restoreRememberedNewChatDraftSession()" in SESSIONS_JS
    assert "messages=0&resolve_model=0" in SESSIONS_JS, (
        "helper should validate the hidden zero-message candidate through /api/session metadata"
    )
    assert "_isRestorableNewChatDraftSession(session, true)" in SESSIONS_JS, (
        "candidate must have a non-empty server-side composer_draft before restore"
    )
    assert "await loadSession(sid, {skipLineageResolve:true});" in SESSIONS_JS, (
        "helper should load the exact hidden empty draft session"
    )


def test_session_switch_awaits_immediate_draft_flush_before_loading_target():
    assert "return api('/api/session/draft'" in SESSIONS_JS, (
        "_saveComposerDraftNow should return its POST promise so switch-away can await it"
    )
    assert "await _saveComposerDraftNow(currentSid" in SESSIONS_JS, (
        "loadSession must flush the current draft before fetching the next session"
    )


def test_pre_switch_draft_flush_rechecks_stale_loading_guard():
    """The awaited draft-save in loadSession yields the event loop. On a rapid
    session switch (B then quickly C) the stale B continuation must bail out
    before the destructive state-clearing block, or it would wipe the
    freshly-loaded C state. The guard is `if (_loadingSessionId !== sid) return;`
    placed AFTER the awaited save and BEFORE the `S.messages = []` clear
    (Codex pre-release CORE catch, #3471)."""
    start = SESSIONS_JS.find("async function loadSession(")
    assert start != -1, "loadSession not found"
    body = SESSIONS_JS[start:start + 4000]
    await_idx = body.find("await _saveComposerDraftNow(currentSid")
    guard_idx = body.find("if (_loadingSessionId !== sid) return;", await_idx)
    clear_idx = body.find("S.messages = [];", await_idx)
    assert await_idx != -1, "pre-switch awaited draft save not found"
    assert guard_idx != -1, "stale-loading guard missing after the awaited draft save"
    assert clear_idx != -1, "destructive S.messages clear not found"
    assert await_idx < guard_idx < clear_idx, (
        "the _loadingSessionId stale-guard must sit between the awaited draft "
        "save and the destructive state clear so a rapid switch can't blank the "
        "newer session"
    )


def test_restorable_candidate_rejects_in_flight_worktree_and_cross_profile_sessions():
    start = SESSIONS_JS.find("function _isRestorableNewChatDraftSession(")
    end = SESSIONS_JS.find("function _rememberNewChatDraftSession", start)
    assert start != -1 and end != -1, "restorable candidate helper not found"
    body = SESSIONS_JS[start:end]
    assert "messageCount !== 0" in body
    assert "session.active_stream_id || session.pending_user_message || session.worktree_path" in body
    assert "_profileMatchesActiveProfile(sessionProfile, activeProfile)" in body
    assert "session.composer_draft || {}" in body
    assert "text || files.length" in body


def test_clear_composer_draft_forgets_same_new_chat_candidate():
    start = SESSIONS_JS.find("function _clearComposerDraft(")
    end = SESSIONS_JS.find("const SESSION_VIEWED_COUNTS_KEY", start)
    assert start != -1 and end != -1, "_clearComposerDraft block not found"
    body = SESSIONS_JS[start:end]
    assert "_clearRememberedNewChatDraftSession(sid);" in body, (
        "sending a draft must stop New Chat from restoring that now-cleared candidate"
    )
