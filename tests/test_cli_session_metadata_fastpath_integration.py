"""#8412: real-producer/consumer regressions for the metadata fast path.

The unit tests stub the catalog loader, which the reviewer correctly noted can
miss the transitions that matter. These use real SQLite profile databases, the
real catalog/cache producer and key construction, the real request-profile
binding, and the real route consumer. No loader is mocked; every assertion
compares the fast path against what the enumeration itself returns.
"""
import sqlite3
from pathlib import Path

import pytest

from api import models, routes


def _make_state_db(path: Path, rows) -> None:
    """Create a real agent-shaped state.db holding ``rows``.

    ``rows`` is a sequence of ``(session_id, source, title)``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_version (version INTEGER);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                reasoning_tokens INTEGER DEFAULT 0,
                billing_provider TEXT,
                billing_base_url TEXT,
                billing_mode TEXT,
                estimated_cost_usd REAL,
                actual_cost_usd REAL,
                cost_status TEXT,
                cost_source TEXT,
                pricing_version TEXT,
                title TEXT,
                api_call_count INTEGER DEFAULT 0,
                handoff_state TEXT,
                handoff_platform TEXT,
                handoff_error TEXT,
                cwd TEXT,
                rewind_count INTEGER DEFAULT 0,
                archived INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp REAL,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_call_count INTEGER DEFAULT 0
            );
            """
        )
        for offset, (sid, source, title) in enumerate(rows):
            started = 1781024055.0 + offset
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, source, model, message_count, started_at, title, cwd) "
                "VALUES (?, ?, ?, 2, ?, ?, '/tmp')",
                (sid, source, "test-model", started, title),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, 'user', 'hi', ?)",
                (sid, started),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, 'assistant', 'hello', ?)",
                (sid, started + 0.5),
            )
        conn.commit()
    finally:
        conn.close()


def _add_newer_sessions(path: Path, count: int = 40, prefix: str = "new") -> None:
    conn = sqlite3.connect(str(path))
    try:
        for i in range(count):
            started = 1781024055.0 + 100 + i
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, source, model, message_count, started_at, title, cwd) "
                "VALUES (?, 'cli', 'test-model', 2, ?, ?, '/tmp')",
                (f"{prefix}-{i:02d}", started, f"New {i}"),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, 'user', 'hi', ?)",
                (f"{prefix}-{i:02d}", started),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """Two real profiles on disk, with the active one switchable mid-test."""
    homes = {
        "alpha": tmp_path / "profiles" / "alpha",
        "bravo": tmp_path / "profiles" / "bravo",
    }
    for home in homes.values():
        home.mkdir(parents=True, exist_ok=True)
    index_path = tmp_path / "sessions" / "_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("[]", encoding="utf-8")

    state = {"active": "alpha"}

    monkeypatch.setattr(
        "api.profiles.get_active_hermes_home", lambda: homes[state["active"]]
    )
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: state["active"])
    monkeypatch.setattr("api.profiles._profiles_root", lambda: tmp_path / "profiles")
    monkeypatch.setattr(
        "api.profiles.get_hermes_home_for_profile",
        lambda name: homes[str(name or "default")]
        if str(name or "default") in homes
        else (tmp_path / "profiles" / str(name)),
    )
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_path)
    projects_file = tmp_path / "projects.json"
    projects_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "_default_claude_code_projects_dir", lambda: None)
    models.clear_cli_sessions_cache()
    try:
        yield state, homes
    finally:
        models.clear_cli_sessions_cache()


def _switch(state, name):
    state["active"] = name


def _count_loads(monkeypatch):
    loads = []
    real_load = models._load_cli_sessions_uncached

    def _counting(*args, **kwargs):
        loads.append(kwargs.get("session_ids"))
        return real_load(*args, **kwargs)

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", _counting)
    return loads


# ── Scope: one profile's snapshot must not answer another ────────────────


