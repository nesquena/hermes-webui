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

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def session_s1_json(tmp_path):
    """Write the synthetic s1 session to a real JSON file and patch SESSION_DIR."""
    s1 = {
        "session_id": "s1",
        "title": "Untitled",
        "profile": "default",
        "messages": [
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "second message"},
            {"role": "user", "content": "NEEDLE in the latest message"},
        ],
    }
    session_file = tmp_path / "s1.json"
    session_file.write_text(json.dumps(s1), encoding="utf-8")
    return tmp_path


# ── Helpers ────────────────────────────────────────────────────────────────

def _run_search(query, session_dir):
    """Invoke _handle_sessions_search against one synthetic session whose match
    lives in its LAST message, capturing the JSON payload/status.

    The session lives at session_dir/s1.json so the ripgrep-backed content
    search can find it.
    """
    import api.routes as routes

    sessions_meta = [{"session_id": "s1", "title": "Untitled", "profile": "default"}]
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.profiles.get_active_profile_name", return_value="default"
    ), patch("api.routes.j", side_effect=fake_j):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


def _run_search_via_api(query, session_dir, monkeypatch=None):
    """Alternative: invoke the full HTTP handler via the routes module."""
    import api.routes as routes

    sessions_meta = [{"session_id": "s1", "title": "Untitled", "profile": "default"}]
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    # Patch both the SESSION_DIR reference in routes.py and the
    # all_sessions so we drive the session list from the mock.
    patches = [
        patch("api.routes.all_sessions", return_value=list(sessions_meta)),
        patch("api.routes.get_session"),  # no longer called by content search
        patch("api.profiles.get_active_profile_name", return_value="default"),
        patch("api.routes.j", side_effect=fake_j),
    ]
    if monkeypatch:
        # Allow monkeypatch to override SESSION_DIR for the imported routes module
        import api.routes as routes
        monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    else:
        patches.append(patch("api.routes.SESSION_DIR", session_dir))

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.routes.get_session",
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


# ── Depth validation tests ───────────────────────────────────────────────────

def test_search_non_numeric_depth_does_not_500(session_s1_json, monkeypatch):
    """depth=deep falls back to 5; the needle in the latest message is found."""
    import api.routes as routes

    monkeypatch.setattr(routes, "SESSION_DIR", session_s1_json)

    sessions_meta = [{"session_id": "s1", "title": "Untitled", "profile": "default"}]
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.routes.get_session"
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(SimpleNamespace(), urlparse("/api/sessions/search?q=needle&content=1&depth=deep"))

    assert captured["status"] == 200
    assert captured["payload"]["count"] == 1


def test_search_negative_depth_still_scans_newest_message(session_s1_json, monkeypatch):
    """depth=-2 is clamped to >= 0 so the latest message is searched."""
    import api.routes as routes

    monkeypatch.setattr(routes, "SESSION_DIR", session_s1_json)

    sessions_meta = [{"session_id": "s1", "title": "Untitled", "profile": "default"}]
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.routes.get_session"
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(
            SimpleNamespace(),
            urlparse("/api/sessions/search?q=needle&content=1&depth=-2"),
        )

    assert captured["status"] == 200
    assert captured["payload"]["count"] == 1


def test_search_valid_depth_still_caps_scan(session_s1_json, monkeypatch):
    """depth=1 scans only the first message; the needle in the last is missed."""
    import api.routes as routes

    monkeypatch.setattr(routes, "SESSION_DIR", session_s1_json)

    sessions_meta = [{"session_id": "s1", "title": "Untitled", "profile": "default"}]
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.routes.get_session"
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(
            SimpleNamespace(),
            urlparse("/api/sessions/search?q=needle&content=1&depth=1"),
        )

    assert captured["status"] == 200
    assert captured["payload"]["count"] == 0


# ── Metacharacter query tests (Blocker 2 regression) ────────────────────────

