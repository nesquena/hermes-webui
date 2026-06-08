"""Regression coverage for #3040 item 4: visible-but-unfocused stream reattach."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _chat_error_handler() -> str:
    return MESSAGES_JS.split("source.addEventListener('error',async e=>{", 1)[1].split(
        "source.addEventListener('cancel'", 1
    )[0]


def test_visible_unfocused_current_session_still_attempts_sse_reconnect():
    """The immediate chat SSE error path should only require current-pane ownership.

    `_isSessionActivelyViewed()` also gates on document focus/visibility. Hidden tabs
    are already deferred by `_deferStreamErrorIfPageHidden(source)`, so the direct
    reconnect branch must not skip a visible-but-unfocused current session.
    """
    handler = _chat_error_handler()
    assert "if(!_isSessionCurrentPane(activeSid)) return;" in handler
    assert "if(!_isSessionActivelyViewed(activeSid)) return;" not in handler
    assert handler.index("if(!_isSessionCurrentPane(activeSid)) return;") < handler.index(
        "if(!_reconnectAttempted && streamId)"
    )


def test_reconnect_gate_comment_matches_current_pane_contract():
    handler = _chat_error_handler()
    guard_idx = handler.index("if(!_isSessionCurrentPane(activeSid)) return;")
    comment_window = handler[max(0, guard_idx - 260):guard_idx]
    assert "different session" in comment_window
    assert "visible" not in comment_window.lower()
    assert "focused" not in comment_window.lower()