def test_primed_profile_does_not_answer_a_switch(profiles):
    state, homes = profiles
    _make_state_db(homes["alpha"] / "state.db", [("alpha-only", "cli", "Alpha only")])
    _make_state_db(homes["bravo"] / "state.db", [("bravo-only", "cli", "Bravo only")])

    assert [r["session_id"] for r in models.get_cli_sessions()] == ["alpha-only"]
    assert models.lookup_cli_session_row("alpha-only")["session_id"] == "alpha-only"

    _switch(state, "bravo")

    assert {r["session_id"] for r in models.get_cli_sessions()} == {"bravo-only"}
    assert models.lookup_cli_session_row("alpha-only") == {}
    assert routes._lookup_cli_session_metadata("alpha-only") == {}
    assert models.lookup_cli_session_row("bravo-only")["session_id"] == "bravo-only"


def test_same_sid_in_both_profiles_resolves_to_the_active_one(profiles):
    state, homes = profiles
    _make_state_db(homes["alpha"] / "state.db", [("shared-sid", "cli", "Alpha title")])
    _make_state_db(homes["bravo"] / "state.db", [("shared-sid", "cli", "Bravo title")])

    assert models.get_cli_sessions()[0]["title"] == "Alpha title"
    assert models.lookup_cli_session_row("shared-sid")["title"] == "Alpha title"

    _switch(state, "bravo")

    assert models.get_cli_sessions()[0]["title"] == "Bravo title"
    assert models.lookup_cli_session_row("shared-sid")["title"] == "Bravo title"
    meta = routes._lookup_cli_session_metadata("shared-sid")
    assert meta["title"] == "Bravo title"
    assert meta["profile"] == "bravo"


def test_all_profiles_duplicate_sid_uses_enumeration_order(profiles):
    state, homes = profiles
    _make_state_db(homes["alpha"] / "state.db", [("dup-sid", "cli", "Alpha copy")])
    _make_state_db(homes["bravo"] / "state.db", [("dup-sid", "cli", "Bravo copy")])

    enumerated = models.get_cli_sessions(all_profiles=True)
    first = next(r for r in enumerated if r["session_id"] == "dup-sid")
    assert first["title"] == "Alpha copy"

    got = models.lookup_cli_session_row("dup-sid", all_profiles=True)
    assert got["title"] == first["title"]


# ── External writes reach the caller ────────────────────────────────────


def test_external_delete_is_not_served_from_the_snapshot(profiles):
    state, homes = profiles
    db = homes["alpha"] / "state.db"
    _make_state_db(db, [("victim", "cli", "Victim"), ("keeper", "cli", "Keeper")])

    models.get_cli_sessions()  # prime
    assert models.lookup_cli_session_row("victim")["session_id"] == "victim"

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM messages WHERE session_id = 'victim'")
        conn.execute("DELETE FROM sessions WHERE id = 'victim'")
        conn.commit()
    finally:
        conn.close()

    assert models.lookup_cli_session_row("victim") == {}
    assert routes._lookup_cli_session_metadata("victim") == {}


def test_external_title_update_reaches_the_caller(profiles):
    state, homes = profiles
    db = homes["alpha"] / "state.db"
    _make_state_db(db, [("rename-me", "cli", "Old title")])

    models.get_cli_sessions()  # prime
    assert models.lookup_cli_session_row("rename-me")["title"] == "Old title"

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE sessions SET title = 'New title' WHERE id = 'rename-me'")
        conn.commit()
    finally:
        conn.close()

    assert routes._lookup_cli_session_metadata("rename-me")["title"] == "New title"


def test_new_insert_after_a_negative_lookup_is_found(profiles):
    state, homes = profiles
    db = homes["alpha"] / "state.db"
    _make_state_db(db, [("existing", "cli", "Existing")])

    assert models.lookup_cli_session_row("late-arrival") == {}

    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO sessions (id, source, model, message_count, started_at, title, cwd) "
            "VALUES ('late-arrival', 'cli', 'test-model', 1, 1781030000.0, 'Late', '/tmp')"
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES ('late-arrival', 'user', 'hi', 1781030000.0)"
        )
        conn.commit()
    finally:
        conn.close()

    assert routes._lookup_cli_session_metadata("late-arrival")["session_id"] == (
        "late-arrival"
    )


