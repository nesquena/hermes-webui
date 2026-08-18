"""Regression tests for #3280 — file manager falls back to state.db for
external (Telegram/CLI) sessions instead of returning 404.

Covers:
  (a) WebUI session — existing behavior preserved (get_session path).
  (b) state.db-only session — fallback returns a workspace-bearing view.
  (c) Unknown session — KeyError still propagates so callers 404.
  (d) Static check: every file-manager handler in api/routes.py calls
      get_session_for_file_ops, not the raw get_session.
"""

from __future__ import annotations

import gc
import io
import json
import logging
import re
import sqlite3
import threading
import weakref
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = ROOT / "api" / "routes.py"


FILE_HANDLERS = [
    "_handle_escape_authorize",
    "_handle_escape_list_dir",
    "_handle_escape_file_read",
    "_handle_escape_file_raw",
    "_handle_folder_download",
    "_handle_file_raw",
    "_handle_file_read",
    "_handle_file_delete",
    "_handle_file_save",
    "_handle_file_create",
    "_handle_file_rename",
    "_handle_create_dir",
    "_handle_file_reveal",
    "_handle_file_path",
    "_handle_file_open_vscode",
    "_handle_office_file_save",
    "_handle_file_move",
]


def _handler_body(src: str, name: str) -> str:
    start = src.index(f"def {name}(")
    # next top-level def or class
    m = re.search(r"\n(?:def |class )", src[start + 1 :])
    end = (start + 1 + m.start()) if m else len(src)
    return src[start:end]


def test_routes_file_handlers_use_fallback():
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert "get_session_for_file_ops" in src, "fallback helper must be imported"
    missing = []
    for name in FILE_HANDLERS:
        body = _handler_body(src, name)
        # Must not call get_session(...) directly inside the handler.
        # (get_session_for_file_ops also contains "get_session(" as a substring,
        # so check word-boundary occurrences.)
        bare = re.findall(r"(?<!_)\bget_session\(", body)
        # Strip occurrences that are actually get_session_for_file_ops( — the
        # regex above already excludes underscore prefix, so any remaining
        # match is a raw get_session call.
        if bare:
            missing.append(name)
    assert not missing, f"raw get_session() still used in: {missing}"


# ---------------------------------------------------------------------------
# Functional tests against api.models.get_session_for_file_ops
# ---------------------------------------------------------------------------

pytestmark_models = pytest.mark.requires_agent_modules


def _make_state_db(path: Path, sid: str) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            message_count INTEGER DEFAULT 0,
            started_at TEXT,
            source TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, title, model, message_count, started_at, source) "
        "VALUES (?, 'telegram session', 'gpt-x', 1, '2026-01-01T00:00:00Z', 'telegram')",
        (sid,),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def models_module():
    return pytest.importorskip("api.models")


def test_get_session_for_file_ops_webui_passthrough(models_module, monkeypatch):
    """(a) WebUI session — delegates to get_session, no state.db consulted."""
    profiles_module = pytest.importorskip("api.profiles")
    sentinel = SimpleNamespace(profile=None)
    called = {"get_session": 0, "profile_match": 0, "state_db": 0}

    def fake_get_session(sid, metadata_only=False):
        called["get_session"] += 1
        return sentinel

    def fake_profiles_match(session_profile, active_profile):
        called["profile_match"] += 1
        assert session_profile is None
        assert active_profile == "default"
        return True

    def fake_has(_sid):
        called["state_db"] += 1
        return True

    monkeypatch.setattr(models_module, "get_session", fake_get_session)
    monkeypatch.setattr(models_module, "state_db_has_session", fake_has)
    monkeypatch.setattr(profiles_module, "_profiles_match", fake_profiles_match)
    monkeypatch.setattr(profiles_module, "get_active_profile_name", lambda: "default")
    result = models_module.get_session_for_file_ops("webui-sid")
    assert result is sentinel
    assert called == {"get_session": 1, "profile_match": 1, "state_db": 0}


