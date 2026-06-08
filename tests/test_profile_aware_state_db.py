"""Regression test for the profile-aware ``state.db`` lookup path.

Background
==========
``_active_state_db_path()`` previously returned a single hard-coded path
derived from the active Hermes profile's home. This broke when the WebUI
displayed sessions owned by a *different* profile (e.g. a session was
created by the ``reverse-engineer`` CLI profile, but the WebUI was
running as ``default``). The session was visible in the JSON sidecar,
but its messages lived in ``~/.hermes/profiles/reverse-engineer/state.db``
— a path the WebUI never looked at. Users saw the session in the
sidebar, opened it, and got an empty transcript.

This module pins the fix: callers that already have a session object
(``session.profile``) must pass it through so the lookup can target the
correct profile-scoped ``state.db`` before falling back to the active
profile.

Test matrix
-----------
1. **Session-scoped resolution wins.** When a Session object declares
   ``profile='reverse-engineer'`` and that profile has its own
   ``state.db``, the helper returns the reverse-engineer DB — not the
   active-profile DB.
2. **Explicit profile arg wins over session profile** (explicit override).
3. **Active-profile fallback.** When ``session.profile`` is unknown or
   its profile DB does not exist, the helper falls back to the active
   profile's DB. Preserves the pre-fix default.
4. **Original ``_active_state_db_path()`` stays available** for
   legacy callers and still points at the active profile's DB.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.requires_agent_modules


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_state_db(home: Path, sid: str, msg: str) -> Path:
    """Create a minimal valid state.db at ``home/state.db`` with one row."""
    db_path = home / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE sessions ("
            "  id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, "
            "  started_at REAL, message_count INTEGER, profile TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE messages ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "  role TEXT, content TEXT, timestamp REAL"
            ")"
        )
        conn.execute(
            "INSERT INTO sessions (id, source, title, model, started_at, "
            "message_count, profile) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, "cli", "Test session", "test-model", 1.0, 1, "reverse-engineer"),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (sid, "user", msg, 1.0),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


class _FakeSession:
    """Minimal stand-in for ``api.models.Session`` exposing ``.profile``."""

    def __init__(self, profile):
        self.profile = profile


@pytest.fixture
def multi_profile_home(monkeypatch, tmp_path):
    """Set up two profile homes each with their own state.db.

    Layout::

        tmp_path/
            default/state.db            (active profile, sid=default-sid)
            profiles/reverse-engineer/state.db  (sid=re-sid)

    Returns ``(default_home, re_home)`` so individual tests can target
    specific profile homes via monkeypatched helpers.
    """
    default_home = tmp_path / "default"
    re_home = tmp_path / "profiles" / "reverse-engineer"
    default_home.mkdir(parents=True)
    re_home.mkdir(parents=True)

    _make_state_db(default_home, "default-sid", "default message")
    _make_state_db(re_home, "re-sid", "reverse-engineer message")

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    # Clear the WebUI session-side HERMES_WEBUI_STATE_DIR overrides so
    # the models module reads the env var fresh.
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)

    return default_home, re_home


# ---------------------------------------------------------------------------
# Helper-resolution tests
# ---------------------------------------------------------------------------


def test_resolve_state_db_path_uses_session_profile(monkeypatch, multi_profile_home):
    """When a Session has profile='reverse-engineer', the helper reads from
    that profile's state.db, not the active profile's."""
    from api import profiles
    from api import models

    default_home, re_home = multi_profile_home

    # Make 'default' the active profile (HERMES_HOME points at default_home).
    # Patch the active-profile resolution to return default_home.
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(default_home))
    # Patch profile-home resolution so 'reverse-engineer' maps to re_home.
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", lambda name: str(re_home) if name == "reverse-engineer" else str(default_home)
    )

    session = _FakeSession(profile="reverse-engineer")
    db_path = models._resolve_state_db_path(session=session)

    assert db_path == re_home / "state.db", (
        f"Expected reverse-engineer state.db at {re_home / 'state.db'}, "
        f"got {db_path}. The session.profile hint is being ignored."
    )

    # Sanity: the chosen DB actually contains the RE message.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", ("re-sid",)
        ).fetchone()
        assert row is not None and row[0] == "reverse-engineer message"
    finally:
        conn.close()


def test_resolve_state_db_path_explicit_profile_wins(monkeypatch, multi_profile_home):
    """An explicit ``profile=`` argument overrides ``session.profile``."""
    from api import profiles
    from api import models

    default_home, re_home = multi_profile_home
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(default_home))
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", lambda name: str(re_home) if name == "reverse-engineer" else str(default_home)
    )

    # session says default, but caller explicitly asks for reverse-engineer.
    session = _FakeSession(profile="default")
    db_path = models._resolve_state_db_path(session=session, profile="reverse-engineer")
    assert db_path == re_home / "state.db"


def test_resolve_state_db_path_falls_back_when_profile_db_missing(monkeypatch, multi_profile_home):
    """If the session's profile DB does not exist, fall back to the active
    profile's DB. This preserves the pre-fix default behavior and keeps
    the WebUI working for sessions whose profile directory was deleted."""
    from api import profiles
    from api import models

    default_home, _re_home = multi_profile_home

    # Delete the reverse-engineer profile DB to simulate the fallback case.
    re_db = _re_home_from_default(default_home) / "state.db"
    re_db.unlink()

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(default_home))
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", lambda name: str(_re_home_from_default(default_home)) if name == "reverse-engineer" else str(default_home)
    )

    session = _FakeSession(profile="reverse-engineer")
    db_path = models._resolve_state_db_path(session=session)
    assert db_path == default_home / "state.db"


def test_active_state_db_path_unchanged_for_legacy_callers(monkeypatch, multi_profile_home):
    """The pre-fix ``_active_state_db_path()`` (no args) must keep returning
    the active profile's DB. Legacy call sites that don't have a session
    object must not regress."""
    from api import models
    from api import profiles

    default_home, _re_home = multi_profile_home
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(default_home))

    db_path = models._active_state_db_path()
    assert db_path == default_home / "state.db"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _re_home_from_default(default_home: Path) -> Path:
    """Convention used by the test fixture: re profile lives at
    ``default_home/../profiles/reverse-engineer``."""
    return default_home.parent / "profiles" / "reverse-engineer"
