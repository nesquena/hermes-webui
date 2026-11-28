"""#8450: the REAL CLI row producer must preserve the agent's own workspace.

greptile P1 on ``api/models.py`` (comment 4711527086, "External actions
target fallback files"):

    If a local profile maps ``/workspace`` to an existing WebUI directory but
    ``/workspace`` is absent on the WebUI host, real CLI rows lose that agent
    path before reaching this branch. Their ``workspace`` comes from
    ``get_last_workspace()``, which rejects the missing host path.
    ``_profile_default_workspace()`` makes the same check on ``terminal.cwd``
    and falls back to the process default. This branch then uses that fallback
    directory, so reads and deletes can affect the wrong project. Preserve the
    agent-visible choice when its mapped local directory is valid, before
    building the CLI row. Add a test using the real row producer; the current
    tests supply ``/workspace`` directly.

The previous suite injected ``{"workspace": "/workspace"}`` straight into the
CLI-row seam, so it never exercised the producer that actually builds those
rows. This module builds a REAL ``state.db``, runs the REAL projection
(``_load_cli_sessions_uncached``), and asserts that the projected row keeps
the agent-visible workspace instead of the host-local fallback.

The world is production-shaped: a real profile home with a real
``config.yaml`` declaring the map, a real SQLite ``state.db`` with the
agent-side ``cwd``, and the real resolver chain. Only the request-profile
binding and the saved-workspace list are provided.
"""

import sqlite3
from pathlib import Path

import pytest

models = pytest.importorskip("api.models")
profiles = pytest.importorskip("api.profiles")
workspace_mod = pytest.importorskip("api.workspace")


# ── production-shaped world ────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_state_db(path: Path, rows) -> None:
    """Create a real agent-shaped state.db holding ``rows``.

    ``rows`` is a sequence of ``(session_id, source, title, cwd)``.
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
                title TEXT,
                api_call_count INTEGER DEFAULT 0,
                handoff_state TEXT,
                handoff_platform TEXT,
                handoff_error TEXT,
                cwd TEXT,
                rewind_count INTEGER DEFAULT 0,
                archived INTEGER DEFAULT 0,
                hidden INTEGER DEFAULT 0,
                project_id TEXT
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
def mapped_profile(tmp_path, monkeypatch):
    """A profile whose map sends agent ``/workspace`` to a real local dir.

    ``/workspace`` does NOT exist on this host — that is the whole point: the
    agent-side path lives in the agent's container, and the WebUI host only
    knows the mapped local root.
    """
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    alice_home = home / "profiles" / "alice"
    alice_home.mkdir(parents=True, exist_ok=True)

    alice_ws = home / "alice-ws"
    alice_ws.mkdir(parents=True, exist_ok=True)
    (alice_ws / "secret.txt").write_text("ALICE-EXTERNAL", encoding="utf-8")

    # The process default — where a fallback would land. Deliberately a
    # DIFFERENT directory that holds a decoy file, so a wrong root is
    # observable rather than merely different.
    process_default = home / "process-default"
    process_default.mkdir(parents=True, exist_ok=True)
    (process_default / "secret.txt").write_text("WRONG-PROJECT", encoding="utf-8")

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
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)

    return {
        "home": home,
        "alice_home": alice_home,
        "alice_ws": alice_ws,
        "process_default": process_default,
    }


def _produce_rows(world, monkeypatch, *, session_id="cli-1", cwd="/workspace"):
    """Run the REAL CLI row producer against a real state.db."""
    db_path = world["alice_home"] / "state.db"
    _make_state_db(
        db_path,
        [(session_id, "cli", "A mapped session", cwd)],
    )

    monkeypatch.setattr(
        profiles, "get_active_hermes_home", lambda: world["alice_home"]
    )
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alice")
    # The saved-workspace list is the one seam the #7217 suites already
    # provide; the last-workspace file is NOT stubbed, so the real
    # get_last_workspace() resolution runs.
    monkeypatch.setattr(
        workspace_mod,
        "load_workspaces",
        lambda profile=None: [{"path": str(world["process_default"])}],
    )

    rows = models._load_cli_sessions_uncached(
        world["alice_home"], db_path, "alice", source_filter="cli"
    )
    return {row["session_id"]: row for row in rows}


# ── the regression ─────────────────────────────────────────────────────────


def test_real_row_producer_keeps_agent_visible_workspace(
    mapped_profile, monkeypatch
):
    """The projected row must carry the agent's own ``/workspace``.

    ``/workspace`` is absent on this host, so the previous behaviour
    (``get_last_workspace()``) replaced it with the process default — and
    every later read/delete resolved inside an unrelated project directory.
    """
    rows = _produce_rows(mapped_profile, monkeypatch)

    assert "cli-1" in rows, rows
    projected = rows["cli-1"]
    assert projected["workspace"] == "/workspace", {
        "workspace": projected["workspace"],
        "note": "the agent-visible workspace must survive the row producer",
    }


def test_real_row_producer_workspace_resolves_to_the_mapped_root(
    mapped_profile, monkeypatch
):
    """The preserved workspace must translate to the mapped local root.

    This is the user-visible consequence: a listing, a read and a delete
    must land inside the OWNER's project, not inside the fallback.
    """
    rows = _produce_rows(mapped_profile, monkeypatch)
    projected = rows["cli-1"]

    resolved = workspace_mod.resolve_webui_workspace(
        projected["workspace"], profile="alice"
    )
    assert Path(resolved) == mapped_profile["alice_ws"].resolve(), {
        "resolved": str(resolved),
        "expected": str(mapped_profile["alice_ws"].resolve()),
    }

    # And the bytes come from the owner-selected project, not the decoy.
    assert (Path(resolved) / "secret.txt").read_text(encoding="utf-8") == (
        "ALICE-EXTERNAL"
    )
    assert (mapped_profile["process_default"] / "secret.txt").read_text(
        encoding="utf-8"
    ) == "WRONG-PROJECT"


def test_row_without_agent_cwd_keeps_the_host_local_fallback(
    mapped_profile, monkeypatch
):
    """A session with no recorded agent cwd keeps the previous behaviour."""
    rows = _produce_rows(
        mapped_profile, monkeypatch, session_id="cli-2", cwd=""
    )

    assert "cli-2" in rows, rows
    projected = rows["cli-2"]
    # No agent cwd -> the host-local last workspace, exactly as before. The
    # value is whatever the real get_last_workspace() resolves on this host,
    # so the assertion is against that resolution rather than a fixed path.
    expected = workspace_mod.get_last_workspace(profile="alice")
    assert projected["workspace"] == str(expected), {
        "workspace": projected["workspace"],
        "expected": str(expected),
    }
    # Never the agent path, and never the decoy: the fallback is the
    # pre-existing host-local behaviour, unchanged.
    assert projected["workspace"] != "/workspace"
    assert projected["workspace"] != str(mapped_profile["alice_ws"])


def test_unmapped_agent_path_does_not_widen_the_trust_boundary(
    mapped_profile, monkeypatch
):
    """An agent path outside every declared map keeps the fallback.

    The fix must preserve the agent's choice only when it is honourable on
    this host; an unmapped path must not slip through and must not become a
    sibling-mount escape.
    """
    rows = _produce_rows(
        mapped_profile, monkeypatch, session_id="cli-3", cwd="/etc"
    )

    assert "cli-3" in rows, rows
    projected = rows["cli-3"]
    assert projected["workspace"] != "/etc", {
        "workspace": projected["workspace"],
        "note": "an unmapped/untrusted agent path must not be adopted",
    }
