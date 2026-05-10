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
    popstate_idx = src.find("addEventListener('popstate'")
    assert popstate_idx != -1, "popstate handler missing from sessions.js"
    # Look at the next ~600 chars of the handler body.
    body = src[popstate_idx : popstate_idx + 600]
    assert "S.busy" in body, (
        "popstate handler must check S.busy before calling loadSession() — "
        "otherwise mid-stream users lose their turn when they hit browser Back. "
        "Mirror the same guard the cross-tab storage handler had."
    )
    assert "loadSession" in body, "popstate handler must call loadSession when allowed"
