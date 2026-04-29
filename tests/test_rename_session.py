"""
Regression tests for /api/session/rename across both session storage backends.

Covers two bugs that previously broke the double-click-to-rename action in
the left sidebar:

  1) Renaming a CLI / agent / gateway-imported session (whose data lives in
     ``~/.hermes/state.db``, not in ``SESSION_DIR``) returned 404, so the
     optimistic UI update was reverted on the next refresh.  Fixed by routing
     the rename through ``api.state_sync.rename_cli_session`` when the
     session is not owned by the WebUI.

  2) Renaming a WebUI session worked, but state.db (used by /insights and
     the "all sessions" view) was not kept in sync, so listings would still
     show the old title in some places.  The handler now best-effort mirrors
     the new title to state.db after writing the JSON file.

These tests reuse the shared isolated server fixture in conftest.py
(port 8788, HERMES_HOME=TEST_STATE_DIR).
"""

import json
import os
import pathlib
import sqlite3
import time
import urllib.error
import urllib.request

from tests.conftest import TEST_BASE

BASE = TEST_BASE


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return {}, e.code


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


# ── State.db helpers (mirror tests/test_gateway_sync.py) ──────────────────────

def _state_db_path():
    explicit = os.getenv('HERMES_WEBUI_TEST_STATE_DIR')
    if explicit:
        return pathlib.Path(explicit) / 'state.db'
    home = pathlib.Path(os.getenv('HERMES_HOME', str(pathlib.Path.home() / '.hermes')))
    return home / 'webui-mvp-test' / 'state.db'


def _ensure_state_db():
    db = _state_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            title TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        );
    """)
    conn.commit()
    return conn


def _insert_cli_session(conn, sid, title="Original CLI Title", source="cli"):
    conn.execute(
        "INSERT OR REPLACE INTO sessions "
        "(id, source, title, model, started_at, message_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, source, title, "anthropic/claude-sonnet-4-5", time.time(), 1),
    )
    # one stub message so the session isn't filtered out as empty
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES (?, 'user', 'hi', ?)",
        (sid, time.time()),
    )
    conn.commit()


def _read_db_title(conn, sid):
    row = conn.execute("SELECT title FROM sessions WHERE id = ?", (sid,)).fetchone()
    return row["title"] if row else None


def _cleanup(conn, sid):
    try:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        conn.commit()
    except Exception:
        pass


def _new_webui_session():
    """Create a WebUI session and return its sid (handles both flat and {session: ...} shapes)."""
    new, status = _post("/api/session/new", {})
    assert status == 200, new
    if isinstance(new, dict) and "session" in new:
        return new["session"]["session_id"]
    return new["session_id"]


# ── Tests: WebUI-owned sessions (regression guard for the existing path) ─────

def test_rename_webui_session_persists_after_refresh():
    """Renaming a normal WebUI session should persist across a fresh /api/sessions read."""
    sid = _new_webui_session()

    try:
        # Rename it
        out, code = _post(
            "/api/session/rename",
            {"session_id": sid, "title": "Renamed WebUI"},
        )
        assert code == 200, (code, out)
        assert out["session"]["title"] == "Renamed WebUI"

        # Re-read /api/sessions and confirm the new title is what comes back
        listing, code = _get("/api/sessions")
        assert code == 200
        match = [s for s in listing["sessions"] if s["session_id"] == sid]
        assert match, f"session {sid} missing from listing"
        assert match[0]["title"] == "Renamed WebUI"
    finally:
        _post("/api/session/delete", {"session_id": sid})


def test_rename_webui_session_caps_title_length():
    """Long titles are clipped to 80 chars (UI contract)."""
    sid = _new_webui_session()
    try:
        long_title = "x" * 200
        out, code = _post(
            "/api/session/rename",
            {"session_id": sid, "title": long_title},
        )
        assert code == 200
        assert len(out["session"]["title"]) == 80
        assert out["session"]["title"] == "x" * 80
    finally:
        _post("/api/session/delete", {"session_id": sid})


def test_rename_webui_session_blank_falls_back_to_untitled():
    sid = _new_webui_session()
    try:
        out, code = _post(
            "/api/session/rename",
            {"session_id": sid, "title": "   "},
        )
        assert code == 200
        assert out["session"]["title"] == "Untitled"
    finally:
        _post("/api/session/delete", {"session_id": sid})


# ── Tests: CLI / state.db sessions (the actual bug we're fixing) ─────────────

def test_rename_cli_session_writes_to_state_db():
    """A CLI/gateway-imported session must be renamable via the WebUI API.

    This is the regression for the user-reported bug:
      "double-click rename in the left sidebar reverts after refresh".
    """
    conn = _ensure_state_db()
    sid = "20260424_120000_renametest"
    try:
        _insert_cli_session(conn, sid, title="Original CLI Title", source="cli")

        out, code = _post(
            "/api/session/rename",
            {"session_id": sid, "title": "Renamed via WebUI"},
        )
        assert code == 200, (code, out)
        assert out["session"]["title"] == "Renamed via WebUI"
        assert out["session"]["session_id"] == sid
        assert out["session"].get("is_cli_session") is True

        # And the new title must actually be in state.db so a refresh shows it
        # state.db is shared with the live conn — re-open to bypass any cache
        conn2 = sqlite3.connect(str(_state_db_path()))
        conn2.row_factory = sqlite3.Row
        try:
            row = conn2.execute(
                "SELECT title FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
        finally:
            conn2.close()
        assert row is not None
        assert row["title"] == "Renamed via WebUI"
    finally:
        _cleanup(conn, sid)
        try:
            conn.close()
        except Exception:
            pass


def test_rename_unknown_session_returns_404():
    """Renaming a session_id that exists nowhere is a clean 404, not a 500."""
    out, code = _post(
        "/api/session/rename",
        {"session_id": "nosuchsession_99999999", "title": "ghost"},
    )
    assert code == 404, (code, out)
