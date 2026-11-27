"""#8450 P1: the /api/list DIRECTORY listing must serve the owner's project.

The greptile P1 named the producer gap; the user-visible surface it
reaches is the ``/api/list`` directory listing, whose CLI branch reads
``cli_meta.get("workspace")`` — the field the row producer fills. This
module closes that last gap with the REAL producer and the REAL handler:

    real state.db -> real row producer -> real /api/list handler

A mapped CLI session names ``/workspace``, which is absent on the WebUI
host by design. Before the fix the row carried the host-local fallback, so
the listing showed an unrelated project directory. After the fix the row
keeps the agent-visible name, the handler translates it under the owner
profile's authority, and the listing shows the OWNER's files.
"""

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import pytest

models = pytest.importorskip("api.models")
routes = pytest.importorskip("api.routes")
profiles = pytest.importorskip("api.profiles")
workspace_mod = pytest.importorskip("api.workspace")


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_state_db(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                model TEXT,
                message_count INTEGER DEFAULT 0,
                started_at REAL,
                title TEXT,
                cwd TEXT,
                archived INTEGER DEFAULT 0,
                hidden INTEGER DEFAULT 0,
                project_id TEXT
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
        for offset, (sid, source, title, cwd) in enumerate(rows):
            started = 1781024055.0 + offset
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, source, model, message_count, started_at, title, cwd) "
                "VALUES (?, ?, 'test-model', 2, ?, ?, ?)",
                (sid, source, started, title, cwd),
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


@pytest.fixture
def mapped_cli_world(tmp_path, monkeypatch):
    """A profile whose map sends agent ``/workspace`` to a real local dir.

    The decoy directory is where a host-local fallback would land; it holds
    a file with the SAME NAME but different bytes, so a listing rooted in
    the wrong project is observable and not merely a different path.
    """
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    alice_home = home / "profiles" / "alice"
    alice_home.mkdir(parents=True, exist_ok=True)

    alice_ws = home / "alice-ws"
    alice_ws.mkdir(parents=True, exist_ok=True)
    (alice_ws / "secret.txt").write_text("ALICE-EXTERNAL", encoding="utf-8")

    decoy = home / "process-default"
    decoy.mkdir(parents=True, exist_ok=True)
    (decoy / "secret.txt").write_text("WRONG-PROJECT", encoding="utf-8")

    _write(
        alice_home / "config.yaml",
        "webui:\n"
        "  workspace_path_maps:\n"
        "    - agent: /workspace\n"
        f"      webui: {alice_ws}\n"
        "terminal:\n"
        "  cwd: /workspace\n",
    )

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(workspace_mod, "_home_path", lambda: home)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: alice_home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alice")
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)

    return {
        "home": home,
        "alice_home": alice_home,
        "alice_ws": alice_ws,
        "decoy": decoy,
    }


def _produce_and_list(world, monkeypatch, *, session_id="cli-1", cwd="/workspace"):
    """Real producer -> real /api/list handler; returns (handler, row)."""
    db_path = world["alice_home"] / "state.db"
    _make_state_db(db_path, [(session_id, "cli", "A mapped session", cwd)])

    rows = models._load_cli_sessions_uncached(
        world["alice_home"], db_path, "alice", source_filter="cli"
    )
    row = {r["session_id"]: r for r in rows}[session_id]

    # The handler's CLI branch reads the metadata lookup; point it at the
    # row the REAL producer just built.
    monkeypatch.setattr(
        models,
        "lookup_cli_session_row",
        lambda sid, all_profiles=False: dict(row) if sid == session_id else {},
    )
    monkeypatch.setattr(
        routes,
        "_lookup_cli_session_metadata",
        lambda sid, all_profiles=False: dict(row) if sid == session_id else {},
    )

    def raise_key(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(models, "get_session", raise_key)
    monkeypatch.setattr(routes, "get_session", raise_key)
    monkeypatch.setattr(models, "state_db_has_session", lambda sid: True)

    handler = _FakeHandler()
    routes._handle_list_dir(
        handler, urlparse(f"/api/list?session_id={session_id}&path=.")
    )
    return handler, row


def test_api_list_serves_the_owner_mapped_root(mapped_cli_world, monkeypatch):
    """The listing must be rooted in the owner's project, not the fallback."""
    handler, row = _produce_and_list(mapped_cli_world, monkeypatch)

    assert handler.status == 200, bytes(handler.body).decode("utf-8", "replace")
    payload = handler.json_body()

    assert str(payload.get("workspace")) == str(
        mapped_cli_world["alice_ws"].resolve()
    ), {
        "workspace": payload.get("workspace"),
        "expected": str(mapped_cli_world["alice_ws"].resolve()),
    }

    names = {entry.get("name") for entry in payload.get("entries", [])}
    assert "secret.txt" in names, names


def test_api_list_reads_the_owner_bytes_not_the_fallback_bytes(
    mapped_cli_world, monkeypatch
):
    """The listed file must be the owner's, not the decoy's same-named file.

    Both directories contain ``secret.txt``; only the bytes differ. The
    listing therefore proves WHICH project the surface is rooted in.
    """
    handler, _row = _produce_and_list(mapped_cli_world, monkeypatch)

    assert handler.status == 200, bytes(handler.body).decode("utf-8", "replace")
    payload = handler.json_body()

    listed = [
        entry
        for entry in payload.get("entries", [])
        if entry.get("name") == "secret.txt"
    ]
    assert listed, payload

    root = Path(payload["workspace"])
    content = (root / "secret.txt").read_text(encoding="utf-8")
    assert content == "ALICE-EXTERNAL", {
        "root": str(root),
        "content": content,
    }
    assert content != (mapped_cli_world["decoy"] / "secret.txt").read_text(
        encoding="utf-8"
    )


def test_api_list_row_keeps_the_agent_visible_workspace(
    mapped_cli_world, monkeypatch
):
    """The row keeps the AGENT-VISIBLE name; the handler translates it."""
    _handler, row = _produce_and_list(mapped_cli_world, monkeypatch)

    assert row["workspace"] == "/workspace", {
        "workspace": row["workspace"],
        "note": "the agent-visible name must survive the row producer",
    }
