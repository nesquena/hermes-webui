"""Regression tests for #8061: exact-id resolution of a sidecar-less Agent session
that has aged out of the bounded sidebar window.

GET /api/session — and the rename/move/archive/delete paths sharing the same
lookup — resolved an id against the sidebar projection only, which is capped at
``CLI_VISIBLE_SESSION_LIMIT`` (20 by default, clamped to 200). A Hermes
Desktop/TUI/CLI conversation that aged out of that window and has no WebUI
sidecar was therefore reported as "Session not found" although its row and
transcript were still in ``state.db``.

The fix adds a profile-scoped, parameterized single-row fallback
(``models.get_cli_session_metadata_by_id`` →
``agent_sessions.read_agent_session_row_by_id``) that runs when the sidebar
lookup misses, so the sidebar itself stays bounded. These tests pin the
contract:

* an out-of-window id resolves — at the default window and beyond the 200 clamp,
* the sidebar payload stays bounded while that resolution succeeds,
* a genuinely unknown id still resolves to nothing,
* the owner profile comes from the resolved DB context (root and named profile),
* the deleted-WebUI tombstone still wins over a surviving ``state.db`` row,
* foreign sources (messaging) are not suppressed by that tombstone,
* the window row wins whenever the sidebar already has the id.
"""

import json
import sqlite3

import pytest

from api import models


def _make_state_db(db_path, sessions, messages=None):
    """Minimal state.db with the columns the projection/reader guard for."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            message_count INTEGER,
            started_at REAL,
            source TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp REAL,
            role TEXT
        )
    """)
    for sid, title, source, started_at in sessions:
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, title, "gpt-x", 1, started_at, source),
        )
    for sid, ts, role in (messages or []):
        conn.execute(
            "INSERT INTO messages (session_id, timestamp, role) VALUES (?, ?, ?)",
            (sid, ts, role),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def fake_hermes_home(tmp_path, monkeypatch):
    """Point the sidebar projection and the exact-id reader at a temp HERMES_HOME."""
    home = tmp_path / "hermes"
    home.mkdir()

    import api.config as cfg
    import api.profiles as profiles
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: None)

    projects_file = tmp_path / "projects.json"
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    projects_file.write_text(json.dumps([]), encoding="utf-8")

    # No WebUI sidecar is materialised in this test: the session lives only in
    # state.db, which is exactly the reported case.
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path / "webui-sessions")
    yield home


def _rows(count, *, newest_ts=1_800_000_000.0, source="cli", prefix="20261001_120000_"):
    """``count`` sessions, oldest first, so index 0 is the oldest."""
    return [
        (f"{prefix}{i:06x}", f"Session {i}", source, newest_ts - (count - i))
        for i in range(count)
    ]


def _messages(rows):
    """One user turn per row: a session is only sidebar-visible with a transcript."""
    return [(sid, ts, "user") for sid, _title, _source, ts in rows]


def test_out_of_window_sidecar_less_session_resolves(fake_hermes_home, monkeypatch):
    """The reported symptom: an aged-out id with no sidecar must resolve."""
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)
    db_path = fake_hermes_home / "state.db"
    rows = _rows(30)
    _make_state_db(db_path, rows, messages=_messages(rows))
    models.clear_cli_sessions_cache()

    oldest_id = rows[0][0]

    # Precondition: the sidebar projection cannot see it (this is the bug's input).
    window_ids = {row["session_id"] for row in models.get_cli_sessions()}
    assert oldest_id not in window_ids
    assert len(window_ids) == 20

    # The fallback resolves it, in the sidebar's row shape.
    meta = models.get_cli_session_metadata_by_id(oldest_id)
    assert meta["session_id"] == oldest_id
    assert meta["profile"] == "default"  # same stamp as the sidebar row
    assert meta["source_tag"] == "cli"
    assert meta["is_cli_session"] is True
    assert meta["actual_message_count"] == 1

    # ... and the shared lookup every caller uses sees it too.
    import api.routes as routes
    assert routes._lookup_cli_session_metadata(oldest_id)["session_id"] == oldest_id


def test_resolution_works_beyond_the_two_hundred_row_clamp(fake_hermes_home, monkeypatch):
    """An install with more sessions than the clamp maximum still resolves."""
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 200)
    db_path = fake_hermes_home / "state.db"
    rows = _rows(260)
    _make_state_db(db_path, rows, messages=_messages(rows))
    models.clear_cli_sessions_cache()

    oldest_id = rows[0][0]
    sidebar = models.get_cli_sessions()
    assert oldest_id not in {row["session_id"] for row in sidebar}
    assert len(sidebar) == 200

    meta = models.get_cli_session_metadata_by_id(oldest_id)
    assert meta["session_id"] == oldest_id
    assert meta["actual_message_count"] == 1


