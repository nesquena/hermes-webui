"""Regression coverage for the cron sidebar-projection I/O blowup (#4842).

`get_cli_sessions()` projects state.db rows into sidebar entries in two passes:
a visible-window pass and a higher-capped (CRON_PROJECT_CHIP_LIMIT=200) cron-only
second pass. For EACH projected row the old code did three pieces of redundant
per-row I/O:

  1. `_state_projection_sidecar_metadata(sid)` — an uncached open() + 64KB read +
     JSON-key scan of the session's sidecar JSON.
  2. `get_last_workspace()` — up to two file reads + an is_dir()/remote probe,
     returning the SAME active workspace for every row.
  3. A full read + parse of `cron/jobs.json` per untitled cron row.

On a cron-heavy profile (the #4842 reporter had 200+ cron sessions) that was
hundreds of file reads per `/api/sessions` build, and because the enclosing
`_CLI_SESSIONS_CACHE` is keyed on a state.db content fingerprint that advances
on every streamed message row, the whole scan was re-paid on essentially every
streaming poll during a live turn — pinning CPU to 100% and making `get_cli_sessions`
take multiple seconds (#4842 / #4808 / #4672).

These tests pin the fix: the expensive per-row work is now O(unique files), the
sidecar parse is memoized across builds (so a streaming re-poll stays warm), and
the jobs.json parse + workspace resolve happen once per build.
"""

import pathlib
import sqlite3
import time
from unittest import mock

import api.models as models

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _make_cron_state_db(path, *, cron_count=30):
    """state.db with a batch of titled cron sessions."""
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
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    now = time.time()
    for i in range(cron_count):
        sid = f"cron_job{i:04d}_{int(now) + i}"
        started = now + i
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model,"
            " started_at, message_count, parent_session_id, ended_at, end_reason)"
            " VALUES (?, 'cron', 'cron', NULL, 'deepseek/deepseek-chat', ?, 1,"
            " NULL, NULL, NULL)",
            (sid, started),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp)"
            " VALUES (?, ?, 'user', 'cron task', ?)",
            (f"cron_msg_{i:04d}", sid, started),
        )
    conn.commit()
    conn.close()


def test_sidecar_metadata_cached_across_rebuilds(tmp_path):
    """The expensive sidecar read happens once per file, not once per build.

    Simulates the streaming re-poll: build the projection twice (the inner CLI
    cache is bypassed here by calling _load_cli_sessions_uncached directly, which
    is exactly what happens when the state.db fingerprint busts the 5s cache on
    every streamed token). The second build must NOT re-read sidecars.
    """
    models.clear_sidecar_metadata_cache()
    db = tmp_path / "state.db"
    _make_cron_state_db(db, cron_count=30)

    # Give every cron session a real WebUI sidecar JSON so the projection's
    # load_metadata_only() path actually opens a file per row.
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sids = []
    conn = sqlite3.connect(str(db))
    for (sid,) in conn.execute("SELECT id FROM sessions"):
        sids.append(sid)
        (session_dir / f"{sid}.json").write_text(
            '{"session_id": "%s", "title": "Renamed %s", "created_at": 1.0,'
            ' "updated_at": 2.0, "archived": false, "messages": []}' % (sid, sid),
            encoding="utf-8",
        )
    conn.close()

    real_prefix = models._read_metadata_json_prefix
    reads = {"n": 0}

    def _counting_prefix(p, *a, **k):
        reads["n"] += 1
        return real_prefix(p, *a, **k)

    with (
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch("api.models.get_last_workspace", return_value=str(tmp_path)),
        mock.patch("api.models.ensure_cron_project", return_value="cron-pid"),
        mock.patch("api.models.SESSION_DIR", session_dir),
        mock.patch("api.models._read_metadata_json_prefix", side_effect=_counting_prefix),
    ):
        first = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None)
        cold_reads = reads["n"]
        second = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None)

    # Cold build read each sidecar (at least once per unique file).
    assert cold_reads >= len(sids)
    # Warm rebuild re-stats but does NOT re-read any sidecar — the streaming
    # re-poll no longer re-pays the per-row file read.
    assert reads["n"] == cold_reads, (
        f"warm rebuild re-read sidecars ({reads['n'] - cold_reads} extra reads); "
        "the projection cache is not engaging across builds"
    )
    # The renamed sidecar title is projected onto the cron rows in both builds.
    titles_first = {s["session_id"]: s["title"] for s in first}
    titles_second = {s["session_id"]: s["title"] for s in second}
    assert titles_first == titles_second
    assert any(t.startswith("Renamed ") for t in titles_first.values())


