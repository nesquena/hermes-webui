"""Availability regressions retained by PR #7555 after #7445.

Master owns the strict read-only state.db connection layer. These tests cover the
remaining cache and multi-profile availability semantics.
"""

import copy
import os
import sqlite3
import time

import pytest

import api.agent_sessions as agent_sessions
import api.models as models


def _make_state_db(path, *, sessions=80, messages_per_session=3, create_messages_index=True, source="cli", session_source="cli"):
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
            project_id TEXT,
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
        """
    )
    base = time.time() - sessions
    for i in range(sessions):
        sid = f"cli_perf_{i:04d}"
        started = base + i
        conn.execute(
            """
            INSERT INTO sessions
            (id, source, session_source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason)
            VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, ?, NULL, NULL, NULL)
            """,
            (sid, source, session_source, sid, started, messages_per_session),
        )
        for j in range(messages_per_session):
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, 'hello', ?)",
                (f"msg_{i:04d}_{j:02d}", sid, "user" if j == 0 else "assistant", started + j / 10),
            )
    if create_messages_index:
        conn.execute("CREATE INDEX idx_messages_session ON messages(session_id, timestamp)")
    conn.commit()
    conn.close()


def _insert_session(path, session_id, *, source="cli", started_at=None):
    started_at = time.time() if started_at is None else started_at
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, session_source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason)
        VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, 1, NULL, NULL, NULL)
        """,
        (session_id, source, source, session_id, started_at),
    )
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'hello', ?)",
        (f"msg-{session_id}", session_id, started_at),
    )
    conn.commit()
    conn.close()


def _assign_project(path, session_id, project_id):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "UPDATE sessions SET project_id = ? WHERE id = ?",
        (project_id, session_id),
    )
    conn.commit()
    conn.close()


def _configure_real_single_profile(monkeypatch, tmp_path, db, *, ttl=60.0):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    projects = tmp_path / "projects.json"
    projects.write_text("[]", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)

    monkeypatch.setattr(models, "PROJECTS_FILE", projects)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "get_last_workspace", lambda profile=None: tmp_path)
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", ttl, raising=False)
    monkeypatch.setattr(
        models,
        "_resolve_cli_sessions_context",
        lambda source_filter=None, **_kwargs: (
            home,
            db,
            "default",
            (
                str(home),
                "default",
                str(db),
                source_filter or "",
                models._sqlite_file_stat_cache_key(db),
                False,
                None,
                None,
                None,
            ),
        ),
    )
    models.clear_cli_sessions_cache()
    return home, projects


def _cli_ids(rows):
    return {
        row["session_id"]
        for row in rows
        if row.get("source_tag") not in {"cron", "webhook", "kanban"}
    }


def test_readonly_projects_file_keeps_cold_primary_rows_visible(tmp_path, monkeypatch):
    """A real projects.json PermissionError is not state.db unavailability."""
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=2, messages_per_session=1)
    _insert_session(db, "webhook-cold", source="webhook")
    _home, projects = _configure_real_single_profile(monkeypatch, tmp_path, db)
    os.chmod(projects, 0o444)

    rows = models.get_cli_sessions()

    assert _cli_ids(rows) == {"cli_perf_0000", "cli_perf_0001"}
    assert isinstance(rows, list)


def test_readonly_projects_file_serves_new_primary_row_after_warm_cache(
    tmp_path, monkeypatch
):
    """A project persistence error must not make a warm cache stale."""
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=1, messages_per_session=1)
    _home, projects = _configure_real_single_profile(monkeypatch, tmp_path, db)

    assert _cli_ids(models.get_cli_sessions()) == {"cli_perf_0000"}
    _insert_session(db, "cli-new-after-warm")
    _insert_session(db, "webhook-after-warm", source="webhook")
    os.chmod(projects, 0o444)

    first_partial = models.get_cli_sessions()
    second_partial = models.get_cli_sessions()

    expected = {"cli_perf_0000", "cli-new-after-warm"}
    assert _cli_ids(first_partial) == expected
    assert _cli_ids(second_partial) == expected


