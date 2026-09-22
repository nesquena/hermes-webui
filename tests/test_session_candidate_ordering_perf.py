"""Slice D — interactive candidate-window ordering: audit (D0) and exact key (D1).

The interactive CLI/agent pass (``read_importable_agent_session_rows`` with
``exclude_sources=("cron", "webhook", "kanban")``) bounds its expensive
messages join to a recency candidate window of ``limit * 8`` rows (the 8x
oversample). The candidate ordering must be the exact
``COALESCE(MAX(mx.timestamp), s.started_at)`` key the display sorts by.
Ordering the window by the denormalized ``COALESCE(s.last_activity_at,
s.started_at)`` column instead (an intermediate form of this series) makes the
window an oversample of a key that *lags* the exact one — upstream #2662 fixed
exactly that regression ("Long-lived CLI sessions that were resumed days later
... stay visible in the candidate window", v0.51.102) — and the lag is
unbounded, so no oversample guarantees the window holds the visible top-N.

D0 audits the metric that matters for that key: candidate-window membership
drift between the approximate and the exact ordering.

Measured on the live DB by the Slice D review: 0 excluded / 0 extra at the
160-row window for the 20-row slice (worst pipeline-top-20 rank by the
candidate key = 19) — headroom, not a bound. Re-verified here in a fixture with
realistic column-vs-join skew (``last_activity_at`` tracks, but lags,
``MAX(messages.timestamp)``: 99.6% of live rows differ at all — seconds for
most rows, but up to ~hours for a handful; live non-NULL median 28.6 s /
p99 490 s / max 10.85 h, NULL-fallback rows up to 17.77 h):
**0 excluded / 0 extra at the 8x oversample, 18/20 top-20 rank churn**.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import api.agent_sessions as agent_sessions

# ---------------------------------------------------------------------------
# Fixture — a state.db whose interactive rows carry realistic skew between the
# denormalized ``sessions.last_activity_at`` column and the exact
# ``MAX(messages.timestamp)`` join, plus the row shapes the pass must tolerate:
# background sources (excluded), zero-message rows (dropped by projection),
# NULL ``last_activity_at`` (fallback to ``started_at``), a subagent pair and a
# compression chain.
# ---------------------------------------------------------------------------

HOT_BASE = 1_800_000_000.0

INTERACTIVE_WHERE = (
    "s.source IS NOT NULL AND s.source NOT IN ('cron','webhook','kanban')"
)
# The exact key the pipeline displays/sorts by (final ORDER BY, unchanged by D1).
EXACT_ORDER = (
    "COALESCE((SELECT MAX(mx.timestamp) FROM messages mx WHERE mx.session_id = s.id),"
    " s.started_at) DESC, s.started_at DESC"
)
# The candidate-window key the D1 swap introduces.
CANDIDATE_ORDER = "COALESCE(s.last_activity_at, s.started_at) DESC, s.started_at DESC"

_SCHEMA = """
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
    end_reason TEXT,
    last_activity_at REAL
);
CREATE INDEX idx_sessions_effective_activity
    ON sessions(COALESCE(last_activity_at, started_at) DESC, started_at DESC);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    role TEXT,
    content TEXT,
    timestamp REAL
);
CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
"""


def _build_state_db(path: Path) -> None:
    """Build the shared Slice D fixture (see module docstring for the shape)."""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    message_seq = 0

    def add(
        sid,
        source,
        started,
        *,
        last_activity_at=None,
        message_count=1,
        parent=None,
        end_reason=None,
        messages=1,
    ):
        nonlocal message_seq
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
            " message_count, parent_session_id, ended_at, end_reason, last_activity_at)"
            " VALUES (?,?,?,?,?,?,?,?,NULL,?,?)",
            (
                sid,
                source,
                source,
                f"title {sid}",
                "test-model",
                started,
                message_count,
                parent,
                end_reason,
                last_activity_at,
            ),
        )
        for index in range(messages):
            message_seq += 1
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp)"
                " VALUES (?,?,?,?,?)",
                (f"msg-{message_seq}", sid, "user", "hi", started + 5 + index),
            )

    # Hot group: 24 rows 7s apart with 0-45s column lag -> heavy rank churn
    # inside the top-20 (18/20 rows change rank between the two keys), while
    # membership stays stable because the whole group is an hour newer than
    # everything else.
    for i in range(24):
        started = HOT_BASE + i * 7
        skew = (i * 13) % 46
        source = {3: "tui", 7: "claude-code", 11: "webui"}.get(i, "desktop")
        add(f"hot-{i:02d}", source, started, last_activity_at=started + 5 - skew)

    # Bulk: 300 rows 5 minutes apart with <=10s lag -> stable membership; the
    # 160-row candidate window boundary lands inside this block.
    bulk_base = HOT_BASE - 3600.0
    for i in range(300):
        started = bulk_base - i * 300
        lag = None if i in (57, 158, 233) else started + 5 - (i % 11)
        add(f"bulk-{i:03d}", "desktop", started, last_activity_at=lag)

    # Zero-message rows: candidate slots that the projection drops.
    for i in range(5):
        started = bulk_base - 200 - i * 300
        add(
            f"empty-{i}",
            "desktop",
            started,
            last_activity_at=started + 1,
            message_count=0,
            messages=0,
        )

    # Subagent parent/child pair (bulk region, outside the top-20).
    add("sub-parent", "subagent", bulk_base - 40 * 300, last_activity_at=bulk_base - 40 * 300 + 5)
    add(
        "sub-child",
        "subagent",
        bulk_base - 41 * 300,
        last_activity_at=bulk_base - 41 * 300 + 5,
        parent="sub-parent",
    )

    # Compression chain root+tip (bulk region): the root's row is what surfaces,
    # carrying the tip's recency.
    add(
        "chain-root",
        "desktop",
        bulk_base - 60 * 300,
        last_activity_at=bulk_base - 60 * 300 + 5,
        end_reason="compression",
    )
    add(
        "chain-tip",
        "desktop",
        bulk_base - 61 * 300,
        last_activity_at=bulk_base - 61 * 300 + 5,
        parent="chain-root",
    )

    # Background sources: newest rows overall, but excluded from this pass.
    for i in range(10):
        add(f"cron-{i}", "cron", HOT_BASE + 400 + i, last_activity_at=HOT_BASE + 400 + i)
        add(f"webhook-{i}", "webhook", HOT_BASE + 500 + i, last_activity_at=HOT_BASE + 500 + i)
        add(f"kanban-{i}", "kanban", HOT_BASE + 600 + i, last_activity_at=HOT_BASE + 600 + i)

    conn.commit()
    conn.close()


def _top_ids(conn: sqlite3.Connection, order_clause: str, limit: int) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            f"SELECT s.id FROM sessions s WHERE {INTERACTIVE_WHERE}"
            f" ORDER BY {order_clause} LIMIT ?",
            (limit,),
        )
    ]


def _interactive_rows(db_path: Path, limit: int = 20):
    return agent_sessions.read_importable_agent_session_rows(
        db_path, limit=limit, exclude_sources=("cron", "webhook", "kanban")
    )


def test_candidate_window_membership_drift_zero_at_eight_x_oversample(tmp_path):
    """D0 audit — the drift between the approximate and the exact key.

    Metric: the pipeline's exact-key top-20/top-160 rows must all be inside the
    160-row candidate window *ordered by the approximate key* (8x oversample of
    the 20-row slice), and no approximate-key row outside the exact top-160 may
    take a slot. Measured 0/0/0 on the live DB; asserted here on the fixture
    (which shows 18/20 rank churn, so the check is not vacuous). Zero drift on
    this data shape is headroom, not a bound — the window therefore orders by
    the exact key (D1); see the completeness regression below.
    """
    db = tmp_path / "state.db"
    _build_state_db(db)
    conn = sqlite3.connect(str(db))
    try:
        exact_top20 = _top_ids(conn, EXACT_ORDER, 20)
        exact_top160 = _top_ids(conn, EXACT_ORDER, 160)
        candidate_window = _top_ids(conn, CANDIDATE_ORDER, 160)
    finally:
        conn.close()

    candidate_set = set(candidate_window)
    excluded_top20 = [sid for sid in exact_top20 if sid not in candidate_set]
    excluded_top160 = [sid for sid in exact_top160 if sid not in candidate_set]
    extra_top160 = [sid for sid in candidate_window if sid not in set(exact_top160)]

    # Fixture sanity: the two keys genuinely disagree (skew is real), else the
    # zero-drift assertion below would be vacuous.
    assert candidate_window[:20] != exact_top20

    # The fail-closed gate: any excluded pipeline row means STOP (the swap
    # cannot be validated on this data shape).
    assert excluded_top20 == [], (
        "candidate window (last_activity_at key) excluded pipeline top-20 rows: "
        f"{excluded_top20}"
    )
    assert excluded_top160 == [], (
        "candidate window (last_activity_at key) excluded pipeline top-160 rows: "
        f"{excluded_top160[:10]}"
    )
    assert extra_top160 == [], (
        "candidate window admitted rows outside the pipeline top-160: "
        f"{extra_top160[:10]}"
    )

    # The live pipeline itself (pre-swap code path) must agree with the audit:
    # its visible top-20 equals the exact ordering and stays inside the window.
    visible = _interactive_rows(db, limit=20)
    visible_ids = [str(row["id"]) for row in visible]
    assert visible_ids == exact_top20
    assert set(visible_ids) <= candidate_set


# ---------------------------------------------------------------------------
# D1 — the candidate-window ordering itself.
#
# The candidate clause must be ``ORDER BY COALESCE((SELECT MAX(mx.timestamp)
# FROM messages mx WHERE mx.session_id = s.id), s.started_at) DESC,
# s.started_at DESC`` — the exact key the display sorts by. Ordering by the
# denormalized, indexed ``COALESCE(s.last_activity_at, s.started_at)`` instead
# makes the window an oversample of a key that lags the exact one, and the lag
# is unbounded (see the D0 audit above), so a resumed row can rank past the
# window and be dropped before the exact sort.
# ---------------------------------------------------------------------------

# The exact candidate selection, re-implemented as the parity reference.
_OLD_FORM_VISIBLE_SQL = f"""
WITH candidates AS (
    SELECT s.id
    FROM sessions s
    WHERE {INTERACTIVE_WHERE}
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
LIMIT ?
"""


class _RecordingCursor:
    def __init__(self, cursor, executed):
        self._cursor = cursor
        self._executed = executed

    def execute(self, sql, params=()):
        self._executed.append((sql, tuple(params)))
        return self._cursor.execute(sql, params)

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _RecordingConnection:
    def __init__(self, connection, executed):
        self._connection = connection
        self._executed = executed

    def cursor(self):
        return _RecordingCursor(self._connection.cursor(), self._executed)

    def close(self):
        return self._connection.close()

    def commit(self):
        return self._connection.commit()

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._connection.row_factory = value

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _record_connect(monkeypatch, executed):
    real_connect = agent_sessions.sqlite3.connect

    def recording_connect(*args, **kwargs):
        return _RecordingConnection(real_connect(*args, **kwargs), executed)

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", recording_connect)


def test_interactive_candidate_window_orders_by_the_exact_activity_key(monkeypatch, tmp_path):
    """D1: the candidate window is ordered by the exact
    ``COALESCE(MAX(mx.timestamp), s.started_at)`` key — the same key the display
    sorts by — with the ``s.started_at DESC`` tie-breaker, and the lagging
    denormalized column never decides membership.

    The correlated per-row probe resolves through ``idx_messages_session``, so
    the window stays bounded per row instead of aggregating the whole store.
    """
    db = tmp_path / "state.db"
    _build_state_db(db)

    executed = []
    _record_connect(monkeypatch, executed)
    rows = _interactive_rows(db, limit=20)
    assert rows

    candidate_calls = [(sql, params) for sql, params in executed if "WITH candidates AS" in sql]
    assert candidate_calls, "expected the candidate-window projection SQL"
    candidate_sql, candidate_params = candidate_calls[-1]
    normalized = " ".join(candidate_sql.split())

    # The exact key + the started_at DESC tie-breaker in the candidate clause.
    assert (
        "ORDER BY COALESCE( (SELECT MAX(mx.timestamp) FROM messages mx "
        "WHERE mx.session_id = s.id), s.started_at ) DESC, s.started_at DESC"
    ) in normalized
    # The lagging denormalized column must not decide window membership.
    assert "COALESCE(s.last_activity_at" not in normalized
    # The final display ordering stays exact (join-based MAX), unchanged.
    assert "ORDER BY COALESCE(MAX(m.timestamp), s.started_at) DESC" in normalized
    # The window stays the 8x oversample of the visible limit.
    assert candidate_params[-1] == 160

    # ... and the correlated probe is an index search per row — no whole-store
    # messages scan in the window selection.
    conn = sqlite3.connect(str(db))
    try:
        plan = [str(row[-1]) for row in conn.execute("EXPLAIN QUERY PLAN " + candidate_sql, candidate_params)]
    finally:
        conn.close()
    assert any("idx_messages_session" in line for line in plan), plan
    assert not any("SCAN messages" in line for line in plan), plan


def test_candidate_selection_keeps_visible_top_n_parity_under_skew(tmp_path):
    """D1(a): with the column and the join disagreeing by seconds (the live
    shape — 99.6% of rows), the reader's candidate selection yields the same
    visible top-N as the exact reference form."""
    db = tmp_path / "state.db"
    _build_state_db(db)
    conn = sqlite3.connect(str(db))
    try:
        exact_top20 = _top_ids(conn, EXACT_ORDER, 20)
        candidate_order_top20 = _top_ids(conn, CANDIDATE_ORDER, 20)
        old_form_visible = [str(row[0]) for row in conn.execute(_OLD_FORM_VISIBLE_SQL, (160, 20))]
    finally:
        conn.close()

    # Fixture sanity: the candidate key genuinely reorders the top-20, so the
    # parity assertion is not vacuous.
    assert candidate_order_top20 != exact_top20

    visible_ids = [str(row["id"]) for row in _interactive_rows(db, limit=20)]

    # Same visible top-N as the old form, in the same (exact-key) order.
    assert visible_ids == old_form_visible
    assert visible_ids == exact_top20


def test_candidate_ordering_falls_back_without_last_activity_column(monkeypatch, tmp_path):
    """Older state.db schemas have no ``sessions.last_activity_at`` column (all
    live DBs do). The pass must keep working there — never referencing the
    missing column — and keep the exact correlated-subquery candidate ordering,
    so a session resumed with a late message still surfaces on top."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
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
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    rows = [
        # sid, started_at, message timestamps
        ("old-root", 1000.0, [1000.0, 5000.0]),
        ("old-newer-start", 2000.0, [2000.0]),
        ("old-mid", 1500.0, [1500.0]),
    ]
    for sid, started_at, timestamps in rows:
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
            " message_count, parent_session_id, ended_at, end_reason)"
            " VALUES (?, 'desktop', 'desktop', ?, 'test-model', ?, 1, NULL, NULL, NULL)",
            (sid, f"title {sid}", started_at),
        )
        for index, timestamp in enumerate(timestamps):
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp)"
                " VALUES (?, ?, 'user', 'hi', ?)",
                (f"{sid}-msg-{index}", sid, timestamp),
            )
    conn.commit()
    conn.close()

    executed = []
    _record_connect(monkeypatch, executed)
    result_ids = [str(row["id"]) for row in _interactive_rows(db, limit=20)]

    # Exact recency: the early-started session with the late message ranks first.
    assert result_ids == ["old-root", "old-newer-start", "old-mid"]
    # The missing column is never referenced (it would raise OperationalError,
    # which the caller swallows into an empty sidebar).
    assert all("last_activity_at" not in sql for sql, _params in executed)


