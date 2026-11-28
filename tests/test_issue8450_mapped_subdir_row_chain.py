"""#8450: the two fixes must work TOGETHER.

A session whose agent-side working directory sits BELOW its mapped root
(``/workspace/project``) exercises both halves of this branch's workspace
contract at once:

  1. the row producer must keep the agent-visible name
     (``/workspace/project``) instead of the host-local fallback;
  2. the translation tier must map it onto the mapped root's sub-directory
     even though that sub-directory is not trusted in its own right;
  3. the user-visible ``/api/list`` directory listing must then serve the
     OWNER's files from that sub-directory.

Neither fix alone produces that outcome: (1) without (2) the listing 404s,
and (2) without (1) the row never names the sub-directory at all.

The mapped root deliberately lives outside the home directory and outside
the boot default, so the mapped root itself is the only thing that can
admit the sub-directory.
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
def mapped_subdir_world(tmp_path, monkeypatch):
    """A profile whose map root is saved and holds a ``project/`` sub-dir."""
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    alice_home = home / "profiles" / "alice"
    alice_home.mkdir(parents=True, exist_ok=True)

    mapped_root = tmp_path / "data" / "alice"
    project = mapped_root / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "file.txt").write_text("OWNER-SUBDIR-BYTES", encoding="utf-8")

    _write(
        alice_home / "config.yaml",
        "webui:\n"
        "  workspace_path_maps:\n"
        "    - agent: /workspace\n"
        f"      webui: {mapped_root}\n",
    )

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(workspace_mod, "_home_path", lambda: home)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: alice_home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alice")
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(
        workspace_mod,
        "load_workspaces",
        lambda profile=None: [{"path": str(mapped_root)}],
    )

    return {
        "home": home,
        "alice_home": alice_home,
        "mapped_root": mapped_root,
        "project": project,
    }


def _produce_and_list(world, monkeypatch, *, session_id="cli-sub", cwd="/workspace/project"):
    db_path = world["alice_home"] / "state.db"
    _make_state_db(db_path, [(session_id, "cli", "A mapped session", cwd)])

    rows = models._load_cli_sessions_uncached(
        world["alice_home"], db_path, "alice", source_filter="cli"
    )
    row = {r["session_id"]: r for r in rows}[session_id]

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


def test_session_below_the_mapped_root_lists_the_owner_files(
    mapped_subdir_world, monkeypatch
):
    """The whole chain works for a session below the mapped root."""
    handler, row = _produce_and_list(mapped_subdir_world, monkeypatch)

    # (1) the row keeps the agent-visible name
    assert row["workspace"] == "/workspace/project", {
        "workspace": row["workspace"],
    }

    # (2) the listing resolves to the mapped root's sub-directory
    assert handler.status == 200, bytes(handler.body).decode("utf-8", "replace")
    payload = handler.json_body()
    assert str(payload.get("workspace")) == str(
        mapped_subdir_world["project"].resolve()
    ), {
        "workspace": payload.get("workspace"),
        "expected": str(mapped_subdir_world["project"].resolve()),
    }

    # (3) and it serves the owner's bytes
    names = {entry.get("name") for entry in payload.get("entries", [])}
    assert "file.txt" in names, names
    content = (Path(payload["workspace"]) / "file.txt").read_text(encoding="utf-8")
    assert content == "OWNER-SUBDIR-BYTES", {"content": content}