def test_sidebar_stays_bounded_while_exact_id_resolves(fake_hermes_home, monkeypatch):
    """The fallback must not widen the sidebar projection (design constraint)."""
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)
    db_path = fake_hermes_home / "state.db"
    rows = _rows(60)
    _make_state_db(db_path, rows, messages=_messages(rows))
    models.clear_cli_sessions_cache()

    sessions = models.get_cli_sessions()
    assert len(sessions) == 20

    # The old id resolves without adding a row to the sidebar payload.
    assert models.get_cli_session_metadata_by_id(rows[0][0])["session_id"] == rows[0][0]
    assert len(models.get_cli_sessions()) == 20


def test_window_row_still_wins_when_present(fake_hermes_home, monkeypatch):
    """In-window ids keep returning the sidebar row (no double source of truth)."""
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)
    db_path = fake_hermes_home / "state.db"
    rows = _rows(5)
    _make_state_db(db_path, rows, messages=_messages(rows))
    models.clear_cli_sessions_cache()

    newest_id = rows[-1][0]
    window_row = next(r for r in models.get_cli_sessions() if r["session_id"] == newest_id)
    meta = models.get_cli_session_metadata_by_id(newest_id)
    for key in ("session_id", "title", "source_tag", "profile", "created_at", "updated_at"):
        assert meta[key] == window_row[key], key


def test_reported_shape_five_older_sidecar_less_sessions_all_resolve(fake_hermes_home, monkeypatch):
    """The reported shape: 5 older sidecar-less sessions behind a full 20-row window.

    Mirrors the evidence table in #8061 (15 Agent-store conversations, five of them
    older than the sidebar window and without a WebUI sidecar): all five must
    resolve by id, while the sidebar listing itself stays at 20 rows.
    """
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)
    db_path = fake_hermes_home / "state.db"

    # 20 newer conversations fill the window; 5 older ones sit behind it. None of
    # them has a sidecar — the failing case in the issue has neither window
    # membership nor a sidecar.
    newer = _rows(20, prefix="20261006_134236_")
    older = _rows(5, newest_ts=1_700_100_000.0, prefix="20261001_145015_")
    _make_state_db(db_path, older + newer, messages=_messages(older + newer))
    models.clear_cli_sessions_cache()

    sidebar_ids = {row["session_id"] for row in models.get_cli_sessions()}
    assert len(sidebar_ids) == 20
    for sid, *_ in older:
        assert sid not in sidebar_ids, sid

    for sid, title, _source, _ts in older:
        meta = models.get_cli_session_metadata_by_id(sid)
        assert meta["session_id"] == sid
        assert meta["title"] == title
        assert meta["actual_message_count"] == 1

    # Sidebar listing unchanged after those five resolutions.
    assert len({row["session_id"] for row in models.get_cli_sessions()}) == 20


def test_unknown_id_resolves_to_nothing(fake_hermes_home, monkeypatch):
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)
    rows = _rows(3)
    _make_state_db(fake_hermes_home / "state.db", rows, messages=_messages(rows))
    models.clear_cli_sessions_cache()

    import api.routes as routes
    assert models.get_cli_session_metadata_by_id("20990101_000000_deadbe") == {}
    assert routes._lookup_cli_session_metadata("20990101_000000_deadbe") == {}
    assert routes._lookup_cli_session_metadata("") == {}


def test_owner_profile_comes_from_the_resolved_db_context(tmp_path, monkeypatch):
    """A named profile resolves its own row; the foreign DB is not consulted."""
    import api.config as cfg
    import api.profiles as profiles

    root_home = tmp_path / "hermes"
    named_home = root_home / "profiles" / "arbeit"
    named_home.mkdir(parents=True)

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: named_home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "arbeit")
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps([]), encoding="utf-8")
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path / "webui-sessions")

    _make_state_db(named_home / "state.db", [("named_000000000000", "Named", "cli", 100.0)])
    _make_state_db(root_home / "state.db", [("root_0000000000000", "Root", "cli", 100.0)])
    models.clear_cli_sessions_cache()

    named = models.get_cli_session_metadata_by_id("named_000000000000")
    assert named["session_id"] == "named_000000000000"
    assert named["profile"] == "arbeit"

    # A row that exists only in the root profile is NOT resolved while the
    # active profile is a named one — the profile gate stays in charge.
    assert models.get_cli_session_metadata_by_id("root_0000000000000") == {}


def test_all_profiles_scan_covers_every_profile_context(fake_hermes_home, monkeypatch, tmp_path):
    """all_profiles=True resolves the id in whichever profile DB owns it."""
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)
    other_home = tmp_path / "hermes-other"
    other_home.mkdir()
    _make_state_db(fake_hermes_home / "state.db", _rows(2, prefix="root_"))
    _make_state_db(other_home / "state.db", [("arbeit_0000000000", "Arbeit", "cli", 500.0)])
    models.clear_cli_sessions_cache()

    monkeypatch.setattr(
        models,
        "_all_profiles_cli_contexts",
        lambda: (
            [(fake_hermes_home, fake_hermes_home / "state.db", "default"),
             (other_home, other_home / "state.db", "arbeit")],
            (),
        ),
    )

    assert models.get_cli_session_metadata_by_id("arbeit_0000000000") == {}
    meta = models.get_cli_session_metadata_by_id("arbeit_0000000000", all_profiles=True)
    assert meta["session_id"] == "arbeit_0000000000"
    assert meta["profile"] == "arbeit"


