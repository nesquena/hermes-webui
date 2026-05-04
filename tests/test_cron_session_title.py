"""Tests for cron session title fallback in get_cli_sessions().

When a CLI session originates from cron and has no title in state.db, the
WebUI sidebar should display the human-friendly job name from cron/jobs.json
instead of a generic "Cron Session" label.

Session ID format produced by hermes-agent: cron_<job_id>_<YYYYMMDD>_<HHMMSS>
"""
import json
import sqlite3

import pytest

import api.models as models


def _make_state_db(path, sessions):
    """Create a state.db with the schema get_cli_sessions() expects.

    `sessions` is a list of (id, title, source) tuples.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            message_count INTEGER,
            started_at REAL,
            source TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp REAL
        )
    """)
    for sid, title, source in sessions:
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, title, "gpt-x", 1, 1700000000.0, source),
        )
        conn.execute(
            "INSERT INTO messages (session_id, timestamp) VALUES (?, ?)",
            (sid, 1700000001.0),
        )
    conn.commit()
    conn.close()


def _write_jobs_json(hermes_home, jobs):
    """Write cron/jobs.json with the given jobs list."""
    cron_dir = hermes_home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": jobs}), encoding="utf-8"
    )


@pytest.fixture
def fake_hermes_home(tmp_path, monkeypatch):
    """Point get_cli_sessions() at a temporary HERMES_HOME and disable
    profile lookups so the test runs hermetically."""
    home = tmp_path / "hermes"
    home.mkdir()

    # Both profile helpers are imported lazily inside get_cli_sessions(),
    # so patching the api.profiles module reaches them.
    import api.profiles as profiles
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: None)

    return home


def test_cron_session_uses_job_name_when_title_missing(fake_hermes_home):
    """A cron session with no title should display the friendly job name."""
    _write_jobs_json(fake_hermes_home, [
        {"id": "cd65df6fc1a8", "name": "wiki-auto-ingest"},
    ])
    _make_state_db(fake_hermes_home / "state.db", [
        ("cron_cd65df6fc1a8_20260417_191049", None, "cron"),
    ])

    sessions = models.get_cli_sessions()

    assert len(sessions) == 1
    assert sessions[0]["title"] == "wiki-auto-ingest"


def test_cron_session_falls_back_when_jobs_json_missing(fake_hermes_home):
    """No jobs.json should not crash; title falls back to 'Cron Session'."""
    _make_state_db(fake_hermes_home / "state.db", [
        ("cron_abc123_20260417_191049", None, "cron"),
    ])

    sessions = models.get_cli_sessions()

    assert sessions[0]["title"] == "Cron Session"


def test_cron_session_falls_back_when_job_id_not_in_jobs_json(fake_hermes_home):
    """Stale session whose job has been deleted falls back gracefully."""
    _write_jobs_json(fake_hermes_home, [
        {"id": "different_job", "name": "Some Other Job"},
    ])
    _make_state_db(fake_hermes_home / "state.db", [
        ("cron_orphan_20260417_191049", None, "cron"),
    ])

    sessions = models.get_cli_sessions()

    assert sessions[0]["title"] == "Cron Session"


def test_explicit_title_is_preserved(fake_hermes_home):
    """If state.db already has a title, the cron job lookup should not
    override it."""
    _write_jobs_json(fake_hermes_home, [
        {"id": "cd65df6fc1a8", "name": "wiki-auto-ingest"},
    ])
    _make_state_db(fake_hermes_home / "state.db", [
        ("cron_cd65df6fc1a8_20260417_191049", "User-edited title", "cron"),
    ])

    sessions = models.get_cli_sessions()

    assert sessions[0]["title"] == "User-edited title"


def test_non_cron_sessions_unaffected(fake_hermes_home):
    """The cron-name lookup must not run for cli-source sessions, so the
    generic 'Cli Session' fallback still applies when title is empty."""
    _write_jobs_json(fake_hermes_home, [
        {"id": "cd65df6fc1a8", "name": "wiki-auto-ingest"},
    ])
    # A 'cli' session whose ID coincidentally starts with 'cron_' must not
    # pick up the job name — the source check guards against this.
    _make_state_db(fake_hermes_home / "state.db", [
        ("cron_cd65df6fc1a8_xx", None, "cli"),
    ])
    # PR #1587 hides one-off default-titled CLI rows. Keep this fixture visible
    # so the test remains focused on the cron-name guard rather than sidebar
    # filtering.
    conn = sqlite3.connect(str(fake_hermes_home / "state.db"))
    conn.execute(
        "INSERT INTO messages (session_id, timestamp) VALUES (?, ?)",
        ("cron_cd65df6fc1a8_xx", 1700000002.0),
    )
    conn.commit()
    conn.close()

    sessions = models.get_cli_sessions()

    assert sessions[0]["title"] == "Cli Session"
