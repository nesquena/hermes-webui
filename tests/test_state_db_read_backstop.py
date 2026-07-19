"""Tests: ``get_state_db_session_messages`` defensive ``limit`` backstop.

The display path (``GET /api/session``) reads state.db rows that feed a
reconciliation merge then a visible-row window. The existing
``since_timestamp`` optimization bounds the common uncompressed tail load but
deliberately bails on compressed sessions (``truncation_watermark`` /
``truncation_boundary``) and ``msg_before`` paging — in those cases the read
used to full-scan with no SQL ``LIMIT``. A pathological/huge state.db could
then materialize unbounded rows into memory.

``get_state_db_session_messages`` now accepts an opt-in ``limit`` (applied as a
SQL ``LIMIT`` that keeps the NEWEST rows). It is a defensive backstop, NOT a
semantic window — full-history model-context callers leave it unset. These
tests verify the newest-row retention and that the default (no limit) is
unchanged.
"""
import sqlite3

import api.models as models
from api.models import get_state_db_session_messages


def _make_state_db(path, n_rows=100):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY)"
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT,
            content TEXT,
            timestamp REAL,
            active INTEGER DEFAULT 1
        )
        """
    )
    conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        [("s1", "user" if i % 2 == 0 else "assistant", f"msg{i}", float(i)) for i in range(n_rows)],
    )
    conn.commit()
    conn.close()


def test_no_limit_returns_full_history_oldest_first(tmp_path, monkeypatch):
    """Default (no limit) — the model-context caller contract — returns every
    row, oldest-first. Unchanged by this fix."""
    db = tmp_path / "state.db"
    _make_state_db(db, n_rows=100)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    msgs = get_state_db_session_messages("s1")
    assert len(msgs) == 100
    assert msgs[0]["content"] == "msg0"
    assert msgs[-1]["content"] == "msg99"


def test_limit_keeps_newest_rows_resorted_oldest_first(tmp_path, monkeypatch):
    """limit=N keeps the NEWEST N rows (the query orders ASC for oldest-first,
    so the cap takes a DESC subquery then re-sorts), so a tail window is
    retained rather than the oldest rows."""
    db = tmp_path / "state.db"
    _make_state_db(db, n_rows=100)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    msgs = get_state_db_session_messages("s1", limit=10)
    assert len(msgs) == 10
    # Newest 10 retained (msg90..msg99), re-sorted oldest-first.
    assert [m["content"] for m in msgs] == [f"msg{i}" for i in range(90, 100)]


def test_limit_above_row_count_returns_all(tmp_path, monkeypatch):
    """A limit larger than the row count returns everything (no truncation)."""
    db = tmp_path / "state.db"
    _make_state_db(db, n_rows=50)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    msgs = get_state_db_session_messages("s1", limit=10000)
    assert len(msgs) == 50


def test_limit_zero_or_negative_clamps_to_one_newest(tmp_path, monkeypatch):
    """A non-positive limit clamps to 1 and returns the single newest row."""
    db = tmp_path / "state.db"
    _make_state_db(db, n_rows=20)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    for bad in (0, -5):
        msgs = get_state_db_session_messages("s1", limit=bad)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "msg19"  # newest


def test_limit_invalid_falls_back_to_unbounded(tmp_path, monkeypatch):
    """A non-numeric limit is ignored (full read) rather than raising."""
    db = tmp_path / "state.db"
    _make_state_db(db, n_rows=30)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    msgs = get_state_db_session_messages("s1", limit="not-a-number")
    assert len(msgs) == 30


def test_limit_composes_with_since_timestamp(tmp_path, monkeypatch):
    """limit and since_timestamp compose: the floor filters rows, then the cap
    keeps the newest of the remainder."""
    db = tmp_path / "state.db"
    _make_state_db(db, n_rows=100)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    # since_timestamp=50.0 → rows with timestamp >= 50 (msg50..msg99 = 50 rows),
    # then limit=10 keeps the newest 10 (msg90..msg99).
    msgs = get_state_db_session_messages("s1", since_timestamp=50.0, limit=10)
    assert len(msgs) == 10
    assert [m["content"] for m in msgs] == [f"msg{i}" for i in range(90, 100)]
