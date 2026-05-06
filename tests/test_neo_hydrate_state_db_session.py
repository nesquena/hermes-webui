"""Onda 6 — chat/start, session/update and list/dir must rehydrate a Session
from state.db when the WebUI sidecar JSON is missing.

Production scenario the test covers: a session created by the WebUI was
mirrored to state.db by the agent runtime, but the sidecar in
``~/.hermes/webui/sessions/<sid>.json`` is gone (deploy state-dir migration,
manual cleanup, mid-write OSError). With the legacy code paths, GET
/api/session returned 200 via a CLI-store fallback, but POST /api/chat/start
and POST /api/session/update returned 404, leaving the user unable to chat
into a session they could see in the sidebar. The Telegram/WhatsApp gateway
case has the same shape — same fix.
"""
import json
import pathlib
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))


def _bootstrap_state_db(tmp_path: Path) -> Path:
    """Create a minimal state.db that matches what hermes-agent ships, with
    one webui-source session and two messages. Schema cribbed from
    /opt/hermes/hermes_state.py:43-90."""
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
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
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, message_count, title) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("76c1dc3ccd9e", "webui", "gpt-5.5", 1700000000.0, 2, "Resumo da reunião"),
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            [
                ("76c1dc3ccd9e", "user", "olá", 1700000010.0),
                ("76c1dc3ccd9e", "assistant", "Olá! Em que posso ajudar?", 1700000011.0),
            ],
        )
    return db


@pytest.fixture
def isolated_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a sandbox that contains the synthetic state.db
    but NO matching ``webui/sessions/<sid>.json`` file — the exact production
    state we are reproducing."""
    home = tmp_path
    _bootstrap_state_db(home)
    # Ensure webui/sessions/ exists but is empty so save() works.
    (home / "webui" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # api.models reads HERMES_HOME via api.profiles.get_active_hermes_home().
    # Force the active-profile resolver onto the sandbox too so we don't
    # accidentally hit ~/.hermes on the developer's machine.
    import api.profiles as profiles
    monkeypatch.setattr(
        profiles, "get_active_hermes_home", lambda: str(home), raising=False
    )
    return home


def test_hydrate_returns_session_with_messages(isolated_hermes_home):
    from api.models import hydrate_session_from_state_db

    s = hydrate_session_from_state_db("76c1dc3ccd9e")
    assert s is not None, "state.db has the row — hydrate must rebuild a Session"
    assert s.session_id == "76c1dc3ccd9e"
    assert s.title == "Resumo da reunião"
    assert s.model == "gpt-5.5"
    assert len(s.messages) == 2
    assert s.messages[0]["role"] == "user"
    assert s.messages[0]["content"] == "olá"
    assert s.messages[1]["role"] == "assistant"


def test_hydrate_returns_none_for_unknown_sid(isolated_hermes_home):
    from api.models import hydrate_session_from_state_db

    assert hydrate_session_from_state_db("ffffffffffff") is None


def test_hydrate_rejects_invalid_sid_format(isolated_hermes_home):
    """Path-traversal guard: only lowercase hex + underscore."""
    from api.models import hydrate_session_from_state_db

    assert hydrate_session_from_state_db("../etc/passwd") is None
    assert hydrate_session_from_state_db("UPPERCASE") is None


def test_chat_start_uses_hydrate_fallback():
    """Static guard: chat/start must call hydrate_session_from_state_db on
    the KeyError path. A future refactor that drops this would re-introduce
    the production 'Session not found' dead-end."""
    routes_src = (REPO_ROOT / "api" / "routes.py").read_text()
    chat_start = routes_src.find("def _handle_chat_start(")
    assert chat_start != -1
    # Inspect only the function body, not the whole file.
    next_def = routes_src.find("\ndef ", chat_start + 1)
    body = routes_src[chat_start:next_def]
    assert "hydrate_session_from_state_db(body[\"session_id\"])" in body, (
        "_handle_chat_start must call hydrate_session_from_state_db on KeyError"
    )


def test_session_update_uses_hydrate_fallback():
    routes_src = (REPO_ROOT / "api" / "routes.py").read_text()
    block = routes_src[routes_src.find('"/api/session/update"'):]
    block = block[: block.find('parsed.path ==', 50)]  # next route block
    assert "hydrate_session_from_state_db(body[\"session_id\"])" in block, (
        "/api/session/update must call hydrate_session_from_state_db on KeyError"
    )


def test_list_dir_uses_hydrate_fallback_after_cli_lookup():
    routes_src = (REPO_ROOT / "api" / "routes.py").read_text()
    fn = routes_src[routes_src.find("def _handle_list_dir("):]
    fn = fn[: fn.find("\ndef ")]
    cli_idx = fn.find("get_cli_sessions()")
    hydrate_idx = fn.find("hydrate_session_from_state_db(sid)")
    assert cli_idx != -1, "list_dir must keep the get_cli_sessions() probe"
    assert hydrate_idx != -1, "list_dir must hydrate on miss"
    assert cli_idx < hydrate_idx, (
        "hydrate_session_from_state_db must run AFTER the get_cli_sessions() "
        "probe so we don't bypass the cheap path"
    )