def test_get_session_for_file_ops_recovers_missing_implicit_workspace(
    models_module, monkeypatch, tmp_path
):
    """A deleted sidecar workspace reloads fully before persisting its binding."""
    profiles_module = pytest.importorskip("api.profiles")
    workspace_module = pytest.importorskip("api.workspace")
    stale = tmp_path / "deleted-workspace"
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    metadata_session = SimpleNamespace(
        session_id="stale-webui-sid",
        profile=None,
        workspace=str(stale),
        _loaded_metadata_only=True,
    )
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = session_dir / "stale-webui-sid.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": "stale-webui-sid",
                "workspace": str(stale),
                "messages": [{"role": "user", "content": "preserve me"}],
                "future_field": {"preserve": True},
            }
        ),
        encoding="utf-8",
    )

    def get_session(_sid, metadata_only=False):
        assert metadata_only is True
        return metadata_session

    monkeypatch.setattr(models_module, "get_session", get_session)
    monkeypatch.setattr(models_module, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models_module, "_write_session_index", lambda **_kwargs: None)
    monkeypatch.setattr(models_module, "get_last_workspace", lambda: str(fallback))
    monkeypatch.setattr(profiles_module, "_profiles_match", lambda *_args: True)
    monkeypatch.setattr(profiles_module, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(workspace_module, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(workspace_module, "load_workspaces", lambda: [])

    recovered = models_module.get_session_for_file_ops(metadata_session.session_id)

    assert recovered is metadata_session
    assert recovered.session_id == metadata_session.session_id
    assert Path(recovered.workspace) == fallback.resolve()
    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert persisted["workspace"] == str(fallback.resolve())
    assert persisted["messages"] == [{"role": "user", "content": "preserve me"}]
    assert persisted["future_field"] == {"preserve": True}


def test_get_session_for_file_ops_recovery_save_failure_fails_closed(
    models_module, monkeypatch, tmp_path
):
    profiles_module = pytest.importorskip("api.profiles")
    workspace_module = pytest.importorskip("api.workspace")
    stale = tmp_path / "deleted-workspace"
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    metadata_session = SimpleNamespace(
        session_id="stale-save-failure",
        profile=None,
        workspace=str(stale),
        _loaded_metadata_only=True,
    )
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = session_dir / f"{metadata_session.session_id}.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": metadata_session.session_id,
                "workspace": str(stale),
                "messages": [{"role": "user", "content": "preserve me"}],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        models_module,
        "get_session",
        lambda _sid, metadata_only=False: metadata_session,
    )
    monkeypatch.setattr(models_module, "SESSION_DIR", session_dir)
    monkeypatch.setattr(
        models_module,
        "_safe_replace",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(models_module, "get_last_workspace", lambda: str(fallback))
    monkeypatch.setattr(profiles_module, "_profiles_match", lambda *_args: True)
    monkeypatch.setattr(profiles_module, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(workspace_module, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(workspace_module, "load_workspaces", lambda: [])

    with pytest.raises(models_module.WorkspaceBindingPersistenceError):
        models_module.get_session_for_file_ops(metadata_session.session_id)

    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert metadata_session.workspace == str(stale)
    assert persisted["workspace"] == str(stale)
    assert persisted["messages"] == [{"role": "user", "content": "preserve me"}]


def test_recovered_workspace_compare_rejects_a_stale_concurrent_binding(
    models_module, monkeypatch, tmp_path
):
    stale = tmp_path / "deleted-workspace"
    fallback_a = tmp_path / "fallback-a"
    fallback_b = tmp_path / "fallback-b"
    fallback_a.mkdir()
    fallback_b.mkdir()
    metadata_session = SimpleNamespace(
        session_id="stale-concurrent-binding",
        workspace=str(stale),
        _loaded_metadata_only=True,
    )
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = session_dir / f"{metadata_session.session_id}.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": metadata_session.session_id,
                "workspace": str(fallback_a.resolve()),
                "messages": [{"role": "user", "content": "preserve me"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(models_module, "SESSION_DIR", session_dir)

    with pytest.raises(
        models_module.WorkspaceBindingPersistenceError,
        match="session workspace changed",
    ):
        models_module.persist_recovered_workspace_binding(
            metadata_session, fallback_b
        )

    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert metadata_session.workspace == str(stale)
    assert persisted["workspace"] == str(fallback_a.resolve())
    assert persisted["messages"] == [{"role": "user", "content": "preserve me"}]


def test_recovery_cas_uses_the_workspace_seen_when_recovery_was_decided(
    models_module, monkeypatch, tmp_path
):
    stale = tmp_path / "deleted-workspace"
    fallback_a = tmp_path / "fallback-a"
    explicit_b = tmp_path / "explicit-b"
    fallback_a.mkdir()
    explicit_b.mkdir()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = SimpleNamespace(
        session_id="explicit-switch-wins",
        workspace=str(explicit_b.resolve()),
        _loaded_metadata_only=True,
    )
    sidecar = session_dir / f"{session.session_id}.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": session.session_id,
                "workspace": str(explicit_b.resolve()),
                "messages": [{"role": "user", "content": "preserve me"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(models_module, "SESSION_DIR", session_dir)

    with pytest.raises(
        models_module.WorkspaceBindingPersistenceError,
        match="session workspace changed",
    ):
        models_module.persist_recovered_workspace_binding(
            session,
            fallback_a,
            expected_workspace=str(stale),
        )

    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert session.workspace == str(explicit_b.resolve())
    assert persisted["workspace"] == str(explicit_b.resolve())
    assert persisted["messages"] == [{"role": "user", "content": "preserve me"}]


def test_recovery_never_recreates_a_missing_session_sidecar(
    models_module, monkeypatch, tmp_path
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    stale = tmp_path / "deleted-workspace"
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    saves = {"count": 0}
    session = SimpleNamespace(
        session_id="deleted-before-recovery",
        workspace=str(stale),
        _loaded_metadata_only=False,
        save=lambda **_kwargs: saves.__setitem__("count", saves["count"] + 1),
    )
    monkeypatch.setattr(models_module, "SESSION_DIR", session_dir)

    with pytest.raises(
        models_module.WorkspaceBindingPersistenceError,
        match="session sidecar is missing",
    ):
        models_module.persist_recovered_workspace_binding(
            session,
            fallback,
            expected_workspace=str(stale),
        )

    assert saves["count"] == 0
    assert session.workspace == str(stale)
    assert not (session_dir / f"{session.session_id}.json").exists()


class _DeleteJSONHandler:
    def __init__(self, body: dict):
        body_bytes = json.dumps(body).encode()
        self.status = None
        self.command = "POST"
        self.rfile = BytesIO(body_bytes)
        self.wfile = BytesIO()
        self.headers = {"Content-Length": str(len(body_bytes))}

    def send_response(self, status):
        self.status = status

    def send_header(self, _key, _value):
        pass

    def end_headers(self):
        pass

    def _safe_webui_print(self, *_args, **_kwargs):
        pass


def _install_delete_route_test_harness(
    models_module, routes_module, monkeypatch, tmp_path
):
    """Isolate the durable session store and non-session delete side effects."""
    config_module = pytest.importorskip("api.config")
    upload_module = pytest.importorskip("api.upload")
    turn_journal_module = pytest.importorskip("api.turn_journal")
    run_journal_module = pytest.importorskip("api.run_journal")
    background_module = pytest.importorskip("api.background_process")
    terminal_module = pytest.importorskip("api.terminal")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    models_module.SESSIONS.clear()
    monkeypatch.setattr(models_module, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models_module, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes_module, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes_module, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(routes_module, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes_module, "get_session", models_module.get_session)
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes_module, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes_module, "_is_messaging_session_id", lambda _sid: False)
    monkeypatch.setattr(
        routes_module, "_worktree_retained_payload_for_session_id", lambda _sid: {}
    )
    monkeypatch.setattr(
        routes_module, "_publish_session_list_changed", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        routes_module, "publish_session_list_changed", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(routes_module, "_sync_session_title_to_insights", lambda _s: None)
    monkeypatch.setattr(config_module, "_evict_session_agent", lambda _sid: None)
    monkeypatch.setattr(
        models_module,
        "delete_cli_session_for_webui_delete",
        lambda _sid: True,
    )
    monkeypatch.setattr(
        upload_module,
        "_session_attachment_dir",
        lambda _sid: tmp_path / "attachments" / _sid,
    )
    monkeypatch.setattr(turn_journal_module, "delete_turn_journal", lambda _sid: None)
    monkeypatch.setattr(run_journal_module, "delete_run_journal", lambda _sid: None)
    monkeypatch.setattr(
        background_module, "forget_bg_task_completion_dedup", lambda _sid: None
    )
    monkeypatch.setattr(terminal_module, "close_terminal", lambda _sid: None)
    return session_dir, index_file


def _delete_via_route(routes_module, sid: str) -> int:
    handler = _DeleteJSONHandler({"session_id": sid})
    routes_module.handle_post(handler, SimpleNamespace(path="/api/session/delete"))
    return handler.status


def _delete_result_via_route(routes_module, sid: str) -> tuple[int, dict]:
    handler = _DeleteJSONHandler({"session_id": sid})
    routes_module.handle_post(handler, SimpleNamespace(path="/api/session/delete"))
    return handler.status, json.loads(handler.wfile.getvalue())


def _seed_late_delete_retry_case(
    models_module, session_dir: Path, index_file: Path, tmp_path: Path, sid: str
) -> dict:
    owner = models_module.Session(
        session_id=sid,
        title="late delete owner",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "retry me"}],
    )
    sibling = models_module.Session(
        session_id=f"{sid}-sibling",
        title="unaffected sibling",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "keep me"}],
    )
    models_module.SESSIONS[sid] = owner
    models_module.SESSIONS[sibling.session_id] = sibling
    owner.save()
    sibling.save()
    sidecar = session_dir / f"{sid}.json"
    backup = session_dir / f"{sid}.json.bak"
    backup.write_bytes(sidecar.read_bytes())
    expected_index = json.loads(index_file.read_text(encoding="utf-8"))
    expected_by_sid = {row["session_id"]: row for row in expected_index}
    return {
        "owner": owner,
        "sibling": sibling,
        "sidecar": sidecar,
        "backup": backup,
        "sidecar_bytes": sidecar.read_bytes(),
        "backup_bytes": backup.read_bytes(),
        "index_by_sid": expected_by_sid,
        "target_row": expected_by_sid[sid],
    }


def _assert_late_delete_retry_restored(
    models_module,
    index_file: Path,
    case: dict,
    sid: str,
    payload: dict,
    *,
    expected_error: str = "Session index cleanup failed; retry deletion",
) -> None:
    assert payload["retry_state_restored"] is True
    assert payload["error"] == expected_error
    assert case["sidecar"].read_bytes() == case["sidecar_bytes"]
    assert case["backup"].read_bytes() == case["backup_bytes"]
    assert sid not in models_module._load_webui_deleted_session_tombstone()
    restored_index = json.loads(index_file.read_text(encoding="utf-8"))
    assert {row["session_id"]: row for row in restored_index} == case["index_by_sid"]

    models_module.SESSIONS.clear()
    sidebar_rows = models_module.all_sessions()
    sidebar_ids = {row["session_id"] for row in sidebar_rows}
    assert {sid, case["sibling"].session_id} <= sidebar_ids
    recovered = models_module.get_session(sid)
    assert recovered.compact() == case["target_row"]


def test_delete_prune_then_raise_restores_exact_index_row_for_cold_retry(
    models_module, monkeypatch, tmp_path
):
    routes_module = pytest.importorskip("api.routes")
    session_dir, index_file = _install_delete_route_test_harness(
        models_module, routes_module, monkeypatch, tmp_path
    )
    sid = "late-delete-prune-raise"
    case = _seed_late_delete_retry_case(
        models_module, session_dir, index_file, tmp_path, sid
    )
    real_prune = routes_module.prune_session_from_index

    def prune_then_raise(candidate_sid):
        real_prune(candidate_sid)
        raise OSError("late prune failure")

    monkeypatch.setattr(routes_module, "prune_session_from_index", prune_then_raise)
    status, payload = _delete_result_via_route(routes_module, sid)

    assert status == 500
    _assert_late_delete_retry_restored(
        models_module, index_file, case, sid, payload,
    )


def test_delete_prune_readback_failure_restores_exact_index_row_for_cold_retry(
    models_module, monkeypatch, tmp_path
):
    routes_module = pytest.importorskip("api.routes")
    session_dir, index_file = _install_delete_route_test_harness(
        models_module, routes_module, monkeypatch, tmp_path
    )
    sid = "late-delete-prune-readback"
    case = _seed_late_delete_retry_case(
        models_module, session_dir, index_file, tmp_path, sid
    )
    monkeypatch.setattr(routes_module, "_session_index_prune_verified", lambda _sid: False)
    status, payload = _delete_result_via_route(routes_module, sid)

    assert status == 500
    _assert_late_delete_retry_restored(
        models_module, index_file, case, sid, payload,
    )


def test_delete_tombstone_failure_restores_exact_index_row_for_cold_retry(
    models_module, monkeypatch, tmp_path
):
    routes_module = pytest.importorskip("api.routes")
    session_dir, index_file = _install_delete_route_test_harness(
        models_module, routes_module, monkeypatch, tmp_path
    )
    sid = "late-delete-tombstone-failure"
    case = _seed_late_delete_retry_case(
        models_module, session_dir, index_file, tmp_path, sid
    )
    real_record = models_module._record_webui_deleted_session_tombstone

    def tombstone_then_raise(candidate_sid):
        real_record(candidate_sid)
        raise OSError("late tombstone failure")

    monkeypatch.setattr(routes_module, "_record_webui_deleted_session_tombstone", tombstone_then_raise)
    status, payload = _delete_result_via_route(routes_module, sid)

    assert status == 500
    _assert_late_delete_retry_restored(
        models_module,
        index_file,
        case,
        sid,
        payload,
        expected_error="Deleted-session tombstone failed; retry deletion",
    )


def test_delete_index_compensation_failure_reports_manual_recovery(
    models_module, monkeypatch, tmp_path
):
    routes_module = pytest.importorskip("api.routes")
    session_dir, index_file = _install_delete_route_test_harness(
        models_module, routes_module, monkeypatch, tmp_path
    )
    sid = "late-delete-index-compensation-failure"
    case = _seed_late_delete_retry_case(
        models_module, session_dir, index_file, tmp_path, sid
    )
    monkeypatch.setattr(
        routes_module,
        "_session_index_prune_verified",
        lambda _sid: False,
    )
    monkeypatch.setattr(
        models_module,
        "_settle_session_index_row_locked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("index write failed")),
    )
    status, payload = _delete_result_via_route(routes_module, sid)

    assert status == 500
    assert payload["retry_state_restored"] is False
    assert payload["error"] == "Delete retry state restoration failed; manual recovery required"
    assert sid not in models_module._load_webui_deleted_session_tombstone()
    assert case["sidecar"].read_bytes() == case["sidecar_bytes"]
    assert case["backup"].read_bytes() == case["backup_bytes"]


def test_successful_delete_revokes_prelock_rename_owner(
    models_module, monkeypatch, tmp_path
):
    """A rename admitted before delete cannot resurrect the deleted SID."""
    routes_module = pytest.importorskip("api.routes")
    session_dir, index_file = _install_delete_route_test_harness(
        models_module, routes_module, monkeypatch, tmp_path
    )
    sid = "delete-revokes-prelock-rename"
    stale = models_module.Session(
        session_id=sid,
        title="before",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "preserve"}],
    )
    models_module.SESSIONS[sid] = stale
    stale.save()
    backup = session_dir / f"{sid}.json.bak"
    backup.write_text(stale.path.read_text(encoding="utf-8"), encoding="utf-8")

    rename_resolved = threading.Event()
    allow_rename_to_lock = threading.Event()
    original_resolve = routes_module._get_or_materialize_session
    resolve_calls = {"count": 0}

    def paused_resolve(candidate_sid):
        assert candidate_sid == sid
        resolve_calls["count"] += 1
        if resolve_calls["count"] > 1:
            return original_resolve(candidate_sid)
        rename_resolved.set()
        assert allow_rename_to_lock.wait(timeout=5)
        return stale

    monkeypatch.setattr(routes_module, "_get_or_materialize_session", paused_resolve)
    rename_result = {}

    def rename():
        handler = _DeleteJSONHandler({"session_id": sid, "title": "after"})
        routes_module.handle_post(
            handler, SimpleNamespace(path="/api/session/rename")
        )
        rename_result["status"] = handler.status

    rename_thread = threading.Thread(target=rename)
    rename_thread.start()
    assert rename_resolved.wait(timeout=5)

    assert _delete_via_route(routes_module, sid) == 200
    allow_rename_to_lock.set()
    rename_thread.join(timeout=5)

    assert not rename_thread.is_alive()
    assert rename_result["status"] in {404, 409}
    assert not stale.path.exists()
    assert not backup.exists()
    assert sid not in {
        row.get("session_id")
        for row in json.loads(index_file.read_text(encoding="utf-8"))
    }
    assert sid in models_module._load_webui_deleted_session_tombstone()
    models_module.SESSIONS.clear()
    assert models_module.Session.load(sid) is None


def test_state_backed_rename_materialization_cannot_recreate_deleted_sid(
    models_module, monkeypatch, tmp_path
):
    """A paused CLI import must not clear a delete tombstone or recreate a sidecar."""
    routes_module = pytest.importorskip("api.routes")
    profiles_module = pytest.importorskip("api.profiles")
    config_module = pytest.importorskip("api.config")
    upload_module = pytest.importorskip("api.upload")
    turn_journal_module = pytest.importorskip("api.turn_journal")
    run_journal_module = pytest.importorskip("api.run_journal")
    background_module = pytest.importorskip("api.background_process")
    terminal_module = pytest.importorskip("api.terminal")

    session_dir = tmp_path / "webui-sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    state_db = hermes_home / "state.db"
    sid = "state-backed-rename-race"
    conn = sqlite3.connect(str(state_db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            model TEXT,
            message_count INTEGER DEFAULT 0,
            started_at REAL,
            ended_at REAL,
            title TEXT,
            cwd TEXT,
            parent_session_id TEXT,
            model_config TEXT,
            end_reason TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions "
        "(id, source, model, message_count, started_at, title, cwd, parent_session_id, model_config) "
        "VALUES (?, 'cli', 'MiniMax-M3', 2, 1781024055.0, 'CLI title', ?, NULL, NULL)",
        (sid, str(tmp_path)),
    )
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        [(sid, "user", "hi", 1781024055.0), (sid, "assistant", "hello", 1781024056.0)],
    )
    conn.commit()
    conn.close()
    (hermes_home / "sessions").mkdir()

    monkeypatch.setattr(models_module, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models_module, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes_module, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes_module, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(models_module, "_active_state_db_path", lambda: state_db)
    monkeypatch.setattr(profiles_module, "get_active_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(profiles_module, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(models_module, "get_last_workspace", lambda: tmp_path)
    models_module.SESSIONS.clear()
    models_module.clear_cli_sessions_cache()

    monkeypatch.setattr(routes_module, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes_module, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes_module, "_is_messaging_session_id", lambda _sid: False)
    monkeypatch.setattr(
        routes_module, "_worktree_retained_payload_for_session_id", lambda _sid: {}
    )
    monkeypatch.setattr(routes_module, "_sync_session_title_to_insights", lambda _s: None)
    monkeypatch.setattr(config_module, "_evict_session_agent", lambda _sid: None)
    monkeypatch.setattr(
        upload_module, "_session_attachment_dir", lambda _sid: tmp_path / "attachments" / _sid
    )
    monkeypatch.setattr(turn_journal_module, "delete_turn_journal", lambda _sid: None)
    monkeypatch.setattr(run_journal_module, "delete_run_journal", lambda _sid: None)
    monkeypatch.setattr(
        background_module, "forget_bg_task_completion_dedup", lambda _sid: None
    )
    monkeypatch.setattr(terminal_module, "close_terminal", lambda _sid: None)

    import_started = threading.Event()
    allow_import = threading.Event()
    original_import = routes_module.import_cli_session

    def paused_import(*args, **kwargs):
        assert args[0] == sid
        import_started.set()
        assert allow_import.wait(timeout=5)
        return original_import(*args, **kwargs)

    monkeypatch.setattr(routes_module, "import_cli_session", paused_import)
    rename_result = {}

    def rename():
        handler = _DeleteJSONHandler({"session_id": sid, "title": "after"})
        routes_module.handle_post(handler, SimpleNamespace(path="/api/session/rename"))
        rename_result["status"] = handler.status

    rename_thread = threading.Thread(target=rename)
    rename_thread.start()
    assert import_started.wait(timeout=5), "rename must reach real import_cli_session"

    try:
        assert _delete_via_route(routes_module, sid) == 200
        assert sid in models_module._load_webui_deleted_session_tombstone()
        assert not (session_dir / f"{sid}.json").exists()
        assert not (session_dir / f"{sid}.json.bak").exists()
    finally:
        allow_import.set()
        rename_thread.join(timeout=5)

    assert not rename_thread.is_alive()
    assert rename_result["status"] in {404, 409}
    assert not (session_dir / f"{sid}.json").exists()
    assert not (session_dir / f"{sid}.json.bak").exists()
    assert not index_file.exists() or sid not in {
        row.get("session_id") for row in json.loads(index_file.read_text(encoding="utf-8"))
    }
    assert sid in models_module._load_webui_deleted_session_tombstone()
    models_module.SESSIONS.clear()
    assert models_module.Session.load(sid) is None
    with sqlite3.connect(str(state_db)) as conn:
        assert conn.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone() is None


def test_session_persistence_generation_registry_releases_unused_sid(
    models_module,
):
    """The in-process deletion fence must not retain every historical SID."""
    sid = "weak-session-persistence-generation"
    session = models_module.Session(session_id=sid)
    capability_ref = weakref.ref(session._persistence_generation)
    assert models_module._SESSION_PERSISTENCE_GENERATIONS[sid] is capability_ref()
    assert "_persistence_generation" not in session.__dict__
    json.dumps(session.__dict__)

    del session
    gc.collect()

    assert capability_ref() is None
    assert sid not in models_module._SESSION_PERSISTENCE_GENERATIONS


def test_session_identity_transition_requires_current_source_capability(models_module):
    """A revoked owner cannot adopt a fresh SID after changing session_id."""
    source_sid = "identity-transition-revoked-source"
    target_sid = "identity-transition-target"
    stale = models_module.Session(session_id=source_sid)
    source_capability = stale._persistence_generation

    with models_module._get_session_persistence_lock(source_sid):
        models_module._advance_session_persistence_generation(source_sid)

    stale.session_id = target_sid
    with pytest.raises(RuntimeError, match="revoked|deleted"):
        models_module._bind_session_persistence_capability_for_identity_transition(
            stale, source_sid, target_sid
        )

    assert stale._persistence_generation is source_capability
    assert source_capability.revoked is True
    assert target_sid not in models_module._SESSION_PERSISTENCE_GENERATIONS


def test_session_identity_transition_rejects_same_sid(models_module):
    """The privileged transition helper cannot refresh one SID in place."""
    sid = "identity-transition-same-sid"
    session = models_module.Session(session_id=sid)

    with pytest.raises(ValueError, match="distinct"):
        models_module._bind_session_persistence_capability_for_identity_transition(
            session, sid, sid
        )


def test_successful_delete_revokes_delayed_session_save(
    models_module, monkeypatch, tmp_path
):
    """A captured Session object loses persistence authority after delete 200."""
    routes_module = pytest.importorskip("api.routes")
    session_dir, index_file = _install_delete_route_test_harness(
        models_module, routes_module, monkeypatch, tmp_path
    )
    sid = "delete-revokes-delayed-save"
    stale = models_module.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "preserve"}],
    )
    models_module.SESSIONS[sid] = stale
    stale.save()

    assert _delete_via_route(routes_module, sid) == 200
    stale.title = "must not return"
    with pytest.raises(RuntimeError, match="revoked|deleted"):
        stale.save()

    assert not (session_dir / f"{sid}.json").exists()
    assert sid not in {
        row.get("session_id")
        for row in json.loads(index_file.read_text(encoding="utf-8"))
    }
    assert sid in models_module._load_webui_deleted_session_tombstone()

    monkeypatch.setattr(
        routes_module,
        "_resolve_cli_import_metadata",
        lambda _sid, **_kwargs: {
            "title": "explicit recreation",
            "model": "unknown",
            "source": "cli",
            "source_tag": "cli",
        },
    )
    monkeypatch.setattr(
        routes_module,
        "get_cli_session_messages",
        lambda _sid, profile=None: [
            {"role": "user", "content": "explicit recreation"},
        ],
    )
    monkeypatch.setattr(
        routes_module,
        "_queue_generated_title_for_imported_session",
        lambda *_args, **_kwargs: None,
    )
    import_handler = _DeleteJSONHandler({"session_id": sid})
    routes_module.handle_post(
        import_handler, SimpleNamespace(path="/api/session/import_cli")
    )
    assert import_handler.status == 200
    import_payload = json.loads(import_handler.wfile.getvalue())
    assert import_payload["imported"] is True
    assert import_payload["session"]["session_id"] == sid
    assert (session_dir / f"{sid}.json").exists()
    assert sid not in models_module._load_webui_deleted_session_tombstone()


def test_archive_materialization_tombstone_race_returns_not_found(
    models_module, monkeypatch, tmp_path
):
    """A delete winning during archive fallback keeps the archive 404 contract."""
    routes_module = pytest.importorskip("api.routes")
    session_dir, _index_file = _install_delete_route_test_harness(
        models_module, routes_module, monkeypatch, tmp_path
    )
    sid = "archive-tombstone-race"
    monkeypatch.setattr(
        routes_module,
        "_lookup_cli_session_metadata",
        lambda _sid: {"title": "CLI archive", "model": "unknown", "source_tag": "cli"},
    )
    monkeypatch.setattr(
        routes_module,
        "get_cli_session_messages",
        lambda _sid, profile=None: [{"role": "user", "content": "archive"}],
    )
    monkeypatch.setattr(
        routes_module,
        "import_cli_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyError(sid)),
    )

    handler = _DeleteJSONHandler({"session_id": sid})
    routes_module.handle_post(handler, SimpleNamespace(path="/api/session/archive"))

    assert handler.status == 404
    payload = json.loads(handler.wfile.getvalue())
    assert payload["error"] == "Session not found"
    assert not (session_dir / f"{sid}.json").exists()


def test_failed_delete_does_not_revoke_session_save(
    models_module, monkeypatch, tmp_path
):
    """A truthful delete 500 keeps the existing owner able to persist/retry."""
    routes_module = pytest.importorskip("api.routes")
    session_dir, _index_file = _install_delete_route_test_harness(
        models_module, routes_module, monkeypatch, tmp_path
    )
    sid = "failed-delete-keeps-owner"
    owner = models_module.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "preserve"}],
    )
    models_module.SESSIONS[sid] = owner
    owner.save()
    sidecar = session_dir / f"{sid}.json"
    real_unlink = Path.unlink

    def fail_primary_unlink(path, *args, **kwargs):
        if path == sidecar:
            raise PermissionError("simulated retained primary")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_primary_unlink)

    assert _delete_via_route(routes_module, sid) == 500
    assert models_module.SESSIONS[sid] is owner
    assert sid not in models_module._load_webui_deleted_session_tombstone()
    owner.title = "retry remains usable"
    owner.save()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["title"] == owner.title