def test_sidecar_cache_invalidates_on_rename(tmp_path):
    """A sidecar rename/archive bumps the stat signature → fresh read, fresh title."""
    models.clear_sidecar_metadata_cache()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sid = "cron_jobAAAA_111"
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(
        '{"session_id": "%s", "title": "First", "created_at": 1.0,'
        ' "updated_at": 2.0, "archived": false, "messages": []}' % sid,
        encoding="utf-8",
    )

    with mock.patch("api.models.SESSION_DIR", session_dir):
        first = models._state_projection_sidecar_metadata(sid)
        assert first["title"] == "First"
        assert first["archived"] is False

        # Rewrite with a new title + archived flag; sleep so ctime/mtime move.
        time.sleep(0.02)
        sidecar.write_text(
            '{"session_id": "%s", "title": "Renamed", "created_at": 1.0,'
            ' "updated_at": 3.0, "archived": true, "messages": []}' % sid,
            encoding="utf-8",
        )
        second = models._state_projection_sidecar_metadata(sid)

    assert second["title"] == "Renamed"
    assert second["archived"] is True


def test_sidecar_metadata_returns_independent_copies(tmp_path):
    """A caller mutating the returned dict must not corrupt the cached entry."""
    models.clear_sidecar_metadata_cache()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sid = "cron_jobBBBB_222"
    (session_dir / f"{sid}.json").write_text(
        '{"session_id": "%s", "title": "Keep", "created_at": 1.0,'
        ' "updated_at": 2.0, "archived": false, "messages": []}' % sid,
        encoding="utf-8",
    )
    with mock.patch("api.models.SESSION_DIR", session_dir):
        first = models._state_projection_sidecar_metadata(sid)
        first["title"] = "MUTATED"
        first["archived"] = True
        second = models._state_projection_sidecar_metadata(sid)
    assert second["title"] == "Keep"
    assert second["archived"] is False


def test_missing_sidecar_returns_default_without_caching_growth(tmp_path):
    """A pure state.db row with no sidecar returns the default and is cheap."""
    models.clear_sidecar_metadata_cache()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    with mock.patch("api.models.SESSION_DIR", session_dir):
        meta = models._state_projection_sidecar_metadata("cron_nope_999")
    assert meta == {"title": None, "archived": False}
    # No file → nothing cached (so the cache can't be poisoned by absent files).
    assert len(models._SIDECAR_METADATA_CACHE) == 0


def test_jobs_json_parsed_once_per_build(tmp_path):
    """cron/jobs.json is read+parsed once per build, not once per untitled row."""
    models.clear_sidecar_metadata_cache()
    db = tmp_path / "state.db"
    _make_cron_state_db(db, cron_count=25)

    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    jobs = {"jobs": [{"id": f"job{i:04d}", "name": f"My Job {i}"} for i in range(25)]}
    import json as _json
    (cron_dir / "jobs.json").write_text(_json.dumps(jobs), encoding="utf-8")

    real_read_text = pathlib.Path.read_text
    jobs_reads = {"n": 0}

    def _counting_read_text(self, *a, **k):
        if self.name == "jobs.json":
            jobs_reads["n"] += 1
        return real_read_text(self, *a, **k)

    with (
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch("api.models.get_last_workspace", return_value=str(tmp_path)),
        mock.patch("api.models.ensure_cron_project", return_value="cron-pid"),
        mock.patch("api.models.Session.load_metadata_only", return_value=None),
        mock.patch.object(pathlib.Path, "read_text", _counting_read_text),
    ):
        result = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None)

    # jobs.json read at most once despite 25 untitled cron rows across both passes.
    assert jobs_reads["n"] <= 1, f"jobs.json read {jobs_reads['n']} times (expected <=1)"
    # The friendly job names were still applied.
    titles = {s["title"] for s in result if s["source_tag"] == "cron"}
    assert any(t.startswith("My Job ") for t in titles)


def test_get_last_workspace_called_once_per_build(tmp_path):
    """get_last_workspace() is resolved once, not once per projected row."""
    models.clear_sidecar_metadata_cache()
    db = tmp_path / "state.db"
    _make_cron_state_db(db, cron_count=20)

    ws_calls = {"n": 0}

    def _counting_ws():
        ws_calls["n"] += 1
        return str(tmp_path)

    with (
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch("api.models.get_last_workspace", side_effect=_counting_ws),
        mock.patch("api.models.ensure_cron_project", return_value="cron-pid"),
        mock.patch("api.models.Session.load_metadata_only", return_value=None),
    ):
        result = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None)

    assert len(result) > 0
    # One resolve for the whole build (lazy, memoized) regardless of row count.
    assert ws_calls["n"] <= 1, (
        f"get_last_workspace() called {ws_calls['n']} times for {len(result)} rows"
    )


def test_clear_cli_sessions_cache_also_clears_sidecar_cache(tmp_path):
    """The lifecycle reset clears the sidecar projection cache too."""
    models.clear_sidecar_metadata_cache()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sid = "cron_jobCCCC_333"
    (session_dir / f"{sid}.json").write_text(
        '{"session_id": "%s", "title": "X", "created_at": 1.0,'
        ' "updated_at": 2.0, "archived": false, "messages": []}' % sid,
        encoding="utf-8",
    )
    with mock.patch("api.models.SESSION_DIR", session_dir):
        models._state_projection_sidecar_metadata(sid)
        assert len(models._SIDECAR_METADATA_CACHE) == 1
        models.clear_cli_sessions_cache()
        assert len(models._SIDECAR_METADATA_CACHE) == 0