def test_metacharacter_query_dollar_sign(session_s1_json, monkeypatch):
    """Query '$5' is matched literally; ripgrep -F prevents regex false negatives."""
    import api.routes as routes

    # Rewrite the session so it contains literal '$5' in a message
    s1_with_metachar = {
        "session_id": "s1",
        "title": "Metachar test",
        "profile": "default",
        "messages": [
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "total is $5"},
            {"role": "user", "content": "done"},
        ],
    }
    session_file = session_s1_json / "s1.json"
    session_file.write_text(json.dumps(s1_with_metachar), encoding="utf-8")

    monkeypatch.setattr(routes, "SESSION_DIR", session_s1_json)

    sessions_meta = [{"session_id": "s1", "title": "Metachar test", "profile": "default"}]
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.routes.get_session"
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(
            SimpleNamespace(),
            urlparse("/api/sessions/search?q=$5&content=1"),
        )

    assert captured["status"] == 200
    assert captured["payload"]["count"] == 1


def test_metacharacter_query_plus(session_s1_json, monkeypatch):
    """Query '1+1' is matched literally; ripgrep -F prevents regex false negatives."""
    import api.routes as routes

    s1_with_plus = {
        "session_id": "s1",
        "title": "Plus test",
        "profile": "default",
        "messages": [
            {"role": "user", "content": "compute 1+1"},
            {"role": "assistant", "content": "result is 2"},
        ],
    }
    session_file = session_s1_json / "s1.json"
    session_file.write_text(json.dumps(s1_with_plus), encoding="utf-8")

    monkeypatch.setattr(routes, "SESSION_DIR", session_s1_json)

    sessions_meta = [{"session_id": "s1", "title": "Plus test", "profile": "default"}]
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.routes.get_session"
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(
            SimpleNamespace(),
            urlparse("/api/sessions/search?q=1%2B1&content=1"),  # 1+1 URL-encoded
        )

    assert captured["status"] == 200
    assert captured["payload"]["count"] == 1


# ── Escaped-character queries (greptile P3) ──────────────────────────────────

def test_query_with_double_quote_is_not_dropped(session_s1_json, monkeypatch):
    """Query 'he said "ok"' must find the session even though the on-disk JSON
    stores the quote as `\"`. The rg prefilter is bypassed for `"`/`\\` chars
    so the full JSON decode+match path is used.
    """
    import api.routes as routes

    s1_with_quote = {
        "session_id": "s1",
        "title": "Quote test",
        "profile": "default",
        "messages": [
            {"role": "user", "content": "he said \"ok\""},
        ],
    }
    session_file = session_s1_json / "s1.json"
    session_file.write_text(json.dumps(s1_with_quote), encoding="utf-8")

    monkeypatch.setattr(routes, "SESSION_DIR", session_s1_json)

    sessions_meta = [{"session_id": "s1", "title": "Quote test", "profile": "default"}]
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.routes.get_session"
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(
            SimpleNamespace(),
            urlparse("/api/sessions/search?q=he%20said%20%22ok%22&content=1"),
        )

    assert captured["status"] == 200
    assert captured["payload"]["count"] == 1


def test_query_with_backslash_is_not_dropped(session_s1_json, monkeypatch):
    """Query 'path\\to' must find the session; the rg prefilter is bypassed for
    backslash so the full decode path handles it.
    """
    import api.routes as routes

    s1_with_backslash = {
        "session_id": "s1",
        "title": "Backslash test",
        "profile": "default",
        "messages": [
            {"role": "user", "content": "path\\to"},
        ],
    }
    session_file = session_s1_json / "s1.json"
    session_file.write_text(json.dumps(s1_with_backslash), encoding="utf-8")

    monkeypatch.setattr(routes, "SESSION_DIR", session_s1_json)

    sessions_meta = [{"session_id": "s1", "title": "Backslash test", "profile": "default"}]
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.routes.get_session"
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(
            SimpleNamespace(),
            urlparse("/api/sessions/search?q=path%5cto&content=1"),  # path\to
        )

    assert captured["status"] == 200
    assert captured["payload"]["count"] == 1
