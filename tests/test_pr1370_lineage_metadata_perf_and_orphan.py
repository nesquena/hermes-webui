"""Regression tests for the v0.50.251 pre-release fixes to PR #1370.

PR #1370 originally did `SELECT id, parent_session_id, end_reason FROM
sessions` (full table scan) on every call. At ~1000 rows the indexed
lookup was 50x faster; the pre-release fix replaces the full scan with
a parameterized `WHERE id IN (...)` query that hits PRIMARY KEY +
idx_sessions_parent.

Also pins: orphan-parent references (parent_session_id points to a
row that doesn't exist in state.db) must NOT be exposed in the API
output, because #1358's frontend `_sessionLineageKey` falls through to
`parent_session_id` when `_lineage_root_id` is missing — and would
group orphans by a key no other session shares (cosmetic dead state).
"""

import os
import sqlite3
import time

import pytest


def _make_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        source TEXT,
        title TEXT,
        model TEXT,
        started_at REAL NOT NULL,
        message_count INTEGER DEFAULT 0,
        parent_session_id TEXT,
        ended_at REAL,
        end_reason TEXT
    );
    CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
    """)
    return conn


def _insert(conn, sid, *, parent=None, end_reason=None, started_at=None):
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, parent_session_id, end_reason) "
        "VALUES (?, 'webui', ?, 'openai/gpt-5', ?, ?, ?)",
        (sid, sid, started_at or time.time(), parent, end_reason),
    )
    conn.commit()


def test_does_not_full_scan_sessions_table(tmp_path, monkeypatch):
    """The perf-critical invariant: must NOT do a `SELECT * FROM sessions`
    or `SELECT id, parent_session_id, end_reason FROM sessions` without a
    WHERE clause. Full scans regress sidebar refresh latency on power users
    with 1000+ session rows.
    """
    from api import agent_sessions

    db = tmp_path / "state.db"
    conn = _make_db(db)
    # Insert 500 unrelated rows + 5 we care about
    for i in range(500):
        _insert(conn, f"unrelated_{i:04d}")
    _insert(conn, "wanted_1")
    _insert(conn, "wanted_2", parent="wanted_1", end_reason=None)
    conn.close()

    # Track every SQL the function issues
    queries = []
    real_connect = sqlite3.connect

    class _TrackingConn:
        def __init__(self, *args, **kw):
            self._real = real_connect(*args, **kw)
        def cursor(self):
            return _TrackingCursor(self._real.cursor())
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return self._real.__exit__(*a)
        @property
        def row_factory(self):
            return self._real.row_factory
        @row_factory.setter
        def row_factory(self, v):
            self._real.row_factory = v

    class _TrackingCursor:
        def __init__(self, real):
            self._real = real
        def execute(self, sql, *args):
            queries.append(sql)
            return self._real.execute(sql, *args)
        def fetchall(self):
            return self._real.fetchall()
        def fetchone(self):
            return self._real.fetchone()

    monkeypatch.setattr(sqlite3, "connect", _TrackingConn)
    agent_sessions.read_session_lineage_metadata(db, ["wanted_1", "wanted_2"])

    # No query may select from sessions without a WHERE clause
    bad = [q for q in queries
           if "from sessions" in q.lower()
           and "where" not in q.lower()
           and "pragma" not in q.lower()]
    assert not bad, (
        f"read_session_lineage_metadata must scope its SELECTs to specific "
        f"session ids (PR #1370 originally did a full scan). Found unscoped "
        f"queries: {bad}"
    )


def test_orphan_parent_reference_not_exposed_in_metadata(tmp_path):
    """If a session row references a parent that doesn't exist in state.db
    (orphan), the API output must NOT include `parent_session_id` — because
    #1358's frontend lineage helper would treat it as a sidebar grouping key
    and cluster the orphan into a never-collapsing single-row group.
    """
    from api.agent_sessions import read_session_lineage_metadata

    db = tmp_path / "state.db"
    conn = _make_db(db)
    _insert(conn, "child", parent="missing_parent", end_reason=None)
    conn.close()

    result = read_session_lineage_metadata(db, ["child"])
    assert "child" not in result or "parent_session_id" not in result["child"], (
        f"Orphan parent_session_id leaked into API output: {result}. "
        f"This causes the frontend lineage helper to group the orphan under "
        f"a key no other session shares — cosmetic dead state on the wire."
    )


def test_old_schema_without_source_or_messages_table_keeps_lineage(tmp_path):
    """Old state.db schemas may lack optional source/message tables but still carry lineage."""
    from api.agent_sessions import read_session_lineage_metadata

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        title TEXT,
        started_at REAL NOT NULL,
        parent_session_id TEXT,
        ended_at REAL,
        end_reason TEXT
    );
    CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
    """)
    t0 = time.time() - 100
    conn.execute(
        "INSERT INTO sessions (id, title, started_at, ended_at, end_reason) VALUES (?, ?, ?, ?, ?)",
        ("old_root", "old_root", t0, t0 + 5, "compression"),
    )
    conn.execute(
        "INSERT INTO sessions (id, title, started_at, parent_session_id) VALUES (?, ?, ?, ?)",
        ("old_tip", "old_tip", t0 + 6, "old_root"),
    )
    conn.commit()
    conn.close()

    result = read_session_lineage_metadata(db, ["old_tip"])

    assert result["old_tip"]["parent_session_id"] == "old_root"
    assert result["old_tip"]["_lineage_root_id"] == "old_root"
    assert result["old_tip"]["_compression_segment_count"] == 2


