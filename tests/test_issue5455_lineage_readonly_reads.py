"""Regression tests for #5455 — extend read-only state.db opens to the
remaining pure-read projections.

The session-listing path already opens the live agent ``state.db`` read-only
(``file:...?mode=ro``) so a write-capable handle doesn't add checkpoint/lock
surface while the agent streams into the same WAL DB
(see ``test_issue5455_listing_readonly_connection``). The lineage-report and
lineage-metadata reads, and the gateway-watcher fingerprint projection (a 5s
poll), were still opening a read-WRITE connection. This shared them onto the
same ``open_state_db_readonly`` helper.
"""
import logging
import sqlite3
from contextlib import closing

import pytest

import api.agent_sessions as agent_sessions
from api.agent_sessions import (
    open_state_db_readonly,
    read_session_lineage_metadata,
    read_session_lineage_report,
)


def _make_lineage_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT,
            source TEXT, session_source TEXT, title TEXT,
            started_at REAL, ended_at REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO sessions (id, parent_session_id, end_reason, source, session_source, "
        "title, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("parent-1", None, "compressed", "cli", "cli", "Root", 1000.0, 1100.0),
            ("child-1", "parent-1", None, "cli", "cli", "Cont", 1100.0, None),
        ],
    )
    conn.commit()
    conn.close()


def _record_connects(monkeypatch):
    """Spy on agent_sessions.sqlite3.connect, recording target + uri per call."""
    real_connect = sqlite3.connect
    calls = []

    def spy(target, *args, **kwargs):
        calls.append({"target": str(target), "uri": bool(kwargs.get("uri"))})
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", spy)
    return calls


# ── open_state_db_readonly (the shared helper) ───────────────────────────────

def test_helper_opens_read_only_uri(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_lineage_db(db)
    calls = _record_connects(monkeypatch)

    with closing(open_state_db_readonly(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2

    assert calls and calls[0]["uri"] is True
    assert "mode=ro" in calls[0]["target"]


def test_helper_connection_rejects_writes(tmp_path):
    db = tmp_path / "state.db"
    _make_lineage_db(db)
    with closing(open_state_db_readonly(db)) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO sessions (id) VALUES ('x')")


def test_helper_encodes_special_path_chars(tmp_path, monkeypatch):
    db_dir = tmp_path / "state dir #1"
    db_dir.mkdir()
    db = db_dir / "state?.db"
    _make_lineage_db(db)
    calls = _record_connects(monkeypatch)

    with closing(open_state_db_readonly(db)):
        pass

    assert calls[0]["target"].startswith("file://")
    assert "%20" in calls[0]["target"]  # space
    assert "%23" in calls[0]["target"]  # '#'
    assert "%3F" in calls[0]["target"]  # '?'
    assert calls[0]["target"].endswith("?mode=ro")


def test_helper_falls_back_to_writable_and_logs(tmp_path, monkeypatch, caplog):
    db = tmp_path / "state.db"
    _make_lineage_db(db)
    real_connect = sqlite3.connect

    def fail_read_only(target, *args, **kwargs):
        if kwargs.get("uri"):
            raise sqlite3.OperationalError("synthetic read-only URI failure")
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", fail_read_only)
    with caplog.at_level(logging.WARNING, logger="api.agent_sessions"):
        conn = open_state_db_readonly(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    finally:
        conn.close()
    assert "read-only open failed" in caplog.text
    assert "synthetic read-only URI failure" in caplog.text


def test_helper_raises_on_missing_db_instead_of_creating_a_ghost(tmp_path):
    """A missing path must raise FileNotFoundError, not have the writable
    fallback silently create an empty state.db there."""
    missing = tmp_path / "does_not_exist" / "state.db"
    with pytest.raises(FileNotFoundError):
        open_state_db_readonly(missing)
    assert not missing.exists(), "helper created a ghost state.db for a missing path"


# ── lineage reads route through the helper ───────────────────────────────────

def test_lineage_report_opens_read_only(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_lineage_db(db)
    calls = _record_connects(monkeypatch)

    report = read_session_lineage_report(db, "child-1")

    assert report  # a non-empty report came back
    assert calls and calls[0]["uri"] is True
    assert "mode=ro" in calls[0]["target"]


def test_lineage_metadata_opens_read_only(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_lineage_db(db)
    calls = _record_connects(monkeypatch)

    meta = read_session_lineage_metadata(db, ["child-1", "parent-1"])

    assert isinstance(meta, dict)
    assert calls and calls[0]["uri"] is True
    assert "mode=ro" in calls[0]["target"]


# ── gateway-watcher fingerprint projection routes through the helper ─────────

def test_gateway_watcher_fingerprint_opens_read_only(tmp_path, monkeypatch):
    # The 5s watcher poll (_cheap_change_fingerprint) calls open_state_db_readonly,
    # which resolves sqlite3.connect in agent_sessions' namespace — so the same
    # spy catches it. The DB needs a `source` column or the fingerprint bails to None.
    import api.gateway_watcher as gateway_watcher

    db = tmp_path / "state.db"
    _make_lineage_db(db)
    calls = _record_connects(monkeypatch)

    fp = gateway_watcher._cheap_change_fingerprint(db)

    assert fp is not None  # a fingerprint (not the schema-bail None) was produced
    assert calls and calls[0]["uri"] is True
    assert "mode=ro" in calls[0]["target"]
