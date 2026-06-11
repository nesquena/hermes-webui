"""Regression test for Issue #2233: per-turn SQLite connection leak.

Two functions on the /api/session/handoff-summary hot path were opening
``sqlite3.connect(...)`` inside a bare ``with`` statement, which commits
the transaction at scope exit but does NOT close the connection. Looping
those calls per chat turn accumulated file descriptors (state.db and
state.db-wal) and CPython heap pages on long-lived worker threads.

The fix wraps both connect() calls with ``contextlib.closing(...)`` so
the connection is closed deterministically:

  * api/models.py :: count_conversation_rounds
  * api/routes.py :: _persist_handoff_summary_to_state_db

This test loops the two patched functions ~20 times against a tmp state.db
and asserts the parent process open-fd count does not climb.

Linux-only because the check reads ``/proc/<pid>/fd`` directly. Skipped
on macOS/Windows.
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_IS_LINUX = sys.platform.startswith("linux")


def _open_fd_count() -> int:
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


def _make_state_db(path: Path) -> None:
    """Create a state.db with the minimum schema the two patched functions touch."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE messages ("
            " session_id TEXT,"
            " role TEXT,"
            " content TEXT,"
            " timestamp REAL"
            ")"
        )
        conn.execute(
            "CREATE TABLE sessions ("
            " id TEXT PRIMARY KEY,"
            " message_count INTEGER"
            ")"
        )
        conn.execute(
            "INSERT INTO sessions (id, message_count) VALUES (?, 0)",
            ("20260101_000000_abcdef",),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, 'user', 'hi', 1.0)",
            ("20260101_000000_abcdef",),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, 'agent', 'hello', 2.0)",
            ("20260101_000000_abcdef",),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.skipif(not _IS_LINUX, reason="fd counting via /proc only available on Linux")
def test_handoff_summary_path_does_not_leak_fds(tmp_path, monkeypatch):
    """Loop both patched functions and assert open-fd count stays bounded."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    state_db = hermes_home / "state.db"
    _make_state_db(state_db)

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from api.models import count_conversation_rounds
    from api.routes import _persist_handoff_summary_to_state_db

    sid = "20260101_000000_abcdef"
    marker = {
        "role": "tool",
        "content": "{\"handoff_summary\": \"test\"}",
        "timestamp": 3.0,
    }

    count_conversation_rounds(sid)
    _persist_handoff_summary_to_state_db(sid, marker)

    fd_before = _open_fd_count()

    for _ in range(20):
        count_conversation_rounds(sid)
        _persist_handoff_summary_to_state_db(sid, marker)

    fd_after = _open_fd_count()
    growth = fd_after - fd_before

    assert growth <= 2, (
        f"open fd count grew by {growth} (before={fd_before}, after={fd_after}); "
        "suggests sqlite3 connections from the handoff-summary path are not being closed"
    )
