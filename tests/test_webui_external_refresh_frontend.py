from pathlib import Path


SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")


def test_load_session_supports_force_reload_for_external_refresh():
    assert "async function loadSession(sid, opts)" in SESSIONS_JS
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
