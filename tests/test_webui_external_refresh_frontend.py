from pathlib import Path


SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")


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
