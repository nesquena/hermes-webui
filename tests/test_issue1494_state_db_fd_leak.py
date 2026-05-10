"""Regression test for #1494: state.db connection FD leak via context-manager use.

The bug: Python's sqlite3 connection context manager (`with sqlite3.connect(...) as
conn:`) commits or rolls back on exit. It does NOT close the connection. In a
long-running server with sidebar polling (`/api/sessions` calls
`read_importable_agent_session_rows` and `read_session_lineage_metadata` on every
poll), every poll leaked one or more open file descriptors against `~/.hermes/state.db`.

In production this drove the WebUI process past macOS's 256-FD soft limit, after
which new requests reset before producing a response (see #1458, #1494) — the
process stayed alive, the port stayed listening, but every connection RST'd because
sqlite3.connect() in a freshly accepted handler raised on FD exhaustion before any
bytes were written.

The fix wraps each `sqlite3.connect(...)` in `contextlib.closing(...)` so the
connection is explicitly closed on scope exit (in addition to the auto-commit /
rollback semantics).

This file pins all four production callsites the issue reporter (insecurejezza)
audited as still leaking on master @ 7fddc33:

  * api/agent_sessions.py:read_importable_agent_session_rows
  * api/agent_sessions.py:read_session_lineage_metadata
  * api/models.py:get_cli_session_messages
  * api/models.py:delete_cli_session

Each test monkeypatches sqlite3.connect to track every connection the function
opens, then asserts every connection is .close()'d after the call returns.
"""
import sqlite3

import pytest


def _make_state_db(path):
    """Minimal state.db schema sufficient for the four functions under test.

    Bypasses sqlite3.connect (uses sqlite3.Connection directly) so the
    seed-data setup is not counted by the _TrackingConn monkeypatch — only
    connections opened by the function under test should appear in
    `_TrackingConn.instances`.
    """
    conn = sqlite3.Connection(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            message_count INTEGER DEFAULT 0,
            started_at TEXT,
            source TEXT,
            parent_session_id TEXT,
            ended_at TEXT,
            end_reason TEXT
        );
        CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp TEXT
        );
        INSERT INTO sessions (id, title, model, message_count, started_at, source)
        VALUES ('s1', 'cli session', 'gpt-x', 2, '2026-01-01T00:00:00Z', 'cli');
        INSERT INTO messages (session_id, role, content, timestamp)
        VALUES ('s1', 'user', 'hi', '2026-01-01T00:00:01Z'),
               ('s1', 'assistant', 'hello', '2026-01-01T00:00:02Z');
        """
    )
    conn.commit()
    conn.close()


class _TrackingConn:
    """Wraps a real sqlite3.Connection to record open/close lifecycle.

    Mirrors the lightweight wrapper pattern already in
    test_pr1370_lineage_metadata_perf_and_orphan.py — keeping it inline here so
    this regression test stays self-contained and survives refactors there.
    """

    instances: list = []

    def __init__(self, *args, **kwargs):
        self._real = sqlite3.Connection(*args, **kwargs)
        self.closed = False
        _TrackingConn.instances.append(self)

    # Connection-shaped delegation
    def cursor(self):
        return self._real.cursor()

    def execute(self, *a, **kw):
        return self._real.execute(*a, **kw)

    def executescript(self, *a, **kw):
        return self._real.executescript(*a, **kw)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        self.closed = True
        return self._real.close()

    # row_factory needs to round-trip onto the real connection
    @property
    def row_factory(self):
        return self._real.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._real.row_factory = value

    # Context-manager protocol — the bug only triggers if a caller relies on
    # __exit__ to close. Defer to the real Connection's CM (commit/rollback)
    # so we faithfully reproduce the leak shape that prompted the fix.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


@pytest.fixture
def tracking_sqlite(monkeypatch):
    """Monkeypatch sqlite3.connect to use _TrackingConn and reset the instance log."""
    _TrackingConn.instances = []

    def _connect(*args, **kwargs):
        return _TrackingConn(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _connect)
    return _TrackingConn


def _assert_all_closed(tracking, fn_name):
    assert tracking.instances, (
        f"{fn_name}: no sqlite connections were opened — test setup is wrong"
    )
    leaked = [c for c in tracking.instances if not c.closed]
    assert not leaked, (
        f"{fn_name} leaked {len(leaked)} of {len(tracking.instances)} sqlite "
        f"connection(s) — context-manager-only `with sqlite3.connect()` does "
        f"not close. Wrap in contextlib.closing(). See #1494."
    )


def test_read_importable_agent_session_rows_closes_connection(tmp_path, tracking_sqlite):
    """`read_importable_agent_session_rows` must close every sqlite connection."""
    db = tmp_path / "state.db"
    _make_state_db(db)

    from api.agent_sessions import read_importable_agent_session_rows

    # Call repeatedly — under the bug each call leaked a connection.
    for _ in range(5):
        read_importable_agent_session_rows(db)

    _assert_all_closed(tracking_sqlite, "read_importable_agent_session_rows")
    assert len(tracking_sqlite.instances) == 5


def test_read_session_lineage_metadata_closes_connection(tmp_path, tracking_sqlite):
    """`read_session_lineage_metadata` must close every sqlite connection."""
    db = tmp_path / "state.db"
    _make_state_db(db)

    from api.agent_sessions import read_session_lineage_metadata

    for _ in range(5):
        read_session_lineage_metadata(db, ["s1"])

    _assert_all_closed(tracking_sqlite, "read_session_lineage_metadata")
    assert len(tracking_sqlite.instances) == 5


def test_get_cli_session_messages_closes_connection(tmp_path, tracking_sqlite, monkeypatch):
    """`get_cli_session_messages` must close every sqlite connection."""
    db = tmp_path / "state.db"
    _make_state_db(db)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Stub get_active_hermes_home so the tmp_path is used regardless of profile state.
    import api.profiles
    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: str(tmp_path))

    from api.models import get_cli_session_messages

    for _ in range(5):
        rows = get_cli_session_messages("s1")
        # Sanity: the seeded messages are returned (proves we hit the real query path).
        assert len(rows) == 2

    _assert_all_closed(tracking_sqlite, "get_cli_session_messages")
    assert len(tracking_sqlite.instances) == 5


def test_delete_cli_session_closes_connection(tmp_path, tracking_sqlite, monkeypatch):
    """`delete_cli_session` must close its sqlite connection (also keeps explicit commit working)."""
    db = tmp_path / "state.db"
    _make_state_db(db)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import api.profiles
    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: str(tmp_path))

    from api.models import delete_cli_session

    deleted = delete_cli_session("s1")
    assert deleted is True, "delete_cli_session should report the row was removed"

    # Second call: row gone, nothing to delete — connection must still close cleanly.
    deleted_again = delete_cli_session("s1")
    assert deleted_again is False

    _assert_all_closed(tracking_sqlite, "delete_cli_session")
    # First call commits; second call short-circuits but still opens+closes a connection.
    assert len(tracking_sqlite.instances) == 2

    # Verify the commit semantics survived the closing() change — row really is gone.
    real = sqlite3.Connection(str(db))
    try:
        cur = real.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", ("s1",))
        assert cur.fetchone()[0] == 0
        cur = real.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", ("s1",))
        assert cur.fetchone()[0] == 0
    finally:
        real.close()
