"""Regression tests for #5455 — the session-listing projection reads read-only.

``read_importable_agent_session_rows()`` is a pure read, but it used to open a
read-WRITE ``sqlite3`` connection on the live (multi-GB, WAL) ``state.db`` and
re-run a defensive ``CREATE INDEX`` self-heal on every sidebar build. Holding a
write-capable handle while the agent streams into the same DB adds needless
checkpoint/lock surface.

The listing path opens the DB read-only (``file:...?mode=ro``) and never
upgrades to a writable handle: a read-only open failure propagates, and a
missing ``idx_messages_session`` degrades to the pre-aggregated path instead
of being self-healed. Index maintenance is an explicit drained operation
(``scripts/ensure_state_db_read_indexes.py``).
"""
import sqlite3

import api.agent_sessions as agent_sessions
from api.agent_sessions import read_importable_agent_session_rows


def _make_db(path, *, with_index=True):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER,
            started_at REAL, source TEXT, session_source TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, title, model, message_count, started_at, source, session_source) "
        "VALUES (?,?,?,?,?,?,?)",
        ("cli-1", "Hello", "gpt", 2, 1000.0, "cli", "cli"),
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL)"
    )
    conn.executemany(
        "INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)",
        [("cli-1", "user", 1001.0), ("cli-1", "assistant", 1002.0)],
    )
    if with_index:
        conn.execute("CREATE INDEX idx_messages_session ON messages(session_id, timestamp)")
    conn.commit()
    conn.close()


def _record_connects(monkeypatch):
    """Wrap agent_sessions.sqlite3.connect and record how each conn was opened."""
    real_connect = sqlite3.connect
    calls = []

    def spy(target, *args, **kwargs):
        calls.append({"target": str(target), "uri": bool(kwargs.get("uri"))})
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", spy)
    return calls


def test_listing_opens_read_only_and_returns_rows(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_db(db, with_index=True)
    calls = _record_connects(monkeypatch)

    out = read_importable_agent_session_rows(db, exclude_sources=None)

    assert "cli-1" in {r["id"] for r in out}
    # The read path is opened read-only via a file: URI.
    assert calls, "expected at least one sqlite connection"
    assert calls[0]["uri"] is True
    assert "mode=ro" in calls[0]["target"]


def test_listing_read_only_uri_encodes_special_path_chars(tmp_path, monkeypatch):
    db_dir = tmp_path / "state dir #1"
    db_dir.mkdir()
    db = db_dir / "state?.db"
    _make_db(db, with_index=True)
    calls = _record_connects(monkeypatch)

    out = read_importable_agent_session_rows(db, exclude_sources=None)

    assert "cli-1" in {r["id"] for r in out}
    assert calls[0]["uri"] is True
    assert calls[0]["target"].startswith("file://")
    assert "%20" in calls[0]["target"]
    assert "%23" in calls[0]["target"]
    assert "%3F" in calls[0]["target"]
    assert calls[0]["target"].endswith("?mode=ro")


def test_read_only_open_failure_never_retries_with_writable_connection(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_db(db, with_index=True)
    calls = []

    def fail_read_only(target, *args, **kwargs):
        calls.append({"target": str(target), "uri": bool(kwargs.get("uri"))})
        raise sqlite3.OperationalError("synthetic read-only URI failure")

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", fail_read_only)

    try:
        read_importable_agent_session_rows(db, exclude_sources=None)
        raise AssertionError("read-only open failure unexpectedly recovered")
    except sqlite3.OperationalError as exc:
        assert "synthetic read-only URI failure" in str(exc)

    # Exactly one attempt, and it was the read-only URI form.
    assert len(calls) == 1
    assert calls[0]["uri"] is True
    assert "mode=ro" in calls[0]["target"]


def test_index_present_performs_no_writable_connection(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_db(db, with_index=True)
    calls = _record_connects(monkeypatch)

    read_importable_agent_session_rows(db, exclude_sources=None)

    # With the index already present, no self-heal write connection is opened:
    # every connection is the read-only URI form.
    assert all(c["uri"] and "mode=ro" in c["target"] for c in calls), calls


def test_missing_index_degrades_without_writable_connection(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_db(db, with_index=False)
    calls = _record_connects(monkeypatch)

    out = read_importable_agent_session_rows(db, exclude_sources=None)

    # Rows still come back...
    assert "cli-1" in {r["id"] for r in out}
    # ...without any implicit schema-maintenance writer.
    assert calls
    assert all(c["uri"] and "mode=ro" in c["target"] for c in calls), calls
    # A missing index stays a maintenance concern, not a listing side effect.
    verify = sqlite3.connect(str(db))
    try:
        names = {row[1] for row in verify.execute("PRAGMA index_list(messages)")}
    finally:
        verify.close()
    assert "idx_messages_session" not in names
