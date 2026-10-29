"""Regression coverage for #8300: deleting a session with gateway runtime history.

The delete used to remove the two ledger tables directly, which still aborts on
``worker_receipts -> worker_executions ON DELETE RESTRICT`` and on the
``sessions_runtime_ledger_delete_guard`` trigger, and it did so *after* the
WebUI had already unlinked the user's files.
"""

import json
import sqlite3
import sys
import threading
import types
from collections import OrderedDict
from io import BytesIO
from types import SimpleNamespace

import pytest


LEDGER_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    started_at TEXT,
    source TEXT,
    parent_session_id TEXT,
    model_config TEXT,
    end_reason TEXT,
    ended_at TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role TEXT,
    content TEXT,
    api_content TEXT,
    timestamp TEXT NOT NULL
);
CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE gateway_routing (
    scope TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    updated_at REAL,
    PRIMARY KEY (scope, session_key)
);
CREATE TABLE session_admissions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    admission_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE RESTRICT,
    lineage_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    intent TEXT NOT NULL CHECK (intent IN ('queue','steer','redirect')),
    status TEXT NOT NULL CHECK (status IN ('queued','started','unknown','terminal')),
    outcome TEXT,
    owner_epoch INTEGER,
    generation INTEGER,
    UNIQUE (principal_id, target_session_id, request_id)
);
CREATE TABLE worker_executions (
    execution_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('cron','child','compute','kanban')),
    owner_epoch INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('registered','running','unknown','terminal')),
    adoption_digest TEXT NOT NULL,
    last_sequence INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE worker_receipts (
    execution_id TEXT NOT NULL REFERENCES worker_executions(execution_id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    payload_digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (execution_id, sequence)
);
CREATE TRIGGER sessions_runtime_ledger_delete_guard
BEFORE DELETE ON sessions
WHEN EXISTS (SELECT 1 FROM session_admissions WHERE target_session_id = old.id)
  OR EXISTS (SELECT 1 FROM worker_executions WHERE session_id = old.id)
BEGIN
    SELECT RAISE(ABORT, 'Refused to delete a session that has gateway runtime history');
END;
"""

OLD_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    started_at TEXT,
    source TEXT,
    parent_session_id TEXT,
    model_config TEXT,
    end_reason TEXT,
    ended_at TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role TEXT,
    content TEXT,
    timestamp TEXT NOT NULL
);
"""


class _AgentRefusal(Exception):
    """Stand-in for hermes_state_runtime.RuntimeStoreError (``.reason``)."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _seed_session(conn, sid, *, parent_id=None, model_config=None):
    conn.execute(
        "INSERT INTO sessions (id, title, started_at, source, parent_session_id,"
        " model_config, end_reason, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid, sid, "2026-07-11T00:00:00Z",
            "subagent" if (model_config or {}).get("_delegate_from") else "cli",
            parent_id, json.dumps(model_config) if model_config else None, None, None,
        ),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp)"
        " VALUES (?, 'user', ?, '2026-07-11T00:00:01Z')",
        (sid, f"message for {sid}"),
    )


def _seed_admission(conn, sid, admission_id, *, status="terminal"):
    conn.execute(
        "INSERT INTO session_admissions (admission_id, request_id, principal_id,"
        " target_session_id, lineage_json, payload_json, payload_digest, intent, status)"
        " VALUES (?, ?, 'p', ?, '[]', '{}', 'digest', 'queue', ?)",
        (admission_id, f"req-{admission_id}", sid, status),
    )


def _seed_worker(conn, sid, execution_id, *, status="terminal", receipts=1):
    conn.execute(
        "INSERT INTO worker_executions (execution_id, session_id, kind, owner_epoch,"
        " generation, status, adoption_digest) VALUES (?, ?, 'cron', 1, 1, ?, 'adopt')",
        (execution_id, sid, status),
    )
    for sequence in range(1, receipts + 1):
        conn.execute(
            "INSERT INTO worker_receipts (execution_id, sequence, payload_digest, result_json)"
            " VALUES (?, ?, 'd', '{}')",
            (execution_id, sequence),
        )


def _make_db(tmp_path, *, schema=LEDGER_SCHEMA):
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    conn.executescript(schema)
    conn.commit()
    return state_db, conn


def _artifacts(tmp_path, sids):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    for sid in sids:
        (sessions_dir / f"{sid}.json").write_text("{}", encoding="utf-8")
        (sessions_dir / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    return sessions_dir


def _install_home(monkeypatch, tmp_path):
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)


def _install_retirement_stub(monkeypatch, fn):
    module = types.ModuleType("hermes_state_mutation_retirement")
    module.retire_sessions = fn
    monkeypatch.setitem(sys.modules, "hermes_state_mutation_retirement", module)


def _agent_like_retirement(recorded):
    """Mimic the Agent fence: refuse live work, otherwise drop the ledger rows."""

    def retire_sessions(conn, session_ids):
        recorded.extend(session_ids)
        for sid in session_ids:
            busy = conn.execute(
                "SELECT 1 FROM session_admissions WHERE target_session_id=?"
                " AND status IN ('queued','started','unknown')", (sid,)
            ).fetchone() or conn.execute(
                "SELECT 1 FROM worker_executions WHERE session_id=?"
                " AND status IN ('registered','running','unknown')", (sid,)
            ).fetchone()
            if busy:
                raise _AgentRefusal("session_busy")
        for sid in session_ids:
            conn.execute("DELETE FROM worker_receipts WHERE execution_id IN"
                         " (SELECT execution_id FROM worker_executions WHERE session_id=?)", (sid,))
            conn.execute("DELETE FROM worker_executions WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM session_admissions WHERE target_session_id=?", (sid,))
            conn.execute("INSERT INTO state_meta (key, value) VALUES (?, '{}')",
                         (f"gateway.retired_session.v1.{sid}",))

    return retire_sessions


# --------------------------------------------------------------------------
# models-level: contract with the Agent fence
# --------------------------------------------------------------------------
def test_delete_retires_ledger_for_every_removed_delegate(tmp_path, monkeypatch):
    import api.models as models

    state_db, conn = _make_db(tmp_path)
    try:
        _seed_session(conn, "parent")
        _seed_session(conn, "delegate", parent_id="parent",
                      model_config={"_delegate_from": "parent"})
        _seed_admission(conn, "parent", "adm-parent")
        _seed_worker(conn, "delegate", "exec-delegate")
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)
    recorded = []
    _install_retirement_stub(monkeypatch, _agent_like_retirement(recorded))

    assert models.delete_cli_session("parent") is True
    assert recorded == ["parent", "delegate"]

    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM worker_receipts").fetchone()[0] == 0
        # The fence survives the delete: a late retry must not recreate the rows.
        keys = {row[0] for row in conn.execute("SELECT key FROM state_meta")}
    finally:
        conn.close()
    assert {"gateway.retired_session.v1.parent", "gateway.retired_session.v1.delegate"} <= keys


@pytest.mark.parametrize("status_kind", ["admission", "worker"])
@pytest.mark.parametrize("code", ["session_busy", "unknown_execution"])
def test_live_gateway_work_refuses_and_rolls_back(tmp_path, monkeypatch, status_kind, code):
    import api.models as models

    state_db, conn = _make_db(tmp_path)
    try:
        _seed_session(conn, "sid")
        if status_kind == "admission":
            _seed_admission(conn, "sid", "adm", status="queued")
        else:
            _seed_worker(conn, "sid", "exec", status="running", receipts=1)
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)
    sessions_dir = _artifacts(tmp_path, ["sid"])

    def refuse(_conn, _ids):
        raise _AgentRefusal(code)

    _install_retirement_stub(monkeypatch, refuse)

    with pytest.raises(models.SessionDeleteRefused) as excinfo:
        models.delete_cli_session("sid")
    assert excinfo.value.code == code
    assert (sessions_dir / "sid.json").exists()

    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM state_meta").fetchone()[0] == 0
    finally:
        conn.close()


def test_missing_retirement_helper_fails_closed(tmp_path, monkeypatch):
    import api.models as models

    state_db, conn = _make_db(tmp_path)
    try:
        _seed_session(conn, "sid")
        _seed_admission(conn, "sid", "adm")
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_state_mutation_retirement", None)

    with pytest.raises(models.SessionDeleteRefused) as excinfo:
        models.delete_cli_session("sid")
    assert excinfo.value.code == "runtime_ledger_unavailable"

    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM session_admissions").fetchone()[0] == 1
    finally:
        conn.close()


def test_partial_ledger_schema_fails_closed(tmp_path, monkeypatch):
    """Ledger tables present but no state_meta: the fence cannot be written."""
    import api.models as models

    schema = LEDGER_SCHEMA.replace("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);", "")
    state_db, conn = _make_db(tmp_path, schema=schema)
    try:
        _seed_session(conn, "sid")
        _seed_admission(conn, "sid", "adm")
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)
    _install_retirement_stub(monkeypatch, _agent_like_retirement([]))

    with pytest.raises(models.SessionDeleteRefused) as excinfo:
        models.delete_cli_session("sid")
    assert excinfo.value.code == "runtime_ledger_unavailable"


def test_old_schema_without_ledger_deletes(tmp_path, monkeypatch):
    import api.models as models

    state_db, conn = _make_db(tmp_path, schema=OLD_SCHEMA)
    try:
        _seed_session(conn, "sid")
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)
    # None makes any import of the helper raise ImportError: proving it is skipped.
    monkeypatch.setitem(sys.modules, "hermes_state_mutation_retirement", None)

    assert models.delete_cli_session("sid") is True

    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    finally:
        conn.close()


def test_row_free_session_deletes_when_the_helper_is_missing(tmp_path, monkeypatch):
    """No runtime rows means nothing for the fence to protect.

    A downgraded or misconfigured Agent must not turn "helper unavailable" into
    "every session is undeletable": the Agent's own trigger allows deleting a
    row-free session too.
    """
    import api.models as models

    state_db, conn = _make_db(tmp_path)
    try:
        _seed_session(conn, "sid")
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_state_mutation_retirement", None)

    assert models.delete_cli_session("sid") is True

    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    finally:
        conn.close()


def test_row_free_session_still_runs_the_fence(tmp_path, monkeypatch):
    """A complete schema plus an importable helper means the fence runs.

    ``retire_sessions`` does more than clear admission rows: it retires
    spillover candidates, mutation receipts, route fences and local receipts.
    Skipping it for a row-free session would leave that Agent-side data behind.
    """
    import api.models as models

    state_db, conn = _make_db(tmp_path)
    try:
        _seed_session(conn, "sid")
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)
    recorded = []
    _install_retirement_stub(monkeypatch, _agent_like_retirement(recorded))

    assert models.delete_cli_session("sid") is True
    assert recorded == ["sid"]

    conn = sqlite3.connect(state_db)
    try:
        keys = {row[0] for row in conn.execute("SELECT key FROM state_meta")}
    finally:
        conn.close()
    assert "gateway.retired_session.v1.sid" in keys


def _fence_whose_lazy_import_fails(recorded):
    def retire_sessions(conn, session_ids):
        recorded.extend(session_ids)
        conn.execute(
            "INSERT INTO state_meta(key,value) VALUES(?,?)",
            ("gateway.retired_session.v1." + session_ids[0], "{}"),
        )
        raise ModuleNotFoundError("No module named 'ruamel'")
    return retire_sessions


def test_fence_lazy_import_failure_degrades_like_a_missing_helper_for_row_free(tmp_path, monkeypatch):
    """A helper that imports but cannot load the Agent's dependencies is the
    same degrade as a missing helper: a row-free session still deletes, and no
    half-written fence survives the failed call."""
    import api.models as models

    state_db, conn = _make_db(tmp_path)
    try:
        _seed_session(conn, "sid")
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)
    recorded = []
    _install_retirement_stub(monkeypatch, _fence_whose_lazy_import_fails(recorded))

    assert models.delete_cli_session("sid") is True
    assert recorded == ["sid"]
    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT 1 FROM sessions WHERE id='sid'").fetchone() is None
        keys = {row[0] for row in conn.execute("SELECT key FROM state_meta")}
    finally:
        conn.close()
    assert "gateway.retired_session.v1.sid" not in keys


def test_fence_lazy_import_failure_refuses_when_rows_exist(tmp_path, monkeypatch):
    import api.models as models

    state_db, conn = _make_db(tmp_path)
    try:
        _seed_session(conn, "sid")
        _seed_admission(conn, "sid", "adm-1")
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)
    _install_retirement_stub(monkeypatch, _fence_whose_lazy_import_fails([]))

    with pytest.raises(models.SessionDeleteRefused) as excinfo:
        models.delete_cli_session("sid")
    assert excinfo.value.code == "runtime_ledger_unavailable"
    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT 1 FROM sessions WHERE id='sid'").fetchone() is not None
        assert conn.execute("SELECT 1 FROM session_admissions WHERE admission_id='adm-1'").fetchone() is not None
    finally:
        conn.close()


def test_row_free_session_deletes_on_a_partial_ledger_schema(tmp_path, monkeypatch):
    import api.models as models

    schema = LEDGER_SCHEMA.replace(
        "CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);", ""
    )
    state_db, conn = _make_db(tmp_path, schema=schema)
    try:
        _seed_session(conn, "sid")
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)
    _install_retirement_stub(monkeypatch, _agent_like_retirement([]))

    assert models.delete_cli_session("sid") is True

    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    finally:
        conn.close()


def test_sidecar_only_session_without_state_db_deletes(tmp_path, monkeypatch):
    """A WebUI-only session has no state.db at all.

    That is nothing to retire rather than an operational failure — reporting it
    as one turned every delete of a pure-WebUI session into a 500 once the
    endpoint stopped treating "files removed" as success.
    """
    import api.models as models

    (tmp_path / "sessions").mkdir()
    _install_home(monkeypatch, tmp_path)

    assert models.delete_cli_session("webui-only") is True


def test_missing_db_with_pending_manifest_still_fails_closed(tmp_path, monkeypatch):
    """A retry manifest needs the DB to prove absence, so it must still refuse."""
    import api.models as models

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / ".cleanup_manifest_x.json").write_text(
        json.dumps(["live"]), encoding="utf-8"
    )
    _install_home(monkeypatch, tmp_path)

    assert models.delete_cli_session("unrelated") is False
    assert (sessions_dir / ".cleanup_manifest_x.json").exists()


def test_unexpected_fence_error_is_operational_failure(tmp_path, monkeypatch):
    import api.models as models

    state_db, conn = _make_db(tmp_path)
    try:
        _seed_session(conn, "sid")
        _seed_admission(conn, "sid", "adm")
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)

    def boom(_conn, _ids):
        raise sqlite3.OperationalError("disk I/O error")

    _install_retirement_stub(monkeypatch, boom)

    assert models.delete_cli_session("sid") is False


def test_manifest_failure_rolls_back_the_retirement(tmp_path, monkeypatch):
    """The fence and the row deletes must commit or roll back together."""
    import uuid

    import api.models as models

    state_db, conn = _make_db(tmp_path)
    try:
        _seed_session(conn, "sid")
        _seed_admission(conn, "sid", "adm")
        _seed_worker(conn, "sid", "exec")
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)
    recorded = []
    _install_retirement_stub(monkeypatch, _agent_like_retirement(recorded))
    sessions_dir = _artifacts(tmp_path, ["sid"])
    # Make the atomic manifest publish fail: its temp path is a directory.
    fixed = uuid.UUID("00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(models.uuid, "uuid4", lambda: fixed)
    (sessions_dir / f".cleanup_manifest_{fixed.hex}.tmp").mkdir()

    assert models.delete_cli_session("sid") is False

    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM session_admissions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM worker_executions").fetchone()[0] == 1
        # No fence may survive an aborted delete: it would make a late retry
        # report the session as deleted while its rows still exist.
        assert conn.execute("SELECT COUNT(*) FROM state_meta").fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# models-level: real Agent fence (skipped without hermes-agent)
# --------------------------------------------------------------------------
def test_real_fence_clears_restrict_rows_and_keeps_receipts(tmp_path, monkeypatch):
    pytest.importorskip("hermes_state_mutation_retirement")
    # retire_sessions imports the rest of the Agent lazily; without its
    # dependencies (ruamel via hermes_state_common) the fence cannot run here.
    pytest.importorskip("tools.tool_result_storage")
    import api.models as models

    state_db, conn = _make_db(tmp_path)
    try:
        _seed_session(conn, "sid")
        _seed_session(conn, "bystander")
        _seed_admission(conn, "sid", "adm")
        _seed_worker(conn, "sid", "exec", receipts=2)
        _seed_admission(conn, "bystander", "adm-other")
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)

    assert models.delete_cli_session("sid") is True

    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id='sid'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM worker_receipts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM worker_executions").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM session_admissions WHERE target_session_id='sid'"
        ).fetchone()[0] == 0
        # The bystander keeps its own ledger row.
        assert conn.execute(
            "SELECT COUNT(*) FROM session_admissions WHERE target_session_id='bystander'"
        ).fetchone()[0] == 1
        keys = {row[0] for row in conn.execute("SELECT key FROM state_meta")}
    finally:
        conn.close()
    fenced = {key for key in keys if key.startswith("gateway.retired_session.v1.")}
    assert fenced == {"gateway.retired_session.v1.sid"}


@pytest.mark.parametrize(
    "seed,status,code",
    [
        (_seed_admission, "queued", "session_busy"),
        (_seed_admission, "started", "session_busy"),
        (_seed_worker, "registered", "session_busy"),
        (_seed_admission, "unknown", "unknown_execution"),
        (_seed_worker, "unknown", "unknown_execution"),
    ],
)
def test_real_fence_refuses_live_work(tmp_path, monkeypatch, seed, status, code):
    pytest.importorskip("hermes_state_mutation_retirement")
    # retire_sessions imports the rest of the Agent lazily; without its
    # dependencies (ruamel via hermes_state_common) the fence cannot run here.
    pytest.importorskip("tools.tool_result_storage")
    import api.models as models

    state_db, conn = _make_db(tmp_path)
    try:
        _seed_session(conn, "sid")
        if seed is _seed_admission:
            _seed_admission(conn, "sid", "adm", status=status)
        else:
            _seed_worker(conn, "sid", "exec", status=status)
        conn.commit()
    finally:
        conn.close()
    _install_home(monkeypatch, tmp_path)

    with pytest.raises(models.SessionDeleteRefused) as excinfo:
        models.delete_cli_session("sid")
    assert excinfo.value.code == code

    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM state_meta").fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# endpoint: refusal and failure must not destroy files
# --------------------------------------------------------------------------
class _PostHandler:
    def __init__(self, body):
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = BytesIO(body)
        self.client_address = ("127.0.0.1", 12345)


def _install_routes_env(monkeypatch, tmp_path, sessions):
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(routes, "SESSIONS", sessions, raising=False)
    monkeypatch.setattr(routes, "LOCK", threading.RLock(), raising=False)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid, **kw: {})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda sid: False)
    monkeypatch.setattr(routes, "_worktree_retained_payload_for_session_id", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda sid: threading.Lock())
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_record_webui_deleted_session_tombstone", lambda sid: None)
    monkeypatch.setattr(routes, "prune_session_from_index", lambda sid: None)

    def _missing(*_a, **_k):
        raise KeyError("no session")

    monkeypatch.setattr(routes, "get_session", _missing)
    import api.config as config

    monkeypatch.setattr(config, "_evict_session_agent", lambda sid: None, raising=False)
    return session_dir


def _post_delete(monkeypatch, sid):
    import api.routes as routes

    captured = {}

    def fake_j(_handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status

    monkeypatch.setattr(routes, "j", fake_j)
    body = json.dumps({"session_id": sid}).encode("utf-8")
    routes.handle_post(_PostHandler(body), SimpleNamespace(path="/api/session/delete"))
    return captured


def test_endpoint_refusal_returns_409_and_keeps_files(tmp_path, monkeypatch):
    import api.models as models
    import api.terminal as terminal

    _install_home(monkeypatch, tmp_path)
    session_dir = _install_routes_env(monkeypatch, tmp_path, OrderedDict({"sid": object()}))
    closed = []
    monkeypatch.setattr(
        terminal, "close_terminal", lambda sid: closed.append(sid), raising=False
    )

    def refuse(_sid):
        raise models.SessionDeleteRefused("session_busy")

    monkeypatch.setattr(models, "delete_cli_session", refuse)
    (session_dir / "sid.json").write_text("{}", encoding="utf-8")
    (session_dir / "sid.json.bak").write_text("{}", encoding="utf-8")

    captured = _post_delete(monkeypatch, "sid")

    assert captured["status"] == 409
    assert captured["payload"]["ok"] is False
    assert captured["payload"]["error"] == "session_busy"
    assert (session_dir / "sid.json").exists()
    assert (session_dir / "sid.json.bak").exists()
    # The terminal is the refused work's shell: closing it would kill the job.
    assert closed == []


def test_endpoint_db_failure_returns_500_and_keeps_files(tmp_path, monkeypatch):
    import api.models as models

    _install_home(monkeypatch, tmp_path)
    _install_retirement_stub(monkeypatch, lambda _conn, _ids: None)
    _state_db, conn = _make_db(tmp_path)
    try:
        # The session row survives an aborted transaction: nothing committed.
        _seed_session(conn, "sid")
        conn.commit()
    finally:
        conn.close()
    session_dir = _install_routes_env(monkeypatch, tmp_path, OrderedDict())
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: False)
    (session_dir / "sid.json").write_text("{}", encoding="utf-8")

    captured = _post_delete(monkeypatch, "sid")

    assert captured["status"] == 500
    assert captured["payload"]["ok"] is False
    assert captured["payload"]["error"] == "state_db_cleanup_failed"
    assert (session_dir / "sid.json").exists()


def test_endpoint_finishes_cleanup_when_the_commit_already_landed(tmp_path, monkeypatch):
    """A pending transcript cleanup must not strand a deleted conversation.

    ``delete_cli_session`` returns False after committing the row delete when
    the transcript unlink failed; the cleanup manifest retries that part. The
    route still has to drop the sidecar, index entry and terminal, or the
    session reappears while its DB rows are gone.
    """
    import api.models as models
    import api.terminal as terminal

    sessions = OrderedDict({"sid": object()})
    _install_home(monkeypatch, tmp_path)
    _state_db, conn = _make_db(tmp_path)
    conn.close()  # row-free state.db: the delete already committed
    session_dir = _install_routes_env(monkeypatch, tmp_path, sessions)
    closed = []
    monkeypatch.setattr(
        terminal, "close_terminal", lambda sid: closed.append(sid), raising=False
    )
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: False)
    (session_dir / "sid.json").write_text("{}", encoding="utf-8")

    captured = _post_delete(monkeypatch, "sid")

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert not (session_dir / "sid.json").exists()
    assert "sid" not in sessions
    assert closed == ["sid"]


def test_endpoint_keeps_files_when_the_database_is_missing(tmp_path, monkeypatch):
    """A missing state.db is unknown, not proof the delete committed.

    delete_cli_session() fails closed there because a retry manifest may still
    need the DB; the route must not read that as "already deleted" and drop the
    sidecar and journals.
    """
    import api.models as models

    _install_home(monkeypatch, tmp_path)  # no state.db at all
    session_dir = _install_routes_env(monkeypatch, tmp_path, OrderedDict())
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: False)
    (session_dir / "sid.json").write_text("{}", encoding="utf-8")

    captured = _post_delete(monkeypatch, "sid")

    assert captured["status"] == 500
    assert captured["payload"]["ok"] is False
    assert (session_dir / "sid.json").exists()


def test_presence_is_unknown_when_profile_resolution_fails(tmp_path, monkeypatch):
    """Never answer from HERMES_HOME when the active profile cannot resolve."""
    import api.models as models
    import api.profiles as profiles

    _make_db(tmp_path)  # a real DB at HERMES_HOME, where the fallback would look
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def boom():
        raise RuntimeError("profile home rejected")

    monkeypatch.setattr(profiles, "get_active_hermes_home", boom)

    assert models.state_db_session_presence("sid") is None
    # The bool wrapper keeps its old contract for the file-manager callers.
    assert models.state_db_has_session("sid") is False


def test_eviction_runs_before_the_session_lock(tmp_path, monkeypatch):
    """The lifecycle commit can wait on a provider; it must not hold the lock."""
    import api.config as config
    import api.models as models
    import api.routes as routes

    _install_home(monkeypatch, tmp_path)
    session_dir = _install_routes_env(monkeypatch, tmp_path, OrderedDict())
    lock = threading.Lock()
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda sid: lock)
    seen = {}

    def evict(sid):
        seen["locked"] = lock.locked()

    monkeypatch.setattr(config, "_evict_session_agent", evict, raising=False)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)
    (session_dir / "sid.json").write_text("{}", encoding="utf-8")

    _post_delete(monkeypatch, "sid")

    assert seen == {"locked": False}


def test_endpoint_success_clears_db_then_files(tmp_path, monkeypatch):
    import api.models as models
    import api.terminal as terminal

    sessions = OrderedDict({"sid": object()})
    _install_home(monkeypatch, tmp_path)
    session_dir = _install_routes_env(monkeypatch, tmp_path, sessions)
    (session_dir / "sid.json").write_text("{}", encoding="utf-8")

    seen = {}
    order = []
    monkeypatch.setattr(
        terminal, "close_terminal", lambda sid: order.append("close"), raising=False
    )

    def delete(sid):
        # The DB delete must run while the user's files still exist.
        seen["sidecar_present"] = (session_dir / f"{sid}.json").exists()
        order.append("db")
        return True

    monkeypatch.setattr(models, "delete_cli_session", delete)

    captured = _post_delete(monkeypatch, "sid")

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert seen["sidecar_present"] is True
    assert order == ["db", "close"]
    assert not (session_dir / "sid.json").exists()
    assert "sid" not in sessions