def test_failed_delete_tombstone_publish_does_not_revoke_session_save(
    models_module, monkeypatch, tmp_path
):
    """A swallowed tombstone write failure cannot commit deletion/revocation."""
    routes_module = pytest.importorskip("api.routes")
    session_dir, _index_file = _install_delete_route_test_harness(
        models_module, routes_module, monkeypatch, tmp_path
    )
    sid = "failed-delete-tombstone-keeps-owner"
    owner = models_module.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "preserve"}],
    )
    models_module.SESSIONS[sid] = owner
    owner.save()
    sidecar = session_dir / f"{sid}.json"
    monkeypatch.setattr(
        models_module, "_save_webui_deleted_session_tombstone", lambda _ids: None
    )

    assert _delete_via_route(routes_module, sid) == 500
    assert models_module.SESSIONS[sid] is owner
    assert sid not in models_module._load_webui_deleted_session_tombstone()
    owner.title = "tombstone retry remains usable"
    owner.save()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["title"] == owner.title


def test_failed_delete_index_settlement_does_not_revoke_session_save(
    models_module, monkeypatch, tmp_path
):
    """A swallowed/no-op index prune cannot commit deletion/revocation."""
    routes_module = pytest.importorskip("api.routes")
    session_dir, index_file = _install_delete_route_test_harness(
        models_module, routes_module, monkeypatch, tmp_path
    )
    sid = "failed-delete-index-keeps-owner"
    owner = models_module.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "preserve"}],
    )
    models_module.SESSIONS[sid] = owner
    owner.save()
    sidecar = session_dir / f"{sid}.json"
    monkeypatch.setattr(routes_module, "prune_session_from_index", lambda _sid: None)

    assert _delete_via_route(routes_module, sid) == 500
    assert models_module.SESSIONS[sid] is owner
    assert sid in {
        row.get("session_id")
        for row in json.loads(index_file.read_text(encoding="utf-8"))
    }
    assert sid not in models_module._load_webui_deleted_session_tombstone()
    owner.title = "index retry remains usable"
    owner.save()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["title"] == owner.title


