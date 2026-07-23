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
        routes_module, "_record_webui_deleted_session_tombstone", lambda _sid: None
    )
    monkeypatch.setattr(
        routes_module, "_publish_session_list_changed", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(config_module, "_evict_session_agent", lambda _sid: None)
    monkeypatch.setattr(models_module, "delete_cli_session", lambda _sid: True)
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