# ---------------------------------------------------------------------------
# Window completeness — the candidate window must never drop a row the exact
# ordering would display, however stale the denormalized activity key is.
#
# The 8x oversample is headroom on the approximate key, not a bound (see the D0
# audit above): a row whose ``last_activity_at`` is missing or hours stale can
# rank arbitrarily far down by that key while ranking at the top by the exact
# ``MAX(messages.timestamp)`` the display sorts by. Past the oversample the row
# fell out of the candidate set BEFORE the exact sort and vanished from the
# visible slice — the regression upstream fixed in #2662 by ordering the
# candidate window by the exact key (v0.51.102: "Long-lived CLI sessions that
# were resumed days later ... stay visible in the candidate window").
# ---------------------------------------------------------------------------

DRIFT_BASE = 1_700_000_000.0


def _build_unbounded_drift_db(path: Path) -> None:
    """200 fillers newer by every denormalized key + one resumed-old row that is
    newest by its messages and NULL in ``last_activity_at`` (so its approximate
    key falls back to its old ``started_at``: candidate rank ~201 of 201)."""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    message_seq = 0

    def add(sid, started, *, last_activity_at, timestamps):
        nonlocal message_seq
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
            " message_count, parent_session_id, ended_at, end_reason, last_activity_at)"
            " VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,?)",
            (
                sid,
                "desktop",
                "desktop",
                f"title {sid}",
                "test-model",
                started,
                len(timestamps),
                last_activity_at,
            ),
        )
        for timestamp in timestamps:
            message_seq += 1
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp)"
                " VALUES (?,?,?,?,?)",
                (f"msg-{message_seq}", sid, "user", "hi", timestamp),
            )

    for i in range(200):
        started = DRIFT_BASE + 1000 + i
        add(f"filler-{i:03d}", started, last_activity_at=started + 5, timestamps=[started])
    # Resumed days later: the exact key is the newest message in the store, the
    # approximate key is the oldest ``started_at`` (NULL column falls back).
    add("resumed-old", DRIFT_BASE + 10, last_activity_at=None,
        timestamps=[DRIFT_BASE + 5000, DRIFT_BASE + 5001])
    conn.commit()
    conn.close()


