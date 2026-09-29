"""Regression tests for v0.50.254 Opus pre-release follow-ups.

Apr 2026 v0.50.254 batch added per-tab session URL anchors (#1392). Opus advisor
flagged that the new popstate handler was missing the same `S.busy` guard the
storage-event handler had — a user mid-stream who hits browser Back would lose
their active turn the same way cross-tab churn used to do. Adds the guard.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_popstate_handler_guards_busy_state():
    """Browser back/forward must not switch sessions while a stream is live.

    The new `popstate` handler in `static/sessions.js` (added by #1392) has to
    mirror the `S.busy` guard that the cross-tab storage handler had. Otherwise
    a user mid-stream who absent-mindedly hits Back will get yanked out of their
    active turn — exactly the regression the storage-event guard was added to
    prevent.
    """
    src = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
    listener_idx = src.find("addEventListener('popstate'")
    assert listener_idx != -1, "popstate handler missing from sessions.js"
    assert "_handleSessionPopstate()" in src[listener_idx:listener_idx + 120], (
        "popstate listener must delegate navigation handling"
    )
    body_idx = src.find("async function _handleSessionPopstate()")
    body_end = src.find("\nasync function removeWorktree", body_idx)
    assert body_idx != -1 and body_end > body_idx, "popstate navigation helper not found"
    body = src[body_idx:body_end]
    busy_guard = body.find("if(S.busy&&currentSid!==sid)")
    navigate = body.find("return _openSessionReference(sid,")
    assert busy_guard != -1 and navigate > busy_guard, (
        "popstate navigation must check S.busy before switching sessions — "
        "otherwise mid-stream users lose their turn when they hit browser Back. "
        "Mirror the same guard the cross-tab storage handler had."
    )
    assert "_openSessionReference(sid," in body, "popstate handler must open the requested reference when allowed"