def test_delete_serializes_with_workspace_recovery_and_sidecar_stays_deleted(
    models_module, monkeypatch, tmp_path
):
    routes_module = pytest.importorskip("api.routes")
    config_module = pytest.importorskip("api.config")
    upload_module = pytest.importorskip("api.upload")
    turn_journal_module = pytest.importorskip("api.turn_journal")
    run_journal_module = pytest.importorskip("api.run_journal")
    background_module = pytest.importorskip("api.background_process")
    terminal_module = pytest.importorskip("api.terminal")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    stale = tmp_path / "deleted-workspace"
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    sid = "delete-recovery-race"
    session = SimpleNamespace(
        session_id=sid,
        workspace=str(stale),
        profile=None,
        _loaded_metadata_only=True,
    )
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": sid,
                "workspace": str(stale),
                "messages": [{"role": "user", "content": "preserve me"}],
            }
        ),
        encoding="utf-8",
    )
    replace_entered = threading.Event()
    allow_replace = threading.Event()
    original_replace = models_module._safe_replace

    def paused_replace(source, target):
        replace_entered.set()
        assert allow_replace.wait(timeout=5)
        original_replace(source, target)

    monkeypatch.setattr(models_module, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes_module, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models_module, "_safe_replace", paused_replace)
    monkeypatch.setattr(models_module, "_write_session_index", lambda **_kwargs: None)
    monkeypatch.setattr(routes_module, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes_module, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes_module, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes_module, "_is_messaging_session_id", lambda _sid: False)
    monkeypatch.setattr(
        routes_module, "_worktree_retained_payload_for_session_id", lambda _sid: {}
    )
    monkeypatch.setattr(routes_module, "prune_session_from_index", lambda _sid: None)
    monkeypatch.setattr(
        routes_module, "_publish_session_list_changed", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(config_module, "_evict_session_agent", lambda _sid: None)
    monkeypatch.setattr(
        models_module,
        "delete_cli_session_for_webui_delete",
        lambda _sid: True,
    )
    monkeypatch.setattr(
        upload_module,
        "_session_attachment_dir",
        lambda _sid: tmp_path / "attachments" / _sid,
    )
    monkeypatch.setattr(turn_journal_module, "delete_turn_journal", lambda _sid: None)
    monkeypatch.setattr(run_journal_module, "delete_run_journal", lambda _sid: None)
    monkeypatch.setattr(
        background_module, "forget_bg_task_completion_dedup", lambda _sid: None
    )
    monkeypatch.setattr(terminal_module, "close_terminal", lambda _sid: None)

    recovery_errors = []

    def recover():
        try:
            models_module.persist_recovered_workspace_binding(
                session,
                fallback,
            )
        except Exception as exc:
            recovery_errors.append(exc)

    recovery_thread = threading.Thread(target=recover)
    recovery_thread.start()
    assert replace_entered.wait(timeout=5)

    delete_result = {}

    def delete():
        handler = _DeleteJSONHandler({"session_id": sid})
        routes_module.handle_post(
            handler, SimpleNamespace(path="/api/session/delete")
        )
        delete_result["status"] = handler.status

    delete_thread = threading.Thread(target=delete)
    delete_thread.start()
    delete_thread.join(timeout=0.2)
    assert delete_thread.is_alive(), "delete must wait for the recovery mutation lock"

    allow_replace.set()
    recovery_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    assert not recovery_errors
    assert delete_result["status"] == 200
    assert not sidecar.exists()


def test_delete_returns_503_without_mutation_when_session_lock_is_busy(
    models_module, monkeypatch, tmp_path
):
    routes_module = pytest.importorskip("api.routes")
    sid = "delete-busy-session"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(
        json.dumps({"session_id": sid, "messages": [{"role": "user", "content": "keep"}]}),
        encoding="utf-8",
    )
    cached_session = SimpleNamespace(session_id=sid, profile=None)
    observed = {"acquire_timeouts": [], "released": 0, "mutations": []}

    class ContendedLock:
        def acquire(self, timeout=None):
            observed["acquire_timeouts"].append(timeout)
            return timeout is None

        def release(self):
            observed["released"] += 1

        def __enter__(self):
            assert self.acquire()
            return self

        def __exit__(self, *_args):
            self.release()

    monkeypatch.setattr(routes_module, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes_module, "SESSIONS", {sid: cached_session})
    monkeypatch.setattr(routes_module, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes_module,
        "get_session",
        lambda *_args, **_kwargs: cached_session,
    )
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(
        routes_module, "_session_is_subagent_view_only", lambda _sid: False
    )
    monkeypatch.setattr(routes_module, "_is_messaging_session_id", lambda _sid: False)
    monkeypatch.setattr(
        routes_module, "_worktree_retained_payload_for_session_id", lambda _sid: {}
    )
    monkeypatch.setattr(
        routes_module, "_get_session_agent_lock", lambda _sid: ContendedLock()
    )
    monkeypatch.setattr(
        routes_module,
        "prune_session_from_index",
        lambda _sid: observed["mutations"].append("index"),
    )
    monkeypatch.setattr(
        routes_module,
        "_record_webui_deleted_session_tombstone",
        lambda _sid: observed["mutations"].append("tombstone"),
    )
    monkeypatch.setattr(
        routes_module,
        "_publish_session_list_changed",
        lambda *_args, **_kwargs: observed["mutations"].append("publish"),
    )
    monkeypatch.setattr(
        models_module,
        "delete_cli_session_for_webui_delete",
        lambda _sid: observed["mutations"].append("state-db"),
    )

    handler = _DeleteJSONHandler({"session_id": sid})
    routes_module.handle_post(handler, SimpleNamespace(path="/api/session/delete"))

    payload = json.loads(handler.wfile.getvalue())
    assert handler.status == 503
    assert payload == {"error": "Session busy, try again"}
    assert observed == {"acquire_timeouts": [5], "released": 0, "mutations": []}
    assert sidecar.exists()
    assert routes_module.SESSIONS[sid] is cached_session


def test_successful_delete_linearizes_missing_index_rebuild_with_owner_retirement(
    models_module, monkeypatch, tmp_path
):
    """A queued real full-index rebuild cannot republish a successful delete."""
    routes_module = pytest.importorskip("api.routes")
    session_dir, index_file = _install_delete_route_test_harness(
        models_module, routes_module, monkeypatch, tmp_path
    )
    sid = "delete-rebuild-owner-fence"
    sibling_sid = f"{sid}-sibling"
    owner = models_module.Session(
        session_id=sid,
        title="delete target",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "delete me"}],
    )
    sibling = models_module.Session(
        session_id=sibling_sid,
        title="keep sibling",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "keep me"}],
    )
    models_module.SESSIONS[sid] = owner
    models_module.SESSIONS[sibling_sid] = sibling
    owner.save()
    sibling.save()
    index_file.unlink()

    stale_rebuild = getattr(models_module, "_SESSION_INDEX_REBUILD_THREAD", None)
    if stale_rebuild is not None:
        stale_rebuild.join(timeout=5)
    models_module._SESSION_INDEX_REBUILD_THREAD = None
    models_module._SESSION_INDEX_REBUILD_THREAD_TARGET = None

    rebuild_captured = threading.Event()
    allow_rebuild_publish = threading.Event()
    real_safe_replace = models_module._safe_replace

    def pause_real_rebuild_publish(source, target):
        if (
            target == index_file
            and threading.current_thread().name == "session-index-rebuild"
        ):
            rebuild_captured.set()
            assert allow_rebuild_publish.wait(timeout=5)
        return real_safe_replace(source, target)

    monkeypatch.setattr(models_module, "_safe_replace", pause_real_rebuild_publish)
    real_record_tombstone = routes_module._record_webui_deleted_session_tombstone

    def queue_real_rebuild_before_tombstone(candidate_sid):
        models_module._start_session_index_rebuild_thread()
        return real_record_tombstone(candidate_sid)

    monkeypatch.setattr(
        routes_module,
        "_record_webui_deleted_session_tombstone",
        queue_real_rebuild_before_tombstone,
    )

    delete_result = {}

    def delete():
        delete_result["status"], delete_result["payload"] = _delete_result_via_route(
            routes_module, sid
        )

    delete_thread = threading.Thread(target=delete, name="delete-target")
    delete_thread.start()
    assert rebuild_captured.wait(timeout=5), (
        "the real missing-index rebuild must capture the retained owner "
        "before its publication fence"
    )
    delete_thread.join(timeout=5)
    assert not delete_thread.is_alive()
    assert delete_result["status"] == 200

    allow_rebuild_publish.set()
    rebuild_thread = getattr(models_module, "_SESSION_INDEX_REBUILD_THREAD", None)
    if rebuild_thread is not None:
        rebuild_thread.join(timeout=5)
        assert not rebuild_thread.is_alive()

    rows = json.loads(index_file.read_text(encoding="utf-8"))
    row_ids = {row.get("session_id") for row in rows}
    assert sid not in row_ids
    assert sibling_sid in row_ids
    assert sid in models_module._load_webui_deleted_session_tombstone()
    assert not (session_dir / f"{sid}.json").exists()
    assert not (session_dir / f"{sid}.json.bak").exists()
    assert (session_dir / f"{sibling_sid}.json").exists()
    assert sid not in models_module.SESSIONS
    assert models_module.SESSIONS[sibling_sid] is sibling
    assert not models_module._session_persistence_generation_is_current(owner)
    assert models_module.Session.load(sid) is None


