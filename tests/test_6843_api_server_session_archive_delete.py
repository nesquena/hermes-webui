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

Review regressions covered (#6855): the ``archived`` projection must not break
older state.db schemas without an ``archived`` column, the state.db
``archived`` fallback is scoped to the api_server class only (gateway /
messaging / cron / subagent rows keep their existing visibility), and the
delete bypass for non-path-safe ids requires the row's source to be the
imported/read-only api_server class (a messaging row still 400s).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.models as models
import api.routes as routes
from api.models import SESSIONS


def _make_state_db(path: Path, sid: str, *, source: str = "api_server",
                   title: str | None = None, archived: int = 0,
                   message_count: int = 2,
                   include_archived_col: bool = True) -> None:
    """Create a minimal state.db with one session row and a few messages.

    Schema mirrors hermes_state.SessionDB closely enough for
    read_importable_agent_session_rows / delete_cli_session.
    ``include_archived_col=False`` reproduces older agent schemas that have NO
    ``archived`` column (the #6843 projection must not break on those).
    """
    archived_ddl = ", archived INTEGER DEFAULT 0" if include_archived_col else ""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        f"""
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
            cwd TEXT{archived_ddl}
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
    if include_archived_col:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(id, source, model, message_count, started_at, title, cwd, archived) "
            "VALUES (?, ?, 'deepseek/deepseek-chat', ?, 1781024055.0, ?, '/root', ?)",
            (sid, source, message_count, title, archived),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(id, source, model, message_count, started_at, title, cwd) "
            "VALUES (?, ?, 'deepseek/deepseek-chat', ?, 1781024055.0, ?, '/root')",
            (sid, source, message_count, title),
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
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400: captured.update(
            payload={"error": msg},
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


def test_archive_read_only_api_server_session_hides_from_listing(
    isolated_state_db, monkeypatch
):
    """A read_only imported row of the api_server class is WebUI-local view
    state for archiving: flip state.db instead of the previous blanket 400,
    AND confirm end-to-end that the row actually disappears from the default
    visible sidebar (review #6855) — the state.db flag alone is not proof of
    visibility, since the sidebar only consumes `archived` for api_server
    class rows.
    """
    sid = "ro-api-server-imported-1"
    _make_state_db(isolated_state_db["db"], sid, source="api_server", title="API imported")
    cli_meta = {
        "session_id": sid,
        "source_tag": "api_server",
        "raw_source": "api_server",
        "session_source": "api_server",
        "read_only": True,
        "profile": "default",
    }
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda s: cli_meta)

    before = _sidebar_sessions()
    assert any(s.get("session_id") == sid for s in before["sessions"]), (
        "read_only api_server row must appear in the default sidebar before archive"
    )

    captured = _capture_post(monkeypatch, {"session_id": sid, "archived": True})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert _read_archived(isolated_state_db["db"], sid) == 1
    after = _sidebar_sessions()
    assert all(s.get("session_id") != sid for s in after["sessions"]), (
        "archived read_only api_server row must leave the default visible sidebar (#6855)"
    )


def test_archive_read_only_telegram_session_still_400s(
    isolated_state_db, monkeypatch
):
    """A read_only row from a NON-api_server source (telegram) must keep the
    prior 400 rejection (review #6855): the sidebar only consumes the state.db
    `archived` flag for api_server-class rows, so flipping it for a telegram
    row would return 200 yet leave the session visible — a silent no-op. The
    flag must stay untouched and the row must stay visible.
    """
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

    assert captured["status"] == 400
    assert "Read-only imported sessions cannot be archived" in str(
        captured["payload"].get("error", "")
    )
    assert _read_archived(isolated_state_db["db"], sid) == 0, (
        "state.db archived flag must not be flipped for a rejected read-only telegram row"
    )
    sessions = _sidebar_sessions()
    assert any(s.get("session_id") == sid for s in sessions["sessions"]), (
        "rejected read-only telegram row must stay visible in the default sidebar"
    )


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
# Session-visibility regression (review #6855)
# ---------------------------------------------------------------------------


def test_non_archived_gateway_session_stays_visible(isolated_state_db, monkeypatch):
    """A non-archived gateway/discord session must still appear in /api/sessions.

    Regression for the #6843 review: the archived-column projection broke on
    older state.db schemas that have NO ``archived`` column (the generated
    ``0 AS archived AS archived`` SQL raised, so the whole projection died and
    sidecar-less gateway/messaging/cron rows vanished), and the sidecar-less
    ``archived`` fallback must not hide non-api_server rows.
    """
    sid = "gw_dc_visible_001"
    _make_state_db(
        isolated_state_db["db"],
        sid,
        source="discord",
        title="DC Visible Chat",
        include_archived_col=False,
    )

    sessions = _sidebar_sessions()
    row = next(
        (s for s in sessions["sessions"] if s.get("session_id") == sid),
        None,
    )
    assert row is not None, "non-archived gateway/discord session must stay visible"
    assert row["archived"] is False


def test_state_db_archived_flag_only_hides_api_server_class(isolated_state_db, monkeypatch):
    """The state.db ``archived`` fallback is scoped to the api_server class.

    A gateway/discord row with ``archived=1`` in state.db (no sidecar) is
    agent-owned state and must KEEP its existing visibility — only imported
    api_server-class rows are hidden by the fallback (#6843 review).
    """
    gw_sid = "gw_dc_archived_002"
    _make_state_db(isolated_state_db["db"], gw_sid, source="discord",
                   title="DC Archived Chat", archived=1)

    api_sid = "miloco:agent:main:miloco-suggest:miloco-suggest:3c4d5e6f7a8b"
    _make_state_db(isolated_state_db["db"], api_sid, title=None, archived=1)

    sessions = _sidebar_sessions()
    assert any(s.get("session_id") == gw_sid for s in sessions["sessions"]), (
        "archived-in-state.db gateway row must stay visible (agent-owned state)"
    )
    assert all(s.get("session_id") != api_sid for s in sessions["sessions"]), (
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


def test_delete_unsafe_messaging_session_still_400s(isolated_state_db, monkeypatch):
    """An unsafe id whose state.db row is a messaging session must still 400.

    The #6843 delete bypass is scoped to the imported/read-only api_server
    class. A non-path-safe id backed by a real messaging row (e.g. a discord
    gateway session id containing colons) must NOT bypass the guard — the row
    and its transcript must be left intact (review #6855).
    """
    sid = "discord:guild:channel:msg:9f8e7d6c5b4a"
    _make_state_db(isolated_state_db["db"], sid, source="discord",
                   title="DC Chat", archived=0)

    assert models.is_safe_session_id(sid) is False, (
        "precondition: the messaging id is not path-safe"
    )

    captured = _capture_post(monkeypatch, {"session_id": sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 400, captured
    assert "Invalid session_id" in str(captured["payload"].get("error", ""))
    assert _row_exists(isolated_state_db["db"], sid) is True, (
        "messaging row must survive an unsafe-id delete attempt"
    )


def test_delete_rejected_unsafe_messaging_session_leaves_artifacts_intact(
    isolated_state_db, monkeypatch
):
    """A rejected unsafe-id delete must leave ALL artifacts intact (review
    #6855): the api_server-class authorization now runs BEFORE the destructive
    cleanup, so the sidecar, the .json.bak snapshot, the session index entry
    and the state.db row all survive the 400.
    """
    sid = "discord:guild:channel:msg:0a1b2c3d4e5f"
    _make_state_db(isolated_state_db["db"], sid, source="discord",
                   title="DC Chat", archived=0)

    # Seed the artifacts the cleanup path would otherwise remove: a sidecar,
    # its .json.bak snapshot and a session-index entry for the rejected id.
    sessions_dir = isolated_state_db["sessions_dir"]
    sidecar = sessions_dir / f"{sid}.json"
    sidecar.write_text('{"session_id": "%s"}' % sid, encoding="utf-8")
    (sessions_dir / f"{sid}.json.bak").write_text(
        '{"session_id": "%s"}' % sid, encoding="utf-8"
    )
    index_path = isolated_state_db["index_path"]
    index_path.write_text(
        json.dumps([{"session_id": sid, "source_tag": "discord"}]), encoding="utf-8"
    )

    assert models.is_safe_session_id(sid) is False

    captured = _capture_post(monkeypatch, {"session_id": sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 400, captured
    assert "Invalid session_id" in str(captured["payload"].get("error", ""))
    assert sidecar.exists(), "sidecar must survive a rejected unsafe-id delete"
    assert (sessions_dir / f"{sid}.json.bak").exists(), (
        ".json.bak snapshot must survive a rejected unsafe-id delete"
    )
    index_entries = json.loads(index_path.read_text(encoding="utf-8"))
    assert any(e.get("session_id") == sid for e in index_entries), (
        "session index entry must survive a rejected unsafe-id delete"
    )
    assert _row_exists(isolated_state_db["db"], sid) is True, (
        "state.db row must survive a rejected unsafe-id delete"
    )


# ---------------------------------------------------------------------------
# Re-gate r3 (#6855): exact-membership, atomic check+act, attachment identity,
# archive fail-open
# ---------------------------------------------------------------------------


def test_api_server_class_source_predicates_require_exact_membership():
    """Findings 1 + re-gate r4 (#6855): the api_server-class predicates must
    be EXACT raw membership on the authoritative source. ``api-server``
    (hyphen), ``API_SERVER`` (case), `` api_server`` (whitespace) and
    ``api_serverx`` (prefix/suffix) are all effectively-unknown sources and
    must NOT authorize the destructive paths (previously strip().lower()
    normalized them into the set)."""
    from api.agent_sessions import is_api_server_class_row

    assert routes._is_api_server_class_state_db_source("api_server") is True
    assert routes._is_api_server_class_state_db_source("api") is True
    assert routes._is_api_server_class_state_db_source("API_SERVER") is False
    assert routes._is_api_server_class_state_db_source("Api_Server") is False
    assert routes._is_api_server_class_state_db_source(" api_server") is False
    assert routes._is_api_server_class_state_db_source("api_server ") is False
    assert routes._is_api_server_class_state_db_source("api_serverx") is False
    assert routes._is_api_server_class_state_db_source("xapi_server") is False
    assert routes._is_api_server_class_state_db_source("api-server") is False
    assert routes._is_api_server_class_state_db_source("api server") is False
    assert routes._is_api_server_class_state_db_source("telegram") is False
    assert routes._is_api_server_class_state_db_source("") is False

    assert is_api_server_class_row({"source": "api_server"}) is True
    assert is_api_server_class_row({"source": "api"}) is True
    assert is_api_server_class_row({"source": "API_SERVER"}) is False
    assert is_api_server_class_row({"source": " api_server"}) is False
    assert is_api_server_class_row({"source": "api_server "}) is False
    assert is_api_server_class_row({"source": "api_serverx"}) is False
    assert is_api_server_class_row({"source": "xapi_server"}) is False
    assert is_api_server_class_row({"source": "api-server"}) is False
    assert is_api_server_class_row({"source": "telegram", "source_tag": "api_server"}) is False, (
        "derived labels must not upgrade a non-api_server authoritative source"
    )


def test_delete_unsafe_api_server_hyphen_source_still_400s(isolated_state_db, monkeypatch):
    """Finding 1 end-to-end: an unsafe id backed by an ``api-server``
    (hyphen) row is NOT the api_server class — delete must 400 and the row
    must survive (the hyphen was previously normalized to api_server)."""
    sid = "hyphen:api:server:imported:1a2b3c4d5e6f"
    _make_state_db(isolated_state_db["db"], sid, source="api-server", title=None)

    assert models.is_safe_session_id(sid) is False

    captured = _capture_post(monkeypatch, {"session_id": sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 400, captured
    assert "Invalid session_id" in str(captured["payload"].get("error", ""))
    assert _row_exists(isolated_state_db["db"], sid) is True


@pytest.mark.parametrize(
    "raw_source",
    [
        "API_SERVER",
        "Api_Server",
        " api_server",
        "api_server ",
        "api_serverx",
        "xapi_server",
        " api ",
    ],
)
def test_delete_unsafe_id_noncanonical_api_server_source_still_400s(
    isolated_state_db, monkeypatch, raw_source
):
    """Re-gate r4 (#6855): only the EXACT canonical ``api``/``api_server``
    raw source authorizes the unsafe-id delete bypass. Uppercase, whitespace,
    prefix and suffix variants of ``api_server`` must fail closed — the row
    and its sidecar survive (previously ``strip().lower()`` normalized them
    into the set and delete removed the row)."""
    sid = "noncanon:%s:imported:1a2b3c4d5e6f" % raw_source.replace(" ", "_")
    _make_state_db(isolated_state_db["db"], sid, source=raw_source, title=None)
    sidecar = isolated_state_db["sessions_dir"] / f"{sid}.json"
    sidecar.write_text('{"session_id": "%s"}' % sid, encoding="utf-8")

    assert models.is_safe_session_id(sid) is False

    captured = _capture_post(monkeypatch, {"session_id": sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 400, captured
    assert "Invalid session_id" in str(captured["payload"].get("error", ""))
    assert _row_exists(isolated_state_db["db"], sid) is True, (
        "non-canonical api_server row must survive a rejected unsafe-id delete"
    )
    assert sidecar.exists(), "sidecar must survive a rejected non-canonical unsafe-id delete"


@pytest.mark.parametrize(
    "raw_source",
    ["API_SERVER", " api_server", "api_serverx", "xapi_server"],
)
def test_archive_unsafe_id_noncanonical_api_server_source_still_400s(
    isolated_state_db, monkeypatch, raw_source
):
    """Re-gate r4 (#6855): archive of an unsafe id whose state.db source is a
    non-canonical api_server variant fails closed — the ``archived`` flag is
    untouched (previously ``strip().lower()`` authorized it and set the flag,
    which the sidebar never consumes for non-canonical rows)."""
    sid = "archnoncanon:%s:imported:1a2b3c4d5e6f" % raw_source.replace(" ", "_")
    _make_state_db(isolated_state_db["db"], sid, source=raw_source, title=None)
    cli_meta = {
        "session_id": sid,
        "source_tag": raw_source,
        "raw_source": raw_source,
        "session_source": "other",
        "read_only": True,
        "profile": "default",
    }
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda s: cli_meta)

    captured = _capture_post(monkeypatch, {"session_id": sid, "archived": True})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True

    assert captured["status"] == 400, captured
    assert _read_archived(isolated_state_db["db"], sid) == 0, (
        "archive must fail closed for non-canonical api_server sources (#6855)"
    )


def test_delete_unsafe_id_source_flip_fails_closed_atomically(isolated_state_db, monkeypatch):
    """Finding 2 (#6855): the api_server-class source is revalidated INSIDE
    the BEGIN IMMEDIATE delete transaction. A source that flips between the
    route-level check and the delete (authorization saw api_server, the row
    is now gateway — a non-api_server, non-messaging source) must fail
    closed: 400, row survives, sidecar untouched. (A flip to a messaging
    source is independently protected by the messaging guard, which skips
    the state.db delete entirely.)"""
    sid = "flip:agent:main:0a1b2c3d4e5f"
    _make_state_db(isolated_state_db["db"], sid, source="gateway", title="Gateway run")
    # Simulate the TOCTOU: the route-level pre-check sees api_server...
    monkeypatch.setattr(routes, "_state_db_session_source", lambda s: "api_server")
    # ...but the state.db row the transaction reads is really gateway.
    sidecar = isolated_state_db["sessions_dir"] / f"{sid}.json"
    sidecar.write_text('{"session_id": "%s"}' % sid, encoding="utf-8")

    assert models.is_safe_session_id(sid) is False
    assert routes._is_messaging_session_id(sid) is False, (
        "precondition: gateway is not messaging, so the in-transaction gate is what denies"
    )

    captured = _capture_post(monkeypatch, {"session_id": sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 400, captured
    assert "Invalid session_id" in str(captured["payload"].get("error", ""))
    assert _row_exists(isolated_state_db["db"], sid) is True, (
        "gateway row must survive an unsafe-id delete whose source flipped after authz"
    )
    assert sidecar.exists(), "sidecar must survive the atomic denial"


def test_delete_cli_session_require_source_in_gates_atomically(isolated_state_db):
    """Finding 2 unit level: delete_cli_session(require_source_in=...) denies
    a row whose CURRENT source is not an exact member — inside the same
    transaction — and still deletes when it is."""
    db = isolated_state_db["db"]
    tel = "tel-gated-001"
    _make_state_db(db, tel, source="telegram", title="Telegram chat")
    assert models.delete_cli_session(tel, require_source_in=("api", "api_server")) is False
    assert _row_exists(db, tel) is True, "denied source must leave the row intact"

    api = "api-gated-001"
    _make_state_db(db, api, source="api_server", title="API imported")
    assert models.delete_cli_session(api, require_source_in=("api", "api_server")) is True
    assert _row_exists(db, api) is False


def test_delete_unsafe_id_skips_attachment_cleanup_collision(
    isolated_state_db, monkeypatch, tmp_path
):
    """Finding 3 (#6855): the attachment sanitizer collapses distinct ids
    (``victim:session`` vs ``victim_session``) onto ONE directory. Deleting
    an unsafe id must therefore SKIP attachment cleanup, so the safe
    session's uploads are never erased (reproduced cross-session data loss)."""
    import re as _re

    import api.upload as upload

    root = tmp_path / "attachments"

    def fake_dir(session_id, *, root=root):
        dest = (
            root / _re.sub(r"[^\w.\-]", "_", str(session_id or "session"))[:120]
        ).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    monkeypatch.setattr(upload, "_session_attachment_dir", fake_dir)

    safe_sid = "victim_session"
    unsafe_sid = "victim:session"
    # Both ids sanitize to the same directory in the real code; mirror that.
    assert fake_dir(unsafe_sid) == fake_dir(safe_sid)

    _make_state_db(isolated_state_db["db"], unsafe_sid, source="api_server", title=None)
    victim_file = fake_dir(safe_sid) / "photo.png"
    victim_file.write_bytes(b"data")

    assert models.is_safe_session_id(unsafe_sid) is False

    captured = _capture_post(monkeypatch, {"session_id": unsafe_sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200, captured
    assert victim_file.exists(), (
        "deleting unsafe id %r must not erase %r's attachments "
        "(identity collision, #6855)" % (unsafe_sid, safe_sid)
    )


def test_delete_safe_id_still_cleans_attachments(isolated_state_db, monkeypatch, tmp_path):
    """Finding 3 guard: the skip is scoped to UNSAFE ids only — a normal
    safe-id delete still removes its own attachment directory."""
    import re as _re

    import api.upload as upload

    root = tmp_path / "attachments"

    def fake_dir(session_id, *, root=root):
        dest = (
            root / _re.sub(r"[^\w.\-]", "_", str(session_id or "session"))[:120]
        ).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    monkeypatch.setattr(upload, "_session_attachment_dir", fake_dir)

    sid = "safe_sess_att_001"
    _make_state_db(isolated_state_db["db"], sid, source="webui", title="Safe")
    attach = fake_dir(sid)
    (attach / "doc.pdf").write_bytes(b"data")

    captured = _capture_post(monkeypatch, {"session_id": sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200, captured
    assert not (attach / "doc.pdf").exists(), (
        "safe-id delete must still clean its own attachments"
    )


def test_delete_path_safe_api_server_class_row_fails_closed(
    isolated_state_db, monkeypatch
):
    """Re-gate r5 (#6855): the path-safe state.db delete must not erase an
    api_server-class row. The branch is reached only for WebUI-owned rows, but
    the destructive call authorizes on the provenance resolved at the call
    site (``cli_meta_for_delete``) — an api_server-class row that slips past
    the upstream read_only guard must survive with ``state_db_cleanup_failed``
    reported (fail closed), never be deleted by an unrestricted call.
    """
    sid = "path-safe-ro-import-1"
    _make_state_db(isolated_state_db["db"], sid, source="api_server", title="API imported")
    # Deliberately NO ``read_only`` key: the destructive call must not depend
    # on the upstream read_only projection staying correct (#6855).
    cli_meta = {
        "session_id": sid,
        "source_tag": "api_server",
        "raw_source": "api_server",
        "session_source": "api_server",
        "profile": "default",
    }
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda s: cli_meta)

    assert models.is_safe_session_id(sid) is True
    assert routes._is_messaging_session_id(sid) is False, (
        "precondition: the api_server row is not messaging, so the branch runs"
    )

    captured = _capture_post(monkeypatch, {"session_id": sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200, captured
    assert captured["payload"]["state_db_cleanup_failed"] is True, (
        "path-safe api_server-class delete must report cleanup failure, not erase the row"
    )
    assert _row_exists(isolated_state_db["db"], sid) is True, (
        "api_server-class row must survive the path-safe delete (#6855)"
    )


def test_delete_path_safe_webui_row_deletes_with_exact_source_gate(
    isolated_state_db, monkeypatch
):
    """Re-gate r5 (#6855): a WebUI-owned path-safe row is deleted with the
    exact source resolved at the call site re-verified inside the delete
    transaction — the positive counterpart of the api_server-class denial.
    """
    sid = "path-safe-webui-own-1"
    _make_state_db(isolated_state_db["db"], sid, source="webui", title="Safe")
    cli_meta = {
        "session_id": sid,
        "source_tag": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "profile": "default",
    }
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda s: cli_meta)

    assert models.is_safe_session_id(sid) is True
    assert routes._is_messaging_session_id(sid) is False

    captured = _capture_post(monkeypatch, {"session_id": sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200, captured
    assert captured["payload"]["state_db_cleanup_failed"] is False, captured
    assert _row_exists(isolated_state_db["db"], sid) is False, (
        "WebUI-owned row must be deleted by the path-safe delete (#6855)"
    )


def test_archive_no_metadata_fallback_when_source_lookup_empty(
    isolated_state_db, monkeypatch
):
    """Finding 4 (#6855): when the authoritative state.db source lookup
    returns empty/error, archive must NOT fall back to the cached metadata
    source_tag. A real telegram row with stale api_server metadata must 400
    with the archived flag untouched (previously archived with HTTP 200)."""
    sid = "ro-telegram-stale-meta-1"
    _make_state_db(isolated_state_db["db"], sid, source="telegram", title="Telegram chat")
    cli_meta = {
        "session_id": sid,
        "source_tag": "api_server",
        "raw_source": "api_server",
        "session_source": "api",
        "read_only": True,
        "profile": "default",
    }
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda s: cli_meta)
    # Authoritative lookup fails/returns empty — the OLD code OR-ed in the
    # cached `_arch_source_tag` (api_server) and archived the telegram row.
    monkeypatch.setattr(routes, "_state_db_session_source", lambda s: "")

    captured = _capture_post(monkeypatch, {"session_id": sid, "archived": True})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True

    assert captured["status"] == 400, captured
    assert "Read-only imported sessions cannot be archived" in str(
        captured["payload"].get("error", "")
    )
    assert _read_archived(isolated_state_db["db"], sid) == 0, (
        "archive must fail closed when the authoritative source lookup is empty (#6855)"
    )


def test_archive_source_flip_fails_closed_in_transaction(isolated_state_db, monkeypatch):
    """Finding 4 (#6855): the archive UPDATE revalidates the authoritative
    source inside the same transaction. When the pre-check passes
    (api_server) but the row's current source is telegram, the archive must
    fail closed with the flag untouched (mirrors the delete-path fix)."""
    sid = "ro-telegram-flip-1"
    _make_state_db(isolated_state_db["db"], sid, source="telegram", title="Telegram chat")
    cli_meta = {
        "session_id": sid,
        "source_tag": "api_server",
        "raw_source": "api_server",
        "session_source": "api",
        "read_only": True,
        "profile": "default",
    }
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda s: cli_meta)
    # Pre-check sees api_server (patched)...
    monkeypatch.setattr(routes, "_state_db_session_source", lambda s: "api_server")

    captured = _capture_post(monkeypatch, {"session_id": sid, "archived": True})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True

    assert captured["status"] in (400, 404), captured
    assert _read_archived(isolated_state_db["db"], sid) == 0, (
        "archive flag must stay untouched when the in-transaction source check denies"
    )


def test_set_state_db_archived_require_source_in(isolated_state_db):
    """Finding 4 unit level: _set_state_db_session_archived with
    require_source_in=... flips only exact api_server-class rows, atomically."""
    db = isolated_state_db["db"]
    tel = "ro-tel-unit-1"
    _make_state_db(db, tel, source="telegram", title="Telegram chat")
    assert routes._set_state_db_session_archived(
        tel, True, require_source_in=("api", "api_server")
    ) is False
    assert _read_archived(db, tel) == 0

    api = "ro-api-unit-1"
    _make_state_db(db, api, source="api_server", title="API imported")
    assert routes._set_state_db_session_archived(
        api, True, require_source_in=("api", "api_server")
    ) is True
    assert _read_archived(db, api) == 1
