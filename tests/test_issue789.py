"""
Regression tests for GitHub issue #789.

Original bug (#789): new sessions immediately disappeared because all_sessions()
filtered out Untitled + 0-message sessions.

Original fix: exempt sessions younger than 60 seconds.

Updated for #1171 / #1182: a session only "exists" from the user's perspective
once the first message is sent. Untitled + 0-message sessions are now hidden
from the sidebar **regardless of age** — no grace window. The button guard
(#1176) and the boot-restore guard (#1182) ensure the user is never locked
out of typing into a fresh session, but the sidebar list never surfaces empty
ones. These tests reflect the new contract.
"""
import json
import time

import pytest

import api.models as models
from api.models import Session, all_sessions


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect SESSION_DIR and SESSION_INDEX_FILE to a temp dir."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)

    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


def _make_untitled_session(age_seconds, messages=None, session_id=None):
    """Create a Session with title='Untitled', updated_at set to age_seconds ago."""
    now = time.time()
    s = Session(
        session_id=session_id or None,
        title="Untitled",
        messages=messages or [],
        updated_at=now - age_seconds,
        created_at=now - age_seconds,
    )
    # Persist to disk so the full-scan fallback can also find it
    s.path.write_text(
        json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return s


def _make_titled_session(age_seconds, session_id=None):
    """Create a Session with a real title and one message."""
    now = time.time()
    s = Session(
        session_id=session_id or None,
        title="My conversation",
        messages=[{"role": "user", "content": "hello"}],
        updated_at=now - age_seconds,
        created_at=now - age_seconds,
    )
    s.path.write_text(
        json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return s


# ── Test 1: Untitled 0-message sessions are hidden regardless of age (#1171) ─

def test_new_untitled_session_is_hidden_from_sidebar():
    """A brand-new (0 s old) Untitled 0-message session must NOT appear (#1171).

    Updated for #1171/#1182: sessions only "exist" once the first message is
    sent. Empty scratch-pad sessions never surface in the sidebar.
    """
    new_session = _make_untitled_session(age_seconds=0)

    result = all_sessions()
    ids = {s["session_id"] for s in result}

    assert new_session.session_id not in ids, (
        "Untitled 0-message session must be hidden regardless of age (#1171)"
    )


def test_recent_untitled_session_under_60s_is_hidden():
    """A 30-second-old empty session must also be hidden (no grace window)."""
    recent_session = _make_untitled_session(age_seconds=30)

    result = all_sessions()
    ids = {s["session_id"] for s in result}

    assert recent_session.session_id not in ids, (
        "Untitled 0-message session younger than 60 s is also hidden (#1171)"
    )


# ── Test 2: old Untitled 0-message session is still filtered ─────────────────

def test_old_untitled_session_over_60s_is_filtered():
    """A ghost session (Untitled, 0 messages, >60 s old) must be hidden."""
    old_session = _make_untitled_session(age_seconds=120)

    result = all_sessions()
    ids = {s["session_id"] for s in result}

    assert old_session.session_id not in ids, (
        "Ghost Untitled 0-message session older than 60 s must be filtered out"
    )


def test_session_exactly_at_boundary_is_filtered():
    """A session at any age (the previous 60 s threshold no longer applies)."""
    boundary_session = _make_untitled_session(age_seconds=61)

    result = all_sessions()
    ids = {s["session_id"] for s in result}

    assert boundary_session.session_id not in ids, (
        "Untitled 0-message session is filtered regardless of age (#1171)"
    )


# ── Test 3: session with messages is always visible regardless of age ─────────

def test_session_with_messages_always_visible_new():
    """A session with messages (even Untitled) is always visible when new."""
    s = Session(
        title="Untitled",
        messages=[{"role": "user", "content": "hello"}],
    )
    s.path.write_text(
        json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    result = all_sessions()
    ids = {r["session_id"] for r in result}
    assert s.session_id in ids, "Session with messages must always appear in sidebar"


def test_session_with_messages_always_visible_old():
    """An old session with messages is always visible."""
    now = time.time()
    s = Session(
        title="Untitled",
        messages=[{"role": "user", "content": "hello"}],
        updated_at=now - 3600,
        created_at=now - 3600,
    )
    s.path.write_text(
        json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    result = all_sessions()
    ids = {r["session_id"] for r in result}
    assert s.session_id in ids, (
        "Old session with messages must always appear in sidebar"
    )


def test_titled_session_with_no_messages_old_is_visible():
    """A titled session with 0 messages (old) should not be filtered — filter
    only targets Untitled sessions."""
    now = time.time()
    s = Session(
        title="Project Alpha",
        messages=[],
        updated_at=now - 3600,
        created_at=now - 3600,
    )
    s.path.write_text(
        json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    result = all_sessions()
    ids = {r["session_id"] for r in result}
    assert s.session_id in ids, (
        "A titled session must always appear regardless of message count"
    )


# ── Test 4: mixed bag — only old Untitled empty sessions are filtered ─────────

def test_mixed_sessions_correct_visibility():
    """With a mix of sessions, only sessions with messages OR titled sessions
    are surfaced (#1171). Both new and old Untitled+empty sessions are hidden."""
    new_ghost = _make_untitled_session(age_seconds=5, session_id="new_ghost")
    old_ghost = _make_untitled_session(age_seconds=200, session_id="old_ghost")
    real_session = _make_titled_session(age_seconds=500, session_id="real_session")

    result = all_sessions()
    ids = {s["session_id"] for s in result}

    assert "new_ghost" not in ids, "New Untitled empty session is also hidden (#1171)"
    assert "old_ghost" not in ids, "Old Untitled session must be hidden"
    assert "real_session" in ids, "Titled session with messages must be visible"