def test_delete_finalization_refuses_replaced_cache_owner(
    models_module, monkeypatch, tmp_path
):
    """A same-SID cache replacement is preserved and never revoked by delete."""
    routes_module = pytest.importorskip("api.routes")
    session_dir, index_file = _install_delete_route_test_harness(
        models_module, routes_module, monkeypatch, tmp_path
    )
    sid = "delete-replaced-cache-owner"
    owner = models_module.Session(
        session_id=sid,
        title="original owner",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "original"}],
    )
    successor = models_module.Session(
        session_id=sid,
        title="successor owner",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "successor"}],
    )
    models_module.SESSIONS[sid] = owner
    owner.save()
    real_record_tombstone = routes_module._record_webui_deleted_session_tombstone

    def replace_owner_then_record(candidate_sid):
        with models_module.LOCK:
            models_module.SESSIONS[candidate_sid] = successor
        return real_record_tombstone(candidate_sid)

    monkeypatch.setattr(
        routes_module,
        "_record_webui_deleted_session_tombstone",
        replace_owner_then_record,
    )

    status, _payload = _delete_result_via_route(routes_module, sid)

    assert status >= 400
    assert models_module.SESSIONS[sid] is successor
    assert not successor._persistence_generation.revoked
    assert not owner._persistence_generation.revoked
    assert index_file.exists()


