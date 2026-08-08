"""Regression tests for #6843 — archive/delete of imported ``api_server`` sessions.

Imported ``api_server`` rows are local Hermes ``state.db`` rows with no WebUI
sidecar; many carry ids that are not path-safe as sidecar filenames (colons,
e.g. ``miloco:agent:main:miloco-suggest:miloco-suggest:<hash>``). Before the
fix:

* ``POST /api/session/archive`` tried to materialize a writable sidecar and
  crashed with an unhandled ``ValueError`` (unsafe session_id) -> HTTP 500, or
  refused ``read_only`` imported rows with a 400;
* ``POST /api/session/delete`` rejected the same ids with
  ``Invalid session_id`` (400);
* the sidebar projection only honored sidecar ``archived`` state, so rows
  archived in state.db kept flooding the default list as
  ``Api_Server Session``.

The fix archives/deletes these rows directly in state.db (archive is
WebUI-local view state — no sidecar materialization) and makes the sidebar
projection honor the state.db ``archived`` flag when no sidecar exists.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.models as models
import api.routes as routes
from api.models import SESSIONS


def _make_state_db(path: Path, sid: str, *, source: str = "api_server",
                   title: str | None = None, archived: int = 0,
                   message_count: int = 2) -> None:
    """Create a minimal state.db with one session row and a few messages.

    Schema mirrors hermes_state.SessionDB closely enough for
    read_importable_agent_session_rows / delete_cli_session.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            model TEXT,
            model_config TEXT,
            parent_session_id TEXT,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            title TEXT,
            cwd TEXT,
            archived INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO sessions "
        "(id, source, model, message_count, started_at, title, cwd, archived) "
        "VALUES (?, ?, 'deepseek/deepseek-chat', ?, 1781024055.0, ?, '/root', ?)",
        (sid, source, message_count, title, archived),
    )
    for i in range(message_count):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, "user" if i % 2 == 0 else "assistant", f"msg {i}", 1781024055.0 + i),
        )
    conn.commit()
    conn.close()


def _read_archived(db: Path, sid: str) -> int | None:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT archived FROM sessions WHERE id = ?", (sid,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _row_exists(db: Path, sid: str) -> bool:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone() is not None
    finally:
        conn.close()


@pytest.fixture
def isolated_state_db(tmp_path, monkeypatch):
    """Point the whole session/state.db stack at an isolated tmp dir.

    ``get_active_hermes_home`` is the single chokepoint used by
    ``_active_state_db_path``, ``_resolve_cli_sessions_context`` (sidebar
    projection) and ``delete_cli_session``, so patching it routes all of them
    to ``tmp_path/state.db``.
    """
    import api.profiles as _profiles

    db = tmp_path / "state.db"
    sessions_dir = tmp_path / "webui-sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    index_path = sessions_dir / "_index.json"
    index_path.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(_profiles, "get_active_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(routes, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_path)
    SESSIONS.clear()
    return {"db": db, "sessions_dir": sessions_dir, "index_path": index_path}


def _capture_post(monkeypatch, body):
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )
    return captured


def _sidebar_sessions():
    """Build the default visible sidebar payload the same way /api/sessions does."""
    return routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        show_claude_code_sessions=False,
        include_archived=False,
        exclude_hidden=True,
        visible_only=True,
        show_webhook_sessions=False,
        source_filter=None,
        sidebar_source=None,
    )


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def test_archive_api_server_session_flips_state_db_instead_of_500(
    isolated_state_db, monkeypatch
):
    """Archiving an imported api_server row with an unsafe (colon) id must not
    crash (previously an unhandled ValueError -> HTTP 500): it flips the
    state.db `archived` flag directly and returns ok."""
    sid = "miloco:agent:main:miloco-suggest:miloco-suggest:9f8e7d6c5b4a"
    _make_state_db(isolated_state_db["db"], sid, title=None, archived=0)

    assert models.is_safe_session_id(sid) is False, (
        "precondition: the imported id is not path-safe (no sidecar possible)"
    )

    captured = _capture_post(monkeypatch, {"session_id": sid, "archived": True})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert _read_archived(isolated_state_db["db"], sid) == 1

    # Round-trip: unarchive restores the flag (archive is a toggle).
    captured = _capture_post(monkeypatch, {"session_id": sid, "archived": False})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True
    assert captured["status"] == 200
    assert _read_archived(isolated_state_db["db"], sid) == 0


def test_archive_read_only_imported_session_flips_state_db_instead_of_400(
    isolated_state_db, monkeypatch
):
    """A read_only imported row is WebUI-local view state for archiving: flip
    state.db instead of the previous blanket 400."""
    sid = "ro-telegram-imported-1"
    _make_state_db(isolated_state_db["db"], sid, source="telegram", title="Telegram chat")
    cli_meta = {
        "session_id": sid,
        "source_tag": "telegram",
        "raw_source": "telegram",
        "session_source": "messaging",
        "read_only": True,
        "profile": "default",
    }
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda s: cli_meta)

    captured = _capture_post(monkeypatch, {"session_id": sid, "archived": True})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert _read_archived(isolated_state_db["db"], sid) == 1


def test_archived_api_server_session_stops_flooding_sidebar(
    isolated_state_db, monkeypatch
):
    """The #6843 flood: untitled api_server rows render as `Api_Server Session`
    in the default sidebar; once archived via the WebUI they must disappear
    from the visible list (state.db archived flag honored, no sidecar)."""
    sid = "miloco:agent:main:miloco-suggest:miloco-suggest:1a2b3c4d5e6f"
    _make_state_db(isolated_state_db["db"], sid, title=None, archived=0)

    before = _sidebar_sessions()
    row = next(
        (s for s in before["sessions"] if s.get("session_id") == sid),
        None,
    )
    assert row is not None, "untitled api_server row must appear in the sidebar"
    assert row["title"] == "Api_Server Session"
    assert row["archived"] is False

    captured = _capture_post(monkeypatch, {"session_id": sid, "archived": True})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True
    assert captured["status"] == 200

    after = _sidebar_sessions()
    assert all(s.get("session_id") != sid for s in after["sessions"]), (
        "archived api_server row must leave the default visible sidebar (#6843)"
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_api_server_session_removes_state_db_row(
    isolated_state_db, monkeypatch
):
    """Deleting an imported api_server row with an unsafe (colon) id must not
    be rejected with Invalid session_id: the state.db row is removed."""
    sid = "miloco:agent:main:miloco-suggest:miloco-suggest:0a1b2c3d4e5f"
    _make_state_db(isolated_state_db["db"], sid, title=None, archived=0)

    assert models.is_safe_session_id(sid) is False

    captured = _capture_post(monkeypatch, {"session_id": sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["state_db_cleanup_failed"] is False
    assert _row_exists(isolated_state_db["db"], sid) is False
    assert not (isolated_state_db["sessions_dir"] / f"{sid}.json").exists()
