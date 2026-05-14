from pathlib import Path


SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")
ROUTES_PY = Path("api/routes.py").read_text(encoding="utf-8")


def test_load_session_supports_force_reload_for_external_refresh():
    assert "async function loadSession(sid)" in SESSIONS_JS
    assert "const opts = arguments[1] || {};" in SESSIONS_JS
    assert "const forceReload = !!opts.force" in SESSIONS_JS
    assert "if(currentSid===sid && !forceReload) return;" in SESSIONS_JS
    assert "loadSession(sid, {force:true" in SESSIONS_JS


def test_active_session_external_refresh_uses_metadata_then_force_reload():
    assert "function ensureActiveSessionExternalRefreshPoll()" in SESSIONS_JS
    assert "async function refreshActiveSessionIfExternallyUpdated(reason)" in SESSIONS_JS
    assert "messages=0&resolve_model=0" in SESSIONS_JS
    assert "remoteCount > localCount || remoteLast > localLast" in SESSIONS_JS
    assert "if(S.busy || S.activeStreamId) return;" in SESSIONS_JS
    assert "document.hidden" in SESSIONS_JS


def test_active_session_external_refresh_has_focus_and_visibility_hooks():
    assert "visibilitychange" in SESSIONS_JS
    assert "window.addEventListener('focus'" in SESSIONS_JS
    assert "ensureActiveSessionExternalRefreshPoll();" in SESSIONS_JS


def test_force_reload_clears_stale_blocking_prompts_immediately():
    """External refresh should not leave old approval/clarify modals blocking the composer.

    hideApprovalCard() and hideClarifyCard() defer hiding for their minimum-visible
    timers unless force=true. That is correct for active streams, but when a
    same-session external state.db update triggers loadSession(..., {force:true}),
    the session has completed elsewhere and stale prompts should be removed now.
    """
    assert "hideApprovalCard(forceReload)" in SESSIONS_JS
    assert "hideClarifyCard(forceReload, forceReload?'external-refresh':'dismissed')" in SESSIONS_JS


def test_same_session_external_refresh_preserves_active_composer_draft():
    """Same-session forced reload must not overwrite text the user is typing.

    Active-session external refresh calls loadSession(currentSid, {force:true}) after
    a metadata poll sees state.db advance. That should refresh the transcript, but
    it must not restore a stale server composer_draft over a focused/dirty textarea.
    """
    assert "function markComposerEditedNow()" in SESSIONS_JS
    assert "function _composerHasRecentLocalEdit" in SESSIONS_JS
    assert "const sameSessionForceReload=currentSid===sid&&forceReload;" in SESSIONS_JS
    assert "const shouldSkipDraftRestore=sameSessionForceReload&&" in SESSIONS_JS
    assert "composerFocused" in SESSIONS_JS
    assert "recentLocalEdit" in SESSIONS_JS
    assert "hasLocalComposerText" in SESSIONS_JS
    assert "if(!shouldSkipDraftRestore) _restoreComposerDraft(_draft, sid);" in SESSIONS_JS


def test_composer_input_marks_local_edit_before_debounced_draft_save():
    """The dirty guard needs to be updated synchronously on every textarea input."""
    assert "if(typeof markComposerEditedNow==='function') markComposerEditedNow();" in Path(
        "static/boot.js"
    ).read_text(encoding="utf-8")


def test_draft_save_does_not_touch_session_updated_at():
    """Draft autosave is UI state and must not look like a transcript update.

    If /api/session/draft bumps updated_at, the active-session metadata poll can
    treat normal typing as an external session update and force-reload the current
    session, reopening the stale-draft overwrite race.
    """
    draft_block = ROUTES_PY[
        ROUTES_PY.index('if parsed.path == "/api/session/draft":') : ROUTES_PY.index(
            'if parsed.path == "/api/session/update":'
        )
    ]
    assert "s.save(touch_updated_at=False)" in draft_block