def test_session_lock_registry_reuses_live_lock_and_reclaims_unused_entry():
    config_module = pytest.importorskip("api.config")
    sid = "weak-session-lock"
    with config_module.SESSION_AGENT_LOCKS_LOCK:
        config_module.SESSION_AGENT_LOCKS.pop(sid, None)

    first = config_module._get_session_agent_lock(sid)
    first_ref = weakref.ref(first)
    assert config_module._get_session_agent_lock(sid) is first

    del first
    gc.collect()

    assert first_ref() is None
    with config_module.SESSION_AGENT_LOCKS_LOCK:
        assert sid not in config_module.SESSION_AGENT_LOCKS


def test_compression_lock_alias_keeps_old_and_new_ids_on_one_live_lock():
    config_module = pytest.importorskip("api.config")
    old_sid = "compression-old-lock"
    new_sid = "compression-new-lock"
    with config_module.SESSION_AGENT_LOCKS_LOCK:
        config_module.SESSION_AGENT_LOCKS.pop(old_sid, None)
        config_module.SESSION_AGENT_LOCKS.pop(new_sid, None)

    compression_lock = config_module._get_session_agent_lock(old_sid)
    waiter_reference = compression_lock
    config_module._alias_session_agent_lock(
        old_sid,
        new_sid,
        compression_lock,
    )

    assert config_module._get_session_agent_lock(old_sid) is waiter_reference
    assert config_module._get_session_agent_lock(new_sid) is waiter_reference
    streaming_source = (Path(__file__).parents[1] / "api" / "streaming.py").read_text(
        encoding="utf-8"
    )
    assert "_alias_session_agent_lock(old_sid, new_sid, _agent_lock)" in streaming_source