def test_all_profiles_readonly_projects_keeps_affected_and_healthy_primary_rows(
    tmp_path, monkeypatch
):
    """One profile's optional failure must not stale the aggregate projection."""
    homes = [tmp_path / "profile-a", tmp_path / "profile-b"]
    for home in homes:
        home.mkdir()
        _make_state_db(home / "state.db", sessions=1, messages_per_session=1)
    _insert_session(homes[0] / "state.db", "a-webhook", source="webhook")
    projects = tmp_path / "projects.json"
    projects.write_text("[]", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()

    monkeypatch.setattr(models, "PROJECTS_FILE", projects)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "get_last_workspace", lambda profile=None: tmp_path)
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)
    monkeypatch.setattr(
        models,
        "_all_profiles_cli_contexts",
        lambda: (
            [
                (homes[0], homes[0] / "state.db", "a"),
                (homes[1], homes[1] / "state.db", "b"),
            ],
            tuple(
                (
                    str(home),
                    profile,
                    models._sqlite_file_stat_cache_key(home / "state.db"),
                )
                for home, profile in zip(homes, ("a", "b"), strict=True)
            ),
        ),
    )
    models.clear_cli_sessions_cache()
    os.chmod(projects, 0o444)

    warm = models.get_cli_sessions(all_profiles=True)
    assert {(row["profile"], row["session_id"]) for row in warm} == {
        ("a", "cli_perf_0000"),
        ("b", "cli_perf_0000"),
    }

    _insert_session(homes[0] / "state.db", "a-new")
    _insert_session(homes[1] / "state.db", "b-new")

    rows = models.get_cli_sessions(all_profiles=True)

    assert {(row["profile"], row["session_id"]) for row in rows} >= {
        ("a", "cli_perf_0000"),
        ("a", "a-new"),
        ("b", "cli_perf_0000"),
        ("b", "b-new"),
    }
    from api import routes

    assert routes._lookup_cli_session_metadata("a-new", all_profiles=True)["profile"] == "a"


