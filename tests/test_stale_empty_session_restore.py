"""Regression tests for stale empty sessions after a WebUI restart.

When a saved session ID returns 404 (e.g. the session was deleted from another
browser, or a state DB rotation removed it), the prior behavior was to show
\"Session not available in web UI.\" and stick there forever — the saved
localStorage entry never got cleared, so every reload reproduced the broken
state.

These tests lock in:
  1. ``api()`` attaches HTTP context (``.status``, ``.statusText``, ``.body``)
     to thrown errors so callers can branch on status without re-parsing text.
  2. ``loadSession()`` clears the stale ``hermes-webui-session`` key on a 404
     for the currently-saved session and rethrows, so boot can fall through
     to the empty state.
"""

from pathlib import Path
import re


REPO = Path(__file__).parent.parent
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _api_body() -> str:
    m = re.search(r"async function api\(path,opts=.*?\n\}", WORKSPACE_JS, re.DOTALL)
    assert m, "api() function must exist in workspace.js"
    return m.group(0)


def _load_session_error_block() -> str:
    start = SESSIONS_JS.find("data = await api(`/api/session?")
    assert start > 0, "loadSession metadata request not found"
    catch_idx = SESSIONS_JS.find("} catch(e) {", start)
    assert catch_idx > start, "loadSession metadata catch block not found"
    end = SESSIONS_JS.find("return;", catch_idx)
    assert end > catch_idx, "loadSession metadata catch return not found"
    return SESSIONS_JS[catch_idx:end]


def test_api_http_errors_preserve_response_status():
    """Callers must be able to distinguish stale-session 404s from generic failures."""
    body = _api_body()
    assert re.search(r"\w+\.status\s*=\s*res\.status", body), (
        "api() must attach res.status to thrown HTTP errors"
    )
    assert re.search(r"\w+\.statusText\s*=\s*res\.statusText", body), (
        "api() must attach res.statusText to thrown HTTP errors"
    )
    assert re.search(r"\w+\.body\s*=\s*text", body), (
        "api() must attach the raw error body to thrown HTTP errors"
    )


def test_load_session_clears_saved_stale_404_and_rethrows_to_boot():
    """A missing saved session should be removed and let boot show the empty state."""
    block = _load_session_error_block()
    assert "e.status===404" in block, "loadSession must keep a 404-specific branch"
    assert "localStorage.getItem('hermes-webui-session')===sid" in block, (
        "loadSession must only clear the saved active session key"
    )
    assert "localStorage.removeItem('hermes-webui-session')" in block, (
        "loadSession must clear stale saved session IDs on 404"
    )
    assert "_loadingSessionId = null" in block, (
        "loadSession must clear the in-flight load marker before rethrowing"
    )
    assert re.search(r"throw\s+e", block), (
        "loadSession must rethrow the stale saved-session 404 so boot can fall "
        "through to the no-session empty state"
    )


def test_click_into_404_does_not_clear_saved_session():
    """A 404 from a click into a different session must NOT clear the saved active key."""
    block = _load_session_error_block()
    # The clearing branch is gated on `!currentSid` — i.e. only when the
    # request was for the *saved* active session, not a user click on another.
    assert "!currentSid" in block, (
        "404 cleanup must be gated on !currentSid so user-initiated clicks "
        "into a missing session don't wipe the saved active-session key"
    )
