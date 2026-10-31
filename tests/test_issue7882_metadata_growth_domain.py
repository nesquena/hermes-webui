"""Regression test for the #8368 re-gate: the metadata polling growth signal.

`get_state_db_session_summary()` is a growth signal, not a display count: its
consumers compare the result against a count that spans every persisted row
and only expose a newer transcript when `state > sidecar`. Filtering
`active = 0` archive rows out of that aggregate changed the count domain while
the consumers' side stayed full, so a session whose state.db rows had been
compacted away stopped reporting external growth: the metadata-only poll kept
answering with the stale sidecar count and timestamp while a full load already
saw the appended turn.

These tests drive the real consumer (`_metadata_only_message_summary`) against
a real SQLite store, with and without archived rows, and keep the
intentional-truncation authority next to them.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import api.models as models  # noqa: E402
import api.routes as routes  # noqa: E402


def _make_growth_db(path, *, archived: int, active: int, start_ts: float) -> dict:
    """Write a state.db where the first ``archived`` rows are compacted away.

    Returns the expected (raw_count, last_timestamp) pair the growth signal
    must report.
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, session_source TEXT, "
        "title TEXT, message_count INTEGER, started_at REAL, ended_at REAL)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "content TEXT, timestamp REAL, active INTEGER)"
    )
    rows = []
    ts = start_ts
    last_ts = start_ts
    for idx in range(archived + active):
        role = "user" if idx % 2 == 0 else "assistant"
        is_active = 1 if idx >= archived else 0
        rows.append(("sess-grow", role, f"m{idx}", ts, is_active))
        last_ts = ts
        ts += 1.0
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp, active) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, message_count, started_at) "
        "VALUES ('sess-grow', 'cli', 'cli', 'Growth', ?, ?)",
        (archived + active, start_ts),
    )
    conn.commit()
    conn.close()
    return {"raw_count": archived + active, "last_ts": last_ts}


def _point_models_at(monkeypatch, db):
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "_strict_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "_agent_state_db_path", lambda *, profile=None: db)


class _SidecarStub:
    """Minimal stand-in for a metadata-only sidecar load."""

    def __init__(self, count: int, updated_at: float, watermark=None):
        self._metadata_message_count = count
        self.updated_at = updated_at
        self.truncation_watermark = watermark

    def compact(self):
        return {"message_count": self._metadata_message_count}


def _patch_sidecar(monkeypatch, stub):
    monkeypatch.setattr(
        routes.Session, "load_metadata_only", staticmethod(lambda sid: stub)
    )


def test_metadata_summary_reports_growth_after_compaction(tmp_path, monkeypatch):
    """Four archived rows must not hide the two appended ones from the poll."""
    db = tmp_path / "state.db"
    expected = _make_growth_db(db, archived=4, active=4, start_ts=1000.0)
    _point_models_at(monkeypatch, db)
    # The sidecar still holds the six pre-archive rows and predates the append.
    _patch_sidecar(monkeypatch, _SidecarStub(count=6, updated_at=1004.0))

    summary = routes._metadata_only_message_summary("sess-grow")

    assert summary["message_count"] == expected["raw_count"] == 8
    assert summary["last_message_at"] == expected["last_ts"] > 1004.0


def test_metadata_summary_reports_growth_without_archives(tmp_path, monkeypatch):
    """Control: with no archived rows the same scene already reported growth."""
    db = tmp_path / "state.db"
    expected = _make_growth_db(db, archived=0, active=8, start_ts=1000.0)
    _point_models_at(monkeypatch, db)
    _patch_sidecar(monkeypatch, _SidecarStub(count=6, updated_at=1004.0))

    summary = routes._metadata_only_message_summary("sess-grow")

    assert summary["message_count"] == expected["raw_count"] == 8
    assert summary["last_message_at"] == expected["last_ts"]


def test_metadata_summary_keeps_the_truncation_watermark_authoritative(
    tmp_path, monkeypatch
):
    """The cheap path must still never resurrect a deliberately truncated turn."""
    db = tmp_path / "state.db"
    _make_growth_db(db, archived=4, active=4, start_ts=1000.0)
    _point_models_at(monkeypatch, db)
    _patch_sidecar(
        monkeypatch, _SidecarStub(count=6, updated_at=1004.0, watermark=1004.0)
    )

    summary = routes._metadata_only_message_summary("sess-grow")

    assert summary == {"message_count": 6, "last_message_at": 1004.0}