@pytest.mark.parametrize("optional_source", ["cron", "webhook", "kanban"])
def test_real_exclusive_lock_during_optional_pass_keeps_primary_rows(
    tmp_path, monkeypatch, optional_source
):
    """A lock acquired after the primary read yields a fresh incomplete result."""
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=1, messages_per_session=1)
    _insert_session(db, f"{optional_source}-locked", source=optional_source)
    home = tmp_path / "home"
    home.mkdir()
    projects = tmp_path / "projects.json"
    projects.write_text("[]", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "PROJECTS_FILE", projects)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "get_last_workspace", lambda profile=None: tmp_path)

    real_open = agent_sessions.open_state_db_readonly

    def fast_locked_open(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        conn.execute("PRAGMA busy_timeout=20")
        return conn

    monkeypatch.setattr(agent_sessions, "open_state_db_readonly", fast_locked_open)
    real_reader = models.read_importable_agent_session_rows
    lock_conn = None
    calls = 0

    def lock_after_primary(*args, **kwargs):
        nonlocal calls, lock_conn
        calls += 1
        rows = real_reader(*args, **kwargs)
        if calls == 1:
            lock_conn = sqlite3.connect(str(db), isolation_level=None)
            lock_conn.execute("BEGIN EXCLUSIVE")
        return rows

    monkeypatch.setattr(models, "read_importable_agent_session_rows", lock_after_primary)
    try:
        result = models._load_cli_sessions_uncached(
            home,
            db,
            "default",
            project_assigned_limit=False,
            cron_project_limit=None if optional_source == "cron" else False,
            webhook_project_limit=None if optional_source == "webhook" else False,
            kanban_project_limit=None if optional_source == "kanban" else False,
            _with_completeness=True,
        )
    finally:
        if lock_conn is not None:
            lock_conn.rollback()
            lock_conn.close()

    assert result.complete is False
    assert _cli_ids(result.sessions) == {"cli_perf_0000"}


@pytest.mark.parametrize("optional_source", ["cron", "webhook", "kanban"])
def test_all_profiles_real_optional_lock_serves_fresh_rows_without_caching(
    tmp_path, monkeypatch, optional_source
):
    """Carry real optional-read incompleteness through aggregate TTL ownership."""
    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
        _make_state_db(home / "state.db", sessions=1, messages_per_session=1)
    db = homes[0] / "state.db"
    _insert_session(db, "optional-row", source=optional_source)
    _configure_real_single_profile(monkeypatch, tmp_path, db, ttl=60.0)
    monkeypatch.setattr(models, "_all_profiles_cli_contexts", lambda: (
        [(homes[0], db, "a"), (homes[1], homes[1] / "state.db", "b")],
        ((str(homes[0]), "a", "unchanged"), (str(homes[1]), "b", "unchanged")),
    ))
    real_loader = models._load_cli_sessions_uncached

    def limited_loader(home, path, profile, **kwargs):
        kwargs.update(
            project_assigned_limit=False,
            cron_project_limit=None if optional_source == "cron" else False,
            webhook_project_limit=None if optional_source == "webhook" else False,
            kanban_project_limit=None if optional_source == "kanban" else False,
        )
        return real_loader(home, path, profile, **kwargs)

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", limited_loader)
    real_open = agent_sessions.open_state_db_readonly

    def short_busy_timeout(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        conn.execute("PRAGMA busy_timeout=20")
        return conn

    monkeypatch.setattr(agent_sessions, "open_state_db_readonly", short_busy_timeout)
    real_reader = models.read_importable_agent_session_rows
    lock_conn = None
    reads = 0

    def lock_after_primary(path, **kwargs):
        nonlocal lock_conn, reads
        rows = real_reader(path, **kwargs)
        if path == db and lock_conn is None:
            reads += 1
            lock_conn = sqlite3.connect(str(db), isolation_level=None)
            lock_conn.execute("BEGIN EXCLUSIVE")
        return rows

    monkeypatch.setattr(models, "read_importable_agent_session_rows", lock_after_primary)
    try:
        for iteration in range(2):
            rows = models.get_cli_sessions(all_profiles=True, include_claude_code=False)
            ids = {(r["profile"], r["session_id"]) for r in rows}
            assert {("a", "cli_perf_0000"), ("b", "cli_perf_0000")} <= ids
            if iteration:
                assert ("a", "new-primary") in ids
            assert not models._CLI_SESSIONS_CACHE
            assert not models._CLI_SESSIONS_LAST_KNOWN_GOOD
            assert lock_conn is not None
            lock_conn.rollback()
            lock_conn.close()
            lock_conn = None
            if not iteration:
                _insert_session(db, "new-primary")
        assert reads == 2
    finally:
        if lock_conn is not None:
            lock_conn.rollback()
            lock_conn.close()


@pytest.mark.parametrize("locked_pass", ["assigned", "unassigned"])
@pytest.mark.parametrize("all_profiles", [False, True])
@pytest.mark.parametrize("warm_cache", [False, True])
def test_real_recovery_lock_keeps_fresh_primary_rows_uncached(
    tmp_path, monkeypatch, locked_pass, all_profiles, warm_cache
):
    """Recovery/refill locks keep the fresh 20-row primary projection."""
    project_id = "project-1"
    target_home = tmp_path / "target"
    target_home.mkdir()
    target_db = target_home / "state.db"
    _make_state_db(target_db, sessions=25, messages_per_session=1)
    _assign_project(target_db, "cli_perf_0024", project_id)

    healthy_home = tmp_path / "healthy"
    healthy_db = healthy_home / "state.db"
    if all_profiles:
        healthy_home.mkdir()
        _make_state_db(healthy_db, sessions=1, messages_per_session=1)

    projects = tmp_path / "projects.json"
    projects.write_text("[]", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "PROJECTS_FILE", projects)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "get_last_workspace", lambda profile=None: tmp_path)
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(
        models,
        "profile_scoped_project_ids",
        lambda _profile: frozenset({project_id}),
    )
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)

    fixed_single_key = (
        str(target_home),
        "default",
        str(target_db),
        "",
        "unchanged-fingerprint",
        False,
        None,
        None,
        None,
    )
    monkeypatch.setattr(
        models,
        "_resolve_cli_sessions_context",
        lambda source_filter=None, **_kwargs: (
            target_home,
            target_db,
            "default",
            fixed_single_key,
        ),
    )
    if all_profiles:
        monkeypatch.setattr(
            models,
            "_all_profiles_cli_contexts",
            lambda: (
                [
                    (target_home, target_db, "target"),
                    (healthy_home, healthy_db, "healthy"),
                ],
                (
                    (str(target_home), "target", "unchanged-fingerprint"),
                    (str(healthy_home), "healthy", "unchanged-fingerprint"),
                ),
            ),
        )

    real_loader = models._load_cli_sessions_uncached

    def focused_loader(home, path, profile, **kwargs):
        kwargs.update(
            cron_project_limit=False,
            webhook_project_limit=False,
            kanban_project_limit=False,
        )
        return real_loader(home, path, profile, **kwargs)

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", focused_loader)
    models.clear_cli_sessions_cache()

    call_kwargs = {
        "all_profiles": all_profiles,
        "include_claude_code": False,
    }
    if warm_cache:
        warm = models.get_cli_sessions(**call_kwargs)
        assert isinstance(warm, list)
        assert "cli_perf_0024" in _cli_ids(warm)
        _insert_session(target_db, "new-after-warm")
        with models._CLI_SESSIONS_CACHE_LOCK:
            for key, (_expires, stamp, rows) in list(models._CLI_SESSIONS_CACHE.items()):
                models._CLI_SESSIONS_CACHE[key] = (0.0, stamp, rows)

    cache_before = copy.deepcopy(models._CLI_SESSIONS_CACHE)
    lkg_before = copy.deepcopy(models._CLI_SESSIONS_LAST_KNOWN_GOOD)

    real_open = agent_sessions.open_state_db_readonly

    def short_busy_timeout(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        conn.execute("PRAGMA busy_timeout=20")
        return conn

    monkeypatch.setattr(agent_sessions, "open_state_db_readonly", short_busy_timeout)
    real_reader = models.read_importable_agent_session_rows
    lock_conn = None
    targeted_reads = 0

    def lock_at_secondary_read(path, **kwargs):
        nonlocal lock_conn, targeted_reads
        if (
            path == target_db
            and kwargs.get("project_assignment") == locked_pass
            and lock_conn is None
        ):
            targeted_reads += 1
            lock_conn = sqlite3.connect(str(target_db), isolation_level=None)
            lock_conn.execute("BEGIN EXCLUSIVE")
        return real_reader(path, **kwargs)

    monkeypatch.setattr(models, "read_importable_agent_session_rows", lock_at_secondary_read)
    try:
        rows = models.get_cli_sessions(**call_kwargs)
        assert isinstance(rows, list)
        target_rows = [
            row for row in rows
            if not all_profiles or row.get("profile") == "target"
        ]
        expected_primary = {f"cli_perf_{i:04d}" for i in range(5, 25)}
        if warm_cache:
            expected_primary = {
                *(f"cli_perf_{i:04d}" for i in range(6, 25)),
                "new-after-warm",
            }
        assert _cli_ids(target_rows) == expected_primary
        if all_profiles:
            assert ("healthy", "cli_perf_0000") in {
                (row.get("profile"), row["session_id"]) for row in rows
            }
        assert targeted_reads == 1
        assert models._CLI_SESSIONS_CACHE == cache_before
        assert models._CLI_SESSIONS_LAST_KNOWN_GOOD == lkg_before
    finally:
        if lock_conn is not None:
            lock_conn.rollback()
            lock_conn.close()

    # The incomplete attempt was not published, so the same cache fingerprint
    # must retry the real loader and observe another committed primary row.
    _insert_session(target_db, "retry-visible")
    retry_rows = models.get_cli_sessions(**call_kwargs)
    assert isinstance(retry_rows, list)
    assert "retry-visible" in _cli_ids(
        [
            row for row in retry_rows
            if not all_profiles or row.get("profile") == "target"
        ]
    )
    if all_profiles:
        assert ("healthy", "cli_perf_0000") in {
            (row.get("profile"), row["session_id"]) for row in retry_rows
        }


def test_cache_owned_projection_preserves_rows_when_real_open_fails(tmp_path, monkeypatch):
    """A real read-only open failure must reach the stale-cache fallback."""
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=1, messages_per_session=1, source="cli", session_source="cli")
    home = tmp_path / "home"
    home.mkdir()
    revision = ["warm"]
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 0.001, raising=False)
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(
        models,
        "_resolve_cli_sessions_context",
        lambda _source_filter=None, **_kwargs: (
            home,
            db,
            "default",
            (str(home), "default", str(db), "", revision[0], False, None, None, None),
        ),
    )
    models.clear_cli_sessions_cache()

    warm = models.get_cli_sessions()
    assert [row["session_id"] for row in warm] == ["cli_perf_0000"]

    def fail_open(*_args, **_kwargs):
        raise OSError("state.db temporarily unavailable")

    monkeypatch.setattr(agent_sessions, "open_state_db_readonly", fail_open)
    revision[0] = "failed"
    assert models.get_cli_sessions() == warm