def test_cycle_in_parent_chain_terminates(tmp_path):
    """Pathological data with a parent cycle (A→B→A) must not infinite-loop."""
    from api.agent_sessions import read_session_lineage_metadata

    db = tmp_path / "state.db"
    conn = _make_db(db)
    _insert(conn, "A", parent="B", end_reason="compression")
    _insert(conn, "B", parent="A", end_reason="compression")
    conn.close()

    import threading
    done = threading.Event()
    result_box = []

    def worker():
        result_box.append(read_session_lineage_metadata(db, ["A"]))
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    finished = done.wait(timeout=2.0)
    assert finished, "read_session_lineage_metadata hung on a parent cycle"

    result = result_box[0]
    # Cycle should terminate; segment count should be bounded (≤ 2 for A→B→A)
    seg = result.get("A", {}).get("_compression_segment_count", 0)
    assert 1 <= seg <= 5, f"Cycle should produce a small bounded count, got {seg}"


def test_chain_walk_returns_correct_root_for_real_compression_chain(tmp_path):
    """End-to-end behavioural test: 4-segment compression chain returns the
    correct root and a segment count of 4."""
    from api.agent_sessions import read_session_lineage_metadata

    db = tmp_path / "state.db"
    conn = _make_db(db)
    _insert(conn, "root", end_reason="compression")
    _insert(conn, "seg2", parent="root", end_reason="compression")
    _insert(conn, "seg3", parent="seg2", end_reason="compression")
    _insert(conn, "tip", parent="seg3", end_reason=None)
    conn.close()

    result = read_session_lineage_metadata(db, ["tip"])
    assert result["tip"]["_lineage_root_id"] == "root"
    assert result["tip"]["_compression_segment_count"] == 4
    assert result["tip"]["parent_session_id"] == "seg3"