def _fast_interactive_rows(db_path: Path, limit: int = 20):
    return agent_sessions.read_fast_sidebar_agent_rows(
        db_path, limit=limit, exclude_sources=("cron", "webhook", "kanban")
    )


def test_candidate_window_is_complete_for_resumed_rows_beyond_the_oversample(tmp_path):
    """A resumed-old row must survive the candidate window even when its
    denormalized activity key ranks it past the 8x oversample.

    Fixture: 200 fillers newer by ``last_activity_at`` plus one row whose
    ``last_activity_at`` is NULL (candidate rank 201 of 201, outside the
    160-row window) and whose latest message is the newest in the store (exact
    rank 1). Both readers project the same window, so the drop hit the fast AND
    the full sidebar projection.
    """
    db = tmp_path / "state.db"
    _build_unbounded_drift_db(db)
    conn = sqlite3.connect(str(db))
    try:
        approx_window = _top_ids(conn, CANDIDATE_ORDER, 160)
        exact_top20 = _top_ids(conn, EXACT_ORDER, 20)
    finally:
        conn.close()

    # Fixture sanity: the two keys disagree on membership, not just rank — the
    # approximate window genuinely excludes the exact-top-1 row.
    assert "resumed-old" not in approx_window
    assert exact_top20[0] == "resumed-old"

    full_ids = [str(row["id"]) for row in _interactive_rows(db, limit=20)]
    fast_ids = [str(row["id"]) for row in _fast_interactive_rows(db, limit=20)]
    assert fast_ids == full_ids
    assert full_ids[0] == "resumed-old"