def test_cache_owned_source_pass_failure_serves_fresh_incomplete_rows_uncached(tmp_path, monkeypatch):
    """A source-specific read failure retries instead of publishing or serving stale."""
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=1, messages_per_session=1, source="cron", session_source="cron")
    home = tmp_path / "home"
    home.mkdir()
    # Isolate projects like the other tests here: a "Cron Jobs" project left in the shared
    # projects file by an earlier test (e.g. test_1079) changes which state.db open is the
    # second one, so the injected failure would land outside the cron pass.
    projects = tmp_path / "projects.json"
    projects.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(models, "PROJECTS_FILE", projects)
    monkeypatch.setattr(models, "_projects_migrated", True)
    revision = ["warm"]
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 0.001, raising=False)
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(
        models,
        "_resolve_cli_sessions_context",
        lambda _source_filter=None, **_kwargs: (
            home,
            db,
            "default",
            (str(home), "default", str(db), "", revision[0], False, None, None, None),
        ),
    )
    models.clear_cli_sessions_cache()
    warm = models.get_cli_sessions()
    assert [row["session_id"] for row in warm] == ["cli_perf_0000"]

    real_open = agent_sessions.open_state_db_readonly
    opens = 0

    def fail_second_open(*args, **kwargs):
        nonlocal opens
        opens += 1
        if opens == 2:
            raise OSError("state.db disappeared during cron pass")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(agent_sessions, "open_state_db_readonly", fail_second_open)
    revision[0] = "failed"
    assert models.get_cli_sessions() == []
    assert opens >= 2

    # The incomplete optional result was not published under the failed key.
    monkeypatch.setattr(agent_sessions, "open_state_db_readonly", real_open)
    assert models.get_cli_sessions() == warm


