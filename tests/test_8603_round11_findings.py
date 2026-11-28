"""Regression coverage for the 2026-11-18 #8603 review findings.

1. ``_sqlite_content_fingerprint()`` is built from ``MAX(rowid)``. An Agent
   rewind marks removed rows ``active = 0`` in place, so the fingerprint never
   moves and the failure-summary cache kept returning a failure for a turn the
   transcript no longer has.
2. ``Session.save()`` reached the index rebuild — which loads every sidecar and
   reads state.db — while already holding ``_INDEX_WRITE_LOCK``, parking every
   unrelated save behind a busy database.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import OrderedDict

import api.models as models


def _make_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, "
        "message_count INTEGER, started_at REAL)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, "
        "tool_calls TEXT, tool_name TEXT, active INTEGER, "
        "display_kind TEXT, display_metadata TEXT)"
    )
    conn.commit()
    return conn


def _insert_failed_turn(conn, sid="sess-rewind", row_id=1):
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp, "
        "active, display_kind, display_metadata) VALUES (?,?,?,?,?,?,?,?)",
        (
            row_id,
            sid,
            "assistant",
            "Your request was not processed.",
            1000.0,
            1,
            "failed_turn",
            json.dumps(
                {
                    "error": "HTTP 402",
                    "error_surface": {"layer": "billing", "code": "billing"},
                }
            ),
        ),
    )
    conn.commit()


def _install_session_store(monkeypatch, tmp_path):
    import api.config as config
    import api.profiles as profiles

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    index_file = session_dir / "_index.json"
    for module in (config, models):
        monkeypatch.setattr(module, "STATE_DIR", tmp_path, raising=False)
        monkeypatch.setattr(module, "SESSION_DIR", session_dir, raising=False)
        monkeypatch.setattr(module, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    models._SESSION_FAILURE_SUMMARY_CACHE.clear()
    models._SESSION_FAILURE_FINGERPRINT_CACHE.clear()
    return session_dir, index_file


def test_rewind_invalidates_the_cached_failure_after_the_recheck_interval(
    monkeypatch, tmp_path
):
    db = tmp_path / "state.db"
    conn = _make_db(db)
    _insert_failed_turn(conn)
    sid = "sess-rewind"

    summary, _revision, answered = models._session_failure_state_from_state_db(sid, db)
    assert answered is True
    assert summary and summary.get("kind") == "failed_turn"

    # A second read within the interval is answered from the cache.
    summary_cached, _rev, answered_cached = models._session_failure_state_from_state_db(
        sid, db
    )
    assert answered_cached is True
    assert summary_cached and summary_cached.get("kind") == "failed_turn"

    # The Agent rewind marks the rows inactive WITHOUT inserting: MAX(rowid),
    # and therefore the content fingerprint, stays exactly where it was.
    before = models._sqlite_content_fingerprint(db)
    conn.execute("UPDATE messages SET active = 0 WHERE session_id = ?", (sid,))
    conn.commit()
    after = models._sqlite_content_fingerprint(db)
    assert after == before, "a rewind must not move the rowid fingerprint"

    # Age the cached verdict past the recheck interval.
    key = (str(db), sid)
    entry = models._SESSION_FAILURE_SUMMARY_CACHE.get(key)
    assert entry is not None
    models._SESSION_FAILURE_SUMMARY_CACHE[key] = (
        entry[0], entry[1], entry[2], time.monotonic() - 10.0,
    )

    summary_after, _rev, answered_after = models._session_failure_state_from_state_db(
        sid, db
    )
    assert answered_after is True, (
        "the rewound session is decisively healthy, not unanswered"
    )
    assert summary_after is None, "the cached failure must not outlive its turn"

    # A session with no row at all still cannot speak (silence is not health).
    conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
    conn.commit()
    blank, _rev, blank_answered = models._session_failure_state_from_state_db(sid, db)
    assert blank is None
    assert blank_answered is False
    conn.close()


def test_save_rebuilds_a_missing_index_outside_the_write_lock(monkeypatch, tmp_path):
    session_dir, index_file = _install_session_store(monkeypatch, tmp_path)
    monkeypatch.setattr(
        models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False
    )

    session = models.Session(
        session_id="sess-save-8603",
        title="save rebuild",
        messages=[{"role": "user", "content": "hello", "timestamp": 1.0}],
        created_at=1.0,
        updated_at=2.0,
    )
    session.save()
    assert index_file.exists()
    index_file.unlink()

    seen = {}
    real_collect = models._collect_session_index_entries

    def spy(session_dir_arg):
        seen["lock_owned"] = models._INDEX_WRITE_LOCK._is_owned()
        return real_collect(session_dir_arg)

    monkeypatch.setattr(models, "_collect_session_index_entries", spy)
    session.save()

    assert "lock_owned" in seen, "the missing index must trigger a rebuild"
    assert seen["lock_owned"] is False, (
        "the rebuild's sidecar/state.db loads must run outside the save lock"
    )
    assert index_file.exists(), "the rebuild must still publish the index"
