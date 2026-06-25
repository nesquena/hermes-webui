"""Regression: a freshly-committed state.db session must invalidate the CLI-session
cache immediately, even when the file-stat stamp would collide (the root cause of
the recurring test_gateway_sync flake).

The CLI-session cache (_CLI_SESSIONS_CACHE) and the session-list cache were keyed
on (st_mtime_ns, st_size) of state.db + its WAL sidecars. Under WAL-mode writes
those stamps can collide, serving a stale cache. The fix adds a commit-reliable
content fingerprint (_sqlite_content_fingerprint) that advances on every commit.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path


def test_content_fingerprint_advances_on_commit():
    """The cache key's content fingerprint must change after any commit, even
    when mtime/size would not reliably change (the WAL-collision flake source).
    """
    from api.models import _sqlite_file_stat_cache_key, _sqlite_content_fingerprint

    d = tempfile.mkdtemp()
    p = Path(d) / "state.db"
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE sessions(id TEXT, source TEXT, started_at REAL)")
    conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT)")
    conn.commit()

    fp_before = _sqlite_content_fingerprint(p)
    key_before = _sqlite_file_stat_cache_key(p)

    conn.execute("INSERT INTO sessions VALUES ('gw_new_001', 'weixin', 1)")
    conn.execute("INSERT INTO messages (session_id) VALUES ('gw_new_001')")
    conn.commit()

    fp_after = _sqlite_content_fingerprint(p)
    key_after = _sqlite_file_stat_cache_key(p)
    conn.close()

    assert fp_before != fp_after, (
        "content fingerprint must advance after a commit so a freshly-inserted "
        "CLI/gateway session is never served from a stale cache"
    )
    # The full cache key (which embeds the fingerprint) must therefore differ too.
    assert key_before != key_after


def test_content_fingerprint_detects_message_only_change():
    """An in-place session row update or message-only insert must also change the
    fingerprint (sessions COUNT/MAX alone could miss a same-rowid REPLACE).
    """
    from api.models import _sqlite_content_fingerprint

    d = tempfile.mkdtemp()
    p = Path(d) / "state.db"
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE sessions(id TEXT, source TEXT, started_at REAL)")
    conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 'weixin', 1)")
    conn.commit()

    fp_before = _sqlite_content_fingerprint(p)
    # Add a message to the existing session (sessions table unchanged).
    conn.execute("INSERT INTO messages (session_id) VALUES ('s1')")
    conn.commit()
    fp_after = _sqlite_content_fingerprint(p)
    conn.close()

    assert fp_before != fp_after, (
        "fingerprint must cover the messages table so a message-only commit "
        "(e.g. a continued gateway conversation) invalidates the cache"
    )


def test_content_fingerprint_safe_on_missing_or_empty_db():
    """The fingerprint must not raise on a missing path or a db without the
    expected tables — it returns None / zeroed parts so the stat fallback applies.
    """
    from api.models import _sqlite_content_fingerprint

    assert _sqlite_content_fingerprint(Path("/nonexistent/state.db")) is None

    d = tempfile.mkdtemp()
    p = Path(d) / "empty.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE unrelated(x)")
    conn.commit()
    conn.close()
    # No sessions/messages tables → None parts (MAX(rowid) on a missing table),
    # no exception. Shape is a 2-tuple of (sessions_max_rowid, messages_max_rowid).
    fp = _sqlite_content_fingerprint(p)
    assert fp == (None, None)
