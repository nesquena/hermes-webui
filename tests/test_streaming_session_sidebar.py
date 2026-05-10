"""
Regression tests for #1327: streaming sessions must not vanish from sidebar.

PR #1184 deferred the first save() until the session has real state. During the
initial streaming turn, the session still looks like Untitled + 0-messages
(title is derived later, user text is in pending_user_message not messages).
The sidebar filter must exempt actively-streaming sessions from the empty-
Untitled rule so they remain visible while the user navigates away.
"""

import pytest

import api.models as models
from api.models import (
    SESSIONS,
    STREAMS,
    all_sessions,
    new_session,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect SESSION_DIR and SESSION_INDEX_FILE to a fresh tmp dir."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    SESSIONS.clear()
    STREAMS.clear()
    yield session_dir
    SESSIONS.clear()
    STREAMS.clear()


# ── Helpers ────────────────────────────────────────────────────────────────


def _simulate_first_turn_streaming(session):
    """Simulate the state of a session during its first streaming turn.

    After _handle_chat/start sets pending_user_message and calls save(),
    but before the first assistant turn completes:
    - title is still 'Untitled'
    - messages is still empty (user text is in pending_user_message)
    - active_stream_id is set
    """
    session.pending_user_message = "Hello, this is a long prompt"
    session.active_stream_id = f"stream-{session.session_id}"
    session.save()
    # Register stream so _active_stream_ids() finds it
    STREAMS[session.active_stream_id] = session.session_id


# ── Index path (sidebar via index file) ────────────────────────────────────


def test_streaming_session_visible_in_sidebar_index_path(_isolate):
    """A session that is actively streaming its first turn must appear in
    all_sessions() even though it is Untitled + 0-messages (#1327)."""
    s = new_session()
    _simulate_first_turn_streaming(s)

    ids = {row["session_id"] for row in all_sessions()}
    assert s.session_id in ids, (
        "Actively streaming session disappeared from sidebar (index path). "
        "The Untitled+0-message filter must exempt sessions with active_stream_id."
    )


def test_empty_session_still_hidden_when_not_streaming(_isolate):
    """A plain empty Untitled session (no stream, no pending message) must
    still be hidden — the #1171 filter must not be weakened."""
    s = new_session()
    # No streaming state set — just a bare empty session

    ids = {row["session_id"] for row in all_sessions()}
    assert s.session_id not in ids, (
        "Empty Untitled session should still be hidden from sidebar. "
        "Only actively streaming sessions are exempt (#1327)."
    )


# ── Full-scan fallback path ────────────────────────────────────────────────


def test_streaming_session_visible_in_sidebar_fullscan(_isolate):
    """Same as above but forces the full-scan fallback path by corrupting
    the index file."""
    s = new_session()
    _simulate_first_turn_streaming(s)

    # Corrupt the index to force the full-scan fallback
    models.SESSION_INDEX_FILE.write_text("INVALID JSON")

    ids = {row["session_id"] for row in all_sessions()}
    assert s.session_id in ids, (
        "Actively streaming session disappeared from sidebar (full-scan path). "
        "The Untitled+0-message filter must exempt sessions with "
        "active_stream_id and pending_user_message."
    )


def test_empty_session_still_hidden_fullscan(_isolate):
    """Empty Untitled session must still be hidden on the full-scan path."""
    s = new_session()

    models.SESSION_INDEX_FILE.write_text("INVALID JSON")

    ids = {row["session_id"] for row in all_sessions()}
    assert s.session_id not in ids, (
        "Empty Untitled session should still be hidden from sidebar (full-scan)."
    )


# ── Edge cases ────────────────────────────────────────────────────────────


def test_session_visible_after_stream_completes(_isolate):
    """After streaming completes and messages are populated, the session
    must remain visible (message_count > 0)."""
    s = new_session()
    _simulate_first_turn_streaming(s)

    # Simulate stream completion: clear pending, add messages
    s.active_stream_id = None
    s.pending_user_message = None
    s.messages.append({"role": "user", "content": "Hello"})
    s.messages.append({"role": "assistant", "content": "Hi there"})
    s.title = "Greeting"
    s.save()
    STREAMS.pop(f"stream-{s.session_id}", None)

    ids = {row["session_id"] for row in all_sessions()}
    assert s.session_id in ids, (
        "Session with messages should be visible after stream completes."
    )


def test_pending_message_without_stream_still_visible(_isolate):
    """A session with pending_user_message but no active_stream_id (edge case:
    stream crashed after setting pending but before setting stream id) should
    still be visible on the full-scan path."""
    s = new_session()
    s.pending_user_message = "Hello"
    # No active_stream_id set
    s.save()

    models.SESSION_INDEX_FILE.write_text("INVALID JSON")

    ids = {row["session_id"] for row in all_sessions()}
    assert s.session_id in ids, (
        "Session with pending_user_message should be visible even without "
        "active_stream_id (full-scan path)."
    )


def test_compact_output_contains_active_stream_id(_isolate):
    """Verify that compact() output includes active_stream_id so the index
    path filter can check it."""
    s = new_session()
    s.active_stream_id = "test-stream-123"
    compact = s.compact()
    assert compact.get("active_stream_id") == "test-stream-123", (
        "compact() must include active_stream_id for the sidebar filter."
    )
