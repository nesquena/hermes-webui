"""Regression: ``loadSession`` must clear stale compression UI state on switch (#6572).

Context
-------
The compression UI state (``window._compressionUi`` plus its elapsed timer,
composer session-lock, and the "Compressing context" barrier element) is
per-session. Before this fix, ``loadSession`` in ``static/sessions.js`` did not
clear it when switching sessions, so a compression card belonging to a prior
session leaked across the load and surfaced as a phantom "Compressing context"
barrier on a fresh session that never triggered compression.

The fix clears the state near the top of ``loadSession`` — alongside the other
per-session resets (``_yoloEnabled=false``, ``stopClarifyPolling``,
``hideClarifyCard``) and before transcript loading — by calling the canonical
``clearCompressionUi()`` teardown (defined in ``static/ui.js``), with a
fail-soft ``window._compressionUi=null`` fallback if the function is not yet
defined:

    if(typeof clearCompressionUi==='function') clearCompressionUi();
    else window._compressionUi=null;

This is safe for a compression genuinely running on the *target* session:
clearing only tears down browser-local UI state; an active automatic
compression re-surfaces from the target session's own SSE ``compressing``
stream, and a manual compression is re-discovered from server job status when
its session loads. Releasing the composer session-lock does not signal or
cancel the backend compression.

Per AGENTS.md the WEBUI suite intentionally avoids a node/jsdom dependency, so
this is a source-level grep + brace-balance assertion — the same convention the
rest of the JS regression suite uses (e.g.
``test_bg_task_complete_loadsession_stream_restart.py``).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_sessions_js() -> str:
    return (REPO_ROOT / "static" / "sessions.js").read_text()


def _load_session_body() -> str:
    """Return the ``async function loadSession(`` source slice → next top-level
    ``function`` / ``async function`` declaration."""
    js = _read_sessions_js()
    start = js.index("async function loadSession(")
    rest = js[start + 1 :]
    m = re.search(r"\n(async function |function )", rest)
    end = start + 1 + (m.start() if m else len(rest))
    return js[start:end]


def test_loadsession_clears_compression_ui_state():
    """loadSession must clear per-session compression UI state on switch."""
    body = _load_session_body()
    # The canonical teardown must be invoked, with a fail-soft fallback so a
    # partial script load cannot leave a dangling barrier/timer/lock.
    assert "clearCompressionUi()" in body, (
        "loadSession must call clearCompressionUi() to clear stale per-session "
        "compression state (#6572 phantom barrier)"
    )
    assert re.search(
        r"typeof\s+clearCompressionUi\s*===\s*['\"]function['\"]", body
    ), "the clearCompressionUi call must be typeof-guarded"
    assert re.search(
        r"else\s+window\._compressionUi\s*=\s*null", body
    ), "a fail-soft window._compressionUi=null fallback must guard the guarded call"


def test_loadsession_clears_compression_before_transcript_load():
    """The clear must precede transcript rendering so no phantom barrier shows.

    It must sit with the other early per-session resets (near hideClarifyCard),
    not after renderMessages(), otherwise a stale barrier could paint for a
    frame on the freshly loaded session.
    """
    body = _load_session_body()
    clear_idx = body.index("clearCompressionUi")
    # Anchored to a sibling early reset that is known to run near the top.
    assert "hideClarifyCard" in body, "expected hideClarifyCard reset in loadSession"
    clarify_idx = body.index("hideClarifyCard")
    # The compression clear should be co-located with the other early resets.
    assert abs(clear_idx - clarify_idx) < 600, (
        "clearCompressionUi() should sit with the other early per-session resets "
        "(near hideClarifyCard), before transcript loading"
    )
    # And it must come before the success-tail render.
    render_match = re.search(r"renderMessages\s*\(", body)
    if render_match:
        assert clear_idx < render_match.start(), (
            "clearCompressionUi() must run before renderMessages() so a stale "
            "compression barrier never paints on the loaded session"
        )
