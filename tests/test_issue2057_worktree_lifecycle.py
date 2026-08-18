import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.models as models
import api.profiles as profiles_module
import api.run_journal as run_journal
import api.routes as routes
from api.models import SESSIONS, Session


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
    return captured


def _isolate_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    SESSIONS.clear()
    return session_dir


def _worktree_session(tmp_path, session_id):
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / f"hermes-{session_id}"
    worktree.mkdir(parents=True)
    s = Session(
        session_id=session_id,
        title="Worktree session",
        workspace=str(worktree),
        worktree_path=str(worktree),
        worktree_branch=f"hermes/{session_id}",
        worktree_repo_root=str(repo),
    )
    s.save()
    return s, worktree


def _make_state_db(path, sid, *, source="telegram"):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            model TEXT,
            message_count INTEGER DEFAULT 0,
            started_at REAL,
            title TEXT,
            cwd TEXT
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
        "INSERT INTO sessions (id, source, model, message_count, started_at, title, cwd) "
        "VALUES (?, ?, 'MiniMax-M3', 2, 1781024055.0, 'Telegram chat', ?)",
        (sid, source, str(path.parent)),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'hi', 1781024055.0)",
        (sid,),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'assistant', 'hello', 1781024056.0)",
        (sid,),
    )
    conn.commit()
    conn.close()