def test_all_profiles_partial_result_never_poison_ttl_or_stable_cache(monkeypatch, tmp_path):
    """A+B warm -> A-only partial -> recovery must retain complete authority."""
    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
    revision = ["warm"]
    mode = ["warm"]
    calls = []

    def contexts():
        return (
            [(homes[0], homes[0] / "state.db", "a"), (homes[1], homes[1] / "state.db", "b")],
            ((str(homes[0]), "a", revision[0]), (str(homes[1]), "b", revision[0])),
        )

    monkeypatch.setattr(models, "_all_profiles_cli_contexts", contexts)
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)
    models.clear_cli_sessions_cache()

    def load(_home, _db_path, profile, **_kwargs):
        calls.append((mode[0], profile))
        if mode[0] == "partial" and profile == "b":
            raise OSError("profile b unavailable")
        if mode[0] == "failed":
            raise OSError(f"profile {profile} unavailable")
        return [{"session_id": f"session-{profile}", "profile": profile}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", load)
    complete = [
        {"session_id": "session-a", "profile": "a"},
        {"session_id": "session-b", "profile": "b"},
    ]
    assert models.get_cli_sessions(all_profiles=True) == complete

    mode[0] = "partial"
    revision[0] = "partial"
    assert models.get_cli_sessions(all_profiles=True) == complete

    mode[0] = "warm"
    # Same fingerprint/key as the partial attempt: it must rebuild because the
    # incomplete result was not published to the TTL cache.
    assert models.get_cli_sessions(all_profiles=True) == complete

    mode[0] = "failed"
    revision[0] = "failed"
    assert models.get_cli_sessions(all_profiles=True) == complete
    assert calls.count(("partial", "b")) == 1


def test_all_profiles_empty_success_counts_as_success(monkeypatch, tmp_path):
    """A healthy profile returning [] is success, not an all-failed signal."""
    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
    revision = ["same"]
    mode = ["partial"]
    monkeypatch.setattr(
        models,
        "_all_profiles_cli_contexts",
        lambda: (
            [(homes[0], homes[0] / "state.db", "a"), (homes[1], homes[1] / "state.db", "b")],
            ((str(homes[0]), "a", revision[0]), (str(homes[1]), "b", revision[0])),
        ),
    )
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)
    models.clear_cli_sessions_cache()

    def load(_home, _db_path, profile, **_kwargs):
        if profile == "a":
            return []
        if mode[0] == "partial":
            raise OSError("profile b unavailable")
        return [{"session_id": "b"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", load)
    assert models.get_cli_sessions(all_profiles=True) == []
    mode[0] = "recovered"
    assert models.get_cli_sessions(all_profiles=True) == [{"session_id": "b"}]


def test_all_profiles_scans_global_claude_when_first_profile_unavailable(monkeypatch, tmp_path):
    """Profile 0 failure must not suppress the independent Claude scan."""
    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
    monkeypatch.setattr(
        models,
        "_all_profiles_cli_contexts",
        lambda: (
            [(homes[0], homes[0] / "state.db", "a"), (homes[1], homes[1] / "state.db", "b")],
            ((str(homes[0]), "a", "same"), (str(homes[1]), "b", "same")),
        ),
    )
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [{"session_id": "claude-1"}])
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(
        models,
        "_load_cli_sessions_uncached",
        lambda _home, _db_path, profile, **_kwargs: (
            (_ for _ in ()).throw(OSError("profile a unavailable"))
            if profile == "a"
            else [{"session_id": "profile-b"}]
        ),
    )
    assert models.get_cli_sessions(all_profiles=True) == [
        {"session_id": "profile-b"},
        {"session_id": "claude-1"},
    ]


def test_all_profiles_incomplete_load_prefers_stale_primary_over_evicted_lkg(monkeypatch, tmp_path):
    """A complete expired primary remains usable when its LKG twin was evicted."""
    home = tmp_path / "home"
    home.mkdir()
    mode = ["warm"]
    monkeypatch.setattr(models, "_all_profiles_cli_contexts", lambda: (
        [(home, home / "state.db", "default")],
        ((str(home), "default", "warm"),),
    ))
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)
    models.clear_cli_sessions_cache()
    rows = [{"session_id": "complete"}]
    monkeypatch.setattr(models, "_load_cli_sessions_uncached", lambda *_a, **_k: (
        rows if mode[0] == "warm" else (_ for _ in ()).throw(OSError("unavailable"))
    ))
    assert models.get_cli_sessions(all_profiles=True) == rows
    cache_key = next(k for k in models._CLI_SESSIONS_CACHE if k[0] == "all_profiles")
    with models._CLI_SESSIONS_CACHE_LOCK:
        expires, stamp, cached = models._CLI_SESSIONS_CACHE[cache_key]
        models._CLI_SESSIONS_CACHE[cache_key] = (0.0, stamp, cached)
        models._CLI_SESSIONS_LAST_KNOWN_GOOD.clear()
    mode[0] = "failed"
    calls = []
    original_loader = models._load_cli_sessions_uncached
    def failing_loader(*args, **kwargs):
        calls.append(True)
        return original_loader(*args, **kwargs)
    monkeypatch.setattr(models, "_load_cli_sessions_uncached", failing_loader)
    assert models.get_cli_sessions(all_profiles=True) == rows
    assert calls == [True]


