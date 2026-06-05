"""Regression: a malformed/negative ``depth`` on the session content-search
endpoint must not crash or silently exclude the newest messages.

``GET /api/sessions/search?...&depth=<x>`` parsed ``depth`` with a bare
``int()``. A non-numeric value (e.g. ``?depth=deep``) raised ValueError, which
propagated to the top-level request handler and surfaced as a generic HTTP 500.

``depth`` caps how many leading messages are scanned per session
(``sess.messages[:depth]``). A negative value sliced as ``messages[:-n]``,
silently dropping the *most recent* messages from the search instead of capping
the scan — so a match in a session's latest turn would be missed. depth is now
clamped to ``>= 0`` (0 keeps its existing "search the whole transcript"
meaning), mirroring the guard sibling handlers already use.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


def _run_search(query):
    """Invoke _handle_sessions_search against one synthetic session whose match
    lives in its LAST message, capturing the JSON payload/status."""
    import api.routes as routes

    sessions_meta = [{"session_id": "s1", "title": "Untitled", "profile": "default"}]
    session = SimpleNamespace(
        session_id="s1",
        messages=[
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "second message"},
            {"role": "user", "content": "NEEDLE in the latest message"},
        ],
    )
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.routes.get_session", return_value=session
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


def test_search_non_numeric_depth_does_not_500():
    # Before the fix this raised ValueError -> 500.
    captured = _run_search("/api/sessions/search?q=needle&content=1&depth=deep")
    assert captured["status"] == 200
    # depth falls back to 5 (>= 3 messages here), so the needle is found.
    assert captured["payload"]["count"] == 1


def test_search_negative_depth_still_scans_newest_message():
    # depth=-2 with 3 messages: an unclamped messages[:-2] scan would look only
    # at the first message and MISS the needle in the latest one. Clamped to a
    # default >= 0, the newest message is searched and the match is found.
    captured = _run_search("/api/sessions/search?q=needle&content=1&depth=-2")
    assert captured["status"] == 200
    assert captured["payload"]["count"] == 1


def test_search_valid_depth_still_caps_scan():
    # depth=1 scans only the first message, which does NOT contain the needle,
    # so no match — proving the cap still works for well-formed input.
    captured = _run_search("/api/sessions/search?q=needle&content=1&depth=1")
    assert captured["status"] == 200
    assert captured["payload"]["count"] == 0
