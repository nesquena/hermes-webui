"""Regression tests — the remaining pure-read state.db projections in
api/models.py open the live agent ``state.db`` read-only.

#5455 moved the session-listing / lineage / gateway-watcher reads onto
``open_state_db_readonly`` (``file:...?mode=ro``) so a write-capable handle
doesn't add checkpoint/lock surface while the agent streams into the same WAL
DB. Eight more pure-read projections in api/models.py were still opening a
read-WRITE connection with ``sqlite3.connect(str(db_path))``. This shares them
onto the same helper. The lone write path (``delete_cli_session``, a DELETE)
deliberately keeps its writable connection.

These pin that each converted read (a) routes through the read-only helper and
(b) still returns correct data, so a refactor can't silently reintroduce a
write-capable handle on the agent DB.
"""
import sqlite3
from contextlib import closing

import api.agent_sessions as agent_sessions
import api.models as models
from api.agent_sessions import open_state_db_readonly


def _make_state_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT,
            source TEXT, session_source TEXT, title TEXT,
            message_count INTEGER, started_at REAL, ended_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            timestamp REAL, tool_calls TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, message_count, "
        "started_at, ended_at) VALUES (?,?,?,?,?,?,?)",
        ("sess-1", "cli", "cli", "Root", 2, 1000.0, 1100.0),
    )
    conn.executemany(
        "INSERT INTO messages (id, session_id, role, content, timestamp, tool_calls) "
        "VALUES (?,?,?,?,?,?)",
        [
            (1, "sess-1", "user", "hi", 1000.0, None),
            (2, "sess-1", "assistant", "hello", 1001.0, None),
        ],
    )
    conn.commit()
    conn.close()


def _record_connects(monkeypatch):
    """Spy on the connect that ``open_state_db_readonly`` uses (agent_sessions
    namespace), recording target + uri per call."""
    real_connect = sqlite3.connect
    calls = []

    def spy(target, *args, **kwargs):
        calls.append({"target": str(target), "uri": bool(kwargs.get("uri"))})
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", spy)
    return calls


def _point_models_at(monkeypatch, db):
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "_agent_state_db_path", lambda *, profile=None: db)


def _assert_read_only(calls):
    assert calls, "no state.db connection was opened"
    assert calls[0]["uri"] is True, "connection was not opened via a URI (mode=ro)"
    assert "mode=ro" in calls[0]["target"], "connection was not opened read-only"


def test_state_db_has_session_opens_read_only(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    _point_models_at(monkeypatch, db)
    calls = _record_connects(monkeypatch)

    assert models.state_db_has_session("sess-1") is True
    assert models.state_db_has_session("nope") is False
    _assert_read_only(calls)


def test_agent_session_rows_existing_opens_read_only(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    _point_models_at(monkeypatch, db)
    calls = _record_connects(monkeypatch)

    result = models.agent_session_rows_existing(["sess-1", "ghost"])
    assert result == frozenset({"sess-1"})
    _assert_read_only(calls)


def test_agent_session_zero_message_sids_opens_read_only(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    _point_models_at(monkeypatch, db)
    calls = _record_connects(monkeypatch)

    # sess-1 has 2 messages → not zero-message.
    result = models.agent_session_zero_message_sids(["sess-1"])
    assert "sess-1" not in result
    _assert_read_only(calls)


def test_sidebar_overrides_opens_read_only(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    calls = _record_connects(monkeypatch)

    overrides = models._read_state_db_sidebar_overrides(db, {"sess-1"})
    assert isinstance(overrides, dict)
    _assert_read_only(calls)


def test_get_state_db_session_messages_opens_read_only(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    _point_models_at(monkeypatch, db)
    calls = _record_connects(monkeypatch)

    msgs = models.get_state_db_session_messages("sess-1")
    assert [m.get("role") for m in msgs] == ["user", "assistant"]
    _assert_read_only(calls)


def test_get_state_db_message_keys_before_timestamp_opens_read_only(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    _point_models_at(monkeypatch, db)
    calls = _record_connects(monkeypatch)

    keys = models.get_state_db_session_message_keys_before_timestamp("sess-1", 1000.5)
    assert keys is not None
    _assert_read_only(calls)


def test_get_state_db_session_summary_opens_read_only(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    _point_models_at(monkeypatch, db)
    calls = _record_connects(monkeypatch)

    summary = models.get_state_db_session_summary("sess-1")
    assert summary["message_count"] == 2
    _assert_read_only(calls)


def test_count_conversation_rounds_opens_read_only(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    # count_conversation_rounds resolves its DB via get_active_hermes_home()
    # (falling back to $HERMES_HOME only on error), so patch that.
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = _record_connects(monkeypatch)

    rounds = models.count_conversation_rounds("sess-1")
    assert rounds == 1  # one user + one assistant = one round
    _assert_read_only(calls)


def test_read_only_handle_rejects_writes(tmp_path):
    """Why the delete path must stay writable: the read-only handle these reads
    now use rejects any write."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    with closing(open_state_db_readonly(db)) as conn:
        try:
            conn.execute("DELETE FROM sessions WHERE id = 'sess-1'")
            raise AssertionError("read-only handle unexpectedly allowed a write")
        except sqlite3.OperationalError:
            pass


def test_delete_cli_session_still_opens_writable_connection():
    """The lone write path (a DELETE) must keep a writable connection — a
    read-only downgrade would make the delete silently no-op."""
    import inspect

    src = inspect.getsource(models.delete_cli_session)
    assert "sqlite3.connect(str(db_path))" in src, (
        "delete_cli_session must keep its writable connection"
    )
    assert "open_state_db_readonly" not in src, (
        "delete_cli_session must not be downgraded to the read-only helper"
    )
