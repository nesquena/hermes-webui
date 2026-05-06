"""Onda 8 — bulk-hydrate WebUI sessions whose sidecar JSON went missing.

Production scenario: Hermes runtime keeps every WebUI conversation in
state.db (source='webui') and the WebUI keeps a sidecar JSON for the same
session under ~/.hermes/webui/sessions/<sid>.json. When the sidecar is lost
(deploy state-dir migration, mid-write OSError, manual cleanup) the sidebar
list — which globs the directory — silently drops the conversation. This
test pins the boot-time recovery so future refactors do not re-introduce the
"my old conversations vanished after deploy" report.
"""
import pathlib
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))


def _bootstrap(tmp_path: Path, sids_present_in_dir, sids_in_state_db):
    """Create a sandbox HERMES_HOME with a state.db that has each sid in
    ``sids_in_state_db`` and pre-populates ``webui/sessions/<sid>.json`` for
    each sid in ``sids_present_in_dir``."""
    home = tmp_path
    sessions_dir = home / "webui" / "sessions"
    sessions_dir.mkdir(parents=True)
    db = home / "state.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                model TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                message_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                estimated_cost_usd REAL,
                title TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL
            );
            """
        )
        for i, sid in enumerate(sids_in_state_db):
            conn.execute(
                "INSERT INTO sessions (id, source, model, started_at, message_count, title) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, "webui", "gpt-5.5", 1700000000.0 + i, 1, f"Session {i}"),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (sid, "user", f"hi from {sid}", 1700000000.0 + i),
            )
    for sid in sids_present_in_dir:
        (sessions_dir / f"{sid}.json").write_text("{}")
    return home, sessions_dir


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home, sessions_dir = _bootstrap(
        tmp_path,
        sids_present_in_dir=["aaaaaaaaaaaa"],          # already on disk
        sids_in_state_db=[
            "aaaaaaaaaaaa",                              # already present — should be skipped
            "bbbbbbbbbbbb",                              # missing — should be restored
            "cccccccccccc",                              # missing — should be restored
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    import api.profiles as profiles
    monkeypatch.setattr(
        profiles, "get_active_hermes_home", lambda: str(home), raising=False
    )
    # api.config.SESSION_DIR is computed from HERMES_HOME at import time, so
    # monkeypatch the module-level constant for both api.config and api.models.
    import api.config as cfg
    import api.models as models
    new_session_dir = home / "webui" / "sessions"
    monkeypatch.setattr(cfg, "STATE_DIR", home / "webui", raising=False)
    monkeypatch.setattr(cfg, "SESSION_DIR", new_session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", new_session_dir, raising=False)
    return home, sessions_dir


def test_hydrate_restores_missing_sidecars(sandbox):
    home, sessions_dir = sandbox
    from api.models import hydrate_orphan_webui_sessions

    report = hydrate_orphan_webui_sessions()
    assert report["candidates"] == 3
    assert report["skipped"] == 1, "the pre-existing sidecar must not be re-written"
    assert report["restored"] == 2, "the two missing sidecars must be rebuilt"
    assert report["failed"] == 0
    # Both restored sids now have a real JSON file on disk.
    assert (sessions_dir / "bbbbbbbbbbbb.json").exists()
    assert (sessions_dir / "cccccccccccc.json").exists()


def test_hydrate_is_idempotent(sandbox):
    """Second call after a successful run reports zero restores and writes
    nothing new — no double-allocation, no log spam."""
    from api.models import hydrate_orphan_webui_sessions

    hydrate_orphan_webui_sessions()
    report = hydrate_orphan_webui_sessions()
    assert report["restored"] == 0
    assert report["failed"] == 0
    assert report["skipped"] == 3


def test_hydrate_handles_missing_state_db(tmp_path, monkeypatch):
    """If state.db is absent (fresh install), the helper must return zero
    counts without raising — boot must not block on this."""
    home = tmp_path
    monkeypatch.setenv("HERMES_HOME", str(home))
    import api.profiles as profiles
    monkeypatch.setattr(
        profiles, "get_active_hermes_home", lambda: str(home), raising=False
    )
    from api.models import hydrate_orphan_webui_sessions

    report = hydrate_orphan_webui_sessions()
    assert report == {"candidates": 0, "restored": 0, "skipped": 0, "failed": 0}


def test_server_boot_calls_hydrate():
    """Static assertion that server.main wires the bulk hydrate so a deploy
    that wipes webui/sessions/ still recovers on next restart."""
    src = (REPO_ROOT / "server.py").read_text()
    assert "hydrate_orphan_webui_sessions" in src, (
        "server.main() must call hydrate_orphan_webui_sessions on boot — "
        "without it, a state-dir migration loses the sidebar entries even "
        "though state.db still has every conversation"
    )
