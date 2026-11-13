"""Regression tests: a recency-sliced subagent leaf must keep its parent row.

`read_importable_agent_session_rows()` applied the visible-window limit as a
flat per-row recency slice. Subagent rows only render as children when their
parent row is in the same payload, so a frozen orchestrator (no longer writing)
lost the recency race against its own still-streaming leaves and fell out of the
window — promoting the leaves to top-level sidebar rows. The projection now
re-adds subagent parents that the oversampled candidate set already projected.
"""
import sqlite3

from api.agent_sessions import read_importable_agent_session_rows


def _make_db(path, sessions, messages):
    """sessions: (id, title, source, parent_session_id); messages: (session_id, role, ts)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT, "
        "message_count INTEGER, started_at REAL, source TEXT, parent_session_id TEXT)"
    )
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL)")
    for sid, title, source, parent in sessions:
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, source, parent_session_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, title, "gpt", 2, 1000.0, source, parent),
        )
    for sid, role, ts in messages:
        conn.execute("INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)", (sid, role, ts))
    conn.commit()
    conn.close()


def _lineage_db(path, parent_source="subagent"):
    """One quiet orchestrator + 3 hot leaves + an unrelated hot subagent row."""
    sessions: list[tuple[str, str, str, str | None]] = [
        ("orch", "Orchestrator", parent_source, None),
        ("loner", "Unrelated", "subagent", None),
    ]
    messages = [("orch", "user", 100.0), ("orch", "assistant", 101.0)]  # went quiet early
    for i in range(3):
        sessions.append((f"leaf{i}", f"Leaf {i}", "subagent", "orch"))
        messages += [(f"leaf{i}", "user", 900.0 + i), (f"leaf{i}", "assistant", 901.0 + i)]
    messages += [("loner", "user", 800.0), ("loner", "assistant", 801.0)]
    _make_db(path, sessions, messages)


def test_evicted_subagent_parent_is_kept_in_window(tmp_path):
    db = tmp_path / "state.db"
    _lineage_db(db)

    # limit=3 is exactly the three hot leaves; the quiet parent is sliced off.
    rows = read_importable_agent_session_rows(db, limit=3, exclude_sources=None)
    by_id = {r["id"]: r for r in rows}

    assert "orch" in by_id, "quiet subagent parent must survive the recency slice"
    for i in range(3):
        leaf = by_id[f"leaf{i}"]
        assert leaf["parent_session_id"] == "orch"
        assert leaf["relationship_type"] == "child_session"


def test_unrelated_older_subagent_row_stays_excluded(tmp_path):
    db = tmp_path / "state.db"
    _lineage_db(db)

    rows = read_importable_agent_session_rows(db, limit=3, exclude_sources=None)

    # Only ancestors are rescued — an unrelated quieter subagent row stays out.
    assert "loner" not in {r["id"] for r in rows}


def test_webui_parent_is_not_force_imported(tmp_path):
    db = tmp_path / "state.db"
    _lineage_db(db, parent_source="webui")

    rows = read_importable_agent_session_rows(db, limit=3, exclude_sources=None)

    # The webui sidebar bucket already owns its own rows; don't duplicate them.
    assert "orch" not in {r["id"] for r in rows}
    assert {f"leaf{i}" for i in range(3)} <= {r["id"] for r in rows}


def test_recovered_parent_is_returned_beyond_limit(tmp_path):
    """`limit` bounds the recency slice, not the row count (documented contract)."""
    db = tmp_path / "state.db"
    _lineage_db(db)

    rows = read_importable_agent_session_rows(db, limit=3, exclude_sources=None)

    # 3 sliced leaves + 1 re-added anchor: callers must iterate, not assume <= limit.
    assert len(rows) == 4
    assert {r["id"] for r in rows} == {"leaf0", "leaf1", "leaf2", "orch"}


def _window_db(path, filler):
    """Quiet parent, `filler` newer unrelated rows, one hot child of that parent."""
    sessions: list[tuple[str, str, str, str | None]] = [("orch", "Orchestrator", "subagent", None)]
    messages = [("orch", "user", 100.0), ("orch", "assistant", 100.5)]
    for i in range(filler):
        sessions.append((f"fill{i}", f"Filler {i}", "subagent", None))
        messages += [(f"fill{i}", "user", 200.0 + i), (f"fill{i}", "assistant", 200.5 + i)]
    sessions.append(("leaf", "Leaf", "subagent", "orch"))
    messages += [("leaf", "user", 9000.0), ("leaf", "assistant", 9001.0)]
    _make_db(path, sessions, messages)


def test_parent_inside_candidate_oversample_is_recovered(tmp_path):
    """The oversample is limit * 8, so a parent ranked within it is still restored."""
    db = tmp_path / "state.db"
    _window_db(db, filler=22)  # orch is candidate #24 of 24

    rows = read_importable_agent_session_rows(db, limit=3, exclude_sources=None)

    assert "orch" in {r["id"] for r in rows}


def test_parent_beyond_candidate_oversample_stays_unresolved(tmp_path):
    """Documented bound: recovery reuses the limit * 8 candidate set, never a new query.

    A parent ranked below that oversample is not fetched, so its child renders
    top-level — the pre-existing behaviour. This pins the boundary so widening
    it becomes a deliberate `candidate_limit` change, not an accidental one.
    """
    db = tmp_path / "state.db"
    _window_db(db, filler=23)  # orch is pushed to candidate #25 of 24

    rows = read_importable_agent_session_rows(db, limit=3, exclude_sources=None)
    ids = {r["id"] for r in rows}

    assert "leaf" in ids
    assert "orch" not in ids


def test_parent_cycle_does_not_hang(tmp_path):
    """A corrupt self/mutual parent link must not spin the ancestor walk."""
    db = tmp_path / "state.db"
    _make_db(
        db,
        [("a", "A", "subagent", "b"), ("b", "B", "subagent", "a")],
        [("a", "user", 900.0), ("a", "assistant", 901.0), ("b", "user", 100.0), ("b", "assistant", 101.0)],
    )

    rows = read_importable_agent_session_rows(db, limit=1, exclude_sources=None)

    assert {r["id"] for r in rows} == {"a", "b"}
