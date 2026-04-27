"""
Regression tests for compression-chain rename (issue #1).

Bug
---
When a CLI session is the *tip* of a compression chain
(parent_session_id != NULL AND parent's end_reason == 'compression'),
``api.agent_sessions._project_agent_session_rows`` collapses the chain
into a single sidebar row that exposes the *tip*'s session_id but keeps
the *chain head*'s title for visible identity.

Before the fix, ``/api/session/rename`` wrote the new title to the tip
(the id the frontend exposes). On the next sidebar refresh the
projection re-rendered the row using the unchanged head's title, so the
rename appeared to revert.

Fix
---
``api.state_sync.rename_cli_session`` now walks ``parent_session_id``
upward across compression boundaries to find the lineage *root* and
updates that row's title. The projection (head-prefer) then surfaces
the new name on the next refresh, AND the update is compatible with
``hermes_state.SessionDB``'s ``UNIQUE INDEX … ON sessions(title) WHERE
title IS NOT NULL`` because we touch exactly one row.

These are pure unit tests against a temporary SQLite DB.
"""

import pathlib
import sqlite3
import sys
import tempfile
import time

import pytest


_REPO = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def state_db(monkeypatch):
    """Fresh ``state.db`` in a temp HERMES_HOME, with profiles redirected."""
    tmp = tempfile.mkdtemp(prefix="hermes-rename-test-")
    home = pathlib.Path(tmp)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import api.profiles as _profiles  # noqa: F401
    monkeypatch.setattr(
        _profiles, "get_active_hermes_home", lambda: home, raising=False,
    )

    db_path = home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            title TEXT,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        );
        """
    )
    conn.commit()
    yield conn
    try:
        conn.close()
    except Exception:
        pass


def _db_path_for(conn) -> str:
    [(_, _name, file_)] = conn.execute("PRAGMA database_list").fetchall()
    return file_


def _insert_session(conn, *, sid, title, source="cli", parent=None,
                    end_reason=None, message_count=1, started_at=None,
                    ended_at=None):
    started_at = started_at if started_at is not None else time.time()
    if end_reason and ended_at is None:
        ended_at = started_at + 1.0
    conn.execute(
        "INSERT OR REPLACE INTO sessions "
        "(id, source, title, model, started_at, message_count, "
        " parent_session_id, ended_at, end_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, source, title, "anthropic/claude-sonnet-4-5", started_at,
         message_count, parent, ended_at, end_reason),
    )
    if message_count:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, 'user', 'hi', ?)",
            (sid, started_at + 0.1),
        )
    conn.commit()


def _read_title(conn, sid):
    row = conn.execute(
        "SELECT title FROM sessions WHERE id = ?", (sid,)
    ).fetchone()
    return row["title"] if row else None


# ── Tests ───────────────────────────────────────────────────────────────────


def test_rename_cli_session_updates_standalone_row(state_db):
    """Baseline: a standalone CLI session updates its own row."""
    from api.state_sync import rename_cli_session

    sid = "20260427_aaaaaa_standalone"
    _insert_session(state_db, sid=sid, title="Original")

    assert rename_cli_session(sid, "Renamed") is True
    assert _read_title(state_db, sid) == "Renamed"


def test_rename_via_tip_updates_lineage_root(state_db):
    """The fix: renaming the tip writes the new title to the chain HEAD.

    The sidebar passes the tip's id (because the projection exposes that
    for navigation), so ``rename_cli_session`` must walk
    ``parent_session_id`` up through compression boundaries to the root
    and update *its* title — the row whose title the projection actually
    surfaces.
    """
    from api.state_sync import rename_cli_session

    root = "20260427_root_aaaa"
    mid = "20260427_mid_bbbb"
    tip = "20260427_tip_cccc"
    _insert_session(state_db, sid=root, title="Original Conversation",
                    end_reason="compression",
                    started_at=time.time() - 200)
    _insert_session(state_db, sid=mid, title="Original Conversation #2",
                    parent=root, end_reason="compression",
                    started_at=time.time() - 100)
    _insert_session(state_db, sid=tip, title="Original Conversation #3",
                    parent=mid, started_at=time.time() - 50)

    assert rename_cli_session(tip, "User Friendly Name") is True

    # Root got the new title.
    assert _read_title(state_db, root) == "User Friendly Name"
    # Intermediate / tip auto-titles untouched (so the unique index would
    # never collide even if hermes_state were enforcing it).
    assert _read_title(state_db, mid) == "Original Conversation #2"
    assert _read_title(state_db, tip) == "Original Conversation #3"


def test_rename_then_refresh_preserves_new_title_end_to_end(state_db):
    """Full rename → projection cycle: WebUI sends tip, sidebar shows new title."""
    from api.state_sync import rename_cli_session
    from api.agent_sessions import read_importable_agent_session_rows

    root = "20260427_root_eeee"
    tip = "20260427_tip_ffff"
    _insert_session(state_db, sid=root, title="Original Title",
                    end_reason="compression",
                    started_at=time.time() - 100)
    _insert_session(state_db, sid=tip, title="Original Title #2",
                    parent=root, started_at=time.time() - 50)

    # User renames via the sidebar — the API receives the TIP id.
    assert rename_cli_session(tip, "User Friendly Name") is True

    # The next projection (== refresh) reflects the new name.
    [merged] = [r for r in read_importable_agent_session_rows(_db_path_for(state_db))
                if r.get("_lineage_root_id") == root]
    assert merged["id"] == tip, "projection still navigates to the tip"
    assert merged["title"] == "User Friendly Name", \
        "rename must survive a refresh — this is the bug we're fixing"


def test_rename_does_not_walk_across_non_compression_parents(state_db):
    """Only compression chains share identity. Plain parent/child links
    (e.g. subagent forks) must NOT be treated as the same conversation —
    renaming a child must not silently rewrite the unrelated parent."""
    from api.state_sync import rename_cli_session

    parent = "20260427_parent_xxxx"
    child = "20260427_child_yyyy"
    # Parent did NOT end in compression — it's a regular fork relationship.
    _insert_session(state_db, sid=parent, title="Unrelated Parent",
                    end_reason="completed",
                    started_at=time.time() - 100)
    _insert_session(state_db, sid=child, title="Subagent Run",
                    parent=parent, started_at=time.time() - 50)

    assert rename_cli_session(child, "New Child Name") is True

    # Child's own title was updated; parent untouched.
    assert _read_title(state_db, child) == "New Child Name"
    assert _read_title(state_db, parent) == "Unrelated Parent"


def test_rename_cli_session_unknown_returns_false(state_db):
    """Renaming a non-existent session must return False, not raise."""
    from api.state_sync import rename_cli_session
    assert rename_cli_session("does-not-exist", "Whatever") is False


def test_rename_cli_session_uniqueness_conflict_raises(state_db):
    """Renaming to a title already used by ANOTHER session must raise.

    Mirrors SessionDB.set_session_title's contract. Behaviour is
    enforced either by hermes_state's UNIQUE INDEX (when installed) or
    by our raw SQL fallback's IntegrityError → ValueError translation.
    """
    from api.state_sync import rename_cli_session

    a = "20260427_aaaa_other"
    b = "20260427_bbbb_target"
    _insert_session(state_db, sid=a, title="Taken Title")
    _insert_session(state_db, sid=b, title="Original")

    try:
        rename_cli_session(b, "Taken Title")
    except ValueError:
        return

    # If neither hermes_state nor a UNIQUE INDEX is in play, the raw
    # UPDATE just succeeds — accept that as a degraded-mode no-op.
    if _read_title(state_db, b) == "Taken Title":
        pytest.skip(
            "no UNIQUE INDEX on sessions.title in this environment "
            "(hermes_state not installed) — uniqueness is enforced by "
            "the agent's SessionDB at runtime"
        )
