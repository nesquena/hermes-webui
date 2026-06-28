"""Regression coverage for capped CLI/agent session sidebar scans (#2628)."""

import pathlib
import sqlite3
import time

import pytest

import api.agent_sessions as agent_sessions

_REAL_SQLITE_CONNECT = sqlite3.connect
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _make_state_db(path, *, sessions=80, messages_per_session=3, create_messages_index=True, source="cli", session_source="cli"):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE INDEX idx_sessions_started ON sessions(started_at);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        """
    )
    base = time.time() - sessions
    for i in range(sessions):
        sid = f"cli_perf_{i:04d}"
        started = base + i
        conn.execute(
            """
            INSERT INTO sessions
            (id, source, session_source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason)
            VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, ?, NULL, NULL, NULL)
            """,
            (sid, source, session_source, sid, started, messages_per_session),
        )
        for j in range(messages_per_session):
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, 'hello', ?)",
                (f"msg_{i:04d}_{j:02d}", sid, "user" if j == 0 else "assistant", started + j / 10),
            )
    if create_messages_index:
        conn.execute("CREATE INDEX idx_messages_session ON messages(session_id, timestamp)")
    conn.commit()
    conn.close()


def _newest_first_reference_ids(db_path, *, include_sources=None, exclude_sources=("webui",)):
    where_clauses = ["s.source IS NOT NULL"]
    params = []
    if include_sources:
        placeholders = ", ".join("?" for _ in include_sources)
        where_clauses.append(f"s.source IN ({placeholders})")
        params.extend(include_sources)
    if exclude_sources:
        placeholders = ", ".join("?" for _ in exclude_sources)
        where_clauses.append(f"s.source NOT IN ({placeholders})")
        params.extend(exclude_sources)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT s.id
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE {' AND '.join(where_clauses)}
            GROUP BY s.id
            ORDER BY COALESCE(MAX(m.timestamp), s.started_at) DESC
            """,
            params,
        ).fetchall()
        return [row["id"] for row in rows]
    finally:
        conn.close()


def _execute_candidate_ordering_baseline_sql(
    db_path,
    candidate_limit,
    *,
    budget_ops,
    interval=1,
    include_sources=None,
    exclude_sources=("webui",),
):
    where_clauses = ["s.source IS NOT NULL"]
    params = []
    if include_sources:
        placeholders = ", ".join("?" for _ in include_sources)
        where_clauses.append(f"s.source IN ({placeholders})")
        params.extend(include_sources)
    if exclude_sources:
        placeholders = ", ".join("?" for _ in exclude_sources)
        where_clauses.append(f"s.source NOT IN ({placeholders})")
        params.extend(exclude_sources)

    def _on_progress():
        nonlocal steps
        steps += 1
        return 1 if steps > budget_ops else 0

    steps = 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    conn.set_progress_handler(_on_progress, interval)
    try:
        return conn.execute(
            f"""
            WITH candidates AS (
                SELECT s.id
                FROM sessions s
                WHERE {' AND '.join(where_clauses)}
                ORDER BY COALESCE(
                    (SELECT MAX(mx.timestamp) FROM messages mx WHERE mx.session_id = s.id),
                    s.started_at
                ) DESC,
                s.started_at DESC
                LIMIT ?
            )
            SELECT s.id
            FROM sessions s
            JOIN candidates c ON c.id = s.id
            LEFT JOIN messages m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY COALESCE(MAX(m.timestamp), s.started_at) DESC
            """,
            [*params, candidate_limit],
        ).fetchall()
    finally:
        conn.close()


def _make_connect_with_progress_budget(*, budget_ops, interval=1):
    def _connect(database, *_, **__):
        steps = {"count": 0}
        database_uri = str(database)
        if database_uri.startswith("file:"):
            target_uri = database_uri
        else:
            target_uri = f"file:{database_uri}?mode=ro&immutable=1"

        def _on_progress():
            steps["count"] += 1
            return 1 if steps["count"] > budget_ops else 0

        conn = _REAL_SQLITE_CONNECT(
            target_uri,
            uri=True,
        )
        conn.set_progress_handler(_on_progress, interval)
        return conn

    return _connect


def _make_connect_with_progress_counter(*, interval=1):
    steps = {"count": 0}

    def _connect(database, *_, **__):
        database_uri = str(database)
        if database_uri.startswith("file:"):
            target_uri = database_uri
        else:
            target_uri = f"file:{database_uri}?mode=ro&immutable=1"

        def _on_progress():
            steps["count"] += 1
            return 0

        conn = _REAL_SQLITE_CONNECT(
            target_uri,
            uri=True,
        )
        conn.set_progress_handler(_on_progress, interval)
        return conn

    return _connect, steps