def test_get_session_for_file_ops_does_not_fallback_existing_untrusted_workspace(
    models_module, monkeypatch, tmp_path
):
    """Recovery must not replace a non-missing trust rejection with a fallback."""
    profiles_module = pytest.importorskip("api.profiles")
    workspace_module = pytest.importorskip("api.workspace")
    home = tmp_path / "home"
    fallback = home / "fallback"
    fallback.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    session = SimpleNamespace(
        session_id="untrusted-webui-sid",
        profile=None,
        workspace=str(outside),
    )

    monkeypatch.setattr(models_module, "get_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(models_module, "get_last_workspace", lambda: str(fallback))
    monkeypatch.setattr(profiles_module, "_profiles_match", lambda *_args: True)
    monkeypatch.setattr(profiles_module, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(workspace_module, "_home_path", lambda: home)
    monkeypatch.setattr(workspace_module, "load_workspaces", lambda: [])
    monkeypatch.setattr(workspace_module, "_BOOT_DEFAULT_WORKSPACE", fallback)

    result = models_module.get_session_for_file_ops(session.session_id)

    assert result is session
    assert result.workspace == str(outside)


@pytest.mark.parametrize(
    "terminal_cfg",
    [
        pytest.param(
            {"backend": "ssh", "cwd": "/Users/joeyshiue"},
            id="cwd-absolute",
        ),
        pytest.param({"backend": "ssh"}, id="cwd-omitted"),
        pytest.param({"backend": "ssh", "cwd": ""}, id="cwd-empty"),
        pytest.param({"backend": "ssh", "cwd": "."}, id="cwd-dot"),
    ],
)
def test_get_session_for_file_ops_does_not_recover_remote_trust_rejection(
    models_module, monkeypatch, tmp_path, terminal_cfg
):
    """A local miss cannot prove that an out-of-scope remote path was deleted."""
    config_module = pytest.importorskip("api.config")
    profiles_module = pytest.importorskip("api.profiles")
    workspace_module = pytest.importorskip("api.workspace")
    candidate = "/Users/other/projects/demo"
    fallback_path = tmp_path / "fallback"
    fallback_path.mkdir()
    session = SimpleNamespace(
        session_id="remote-untrusted-webui-sid",
        profile=None,
        workspace=candidate,
    )
    fallback_calls = {"count": 0}

    monkeypatch.setattr(models_module, "get_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(profiles_module, "_profiles_match", lambda *_args: True)
    monkeypatch.setattr(profiles_module, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"terminal": terminal_cfg},
    )
    monkeypatch.setattr(workspace_module, "_home_path", lambda: tmp_path)

    def fallback():
        fallback_calls["count"] += 1
        return fallback_path

    monkeypatch.setattr(models_module, "get_last_workspace", fallback)

    result = models_module.get_session_for_file_ops(session.session_id)

    assert result is session
    assert result.workspace == candidate
    assert fallback_calls["count"] == 0


def test_get_session_for_file_ops_rejects_foreign_profile(
    models_module, monkeypatch, tmp_path, caplog
):
    """WebUI sessions must belong to the active profile before file access."""
    profiles_module = pytest.importorskip("api.profiles")
    foreign_session = SimpleNamespace(profile="research", workspace=str(tmp_path))
    called = {"get_session": 0, "profile_match": 0, "state_db": 0}

    def fake_get_session(sid, metadata_only=False):
        called["get_session"] += 1
        return foreign_session

    def fake_profiles_match(session_profile, active_profile):
        called["profile_match"] += 1
        assert session_profile == "research"
        assert active_profile == "default"
        return False

    def fake_has(_sid):
        called["state_db"] += 1
        return True

    monkeypatch.setattr(models_module, "get_session", fake_get_session)
    monkeypatch.setattr(models_module, "state_db_has_session", fake_has)
    monkeypatch.setattr(profiles_module, "_profiles_match", fake_profiles_match)
    monkeypatch.setattr(profiles_module, "get_active_profile_name", lambda: "default")

    with caplog.at_level(logging.DEBUG, logger=models_module.logger.name):
        with pytest.raises(KeyError):
            models_module.get_session_for_file_ops("foreign-webui-sid")
    # A found-but-foreign WebUI sidecar is an authorization failure, not a
    # missing-session condition that can fall through to the state.db fallback.
    assert called == {"get_session": 1, "profile_match": 1, "state_db": 0}
    assert "Rejected file-manager session for foreign profile" in caplog.text
    assert "foreign-webui-sid" in caplog.text
    assert "session_profile='research'" in caplog.text
    assert "active_profile='default'" in caplog.text


def test_file_read_rejects_foreign_profile_session(
    models_module, monkeypatch, tmp_path
):
    """A default-profile file route cannot read a named-profile workspace."""
    profiles_module = pytest.importorskip("api.profiles")
    routes_module = pytest.importorskip("api.routes")
    workspace = tmp_path / "named-workspace"
    workspace.mkdir()
    (workspace / "marker.txt").write_text("foreign profile marker")
    session = models_module.Session(
        session_id="foreign-profile-file-read",
        workspace=str(workspace),
        profile="research",
    )
    models_module.SESSIONS[session.session_id] = session

    class Handler:
        command = "GET"
        headers = {}

        def __init__(self):
            self.status = None
            self.headers_sent = []
            self.wfile = io.BytesIO()

        def send_response(self, code):
            self.status = code

        def send_header(self, key, value):
            self.headers_sent.append((key, value))

        def end_headers(self):
            pass

    monkeypatch.setattr(profiles_module, "get_active_profile_name", lambda: "default")
    try:
        handler = Handler()
        routes_module._handle_file_read(
            handler,
            urlparse(
                "/api/file?session_id=foreign-profile-file-read&path=marker.txt"
            ),
        )
        assert handler.status == 404
        assert b"foreign profile marker" not in handler.wfile.getvalue()
    finally:
        models_module.SESSIONS.pop(session.session_id, None)


def test_get_session_for_file_ops_state_db_fallback(
    models_module, monkeypatch, tmp_path
):
    """(b) state.db-only session — returns view with workspace populated."""
    db = tmp_path / "state.db"
    _make_state_db(db, "tg-123")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hi from telegram session")

    def raise_key(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(models_module, "get_session", raise_key)
    monkeypatch.setattr(models_module, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(
        models_module, "get_last_workspace", lambda: str(workspace)
    )

    view = models_module.get_session_for_file_ops("tg-123")
    assert view.session_id == "tg-123"
    assert Path(view.workspace) == workspace
    # The workspace is real and readable — file-manager handlers will
    # successfully serve files relative to it instead of returning 404.
    assert (Path(view.workspace) / "hello.txt").read_text() == "hi from telegram session"


def test_get_session_for_file_ops_unknown_session_raises(
    models_module, monkeypatch, tmp_path
):
    """(c) Unknown session — KeyError propagates so callers still 404."""
    db = tmp_path / "state.db"
    _make_state_db(db, "tg-123")

    def raise_key(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(models_module, "get_session", raise_key)
    monkeypatch.setattr(models_module, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models_module, "get_last_workspace", lambda: str(tmp_path))

    with pytest.raises(KeyError):
        models_module.get_session_for_file_ops("does-not-exist")


def test_state_db_has_session_missing_db(models_module, monkeypatch, tmp_path):
    monkeypatch.setattr(
        models_module, "_active_state_db_path", lambda: tmp_path / "missing.db"
    )
    assert models_module.state_db_has_session("any") is False


def test_state_db_has_session_present(models_module, monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db, "cli-9")
    monkeypatch.setattr(models_module, "_active_state_db_path", lambda: db)
    assert models_module.state_db_has_session("cli-9") is True
    assert models_module.state_db_has_session("nope") is False
