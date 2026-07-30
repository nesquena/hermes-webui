"""Regression: ``loadSession`` must restart the session SSE stream on an early
failure exit (Greptile review on PR #2979).

Context
-------
``loadSession`` in ``static/sessions.js`` stops the per-session SSE stream
unconditionally near the top (mirroring ``stopApprovalPolling``):

    if(typeof stopSessionStream==='function') stopSessionStream();

On the happy path it is restarted ~120 lines later at the success tail:

    if(typeof startSessionStream==='function') startSessionStream(S.session.session_id);

But the metadata-fetch ``catch`` block (network error / 4xx / 5xx) returns
early WITHOUT reaching that restart. The session stream is the new feature's
primary delivery path for ``bg_task_complete`` events, so leaving it stopped
silently drops every completion event for the session still on screen until
the user explicitly navigates to a session again.

The fix restarts the stream for the session that remains on screen
(``currentSid``) inside the ``catch`` block, guarded so it does NOT fire when:
  - a newer load is already in flight (``_loadingSessionId`` reset to a newer
    sid owns the restart), or
  - the failure self-healed away the current session (404 on the current
    session) — there is no live session to stream for.

We can't drive JS from pytest (the repo intentionally avoids a node/jsdom dep
per AGENTS.md), so this file does string-grep + brace-balance assertions on
``static/sessions.js`` — the same convention the rest of the WEBUI suite uses.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_sessions_js() -> str:
    return (REPO_ROOT / "static" / "sessions.js").read_text()


def _load_session_body() -> str:
    """Return the source slice of ``async function loadSession(`` start →
    next top-level ``function`` / ``async function`` declaration."""
    js = _read_sessions_js()
    start = js.index("async function loadSession(")
    rest = js[start + 1 :]
    m = re.search(r"\n(async function |function )", rest)
    end = start + 1 + (m.start() if m else len(rest))
    return js[start:end]


def _catch_block() -> str:
    """Return the metadata-fetch ``catch(e){ ... }`` slice within loadSession.

    Anchors on the ``data = await api(`/api/session?...messages=0...`)`` try and
    walks brace balance over the following ``catch (e) { ... }`` block.
    """
    body = _load_session_body()
    # The metadata fetch is the first `catch(` after the messages=0 api() call.
    anchor = re.search(r"messages=0[^\n]*resolve_model=0", body)
    assert anchor is not None, "metadata fetch (messages=0&resolve_model=0) not found"
    cm = re.search(r"catch\s*\(\s*e\s*\)\s*\{", body[anchor.end():])
    assert cm is not None, "catch(e){ for metadata fetch not found"
    open_abs = anchor.end() + cm.end() - 1  # index of '{'
    depth = 0
    for i in range(open_abs, len(body)):
        c = body[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return body[open_abs : i + 1]
    raise AssertionError("unbalanced catch block in loadSession")


def test_load_session_stops_stream_on_entry():
    """Sanity: the unconditional stop at the top of loadSession still exists —
    it is the precondition that makes the restart-on-failure necessary."""
    body = _load_session_body()
    assert "stopSessionStream()" in body, (
        "loadSession no longer stops the session stream on entry; the restart "
        "guard's precondition has changed — re-derive this test."
    )


def test_catch_restarts_session_stream_for_current_sid():
    """The metadata-fetch catch block must restart the session stream for the
    session still on screen (currentSid)."""
    catch = _catch_block()
    m = re.search(r"startSessionStream\s*\(\s*currentSid\s*\)", catch)
    assert m is not None, (
        "loadSession's metadata-fetch catch block does not restart "
        "startSessionStream(currentSid); bg_task_complete events would be "
        "silently dropped for the on-screen session after a failed load."
    )


def test_restart_is_guarded_against_newer_inflight_load():
    """The restart must be gated on ``_loadingSessionId === null`` so a newer
    in-flight load (rapid session switch) owns the stream instead — the newer
    load starts its own stream and must not be clobbered."""
    catch = _catch_block()
    # The guard and the restart call live in the same if-condition; assert the
    # null-check precedes the startSessionStream(currentSid) call.
    restart = re.search(r"startSessionStream\s*\(\s*currentSid\s*\)", catch)
    assert restart is not None
    guard = re.search(r"_loadingSessionId\s*===\s*null", catch[: restart.start()])
    assert guard is not None, (
        "restart of startSessionStream(currentSid) is not guarded by "
        "_loadingSessionId === null; a newer in-flight load could be clobbered."
    )


def test_restart_skipped_when_current_session_self_healed():
    """A 404 on the *current* session self-heals it away (clears localStorage +
    URL). There is then no live session to stream for, so the restart must be
    skipped to avoid spinning the SSE reconnect loop against a dead id."""
    catch = _catch_block()
    # A self-heal guard distinguishing the 404-on-current case must exist and
    # gate the restart (negated in the restart condition).
    assert re.search(r"_selfHealedCurrent", catch), (
        "no _selfHealedCurrent guard found; a 404 on the current session would "
        "wrongly restart a stream against a dead session id."
    )
    restart = re.search(r"startSessionStream\s*\(\s*currentSid\s*\)", catch)
    assert restart is not None
    neg_guard = re.search(r"!\s*_selfHealedCurrent", catch[: restart.start()])
    assert neg_guard is not None, (
        "restart is not gated on !_selfHealedCurrent; the self-healed-current "
        "case would wrongly re-open a stream."
    )