# ---------------------------------------------------------------------------
# The fast reader's candidate set: a bounded UNION of index-ordered pre-windows
# (``_fast_candidate_union_cte``). SQLite evaluates the exact ordering key per
# *qualifying* row before LIMIT, so ordering the window directly by it costs one
# indexed message probe per session at any window size (measured on fixture
# stores, warm medians: 0.7 ms at 1k sessions, 9.9 ms at 10k, 54.9 ms at 50k,
# 5.3 ms on a clone of the live ~3k-session store; the fast build's whole
# budget is ~100 ms). The union bounds the probe count to the pre-window depth
# at any store size; the final exact sort is unchanged. The regression that
# matters: a resumed row whose session-row keys are all stale must still be
# seeded — by the message-recency pre-window.
# ---------------------------------------------------------------------------

RESUMED_BASE = 1_700_000_000.0


def _build_resumed_first_db(path: Path, fillers: int = 200) -> None:
    """Resumed-old row FIRST — lowest rowid, oldest ``started_at``, NULL
    ``last_activity_at`` — with its messages appended LAST (newest message
    rowids and timestamps): only the message-recency pre-window can see it.

    Every session-row key ranks it outside the 160-row pre-windows: the
    activity key falls back to the old ``started_at``, and both ``started_at``
    and ``rowid`` order it behind all ``fillers`` newer rows.
    """
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
        " message_count, parent_session_id, ended_at, end_reason, last_activity_at)"
        " VALUES ('resumed-old', 'desktop', 'desktop', 'Resumed old', 'test-model', ?, 2,"
        " NULL, NULL, NULL, NULL)",
        (RESUMED_BASE + 10,),
    )
    for i in range(fillers):
        started = RESUMED_BASE + 1000 + i
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
            " message_count, parent_session_id, ended_at, end_reason, last_activity_at)"
            " VALUES (?, 'desktop', 'desktop', ?, 'test-model', ?, 1, NULL, NULL, NULL, ?)",
            (f"filler-{i:03d}", f"title filler-{i:03d}", started, started + 5),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
            (f"msg-filler-{i:03d}", f"filler-{i:03d}", "user", "hi", started),
        )
    for index, timestamp in enumerate((RESUMED_BASE + 5000, RESUMED_BASE + 5001)):
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
            (f"msg-resumed-{index}", "resumed-old",
             "user" if index == 0 else "assistant", "hi", timestamp),
        )
    conn.commit()
    conn.close()