@pytest.mark.parametrize("source_filter", ["cron", "webhook", "kanban"])
def test_all_profiles_filtered_load_excludes_global_claude_on_first_load_and_cache_hit(
    monkeypatch, tmp_path, source_filter
):
    """Profile-source filters must not admit global Claude rows."""
    home = tmp_path / "home"
    home.mkdir()
    claude_calls = []
    monkeypatch.setattr(models, "_all_profiles_cli_contexts", lambda: (
        [(home, home / "state.db", "default")],
        ((str(home), "default", "same"),),
    ))
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: claude_calls.append(1) or [{"session_id": "claude"}])
    monkeypatch.setattr(
        models,
        "_load_cli_sessions_uncached",
        lambda *_a, **_k: [{"session_id": source_filter, "source": source_filter}],
    )
    expected = [{"session_id": source_filter, "source": source_filter}]
    assert models.get_cli_sessions(source_filter, all_profiles=True) == expected
    assert models.get_cli_sessions(source_filter, all_profiles=True) == expected
    assert claude_calls == []


def test_unavailable_profile_warning_names_profile_and_database_path(
    monkeypatch, tmp_path, caplog
):
    healthy_home = tmp_path / "healthy"
    failed_home = tmp_path / "failed"
    healthy_home.mkdir()
    failed_home.mkdir()
    failed_db = failed_home / "state.db"
    monkeypatch.setattr(
        models,
        "_all_profiles_cli_contexts",
        lambda: (
            [
                (healthy_home, healthy_home / "state.db", "healthy"),
                (failed_home, failed_db, "broken-profile"),
            ],
            ((str(healthy_home), "healthy", "same"), (str(failed_home), "broken-profile", "same")),
        ),
    )
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(models, "_cli_sessions_cache_ttl_seconds", lambda: 0.0)

    def load(_home, _db_path, profile, **_kwargs):
        if profile == "broken-profile":
            raise OSError("temporarily unavailable")
        return [{"session_id": "healthy-row"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", load)
    with caplog.at_level("WARNING"):
        rows = models.get_cli_sessions(all_profiles=True, include_claude_code=False)

    assert rows == [{"session_id": "healthy-row"}]
    warning = next(
        record for record in caplog.records
        if record.getMessage().startswith("get_cli_sessions() skipped unavailable profile")
    )
    assert "broken-profile" in warning.getMessage()
    assert str(failed_db) in warning.getMessage()


def test_all_profiles_keeps_healthy_profile_when_another_is_unavailable(monkeypatch, tmp_path):
    """An unavailable profile must not hide rows loaded from healthy profiles."""
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    contexts = lambda: (
        [(home_a, home_a / "state.db", "a"), (home_b, home_b / "state.db", "b")],
        ((str(home_a), "a", "a-rev"), (str(home_b), "b", "b-rev")),
    )
    monkeypatch.setattr(models, "_all_profiles_cli_contexts", contexts)
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 0.0, raising=False)

    def load(home, _db_path, profile, **_kwargs):
        if profile == "b":
            raise OSError("profile b state.db unavailable")
        return [{"session_id": "healthy-a", "profile": "a"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", load)
    assert models.get_cli_sessions(all_profiles=True) == [
        {"session_id": "healthy-a", "profile": "a"}
    ]


def test_all_profiles_idle_and_streaming_fallback_share_stable_identity(monkeypatch, tmp_path):
    """Idle and streaming-frozen all-profile failures share last-known-good rows."""
    home = tmp_path / "home"
    home.mkdir()
    db = home / "state.db"
    db.write_text("placeholder", encoding="utf-8")
    marker: list[object] = [None]
    revision = ["warm"]
    calls = 0
    rows = [{"session_id": "all-profile-1", "title": "Known good"}]
    contexts = lambda: ([(home, db, "default")], ((str(home), "default", revision[0]),))
    monkeypatch.setattr(models, "_all_profiles_cli_contexts", contexts)
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "_cli_sessions_streaming_freeze_marker", lambda: marker[0])
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 0.001, raising=False)

    def load(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("all-profile state.db unavailable")
        return list(rows)

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", load)
    models.clear_cli_sessions_cache()
    assert models.get_cli_sessions(all_profiles=True) == rows
    revision[0] = "streaming"
    marker[0] = ("streaming", ("session-1",))
    assert models.get_cli_sessions(all_profiles=True) == rows
    revision[0] = "idle-again"
    marker[0] = None
    assert models.get_cli_sessions(all_profiles=True) == rows
    assert calls == 3