def test_deleted_webui_tombstone_is_not_resurrected(fake_hermes_home, monkeypatch):
    """A tombstoned WebUI row must stay gone even though state.db keeps it."""
    db_path = fake_hermes_home / "state.db"
    _make_state_db(db_path, [("webui_000000000000", "Deleted", "webui", 900.0)])
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(
        models, "_load_webui_deleted_session_tombstone",
        lambda: frozenset({"webui_000000000000"}),
    )

    assert models.get_cli_session_metadata_by_id("webui_000000000000") == {}


def test_live_sidecar_beats_a_stale_tombstone(fake_hermes_home, monkeypatch):
    """The projection's own rule: a re-created sidecar wins over the tombstone."""
    db_path = fake_hermes_home / "state.db"
    _make_state_db(db_path, [("webui_000000000001", "Recreated", "webui", 900.0)])
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(
        models, "_load_webui_deleted_session_tombstone",
        lambda: frozenset({"webui_000000000001"}),
    )
    models.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (models.SESSION_DIR / "webui_000000000001.json").write_text("{}", encoding="utf-8")

    meta = models.get_cli_session_metadata_by_id("webui_000000000001")
    assert meta["session_id"] == "webui_000000000001"


def test_foreign_messaging_source_resolves_with_its_source_metadata(fake_hermes_home, monkeypatch):
    """Foreign sources resolve too — read-only/writability stays with the callers."""
    db_path = fake_hermes_home / "state.db"
    _make_state_db(db_path, [("telegram_0000000", "Chat", "telegram", 900.0)])
    models.clear_cli_sessions_cache()

    meta = models.get_cli_session_metadata_by_id("telegram_0000000")
    assert meta["session_id"] == "telegram_0000000"
    assert meta["source_tag"] == "telegram"
    assert meta["session_source"] == "messaging"


# ---------------------------------------------------------------------------
# The reported HTTP symptom, end to end
# ---------------------------------------------------------------------------


def _seed_state_db_for_readers(path, rows):
    """state.db with the columns the transcript reader touches."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        "id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER,"
        "started_at REAL, source TEXT, cwd TEXT, archived INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
        "content TEXT, timestamp REAL)"
    )
    for sid, title, source, ts in rows:
        conn.execute(
            "INSERT INTO sessions (id,title,model,message_count,started_at,source,cwd)"
            " VALUES (?,?,?,?,?,?,?)",
            (sid, title, "gpt-x", 2, ts, source, "/tmp"),
        )
        for i, role in enumerate(("user", "assistant")):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                (sid, role, f"{role} turn {i}", ts + i),
            )
    conn.commit()
    conn.close()


def test_get_session_serves_an_aged_out_sidecar_less_session(tmp_path, monkeypatch):
    """#8061 end to end: the reported 404 becomes 200.

    Reproduces the reporter's shape — a *named* profile whose state.db holds more
    conversations than the window, a target older than the window with no WebUI
    sidecar (so ``get_session()`` raises KeyError and the missing-sidecar branch
    must resolve identity from state.db). Before the fix that branch had no
    profile to check, so the profile gate answered 404.
    """
    from types import SimpleNamespace
    from urllib.parse import urlparse

    import api.config as cfg
    import api.profiles as profiles
    import api.routes as routes

    home = tmp_path / "hermes" / "profiles" / "arbeit"
    home.mkdir(parents=True)
    state_dir = tmp_path / "webui-state"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    index = sessions_dir / "_index.json"
    index.write_text("[]", encoding="utf-8")
    projects = tmp_path / "projects.json"
    projects.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "arbeit")
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "arbeit")
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index)
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)

    older = _rows(1, newest_ts=1_700_100_000.0, prefix="20261001_145015_")
    newer = _rows(25, prefix="20261006_134236_")
    _seed_state_db_for_readers(home / "state.db", older + newer)
    models.clear_cli_sessions_cache()
    sid = older[0][0]
    assert sid not in {row["session_id"] for row in models.get_cli_sessions()}

    captured = {}

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured["data"], captured["status"] = data, status
        return data

    # ``bad()`` writes through api.helpers.j, the 200 path through api.routes.j —
    # patch both so the test observes either outcome instead of writing a response.
    import api.helpers as helpers
    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(helpers, "j", fake_j)
    parsed = urlparse(f"/api/session?session_id={sid}&messages=0&resolve_model=0")

    routes.handle_get(SimpleNamespace(), parsed)
    assert captured.get("status") == 200, captured
    assert captured["data"]["session"]["session_id"] == sid
    assert captured["data"]["session"]["profile"] == "arbeit"

    # Pre-fix behaviour: with the exact-id fallback neutralised, the same request
    # is the reported 404 — the window miss carries no profile to check.
    captured.clear()
    monkeypatch.setattr(routes, "get_cli_session_metadata_by_id", lambda *a, **k: {})
    models.clear_cli_sessions_cache()
    routes.handle_get(SimpleNamespace(), parsed)
    assert captured.get("status") == 404, captured
