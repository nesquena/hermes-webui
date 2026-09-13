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
    assert "candidate_limit = max(result_limit * multiplier, result_limit)" in src
    # The first (and normally only) candidate window is still 8x the requested
    # limit. The wider retry in CANDIDATE_WINDOW_MULTIPLIERS only runs when that
    # window was fully consumed AND still under-delivered conversations (#6659),
    # so the capped-scan guarantee this test pins is unchanged.
    assert agent_sessions.CANDIDATE_WINDOW_MULTIPLIERS[0] == 8


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


def _lineage_and_subagent_db(path):
    """Two 12-segment compression lineages own the 24 newest raw rows.

    They collapse to 2 logical conversations, so a ``limit=3`` request consumes
    the whole ``limit * 8`` window and still under-delivers. A hot subagent leaf
    and its quiet subagent parent sit BELOW that window, reachable only once
    ``CANDIDATE_WINDOW_MULTIPLIERS`` re-widens it.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
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
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        """
    )

    def add(sid, title, started, source, parent=None, ended_at=None, end_reason=None):
        conn.execute(
            "INSERT INTO sessions (id, source, title, model, started_at, message_count, "
            "parent_session_id, ended_at, end_reason) VALUES (?, ?, ?, 'openai/gpt-5', ?, 2, ?, ?, ?)",
            (sid, source, title, started, parent, ended_at, end_reason),
        )
        for index, role in enumerate(("user", "assistant")):
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, 'hi', ?)",
                (f"{sid}_m{index}", sid, role, started + (index + 4) / 10),
            )

    for name, base in (("a", 9000.0), ("b", 8000.0)):
        for index in range(12):
            last = index == 11
            started = base + index
            add(
                f"lin{name}_seg{index}",
                f"Lineage {name} part {index}",
                started,
                "cli",
                parent=None if index == 0 else f"lin{name}_seg{index - 1}",
                ended_at=None if last else started + 0.9,
                end_reason=None if last else "compression",
            )
    add("hot_leaf", "Hot leaf", 7000.0, "subagent", parent="quiet_orch")
    add("quiet_orch", "Quiet orchestrator", 100.0, "subagent")
    conn.commit()
    conn.close()


def test_rewidened_candidate_window_still_recovers_subagent_parents(tmp_path):
    """The parent-recovery walk must run on the window that ACTUALLY executed.

    ``read_importable_agent_session_rows`` re-adds subagent parents that the
    oversampled candidate set already projected, so a frozen orchestrator is not
    evicted while its streaming leaves stay (upstream #7089 / supersedes #7031).
    That walk reads the projection of the candidate window, and #6659 turned the
    single window into a re-widening loop. This pins that the walk consumes the
    LAST window rather than a stale first pass: on the 8x window neither the leaf
    nor its parent is even fetched, and only the widened window can anchor them.
    """
    db = tmp_path / "state.db"
    _lineage_and_subagent_db(db)

    rows = agent_sessions.read_importable_agent_session_rows(db, limit=3, exclude_sources=None)
    ids = [row["id"] for row in rows]

    # The 8x window (24 raw rows) is exactly the two lineages: under-delivered.
    assert ids[:2] == ["lina_seg11", "linb_seg11"]
    # The widened window reaches the leaf...
    assert "hot_leaf" in ids
    # ...and the walk re-adds its quiet parent even though the recency slice
    # (limit=3) had already dropped it, so the leaf still renders as a child.
    assert "quiet_orch" in ids
    assert next(row for row in rows if row["id"] == "hot_leaf")["parent_session_id"] == "quiet_orch"
    # Contract from the docstring: the anchor pushes the result past ``limit``.
    assert len(rows) == 4


def test_first_candidate_window_alone_cannot_anchor_the_subagent_parent(tmp_path):
    """Negative control: without the re-widening pass the same db under-delivers.

    Pins that the previous test is not vacuous — the 8x window really does stop
    at the two lineages, so the parent recovery above is attributable to the
    widened window and not to the first one.
    """
    db = tmp_path / "state.db"
    _lineage_and_subagent_db(db)

    single_window = agent_sessions.CANDIDATE_WINDOW_MULTIPLIERS[:1]
    original = agent_sessions.CANDIDATE_WINDOW_MULTIPLIERS
    agent_sessions.CANDIDATE_WINDOW_MULTIPLIERS = single_window
    try:
        rows = agent_sessions.read_importable_agent_session_rows(db, limit=3, exclude_sources=None)
    finally:
        agent_sessions.CANDIDATE_WINDOW_MULTIPLIERS = original

    ids = [row["id"] for row in rows]
    assert ids == ["lina_seg11", "linb_seg11"]
    assert "hot_leaf" not in ids
    assert "quiet_orch" not in ids