def test_importable_agent_rows_push_sidebar_limit_into_sql(tmp_path):
    """A capped sidebar scan should not aggregate the entire state.db first."""
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=120, messages_per_session=5)

    rows = agent_sessions.read_importable_agent_session_rows(db, limit=20, exclude_sources=("webui",))

    assert len(rows) == 20
    assert [row["id"] for row in rows][:3] == ["cli_perf_0119", "cli_perf_0118", "cli_perf_0117"]
    assert {row["actual_message_count"] for row in rows} == {5}

    src = (REPO_ROOT / "api" / "agent_sessions.py").read_text()
    assert "WITH candidates AS" in src
    assert "JOIN candidates c ON c.id = s.id" in src
    assert "latest_messages AS" in src
    assert "LEFT JOIN latest_messages lm ON lm.session_id = s.id" in src
    assert 'included == ("cron",)' in src
    assert "not messages_index_present" in src
    assert "PRAGMA index_list(messages)" in src
    assert "CREATE INDEX IF NOT EXISTS idx_messages_session" in src
    assert "_CRON_PREAGGREGATE_CANDIDATE_ORDER_MIN_MESSAGES" not in src
    assert "MAX(mx.timestamp) FROM messages mx WHERE mx.session_id = s.id" in src
    assert "candidate_limit = max(result_limit * 8, result_limit)" in src


def test_importable_agent_rows_candidate_ordering_stays_under_progress_budget(tmp_path, monkeypatch):
    """Cron-only missing-index scans should fail under the old shape budget, then pass after pre-aggregation."""
    db = tmp_path / "state.db"
    _make_state_db(
        db,
        sessions=120,
        messages_per_session=900,
        create_messages_index=False,
        source="cron",
        session_source="cron",
    )
    reference_ids = _newest_first_reference_ids(db, include_sources=("cron",), exclude_sources=None)
    candidate_limit = max(20 * 8, 20)
    progress_interval = 100

    original_connect = agent_sessions.sqlite3.connect
    connect_with_progress_counter, progress_counter = _make_connect_with_progress_counter(
        interval=progress_interval
    )
    monkeypatch.setattr(agent_sessions.sqlite3, "connect", connect_with_progress_counter)
    try:
        measured_rows = agent_sessions.read_importable_agent_session_rows(
            db,
            limit=20,
            exclude_sources=None,
            include_sources=("cron",),
        )
        assert [row["id"] for row in measured_rows] == reference_ids[:20]
    finally:
        # Keep this helper isolated; the baseline must still run without the
        # counting handler to validate raw cost differences.
        monkeypatch.setattr(agent_sessions.sqlite3, "connect", original_connect)

    # Give the head path a small deterministic margin, then require the old
    # correlated query to exceed the same budget on the missing-index branch.
    progress_budget_ops = max(progress_counter["count"] + 200, 1)

    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        _execute_candidate_ordering_baseline_sql(
            db,
            candidate_limit,
            budget_ops=progress_budget_ops,
            interval=progress_interval,
            include_sources=("cron",),
            exclude_sources=None,
        )

    monkeypatch.setattr(
        agent_sessions.sqlite3,
        "connect",
        _make_connect_with_progress_budget(
            budget_ops=progress_budget_ops,
            interval=progress_interval,
        ),
    )

    rows = agent_sessions.read_importable_agent_session_rows(
        db,
        limit=20,
        exclude_sources=None,
        include_sources=("cron",),
    )
    assert [row["id"] for row in rows] == reference_ids[:20]


def test_importable_agent_rows_limit_includes_resumed_old_session(tmp_path):
    """The capped candidate window must not hide old sessions resumed recently."""
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=200, messages_per_session=1)

    old_started = time.time() - 60 * 60 * 24 * 30
    recent_activity = time.time() + 60
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, session_source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason)
        VALUES ('cli_resumed_old', 'cli', 'cli', 'Old resumed session', 'openai/gpt-5', ?, 2, NULL, NULL, NULL)
        """,
        (old_started,),
    )
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES ('old_msg_1', 'cli_resumed_old', 'user', 'old hello', ?)",
        (old_started,),
    )
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES ('old_msg_2', 'cli_resumed_old', 'assistant', 'recent reply', ?)",
        (recent_activity,),
    )
    conn.commit()
    conn.close()

    rows = agent_sessions.read_importable_agent_session_rows(db, limit=20, exclude_sources=("webui",))

    assert rows[0]["id"] == "cli_resumed_old"
    assert rows[0]["actual_message_count"] == 2


def test_importable_agent_rows_zero_limit_skips_query_work(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=5, messages_per_session=1)

    assert agent_sessions.read_importable_agent_session_rows(db, limit=0, exclude_sources=("webui",)) == []
