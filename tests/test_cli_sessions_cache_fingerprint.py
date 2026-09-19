"""Regression: a freshly-committed state.db session must invalidate the CLI-session
cache immediately, even when the file-stat stamp would collide (the root cause of
the recurring test_gateway_sync flake).

The CLI-session cache (_CLI_SESSIONS_CACHE) and the session-list cache were keyed
on (st_mtime_ns, st_size) of state.db + its WAL sidecars. Under WAL-mode writes
those stamps can collide, serving a stale cache. The fix adds a commit-reliable
content fingerprint (_sqlite_content_fingerprint) that advances on every commit.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


def _use_active_home(monkeypatch, tmp_path):
    from api import models, profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        models,
        "_default_claude_code_projects_dir",
        lambda: tmp_path / "claude-projects",
    )
    return models


def _open_wal_projects_db(projects_db: Path) -> sqlite3.Connection:
    writer = sqlite3.connect(projects_db)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    writer.execute("INSERT INTO projects VALUES ('project-1', 'Project One')")
    writer.commit()
    return writer


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unsupported: {exc}")


def test_single_profile_cache_key_tracks_projects_db_creation_and_change(
    monkeypatch, tmp_path
):
    models = _use_active_home(monkeypatch, tmp_path)
    projects_db = tmp_path / "projects.db"

    assert not projects_db.exists()
    key_missing = models._resolve_cli_sessions_context()[3]

    projects_db.write_bytes(b"first")
    key_created = models._resolve_cli_sessions_context()[3]

    projects_db.write_bytes(b"second-longer")
    key_changed = models._resolve_cli_sessions_context()[3]

    assert key_missing != key_created
    assert key_created != key_changed


def test_projects_db_change_invalidates_fixed_streaming_cache_key(monkeypatch, tmp_path):
    models = _use_active_home(monkeypatch, tmp_path)
    monkeypatch.setattr(models, "_active_stream_ids", lambda: {"fixed-stream"})
    projects_db = tmp_path / "projects.db"
    projects_db.write_bytes(b"first")

    key_before = models._resolve_cli_sessions_context()[3]
    projects_db.write_bytes(b"second-longer")
    key_after = models._resolve_cli_sessions_context()[3]

    assert key_before != key_after


def test_projects_db_wal_cache_key_is_stable_and_read_only(monkeypatch, tmp_path):
    models = _use_active_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        models,
        "_cli_sessions_streaming_freeze_marker",
        lambda: ("streaming", ("fixed-stream",)),
    )
    projects_db = tmp_path / "projects.db"
    writer = _open_wal_projects_db(projects_db)
    try:
        paths = [
            projects_db,
            Path(f"{projects_db}-wal"),
            Path(f"{projects_db}-shm"),
        ]

        def snapshot():
            result = {}
            for path in paths:
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    result[path.name] = None
                else:
                    result[path.name] = (
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                        stat.st_size,
                    )
            return result

        assert paths[1].stat().st_size > 0
        before = snapshot()
        key_first = models._resolve_cli_sessions_context()[3]
        between = snapshot()
        key_second = models._resolve_cli_sessions_context()[3]
        after = snapshot()

        assert key_first == key_second
        assert before == between == after

        writer.execute("INSERT INTO projects VALUES ('project-2', 'Project Two')")
        writer.commit()

        key_after_write = models._resolve_cli_sessions_context()[3]
        assert key_after_write != key_second
    finally:
        writer.close()


def test_projects_db_cache_key_excludes_shm(monkeypatch, tmp_path):
    models = _use_active_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        models,
        "_cli_sessions_streaming_freeze_marker",
        lambda: ("streaming", ("fixed-stream",)),
    )
    projects_db = tmp_path / "projects.db"
    writer = _open_wal_projects_db(projects_db)
    try:
        shm_path = Path(f"{projects_db}-shm")
        assert shm_path.exists()
        key_before = models._resolve_cli_sessions_context()[3]
        shm_stat = shm_path.stat()

        os.utime(
            shm_path,
            ns=(shm_stat.st_atime_ns, shm_stat.st_mtime_ns + 1_000_000_000),
        )

        key_after = models._resolve_cli_sessions_context()[3]
        assert key_after == key_before
    finally:
        writer.close()


def test_missing_projects_db_cache_fingerprint_is_read_only(monkeypatch, tmp_path):
    models = _use_active_home(monkeypatch, tmp_path)
    projects_db = tmp_path / "projects.db"

    fingerprint_first = models._projects_db_stat_cache_key(projects_db)
    fingerprint_second = models._projects_db_stat_cache_key(projects_db)
    key_first = models._resolve_cli_sessions_context()[3]
    key_second = models._resolve_cli_sessions_context()[3]

    assert fingerprint_first == fingerprint_second == (None, None)
    assert key_first == key_second
    assert not projects_db.exists()
    assert not Path(f"{projects_db}-wal").exists()
    assert not Path(f"{projects_db}-shm").exists()


def test_projects_db_cache_fingerprint_ignores_empty_wal(monkeypatch, tmp_path):
    models = _use_active_home(monkeypatch, tmp_path)
    projects_db = tmp_path / "projects.db"
    projects_db.write_bytes(b"project-data")

    without_wal = models._projects_db_stat_cache_key(projects_db)
    Path(f"{projects_db}-wal").touch()
    with_empty_wal = models._projects_db_stat_cache_key(projects_db)

    assert with_empty_wal == without_wal


def test_projects_db_cache_fingerprint_does_not_follow_main_symlink(
    monkeypatch, tmp_path
):
    models = _use_active_home(monkeypatch, tmp_path)
    outside_db = tmp_path.parent / f"{tmp_path.name}-outside-projects.db"
    outside_db.write_bytes(b"outside-project-data")
    projects_db = tmp_path / "projects.db"
    _symlink_or_skip(projects_db, outside_db)

    key_before = models._projects_db_stat_cache_key(projects_db)
    outside_db.write_bytes(b"changed-outside-project-data-and-metadata")
    key_after = models._projects_db_stat_cache_key(projects_db)

    assert key_after == key_before
    assert key_before[0][0] == "non-regular"
    assert str(outside_db) not in repr(key_before)


def test_projects_db_cache_fingerprint_ignores_symlinked_wal(
    monkeypatch, tmp_path
):
    models = _use_active_home(monkeypatch, tmp_path)
    projects_db = tmp_path / "projects.db"
    projects_db.write_bytes(b"project-data")
    outside_wal = tmp_path.parent / f"{tmp_path.name}-outside-projects.db-wal"
    outside_wal.write_bytes(b"outside-wal-data")
    _symlink_or_skip(Path(f"{projects_db}-wal"), outside_wal)

    key_before = models._projects_db_stat_cache_key(projects_db)
    outside_wal.write_bytes(b"changed-outside-wal-data-and-metadata")
    key_after = models._projects_db_stat_cache_key(projects_db)

    assert key_after == key_before
    assert key_before[1] is None
    assert str(outside_wal) not in repr(key_before)


def test_content_fingerprint_advances_on_commit():
    """The cache key's content fingerprint must change after any commit, even
    when mtime/size would not reliably change (the WAL-collision flake source).
    """
    from api.models import _sqlite_file_stat_cache_key, _sqlite_content_fingerprint

    d = tempfile.mkdtemp()
    p = Path(d) / "state.db"
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE sessions(id TEXT, source TEXT, started_at REAL)")
    conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT)")
    conn.commit()

    fp_before = _sqlite_content_fingerprint(p)
    key_before = _sqlite_file_stat_cache_key(p)

    conn.execute("INSERT INTO sessions VALUES ('gw_new_001', 'weixin', 1)")
    conn.execute("INSERT INTO messages (session_id) VALUES ('gw_new_001')")
    conn.commit()

    fp_after = _sqlite_content_fingerprint(p)
    key_after = _sqlite_file_stat_cache_key(p)
    conn.close()

    assert fp_before != fp_after, (
        "content fingerprint must advance after a commit so a freshly-inserted "
        "CLI/gateway session is never served from a stale cache"
    )
    # The full cache key (which embeds the fingerprint) must therefore differ too.
    assert key_before != key_after


def test_content_fingerprint_detects_message_only_change():
    """An in-place session row update or message-only insert must also change the
    fingerprint (sessions COUNT/MAX alone could miss a same-rowid REPLACE).
    """
    from api.models import _sqlite_content_fingerprint

    d = tempfile.mkdtemp()
    p = Path(d) / "state.db"
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE sessions(id TEXT, source TEXT, started_at REAL)")
    conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 'weixin', 1)")
    conn.commit()

    fp_before = _sqlite_content_fingerprint(p)
    # Add a message to the existing session (sessions table unchanged).
    conn.execute("INSERT INTO messages (session_id) VALUES ('s1')")
    conn.commit()
    fp_after = _sqlite_content_fingerprint(p)
    conn.close()

    assert fp_before != fp_after, (
        "fingerprint must cover the messages table so a message-only commit "
        "(e.g. a continued gateway conversation) invalidates the cache"
    )


def test_content_fingerprint_safe_on_missing_or_empty_db():
    """The fingerprint must not raise on a missing path or a db without the
    expected tables — it returns None / zeroed parts so the stat fallback applies.
    """
    from api.models import _sqlite_content_fingerprint

    assert _sqlite_content_fingerprint(Path("/nonexistent/state.db")) is None

    d = tempfile.mkdtemp()
    p = Path(d) / "empty.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE unrelated(x)")
    conn.commit()
    conn.close()
    # No sessions/messages tables → None parts (MAX(rowid) on a missing table),
    # no exception. Shape is a 2-tuple of (sessions_max_rowid, messages_max_rowid).
    fp = _sqlite_content_fingerprint(p)
    assert fp == (None, None)