def test_explicit_invalidation_drops_the_snapshot(profiles):
    state, homes = profiles
    _make_state_db(homes["alpha"] / "state.db", [("keep-me", "cli", "Keep")])

    models.get_cli_sessions()
    assert models.lookup_cli_session_row("keep-me")["session_id"] == "keep-me"

    models.clear_cli_sessions_cache()
    with models._CLI_SESSIONS_CACHE_LOCK:
        assert not models._CLI_SESSIONS_CACHE

    assert routes._lookup_cli_session_metadata("keep-me")["session_id"] == "keep-me"


# ── The optimisation itself, proven on a real store ──────────────────────


def test_current_snapshot_answers_without_reprojecting(profiles, monkeypatch):
    """A lookup against an unchanged catalog must not re-run the projection."""
    state, homes = profiles
    _make_state_db(homes["alpha"] / "state.db", [("live-one", "cli", "Live one")])

    models.get_cli_sessions()  # prime
    loads = _count_loads(monkeypatch)

    row = models.lookup_cli_session_row("live-one")
    assert row["session_id"] == "live-one"
    assert loads == [], loads


def test_expired_snapshot_falls_back_to_the_enumeration(profiles, monkeypatch):
    """TTL expiry is the producer's rebuild trigger, so the lookup defers."""
    state, homes = profiles
    _make_state_db(homes["alpha"] / "state.db", [("ttl-one", "cli", "TTL one")])

    models.get_cli_sessions()  # prime
    with models._CLI_SESSIONS_CACHE_LOCK:
        for key in list(models._CLI_SESSIONS_CACHE):
            entry = models._CLI_SESSIONS_CACHE[key]
            models._CLI_SESSIONS_CACHE[key] = (0.0, entry[1], entry[2])

    loads = _count_loads(monkeypatch)
    assert models.lookup_cli_session_row("ttl-one") == {}
    assert models.lookup_cli_session_row("ttl-one") == {}
    assert loads == [], "an expired snapshot must not answer"


def test_changed_revision_falls_back_to_the_enumeration(profiles, monkeypatch):
    """Once the catalog revision moves, the snapshot may not answer."""
    state, homes = profiles
    db = homes["alpha"] / "state.db"
    _make_state_db(db, [("moved-one", "cli", "Moved one")])

    models.get_cli_sessions()  # prime at revision R1

    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES ('moved-one', 'user', 'another', 1781099999.0)"
        )
        conn.commit()
    finally:
        conn.close()

    loads = _count_loads(monkeypatch)
    assert models.lookup_cli_session_row("moved-one") == {}
    assert models.lookup_cli_session_row("moved-one") == {}
    assert loads == []


def test_recency_eviction_is_not_undone(profiles):
    """A session evicted from the catalog window must stay absent.

    The lookup happens BEFORE any re-enumeration on purpose: enumerating first
    would replace the snapshot and hide the stale-snapshot schedule entirely.
    """
    state, homes = profiles
    db = homes["alpha"] / "state.db"
    _make_state_db(db, [("old-one", "cli", "Old one")])

    models.get_cli_sessions()  # prime while it is still in the window
    assert models.lookup_cli_session_row("old-one")["session_id"] == "old-one"

    _add_newer_sessions(db, prefix="new")

    assert routes._lookup_cli_session_metadata("old-one") == {}
    assert "old-one" not in {r["session_id"] for r in models.get_cli_sessions()}


def test_streaming_transition_re_enumerates(profiles, monkeypatch):
    """A marker transition changes the key, so the idle snapshot cannot answer."""
    state, homes = profiles
    _make_state_db(homes["alpha"] / "state.db", [("mid-turn", "cli", "Mid turn")])

    models.get_cli_sessions()  # prime while idle
    monkeypatch.setattr(
        models,
        "_cli_sessions_streaming_freeze_marker",
        lambda: ("streaming", ("turn-1",)),
    )

    assert models.lookup_cli_session_row("mid-turn") == {}
    assert routes._lookup_cli_session_metadata("mid-turn")["session_id"] == "mid-turn"