def test_candidate_union_keeps_a_resumed_row_visible_only_through_message_recency(tmp_path):
    """A resumed row whose only recency evidence is its messages must survive
    the fast reader's bounded candidate union.

    Fixture (``_build_resumed_first_db``): the resumed row is the oldest by
    every session-row key — lowest rowid, oldest ``started_at``, NULL
    ``last_activity_at`` — while its messages are the newest in the store. All
    three session-row pre-windows miss it; the message-recency pre-window
    (sessions of the newest ``candidate_limit * 8`` message rows) carries it
    into the union, where the exact key ranks it first. Without that seed the
    fast payload would drop a row the full reader returns (parity break).
    """
    db = tmp_path / "state.db"
    _build_resumed_first_db(db)
    conn = sqlite3.connect(str(db))
    try:
        # Fixture sanity: the session-row pre-windows genuinely miss the row...
        for order in (CANDIDATE_ORDER, "s.started_at DESC", "s.rowid DESC"):
            assert "resumed-old" not in _top_ids(conn, order, 160), order
        # ... and only the message-recency seed (insertion order) sees it.
        message_seed = [
            str(row[0]) for row in conn.execute(
                "SELECT DISTINCT m.session_id FROM (SELECT m.session_id FROM messages m"
                " ORDER BY m.rowid DESC LIMIT 1280) m"
            )
        ]
        assert "resumed-old" in message_seed
        exact_top20 = _top_ids(conn, EXACT_ORDER, 20)
    finally:
        conn.close()
    assert exact_top20[0] == "resumed-old"

    fast_ids = [str(row["id"]) for row in _fast_interactive_rows(db, limit=20)]
    full_ids = [str(row["id"]) for row in _interactive_rows(db, limit=20)]
    assert fast_ids == full_ids
    assert fast_ids[0] == "resumed-old"


