import sqlite3

import pytest

from api._sqlite import DEFAULT_BUSY_TIMEOUT_MS, connect_state_db_ro, connect_state_db_rw


def test_state_db_ro_connection_sets_busy_timeout_and_blocks_writes(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO t(name) VALUES ('ok')")

    conn = connect_state_db_ro(db)
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == DEFAULT_BUSY_TIMEOUT_MS
        assert conn.execute("SELECT name FROM t").fetchone()["name"] == "ok"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t(name) VALUES ('blocked')")
    finally:
        conn.close()


def test_state_db_rw_connection_sets_busy_timeout(tmp_path):
    db = tmp_path / "state.db"
    conn = connect_state_db_rw(db)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == DEFAULT_BUSY_TIMEOUT_MS
    finally:
        conn.close()