def test_in_clause_chunked_for_large_session_set(tmp_path, monkeypatch):
    """The first hop must chunk the IN clause into batches of <= 500.
    Without chunking, a power user with 2000+ sessions in the sidebar would
    trigger SQLITE_MAX_VARIABLE_NUMBER on Python 3.9's sqlite 3.31 (default
    limit 999), the OperationalError gets swallowed by the except wrapper,
    and lineage collapse silently disables forever for that user.
    (Opus pre-release review of v0.50.251, SHOULD-FIX 2.)
    """
    from api import agent_sessions

    db = tmp_path / "state.db"
    conn = _make_db(db)
    # 1500 unrelated rows
    for i in range(1500):
        _insert(conn, f"session_{i:04d}")
    conn.close()

    # Track each query's IN clause variable count
    in_counts = []
    real_connect = sqlite3.connect

    class _TrackingConn:
        def __init__(self, *args, **kw):
            self._real = real_connect(*args, **kw)
        def cursor(self):
            return _TrackingCursor(self._real.cursor())
        def __enter__(self): return self
        def __exit__(self, *a): return self._real.__exit__(*a)
        @property
        def row_factory(self): return self._real.row_factory
        @row_factory.setter
        def row_factory(self, v): self._real.row_factory = v

    class _TrackingCursor:
        def __init__(self, real): self._real = real
        def execute(self, sql, *args):
            if "IN (" in sql:
                in_counts.append(sql.count("?"))
            return self._real.execute(sql, *args)
        def fetchall(self): return self._real.fetchall()
        def fetchone(self): return self._real.fetchone()

    monkeypatch.setattr(sqlite3, "connect", _TrackingConn)

    # Request 1500 ids (the full set we inserted)
    wanted = [f"session_{i:04d}" for i in range(1500)]
    agent_sessions.read_session_lineage_metadata(db, wanted)

    assert in_counts, "Expected at least one IN-clause query"
    over_limit = [n for n in in_counts if n > 999]
    assert not over_limit, (
        f"IN clause must be chunked to <= 999 vars to stay under SQLite default "
        f"limit (chunked to 500 in the implementation). Found queries with "
        f"{over_limit} variables — would raise OperationalError on older sqlite."
    )


def test_two_children_sharing_non_continuation_parent_not_collapsed(tmp_path):
    """Two distinct child sessions may share the same non-continuation parent,
    but they must be marked as child sessions so frontend lineage collapse does
    not group them under the parent's id.
    """
    from api.agent_sessions import read_session_lineage_metadata

    db = tmp_path / "state.db"
    conn = _make_db(db)
    _insert(conn, "shared_parent", end_reason="user_stop")
    _insert(conn, "child_a", parent="shared_parent", end_reason=None)
    _insert(conn, "child_b", parent="shared_parent", end_reason=None)
    conn.close()

    result = read_session_lineage_metadata(db, ["child_a", "child_b"])
    for sid in ["child_a", "child_b"]:
        entry = result.get(sid, {})
        assert entry.get("parent_session_id") == "shared_parent"
        assert entry.get("relationship_type") == "child_session"
        assert entry.get("_parent_lineage_root_id") == "shared_parent"
        assert "_lineage_root_id" not in entry


def test_non_compression_parent_does_not_extend_lineage(tmp_path):
    """If parent's end_reason is something OTHER than 'compression' or
    'cli_close' (e.g. 'user_stop', 'session_reset', 'cron_complete'),
    the chain stops at that boundary. The parent link is surfaced as an
    explicit child-session relationship rather than compression lineage.
    """
    from api.agent_sessions import read_session_lineage_metadata

    db = tmp_path / "state.db"
    conn = _make_db(db)
    _insert(conn, "parent", end_reason="user_stop")  # NOT compression
    _insert(conn, "child", parent="parent", end_reason=None)
    conn.close()

    result = read_session_lineage_metadata(db, ["child"])
    entry = result.get("child", {})
    assert entry.get("parent_session_id") == "parent"
    assert entry.get("relationship_type") == "child_session"
    assert entry.get("_parent_lineage_root_id") == "parent"
    # _lineage_root_id should NOT be set — chain doesn't span the boundary
    assert "_lineage_root_id" not in entry
    assert "_compression_segment_count" not in entry



# ── #3751 backward-compat: messages-table schema variants must not collapse
# the lineage metadata (regression for the gate finding on stage-a2/v0.51.306) ──