def test_fast_candidate_union_is_bounded_and_seeded_by_pre_windows(monkeypatch, tmp_path):
    """The fast reader's candidate set is the bounded UNION of index-ordered
    pre-windows, and the exact key is applied only over that union.

    Pins the mechanism (not just the exact-key text): the pre-window LIMITs are
    the candidate window (160), the message-recency window is 8x that (1280),
    the candidates CTE intersects the union, and the correlated probe still
    resolves through ``idx_messages_session``.
    """
    db = tmp_path / "state.db"
    _build_state_db(db)

    executed = []
    _record_connect(monkeypatch, executed)
    rows = _fast_interactive_rows(db, limit=20)
    assert rows

    candidate_calls = [(sql, params) for sql, params in executed if "candidates AS (" in sql]
    assert candidate_calls, "expected the fast candidate-window SQL"
    sql, params = candidate_calls[-1]
    normalized = " ".join(sql.split())

    for name in ("pre_activity", "pre_rowid", "pre_messages"):
        assert f"{name} AS" in normalized, normalized[:400]
    assert "s.id IN (" in normalized
    assert "UNION SELECT id FROM pre_messages" in normalized
    assert (
        "ORDER BY COALESCE((SELECT MAX(mx.timestamp) FROM messages mx"
        " WHERE mx.session_id = s.id), s.started_at) DESC, s.started_at DESC"
    ) in normalized
    # Bounded: pre-window LIMITs = the candidate window, message window = 8x.
    limits = [value for value in params if isinstance(value, int)]
    assert limits == [160, 160, 1280, 160], limits
    assert agent_sessions.FAST_SIDEBAR_PREWINDOW_OVERSAMPLE == 8

    # The correlated exact-key probe is index-backed (no whole-store messages
    # aggregate), and the candidates CTE is driven by the union subquery.
    conn = sqlite3.connect(str(db))
    try:
        plan = [str(row[-1]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    finally:
        conn.close()
    assert any("idx_messages_session" in line for line in plan), plan
    assert any("UNION" in line or "MERGE" in line for line in plan), plan


# ---------------------------------------------------------------------------
# The documented no-index fallback (``_fast_candidate_union_cte`` → (None, [])).
#
# The union's session-row seeds must each be an index-ordered bounded read, so a
# store without the agent's standard sessions indexes (but WITH a usable
# ``messages.timestamp``) takes the plain exact-key window over all qualifying
# sessions — the pre-union behavior. Every legacy fixture in this repo omits
# ``messages.timestamp`` (or the whole messages table) and therefore never
# reaches that branch; this store shape is the one that does.
# ---------------------------------------------------------------------------


def _build_no_sessions_index_db(path: Path) -> None:
    """The Slice D fixture with both standard sessions indexes dropped."""
    _build_state_db(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DROP INDEX IF EXISTS idx_sessions_effective_activity")
        conn.execute("DROP INDEX IF EXISTS idx_sessions_started")
        conn.commit()
    finally:
        conn.close()


def test_fast_reader_without_sessions_indexes_uses_the_exact_window_fallback(monkeypatch, tmp_path):
    """No sessions indexes: the fast reader falls back to the plain exact-key
    window with its OWN bindings and returns the full reader's rows.

    Regression (round-2 defect A-D1): the fallback reused the union's parameter
    list — which is ``[]`` exactly when the union is declined — while its SQL
    still binds ``where_sql`` + ``LIMIT ?``. The statement raised
    ``sqlite3.ProgrammingError: Incorrect number of bindings supplied``, and the
    models-layer fast loader swallowed that into an empty CLI list, so the fast
    first paint silently dropped every CLI/agent row on such a store.
    """
    db = tmp_path / "state.db"
    _build_no_sessions_index_db(db)

    executed = []
    _record_connect(monkeypatch, executed)
    fast = _fast_interactive_rows(db, limit=20)
    full = _interactive_rows(db, limit=20)

    assert [row["id"] for row in fast] == [row["id"] for row in full]
    assert len(fast) == 20

    window_calls = [(sql, params) for sql, params in executed if "candidates AS (" in sql]
    assert window_calls, "expected the fast candidate-window statement"
    sql, params = window_calls[-1]
    normalized = " ".join(sql.split())
    # The union is unavailable on this store: the plain exact-key window over
    # all qualifying sessions runs instead (no pre-window seeds, no union).
    assert "pre_activity AS" not in normalized
    assert "s.id IN (" not in normalized
    assert "LIMIT ?" in normalized
    # Its bindings are its own (where-params + the window LIMIT), never the
    # union's: a param-count drift here is the regression that made the fast
    # first paint serve zero CLI rows. The placeholder count is asserted
    # directly so any re-break of the binding list fails here.
    assert list(params) == ["cron", "webhook", "kanban", 160]
    assert normalized.count("?") == len(params)


# ---------------------------------------------------------------------------
# The candidate union's documented residual bound (round-2 finding A-F1).
#
# The union is exact when every session in the exact top-8N over all qualifying
# sessions is seeded by one of the four pre-windows. It is NOT a theorem: a
# session whose only recency evidence is a message more than 64N message rows
# behind the tail, and which fails all three session-row seeds, is not seeded —
# the fast window can then drop a row the full reader (and the pre-union fast
# reader) returns. The bound, its measured headroom, and this counterexample are
# documented in ``docs/architecture/session-list-fast-path.md``.
# ---------------------------------------------------------------------------

BOUND_BASE = 1_790_000_000.0


def _build_message_gap_db(path: Path) -> None:
    """The counterexample for the union's documented bound (see above).

    ``target`` sits at exact rank 20 (19 sessions outrank it) while its newest
    message is behind 1530 newer message rows — more than the message seed's
    1280-row window at limit 20 — and it fails the three session-row seeds too
    (oldest ``started_at``, lowest rowid, NULL ``last_activity_at``).
    """
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    message_seq = 0

    def add(sid, started, *, last_activity_at, timestamps):
        nonlocal message_seq
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
            " message_count, parent_session_id, ended_at, end_reason, last_activity_at)"
            " VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,?)",
            (sid, "desktop", "desktop", f"title {sid}", "test-model", started,
             len(timestamps), last_activity_at),
        )
        for timestamp in timestamps:
            message_seq += 1
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp)"
                " VALUES (?,?,?,?,?)",
                (f"msg-{message_seq}", sid, "user", "hi", timestamp),
            )

    # Inserted first: lowest rowid, oldest started_at, NULL last_activity_at.
    add("target", BOUND_BASE, last_activity_at=None,
        timestamps=[BOUND_BASE + 100, BOUND_BASE + 900])
    # Fillers: newer by started_at/rowid (so they fill those two seeds) but a
    # low exact key (one old message each) -> they rank below the target.
    for i in range(200):
        add(f"filler-{i:03d}", BOUND_BASE + 1000 + i, last_activity_at=BOUND_BASE + 10,
            timestamps=[BOUND_BASE + 10])
    # 19 sessions x 70 messages appended AFTER the target's messages: 1330 rows
    # (> 1280 = 64N) so the message seed covers only them, and their exact keys
    # outrank the target's (ranks 1-19).
    for i in range(19):
        add(f"chatty-{i:02d}", BOUND_BASE + 2000 + i, last_activity_at=None,
            timestamps=[BOUND_BASE + 1000 + i * 70 + j for j in range(70)])
    conn.commit()
    conn.close()


