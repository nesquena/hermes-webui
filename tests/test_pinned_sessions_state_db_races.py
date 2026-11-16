"""Review follow-ups: a late manual-rename mirror after a reset, and id-scoped flag reads."""

import sqlite3
import threading
from contextlib import closing

from tests.test_pinned_sessions_state_db_mirror import _PinSession, _homes, _make_state_db


def test_late_rename_mirror_cannot_undo_a_mirrored_reset(tmp_path, monkeypatch):
    from api import routes
    from api.session_ops import apply_session_title_rename

    db = _homes(tmp_path, monkeypatch)["default"] / "state.db"
    _make_state_db(db, rows=(("s1", 0),))
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: threading.RLock())
    session = _PinSession("s1")
    monkeypatch.setattr(routes, "SESSIONS", {"s1": session})
    apply_session_title_rename(session, "Old manual name")
    apply_session_title_rename(session, "Untitled")  # the reset lands and mirrors first
    routes._mirror_session_title(session)

    routes._mirror_to_state_db(session, "title", "Old manual name")  # the rename's mirror arrives late

    with closing(sqlite3.connect(str(db))) as conn:
        assert conn.execute("SELECT title, title_source FROM sessions WHERE id = 's1'").fetchone() == (None, None)


def test_flag_reader_fetches_only_listed_ids_in_bounded_batches(tmp_path, monkeypatch):
    from api import state_sync

    db = _homes(tmp_path, monkeypatch)["default"] / "state.db"
    _make_state_db(db, rows=[(f"s{i}", i % 2) for i in range(3000)])
    wanted = {f"s{i}" for i in range(0, 2400, 2)} | {"missing"}
    queries = []
    real_open = state_sync.open_state_db_readonly

    def tracing_open(path):
        conn = real_open(path)
        conn.set_trace_callback(queries.append)
        return conn

    monkeypatch.setattr(state_sync, "open_state_db_readonly", tracing_open)
    flags = state_sync.read_state_db_flags("default", wanted)

    assert set(flags) == wanted - {"missing"}
    assert all(f == {"pinned": False, "archived": False} for f in flags.values())
    selects = [q for q in queries if q.startswith("SELECT id")]
    assert len(selects) == 3 and all("WHERE id IN" in q for q in selects)
    assert state_sync.read_state_db_flags("default", ()) == {}


def test_flag_reader_without_flag_columns_returns_nothing(tmp_path, monkeypatch):
    from api import state_sync

    db = _homes(tmp_path, monkeypatch)["default"] / "state.db"
    _make_state_db(db, rows=(("s1", 0),), pinned_column=False)
    assert state_sync.read_state_db_flags("default", {"s1"}) == {}