def _make_db_with_messages(path, *, timestamp_type):
    """Build a state.db with a compression lineage and a messages table whose
    timestamp column is REAL, absent, or TEXT (ISO-8601)."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        source TEXT,
        title TEXT,
        model TEXT,
        started_at REAL NOT NULL,
        message_count INTEGER DEFAULT 0,
        parent_session_id TEXT,
        ended_at REAL,
        end_reason TEXT
    );
    CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
    """)
    if timestamp_type == "absent":
        conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT)")
    else:
        conn.execute(f"CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, timestamp {timestamp_type})")
    # Compression chain: root --(compression)--> tip
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, parent_session_id, ended_at, end_reason) "
        "VALUES ('root', 'webui', 'root', 'openai/gpt-5', ?, NULL, ?, 'compression')",
        (now - 100, now - 50),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, parent_session_id, ended_at, end_reason) "
        "VALUES ('tip', 'webui', 'tip', 'openai/gpt-5', ?, 'root', NULL, NULL)",
        (now - 40,),
    )
    if timestamp_type == "absent":
        conn.execute("INSERT INTO messages (session_id, role, content) VALUES ('tip', 'user', 'hi')")
    elif timestamp_type == "TEXT":
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('tip', 'user', 'hi', '2026-06-06T12:00:00Z')"
        )
    else:  # REAL
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('tip', 'user', 'hi', ?)",
            (now - 35,),
        )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("timestamp_type", ["REAL", "absent", "TEXT"])
def test_lineage_metadata_survives_messages_timestamp_schema_variants(tmp_path, timestamp_type):
    """The branchy-lineage tip resolver pulls per-session message stats from the
    messages table. Older/minimal state.db schemas can have a messages table with
    NO timestamp column, or a non-numeric (ISO-8601 text) timestamp. Neither may
    raise out of the DB block and collapse ALL lineage metadata to {} — which is
    a silent regression vs. the prior behavior that returned metadata.
    """
    from api.agent_sessions import read_session_lineage_metadata

    db = tmp_path / "state.db"
    _make_db_with_messages(db, timestamp_type=timestamp_type)

    result = read_session_lineage_metadata(db, ["tip"])
    entry = result.get("tip", {})
    # The compression lineage must still be reported (not silently dropped).
    assert entry.get("_lineage_root_id") == "root", (
        f"lineage metadata collapsed for messages.timestamp={timestamp_type}: {result!r}"
    )
    # And the canonical tip must resolve to the messageful continuation.
    assert entry.get("_lineage_tip_id") == "tip"


def test_importable_rows_survive_text_timestamp_in_messages(tmp_path):
    """read_importable_agent_session_rows() builds a per-session MAX(timestamp)
    and the compression_tip() DFS scores tips by last_activity. An older/
    non-standard messages.timestamp stored as ISO-8601 TEXT must not raise a
    TypeError out of the projection (get_cli_sessions() would swallow it and
    return [] — silently hiding ALL imported agent rows). Sibling-path guard to
    the read_session_lineage_metadata fix.
    """
    from api.agent_sessions import read_importable_agent_session_rows

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        source TEXT,
        title TEXT,
        model TEXT,
        started_at REAL NOT NULL,
        message_count INTEGER DEFAULT 0,
        parent_session_id TEXT,
        ended_at REAL,
        end_reason TEXT
    );
    CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
    CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp TEXT);
    """)
    now = time.time()
    # compression chain root --(compression)--> tip, tip has a messageful row
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason) "
        "VALUES ('root', 'cli', 'root', 'openai/gpt-5', ?, 0, NULL, ?, 'compression')",
        (now - 100, now - 50),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason) "
        "VALUES ('tip', 'cli', 'tip', 'openai/gpt-5', ?, 1, 'root', NULL, NULL)",
        (now - 40,),
    )
    # ISO-8601 TEXT timestamp — the value MAX() would return as a string
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('tip', 'user', 'hi', '2026-06-06T12:00:00Z')"
    )
    conn.commit()
    conn.close()

    # Must NOT raise; must surface the imported chain (cli source).
    rows = read_importable_agent_session_rows(db, limit=None, exclude_sources=None)
    ids = {r["id"] for r in rows}
    assert ids, "import projection returned no rows on a TEXT messages.timestamp (would hide all CLI sessions)"
    # the chain collapses to its tip
    assert "tip" in ids