def test_candidate_union_bound_drops_a_row_beyond_the_message_window(tmp_path):
    """Pins the union's documented residual bound on its counterexample shape.

    The fast window drops ``target`` (exact rank 20) because its newest message
    is outside the 1280-row message seed and every session-row seed misses it,
    while the full reader — the parity reference and the background rebuild —
    returns it. This is the accepted trade of the bounded union (a bounded seed
    set instead of a provable prefix); the shape and its reachability are
    documented in ``docs/architecture/session-list-fast-path.md``. If a future
    change makes the union exact for this shape (a deeper/message-ordered seed,
    or a provable bound check), this test must be updated to assert parity.
    """
    db = tmp_path / "state.db"
    _build_message_gap_db(db)
    conn = sqlite3.connect(str(db))
    try:
        exact_top20 = _top_ids(conn, EXACT_ORDER, 20)
        exact_top160 = _top_ids(conn, EXACT_ORDER, 160)
        # All three session-row seeds miss the target ...
        for order in (CANDIDATE_ORDER, "s.started_at DESC", "s.rowid DESC"):
            assert "target" not in _top_ids(conn, order, 160), order
        # ... and so does the message seed (newest 1280 message rows).
        message_seed = [
            str(row[0]) for row in conn.execute(
                "SELECT DISTINCT m.session_id FROM (SELECT m.session_id FROM messages m"
                " ORDER BY m.rowid DESC LIMIT 1280) m"
            )
        ]
        rows_behind = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE rowid > (SELECT MAX(rowid) FROM messages"
            " WHERE session_id = 'target')"
        ).fetchone()[0]
    finally:
        conn.close()

    # Fixture sanity: the target is inside the exact window, outside every seed,
    # and its newest message really is beyond the 64N-row message window.
    assert exact_top20[-1] == "target"
    assert "target" in exact_top160
    assert "target" not in message_seed
    assert rows_behind == 1530 > 1280

    full_ids = [str(row["id"]) for row in _interactive_rows(db, limit=20)]
    fast_ids = [str(row["id"]) for row in _fast_interactive_rows(db, limit=20)]

    assert "target" in full_ids
    assert "target" not in fast_ids
    assert fast_ids != full_ids
    # Exactly one row differs: the dropped target, replaced by a filler row.
    assert len(set(fast_ids) ^ set(full_ids)) == 2
    assert len(fast_ids) == len(full_ids) == 20



