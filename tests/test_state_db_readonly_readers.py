"""state.db readers stay strictly read-only and idle watchers do not poll.

Contract under test:

* ``open_state_db_readonly`` opens ``file:...?mode=ro`` once and propagates a
  failure — it never retries with a write-capable handle;
* the gateway watcher does not touch the database while nobody is subscribed;
* the session listing never runs ``CREATE INDEX`` (a writer lock held for
  minutes on a large ``messages`` table) — a missing index degrades instead;
* index maintenance is an explicit drained operation
  (``scripts/ensure_state_db_read_indexes.py``);
* the cron sidebar read in ``api/routes.py`` uses the read-only opener.
"""
import fcntl
import sqlite3
from contextlib import closing

import pytest


def test_readonly_open_failure_never_retries_writable(tmp_path, monkeypatch):
    from api import agent_sessions
    path = tmp_path / "state.db"
    path.touch()
    calls = []

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        raise sqlite3.OperationalError("readonly unavailable")

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError):
        agent_sessions.open_state_db_readonly(path)
    assert len(calls) == 1
    assert calls[0][1] == {"uri": True}


def test_no_subscribers_means_no_database_poll(tmp_path, monkeypatch):
    from api import gateway_watcher as watcher
    path = tmp_path / "state.db"
    path.touch()
    instance = watcher.GatewayWatcher(hermes_home=tmp_path)
    monkeypatch.setattr(watcher, "_cheap_change_fingerprint", lambda *a: pytest.fail("polled DB"))
    monkeypatch.setattr(watcher, "_get_agent_sessions_from_db", lambda *a: pytest.fail("projected payload"))
    assert instance._poll_once(now=500) is False


def test_listing_does_not_create_indexes(tmp_path):
    from api.agent_sessions import read_importable_agent_session_rows
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, model TEXT,
          message_count INTEGER, started_at REAL, source TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL, role TEXT);
        INSERT INTO sessions VALUES ('one', 'Test', 'test', 2, 1, 'cli');
        INSERT INTO messages VALUES (1, 'one', 2, 'user');
        INSERT INTO messages VALUES (2, 'one', 3, 'assistant');
        """)
    rows = read_importable_agent_session_rows(path)
    assert rows[0]["id"] == "one"
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA index_list(messages)").fetchall() == []


def _maintenance_schema(path):
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
        CREATE TABLE sessions(source TEXT, id TEXT, message_count INTEGER, last_activity_at REAL);
        CREATE TABLE messages(session_id TEXT, timestamp REAL, role TEXT);
        """)


def test_index_maintenance_requires_drain_and_is_idempotent(tmp_path):
    from scripts.ensure_state_db_read_indexes import ensure_read_indexes
    path = tmp_path / "state.db"
    _maintenance_schema(path)
    with pytest.raises(RuntimeError, match="confirm-drained"):
        ensure_read_indexes(path)
    lock = tmp_path / "turns.lock"
    first = ensure_read_indexes(path, confirmed_drained=True, lock_file=lock)
    second = ensure_read_indexes(path, confirmed_drained=True, lock_file=lock)
    assert set(first.values()) == {"created"}
    assert set(second.values()) == {"existing"}
    assert "idx_messages_session_role" in first


def test_index_maintenance_without_lock_file_still_requires_drain(tmp_path):
    from scripts.ensure_state_db_read_indexes import ensure_read_indexes
    path = tmp_path / "state.db"
    _maintenance_schema(path)
    result = ensure_read_indexes(path, confirmed_drained=True)
    assert set(result.values()) == {"created"}


def test_index_maintenance_refuses_when_lock_is_held(tmp_path):
    from scripts.ensure_state_db_read_indexes import ensure_read_indexes
    path = tmp_path / "state.db"
    _maintenance_schema(path)
    lock = tmp_path / "turns.lock"
    with open(lock, "a+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            ensure_read_indexes(path, confirmed_drained=True, lock_file=lock)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA index_list(messages)").fetchall() == []


def test_cron_sidebar_read_uses_readonly_connection(tmp_path, monkeypatch):
    from api import routes
    path = tmp_path / 'state.db'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE sessions(id TEXT, source TEXT)')
    monkeypatch.setattr(routes, '_active_state_db_path', lambda: path)
    connect = sqlite3.connect
    calls = []
    def recording(*args, **kwargs):
        calls.append((args, kwargs))
        return connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', recording)
    routes._latest_cron_session_info_for_jobs(['test'])
    assert len(calls) == 1
    assert 'mode=ro' in calls[0][0][0]
    assert calls[0][1].get('uri') is True
