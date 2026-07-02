"""Regression test pinning the #3162 fix.

#3162: `_ensureMessagesLoaded` in static/sessions.js declared `const msgs` but the
#3018 ephemeral-field carry-forward reassigns it (`msgs = window._carryForwardEphemeralTurnFields(...)`).
`const` -> runtime TypeError -> "Failed to load conversation messages" toast on every
mobile message (v0.51.161-166). Fix: `const` -> `let`.

This is the targeted pin; tests/test_static_js_runtime_lint.py is the general guard.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _ensure_messages_loaded_body() -> str:
    start = SESSIONS_JS.index("async function _ensureMessagesLoaded")
    # Window widened (#3326 added reload-width-hint handling inside this function,
    # pushing the carry-forward reassignment further down; #3790 added the
    # cold-load expand_renderable param + comment, pushing it further still).
    return SESSIONS_JS[start: start + 4500]


def test_ensure_messages_loaded_declares_msgs_with_let():
    body = _ensure_messages_loaded_body()
    assert "let msgs = (data.session.messages" in body, (
        "_ensureMessagesLoaded must declare `let msgs` — it is reassigned by the #3018 "
        "carry-forward, and `const` throws a runtime TypeError on every mobile message (#3162)"
    )
    assert "const msgs = (data.session.messages" not in body, (
        "found `const msgs` in _ensureMessagesLoaded — this is the #3162 brick-class bug; "
        "must be `let` because msgs is reassigned"
    )


def test_ensure_messages_loaded_reassignment_still_present():
    """Keep this test meaningful: confirm the reassignment that requires `let` exists.
    If the carry-forward is removed, revisit whether `let` is still needed."""
    body = _ensure_messages_loaded_body().replace(" ", "")
    assert "msgs=window._carryForwardEphemeralTurnFields" in body, (
        "the #3018 carry-forward reassignment of msgs is gone — re-evaluate the let/const "
        "decision in _ensureMessagesLoaded"
    )
