"""Regression tests for Issue #8356:
New Chat can show previous session's transcript if session load in flight.
"""

from pathlib import Path
import re
import pytest

REPO = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _normalise_ws(s: str) -> str:
    return re.sub(r"\s+", "", s)


def test_new_session_invalidates_loading_session_and_increments_generation():
    # Verify newSession resets _loadingSessionId and advances generation
    start = SESSIONS_SRC.find("async function newSession(")
    assert start >= 0, "newSession not found in static/sessions.js"
    end = SESSIONS_SRC.find("function ", start + 20)
    body = SESSIONS_SRC[start:end]

    norm = _normalise_ws(body)
    assert "_loadingSessionId=null" in norm, (
        "newSession() must clear _loadingSessionId to abort in-flight session loads"
    )
    assert "_loadSessionGeneration++" in norm or "++_loadSessionGeneration" in norm, (
        "newSession() must advance _loadSessionGeneration so pending loads are superseded"
    )


def test_ensure_messages_loaded_checks_active_session():
    # Verify _ensureMessagesLoaded verifies active session matches sid
    start = SESSIONS_SRC.find("async function _ensureMessagesLoaded(")
    assert start >= 0, "_ensureMessagesLoaded not found in static/sessions.js"
    end = SESSIONS_SRC.find("async function ", start + 30)
    if end < 0:
        end = SESSIONS_SRC.find("function ", start + 30)
    body = SESSIONS_SRC[start:end]

    norm = _normalise_ws(body)
    assert "S.session&&S.session.session_id===sid" in norm, (
        "_ensureMessagesLoaded() must ensure S.session.session_id matches sid before mutating transcript"
    )