def _make_delete_state_db(path, sid):
    """Create the minimum current Agent schema needed by delete_cli_session()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
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
        "VALUES (?, 'cli', 'MiniMax-M3', 2, 1781024055.0, 'CLI delete', ?, NULL, NULL)",
        (sid, str(path.parent)),
    )
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        [(sid, "user", "hi", 1781024055.0), (sid, "assistant", "hello", 1781024056.0)],
    )
    conn.commit()
    conn.close()


def test_delete_worktree_session_reports_retained_worktree_without_cleanup(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    session, worktree = _worktree_session(tmp_path, "wtdelete1")
    captured = _capture_post(monkeypatch, {"session_id": session.session_id})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    state_db_delete_calls = []
    monkeypatch.setattr(
        models,
        "delete_cli_session_for_webui_delete",
        lambda sid: state_db_delete_calls.append(sid) or True,
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["state_db_cleanup_failed"] is False
    assert captured["payload"]["worktree_retained"] is True
    assert captured["payload"]["worktree_path"] == str(worktree.resolve())
    assert captured["payload"]["worktree_branch"] == "hermes/wtdelete1"
    assert not (session_dir / "wtdelete1.json").exists()
    assert state_db_delete_calls == [session.session_id]
    assert worktree.exists(), "session delete must not remove the git worktree directory"


def test_delete_session_state_db_failure_keeps_retryable_owner_without_tombstone(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "dbfaildelete1"
    session = Session(
        session_id=sid,
        title="Delete failure",
        messages=[{"role": "user", "content": "keep deleted"}],
    )
    session.save()
    (session_dir / f"{sid}.json.bak").write_text("backup", encoding="utf-8")
    captured = _capture_post(monkeypatch, {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)

    def fail_delete(value):
        raise RuntimeError("state.db locked")

    monkeypatch.setattr(models, "delete_cli_session_for_webui_delete", fail_delete)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 500
    assert captured["payload"]["ok"] is False
    assert captured["payload"]["state_db_cleanup_failed"] is True
    assert (session_dir / f"{sid}.json").exists()
    assert (session_dir / f"{sid}.json.bak").exists()
    assert sid in models.SESSIONS
    assert sid not in models._load_webui_deleted_session_tombstone()


def test_delete_session_is_not_blocked_by_unrelated_stale_cli_cleanup(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "webuionlydelete1"
    session = Session(
        session_id=sid,
        title="WebUI-only delete",
        messages=[{"role": "user", "content": "delete only this session"}],
    )
    session.save()

    hermes_home = tmp_path / "hermes-home"
    state_db = hermes_home / "state.db"
    _make_delete_state_db(state_db, sid)
    agent_sessions = hermes_home / "sessions"
    agent_sessions.mkdir(parents=True, exist_ok=True)
    unrelated_manifest = agent_sessions / ".cleanup_manifest_unrelated.json"
    unrelated_manifest.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(profiles_module, "get_active_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
    captured = _capture_post(monkeypatch, {"session_id": sid})

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200, captured
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["state_db_cleanup_failed"] is False
    assert not (session_dir / f"{sid}.json").exists()
    assert sid in models._load_webui_deleted_session_tombstone()
    assert unrelated_manifest.exists(), "another SID's retry record stays actionable"


def test_scoped_state_cleanup_missing_db_requires_no_state_only_evidence(tmp_path, monkeypatch):
    sid = "webuionlymissingdb1"
    hermes_home = tmp_path / "hermes-home"
    agent_sessions = hermes_home / "sessions"
    agent_sessions.mkdir(parents=True)
    (agent_sessions / f"{sid}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(models, "SESSION_DIR", agent_sessions)
    monkeypatch.setattr(profiles_module, "get_active_hermes_home", lambda: hermes_home)

    assert models.delete_cli_session_for_webui_delete(sid) is True
    assert (agent_sessions / f"{sid}.json").exists(), (
        "the route, not state cleanup, owns the retry sidecar"
    )

    state_transcript = agent_sessions / f"{sid}.jsonl"
    state_transcript.write_text('{"role":"user","content":"retain"}\n', encoding="utf-8")
    assert models.delete_cli_session_for_webui_delete(sid) is False
    state_transcript.unlink()

    (agent_sessions / ".cleanup_manifest_pending.json").write_text(
        json.dumps([sid]),
        encoding="utf-8",
    )
    assert models.delete_cli_session_for_webui_delete(sid) is False


def test_delete_retry_preserves_colocated_sidecar_during_stale_manifest_cleanup(
    tmp_path, monkeypatch
):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "colocated-stale-delete1"
    owner = Session(
        session_id=sid,
        title="Colocated retry owner",
        messages=[{"role": "user", "content": "keep retry handle"}],
    )
    models.SESSIONS[sid] = owner
    owner.save()
    sidecar = session_dir / f"{sid}.json"

    # A previous post-commit artifact cleanup left a durable retry record.
    # Simulate the state owner disappearing after the scoped entry check but
    # before the stale-manifest prepass (for example, an Agent-side delete that
    # does not participate in the WebUI process lock).
    state_db = tmp_path / "state.db"
    _make_delete_state_db(state_db, sid)
    stale_manifest = session_dir / ".cleanup_manifest_colocated-stale.json"
    stale_manifest.write_text(json.dumps([sid]), encoding="utf-8")
    state_transcript = session_dir / f"{sid}.jsonl"
    state_transcript.write_text('{"role":"user","content":"private"}\n', encoding="utf-8")
    request_dump = session_dir / f"request_dump_{sid}_1.json"
    request_dump.write_text('{"secret":"private"}', encoding="utf-8")

    monkeypatch.setattr(profiles_module, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda _sid: False)
    captured = _capture_post(monkeypatch, {"session_id": sid})

    real_unlink = Path.unlink
    block_route_sidecar = True
    state_cleanup_finished = False

    def fail_route_sidecar_cleanup(path, *args, **kwargs):
        if (
            block_route_sidecar
            and state_cleanup_finished
            and Path(path).resolve() == sidecar.resolve()
        ):
            raise PermissionError("route-owned sidecar locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_route_sidecar_cleanup)

    real_delete_locked = models._delete_cli_session_locked
    remove_state_row_once = True

    def delete_after_external_state_commit(
        candidate_sid, hermes_home, *, require_all_stale_cleanup=True
    ):
        nonlocal remove_state_row_once, state_cleanup_finished
        if remove_state_row_once:
            remove_state_row_once = False
            with sqlite3.connect(str(state_db)) as conn:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (candidate_sid,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (candidate_sid,))
                conn.commit()
        result = real_delete_locked(
            candidate_sid,
            hermes_home,
            require_all_stale_cleanup=require_all_stale_cleanup,
        )
        state_cleanup_finished = True
        return result

    monkeypatch.setattr(models, "_delete_cli_session_locked", delete_after_external_state_commit)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 500
    assert captured["payload"]["ok"] is False
    assert captured["payload"]["state_db_cleanup_failed"] is False
    assert captured["payload"]["session_artifact_cleanup_failed"] is True
    assert sidecar.exists(), "failed deletion must retain the colocated retry sidecar"
    assert not state_transcript.exists()
    assert not request_dump.exists()
    assert not stale_manifest.exists()
    assert models.SESSIONS[sid] is owner

    block_route_sidecar = False
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200, captured
    assert captured["payload"]["ok"] is True
    assert not sidecar.exists()
    assert not state_transcript.exists()
    assert not request_dump.exists()
    assert not list(session_dir.glob(".cleanup_manifest_*.json"))
    assert sid not in models.SESSIONS


def test_scoped_stale_cleanup_preserves_unrelated_manifest_entries(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "scoped-manifest-target1"
    unrelated_sid = "scoped-manifest-unrelated1"
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text('{"title":"retry owner"}', encoding="utf-8")
    state_transcript = session_dir / f"{sid}.jsonl"
    state_transcript.write_text('{"role":"user"}\n', encoding="utf-8")
    request_dump = session_dir / f"request_dump_{sid}_1.json"
    request_dump.write_text('{"secret":"target"}', encoding="utf-8")
    unrelated_json = session_dir / f"{unrelated_sid}.json"
    unrelated_json.write_text('{"secret":"unrelated"}', encoding="utf-8")
    unrelated_jsonl = session_dir / f"{unrelated_sid}.jsonl"
    unrelated_jsonl.write_text('{"role":"user"}\n', encoding="utf-8")
    manifest = session_dir / ".cleanup_manifest_mixed.json"
    manifest.write_text(json.dumps([sid, unrelated_sid]), encoding="utf-8")

    _make_delete_state_db(tmp_path / "state.db", "unrelated-live-placeholder")
    monkeypatch.setattr(profiles_module, "get_active_hermes_home", lambda: tmp_path)

    assert models.delete_cli_session_for_webui_delete(sid) is True

    assert sidecar.exists(), "the WebUI route still owns its retry sidecar"
    assert not state_transcript.exists()
    assert not request_dump.exists()
    assert unrelated_json.exists()
    assert unrelated_jsonl.exists()
    assert json.loads(manifest.read_text(encoding="utf-8")) == [unrelated_sid]


def test_delete_session_reports_run_journal_cleanup_failure(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "runjournaldelete1"
    session = Session(session_id=sid, title="Journal cleanup failure")
    session.save()
    captured = _capture_post(monkeypatch, {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
    state_db_delete_calls = []
    monkeypatch.setattr(
        models,
        "delete_cli_session_for_webui_delete",
        lambda value: state_db_delete_calls.append(value) or True,
    )
    published = []
    monkeypatch.setattr(
        routes,
        "_publish_session_list_changed",
        lambda event, **kwargs: published.append((event, kwargs)),
    )

    delete_calls = []

    def fail_once_then_succeed(value):
        delete_calls.append(value)
        if len(delete_calls) == 1:
            raise OSError("run journal locked")
        return True

    monkeypatch.setattr(run_journal, "delete_run_journal", fail_once_then_succeed)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 500
    assert captured["payload"]["ok"] is False
    assert captured["payload"]["state_db_cleanup_failed"] is False
    assert captured["payload"]["run_journal_cleanup_failed"] is True
    assert captured["payload"]["error"] == "Run journal cleanup failed; retry deletion"
    assert (session_dir / f"{sid}.json").exists()
    assert sid in SESSIONS
    assert state_db_delete_calls == []
    assert published == []

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert delete_calls == [sid, sid]
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["state_db_cleanup_failed"] is False
    assert captured["payload"]["run_journal_cleanup_failed"] is False
    assert "error" not in captured["payload"]
    assert not (session_dir / f"{sid}.json").exists()
    assert sid not in SESSIONS
    assert state_db_delete_calls == [sid]
    assert [event for event, _kwargs in published] == ["session_delete"]


@pytest.mark.parametrize("blocked_suffix", [".json", ".json.bak"])
def test_delete_session_reports_durable_artifact_cleanup_failure(
    tmp_path, monkeypatch, blocked_suffix
):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = f"{blocked_suffix.removeprefix('.').replace('.', '')}artifactdelete1"
    session = Session(session_id=sid, title="Artifact cleanup failure")
    session.save()
    backup = session_dir / f"{sid}.json.bak"
    backup.write_text("backup", encoding="utf-8")
    captured = _capture_post(monkeypatch, {"session_id": sid})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)

    state_db_delete_calls = []
    monkeypatch.setattr(
        models,
        "delete_cli_session_for_webui_delete",
        lambda value: state_db_delete_calls.append(value) or True,
    )
    published = []
    monkeypatch.setattr(
        routes,
        "_publish_session_list_changed",
        lambda event, **kwargs: published.append((event, kwargs)),
    )

    real_unlink = Path.unlink
    blocked_once = True

    def fail_artifact_unlink_once(path, *args, **kwargs):
        nonlocal blocked_once
        if blocked_once and path.name == f"{sid}{blocked_suffix}":
            blocked_once = False
            raise PermissionError("session artifact locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_artifact_unlink_once)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 500
    assert captured["payload"]["ok"] is False
    assert captured["payload"]["session_artifact_cleanup_failed"] is True
    assert captured["payload"]["run_journal_cleanup_failed"] is False
    assert (session_dir / f"{sid}{blocked_suffix}").exists()
    assert sid in SESSIONS
    assert state_db_delete_calls == [sid]
    assert published == []

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert not (session_dir / f"{sid}.json").exists()
    assert not backup.exists()
    assert sid not in SESSIONS
    assert state_db_delete_calls == [sid, sid]
    assert [event for event, _kwargs in published] == ["session_delete"]


@pytest.mark.parametrize(
    "cleanup_failure",
    ["attachments", "turn_journal", "state_db"],
    ids=["attachment-cleanup", "turn-journal-shard", "state-db-delete"],
)
def test_delete_non_terminal_cleanup_failure_keeps_retryable_owner(
    tmp_path, monkeypatch, cleanup_failure
):
    """Every post-sidecar cleanup boundary must fail closed and remain retryable."""
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = f"nonterminal-{cleanup_failure}"
    owner = Session(
        session_id=sid,
        title="Retryable cleanup",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "keep me"}],
    )
    models.SESSIONS[sid] = owner
    owner.save()
    sidecar = session_dir / f"{sid}.json"
    backup = session_dir / f"{sid}.json.bak"
    backup.write_text(sidecar.read_text(encoding="utf-8"), encoding="utf-8")
    capability = owner._persistence_generation

    config_module = pytest.importorskip("api.config")
    upload_module = pytest.importorskip("api.upload")
    turn_journal_module = pytest.importorskip("api.turn_journal")
    run_journal_module = pytest.importorskip("api.run_journal")
    background_module = pytest.importorskip("api.background_process")
    terminal_module = pytest.importorskip("api.terminal")
    profiles_module = pytest.importorskip("api.profiles")

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda _sid: False)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_worktree_retained_payload_for_session_id", lambda _sid: {})
    monkeypatch.setattr(routes, "_sync_session_title_to_insights", lambda _s: None)
    monkeypatch.setattr(config_module, "_evict_session_agent", lambda _sid: None)
    monkeypatch.setattr(run_journal_module, "delete_run_journal", lambda _sid: None)
    monkeypatch.setattr(background_module, "forget_bg_task_completion_dedup", lambda _sid: None)
    monkeypatch.setattr(terminal_module, "close_terminal", lambda _sid: None)

    attachment_dir = tmp_path / "attachments" / sid
    attachment_plaintext = attachment_dir / "secret.txt"
    attachment_dir.mkdir(parents=True)
    attachment_plaintext.write_text("attachment secret", encoding="utf-8")
    monkeypatch.setattr(upload_module, "_session_attachment_dir", lambda _sid: attachment_dir)

    turn_journal_dir = session_dir / "_turn_journal"
    turn_shard = turn_journal_dir / f"{sid}~test.jsonl"
    turn_journal_dir.mkdir()
    turn_shard.write_text('{"event":"submitted","content":"turn secret"}\n', encoding="utf-8")

    hermes_home = tmp_path / "hermes-home"
    state_db = hermes_home / "state.db"
    state_artifact = hermes_home / "sessions" / f"{sid}.json"
    if cleanup_failure == "state_db":
        _make_delete_state_db(state_db, sid)
        state_artifact.parent.mkdir(parents=True)
        state_artifact.write_text('{"secret":"agent artifact"}', encoding="utf-8")
        monkeypatch.setattr(profiles_module, "get_active_hermes_home", lambda: hermes_home)
        monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db)

    published = []
    monkeypatch.setattr(
        routes,
        "_publish_session_list_changed",
        lambda event, **kwargs: published.append((event, kwargs)),
    )

    if cleanup_failure == "attachments":
        real_rmtree = routes.shutil.rmtree
        blocked = True

        def fail_attachment_cleanup(path, *args, **kwargs):
            nonlocal blocked
            if blocked and Path(path).resolve() == attachment_dir.resolve():
                blocked = False
                raise OSError("attachment cleanup locked")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(routes.shutil, "rmtree", fail_attachment_cleanup)
        monkeypatch.setattr(models, "delete_cli_session_for_webui_delete", lambda _sid: True)
    elif cleanup_failure == "turn_journal":
        real_delete_turn_journal = turn_journal_module.delete_turn_journal
        blocked = True

        def fail_turn_journal(candidate_sid, *args, **kwargs):
            nonlocal blocked
            if blocked and candidate_sid == sid:
                blocked = False
                raise OSError("turn journal shard locked")
            return real_delete_turn_journal(candidate_sid, *args, **kwargs)

        monkeypatch.setattr(turn_journal_module, "delete_turn_journal", fail_turn_journal)
        monkeypatch.setattr(models, "delete_cli_session_for_webui_delete", lambda _sid: True)
    else:
        real_delete_cli_session = models.delete_cli_session_for_webui_delete
        delete_calls = []

        def fail_state_db_once(candidate_sid):
            delete_calls.append(candidate_sid)
            if len(delete_calls) == 1:
                return False
            return real_delete_cli_session(candidate_sid)

        monkeypatch.setattr(models, "delete_cli_session_for_webui_delete", fail_state_db_once)

    captured = _capture_post(monkeypatch, {"session_id": sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] >= 400
    assert captured["payload"]["ok"] is False
    assert not published, "a failed cleanup must not publish session_delete"
    assert models.SESSIONS[sid] is owner
    assert sidecar.exists()
    assert backup.exists()
    assert sid in {
        row.get("session_id")
        for row in json.loads((session_dir / "_index.json").read_text(encoding="utf-8"))
    }
    assert capability.revoked is False
    assert models._SESSION_PERSISTENCE_GENERATIONS.get(sid) is capability
    owner.title = "retry handle remains writable"
    owner.save()

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert published == [("session_delete", {"profile": None})]
    assert not sidecar.exists()
    assert not backup.exists()
    assert not (session_dir / "_index.json").exists() or sid not in {
        row.get("session_id")
        for row in json.loads((session_dir / "_index.json").read_text(encoding="utf-8"))
    }
    assert not attachment_dir.exists()
    assert not turn_shard.exists()
    assert sid in models._load_webui_deleted_session_tombstone()
    assert sid not in models.SESSIONS
    assert capability.revoked is True
    assert models._SESSION_PERSISTENCE_GENERATIONS.get(sid) is None
    if cleanup_failure == "state_db":
        with sqlite3.connect(str(state_db)) as conn:
            assert conn.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone() is None
        assert not state_artifact.exists()


@pytest.mark.parametrize(
    "late_failure",
    ["session_index", "deleted_tombstone"],
)
def test_delete_late_gate_failure_restores_restartable_retry_owner(
    tmp_path, monkeypatch, late_failure
):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = f"late-delete-{late_failure}"
    owner = Session(
        session_id=sid,
        title="Late delete retry",
        messages=[{"role": "user", "content": "survive restart"}],
    )
    models.SESSIONS[sid] = owner
    owner.save()
    sidecar = session_dir / f"{sid}.json"
    backup = session_dir / f"{sid}.json.bak"
    backup.write_text(sidecar.read_text(encoding="utf-8"), encoding="utf-8")
    capability = owner._persistence_generation

    run_journal_module = pytest.importorskip("api.run_journal")
    turn_journal_module = pytest.importorskip("api.turn_journal")
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda _sid: False)
    monkeypatch.setattr(routes, "_worktree_retained_payload_for_session_id", lambda _sid: {})
    monkeypatch.setattr(routes, "_delete_session_attachments_verified", lambda _sid: None)
    monkeypatch.setattr(run_journal_module, "delete_run_journal", lambda _sid: None)
    monkeypatch.setattr(turn_journal_module, "delete_turn_journal_verified", lambda _sid: None)
    monkeypatch.setattr(models, "delete_cli_session_for_webui_delete", lambda _sid: True)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *_args, **_kwargs: None)

    if late_failure == "session_index":
        real_gate = routes.prune_session_from_index
        failed_once = False

        def fail_gate_once(candidate_sid):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise OSError("session index locked")
            return real_gate(candidate_sid)

        monkeypatch.setattr(routes, "prune_session_from_index", fail_gate_once)
        expected_flag = "session_index_cleanup_failed"
    else:
        real_gate = routes._record_webui_deleted_session_tombstone
        failed_once = False

        def fail_gate_once(candidate_sid):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise OSError("deleted tombstone locked")
            return real_gate(candidate_sid)

        monkeypatch.setattr(routes, "_record_webui_deleted_session_tombstone", fail_gate_once)
        expected_flag = "deleted_session_tombstone_cleanup_failed"

    captured = _capture_post(monkeypatch, {"session_id": sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 500
    assert captured["payload"]["ok"] is False
    assert captured["payload"][expected_flag] is True
    assert captured["payload"]["retry_state_restored"] is True
    assert sidecar.exists(), "a truthful retryable 500 needs durable restart authority"
    assert models.SESSIONS[sid] is owner
    assert capability.revoked is False

    # Model a process restart: only the durable sidecar may recover the owner.
    models.SESSIONS.clear()
    restarted_owner = Session.load(sid)
    assert restarted_owner is not None
    assert restarted_owner.messages[0]["content"] == "survive restart"

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert not sidecar.exists()
    assert not backup.exists()
    assert sid in models._load_webui_deleted_session_tombstone()
    assert sid not in models.SESSIONS


def test_delete_late_gate_restore_failure_does_not_claim_retryable_state(
    tmp_path, monkeypatch
):
    _isolate_session_store(tmp_path, monkeypatch)
    sid = "late-delete-restore-failed"
    owner = Session(
        session_id=sid,
        title="Late delete restore failure",
        messages=[{"role": "user", "content": "manual recovery"}],
    )
    models.SESSIONS[sid] = owner
    owner.save()

    run_journal_module = pytest.importorskip("api.run_journal")
    turn_journal_module = pytest.importorskip("api.turn_journal")
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda _sid: False)
    monkeypatch.setattr(routes, "_worktree_retained_payload_for_session_id", lambda _sid: {})
    monkeypatch.setattr(routes, "_delete_session_attachments_verified", lambda _sid: None)
    monkeypatch.setattr(run_journal_module, "delete_run_journal", lambda _sid: None)
    monkeypatch.setattr(turn_journal_module, "delete_turn_journal_verified", lambda _sid: None)
    monkeypatch.setattr(models, "delete_cli_session_for_webui_delete", lambda _sid: True)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *_args, **_kwargs: None)

    def fail_index_prune(_sid):
        raise OSError("session index locked")

    monkeypatch.setattr(
        routes,
        "prune_session_from_index",
        fail_index_prune,
    )
    monkeypatch.setattr(
        routes,
        "_restore_deleted_session_retry_persistence",
        lambda *_args, **_kwargs: False,
    )

    captured = _capture_post(monkeypatch, {"session_id": sid})
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True

    assert captured["status"] == 500
    assert captured["payload"]["ok"] is False
    assert captured["payload"]["session_index_cleanup_failed"] is True
    assert captured["payload"]["retry_state_restored"] is False
    assert captured["payload"]["error"] == (
        "Delete retry state restoration failed; manual recovery required"
    )
    assert "retry deletion" not in captured["payload"]["error"]


def test_delete_messaging_session_reopens_read_only_without_deleted_webui_tombstone(
    tmp_path, monkeypatch
):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "telegramdelete1"
    state_db = tmp_path / "state.db"
    _make_state_db(state_db, sid)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db)
    session = Session(session_id=sid, title="Telegram chat")
    session.save()
    captured = _capture_post(monkeypatch, {"session_id": sid})
    cli_meta = {
        "session_id": sid,
        "source_tag": "telegram",
        "raw_source": "telegram",
        "session_source": "messaging",
    }
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: cli_meta)
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: True)
    delete_calls = []
    monkeypatch.setattr(
        models,
        "delete_cli_session_for_webui_delete",
        lambda value: delete_calls.append(value),
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True
    sess, reason = routes._claim_or_synthesize_cli_session(sid)

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["state_db_cleanup_failed"] is False
    assert not (session_dir / f"{sid}.json").exists()
    assert sid not in models._load_webui_deleted_session_tombstone()
    assert delete_calls == []
    assert reason == "not_claimable"
    assert sess is not None
    assert sess.read_only is True
    assert sess.session_source == "messaging"


def test_archive_worktree_session_reports_retained_worktree_without_cleanup(tmp_path, monkeypatch):
    _isolate_session_store(tmp_path, monkeypatch)
    session, worktree = _worktree_session(tmp_path, "wtarchive1")
    captured = _capture_post(
        monkeypatch,
        {"session_id": session.session_id, "archived": True},
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/archive")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["session"]["archived"] is True
    assert captured["payload"]["worktree_retained"] is True
    assert captured["payload"]["worktree_path"] == str(worktree.resolve())
    assert worktree.exists(), "session archive must not remove the git worktree directory"
    assert Session.load("wtarchive1").archived is True
