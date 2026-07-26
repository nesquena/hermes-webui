"""Runtime regression coverage for dedicated composer draft sidecars."""

import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest


def _build_draft_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BASE_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_WEBUI_DEFAULT_WORKSPACE", str(tmp_path / "workspace"))

    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return models, routes, session_dir


def _post_json(monkeypatch, routes, path, body):
    captured = {}
    handler = SimpleNamespace(
        command="POST",
        wfile=None,
        send_response=lambda status: None,
        send_header=lambda key, value: None,
        end_headers=lambda: None,
    )

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )

    parsed = SimpleNamespace(path=path)
    assert routes.handle_post(handler, parsed) is True
    return captured["status"], captured["payload"]


def _cleanup_route(monkeypatch, routes, *, zero_only=False):
    captured = {}
    handler = SimpleNamespace(
        wfile=None,
        send_response=lambda status: None,
        send_header=lambda key, value: None,
        end_headers=lambda: None,
    )

    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )

    assert routes._handle_sessions_cleanup(handler, {}, zero_only=zero_only) is True
    return captured["status"], captured["payload"]


def _write_cli_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                title TEXT,
                parent_session_id TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT
            );
            INSERT INTO sessions (id, source, title, parent_session_id)
            VALUES ('sid-model-delete', 'cli', 'model deleted', NULL);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _build_pre_compression_migration_state(tmp_path, monkeypatch):
    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)
    import api.streaming as streaming

    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)

    old_sid = "sid-rotation-old"
    continuation_sid = "sid-rotation-new"
    old_session = models.Session(
        session_id=old_sid,
        title="Compression snapshot",
        messages=[{"role": "user", "content": "question"}],
    )
    old_session.save(touch_updated_at=False, skip_index=True)

    continuation = models.Session(
        session_id=continuation_sid,
        title="Compression snapshot",
        parent_session_id=old_sid,
        messages=[
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "compressed reply"},
        ],
    )
    continuation.save(touch_updated_at=False, skip_index=True)
    streaming._preserve_pre_compression_snapshot(continuation, old_sid)
    continuation.save(touch_updated_at=False, skip_index=True)

    with routes.LOCK:
        models.SESSIONS.clear()
        models.SESSIONS[continuation_sid] = continuation

    old_lock = routes._get_session_agent_lock(old_sid)
    with routes.SESSION_AGENT_LOCKS_LOCK:
        routes.SESSION_AGENT_LOCKS[continuation_sid] = old_lock
        routes.SESSION_AGENT_LOCKS.pop(old_sid, None)

    return models, routes, session_dir, old_sid, continuation_sid


def test_dedicated_draft_overlays_full_and_metadata_loads(tmp_path, monkeypatch):
    models, _, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-overlay"
    models.Session(
        session_id=sid,
        title="Session",
        composer_draft={"text": "legacy", "files": []},
    ).save(touch_updated_at=False, skip_index=True)
    models._save_session_draft(sid, {"text": "dedicated", "files": []})

    loaded = models.Session.load(sid)
    loaded_meta = models.Session.load_metadata_only(sid)
    assert loaded.composer_draft == {"text": "dedicated", "files": []}
    assert loaded_meta.composer_draft == {"text": "dedicated", "files": []}

    models._delete_session_draft(sid)
    fallback = models.Session.load(sid)
    fallback_meta = models.Session.load_metadata_only(sid)
    assert fallback.composer_draft == {"text": "legacy", "files": []}
    assert fallback_meta.composer_draft == {"text": "legacy", "files": []}
    assert (session_dir / ".drafts" / f"{sid}.json").exists() is False


def test_corrupted_draft_file_falls_back_to_legacy_with_warning(tmp_path, monkeypatch, caplog):
    models, _, _ = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-corrupt"
    models.Session(
        session_id=sid,
        title="Session",
        composer_draft={"text": "from-sidecar"},
    ).save(touch_updated_at=False, skip_index=True)

    draft_dir = models._session_draft_dir()
    draft_dir.mkdir(exist_ok=True)
    (draft_dir / f"{sid}.json").write_text("{malformed", encoding="utf-8")

    with caplog.at_level("WARNING"):
        loaded = models.Session.load(sid)

    assert loaded.composer_draft == {"text": "from-sidecar"}
    assert any("Ignoring malformed draft sidecar" in record.getMessage() for record in caplog.records)


def test_drafts_directory_is_ignored_by_session_file_scanners(tmp_path, monkeypatch):
    models, _, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-scan"
    models.Session(session_id=sid, title="Session").save(touch_updated_at=False, skip_index=True)
    models._save_session_draft(sid, {"text": "scan draft", "files": []})

    top_level = {p.name for p in session_dir.glob("*.json")}
    session_ids = models._persisted_session_ids_snapshot()

    assert sid in session_ids
    assert f"{sid}.json" in top_level
    assert all(p.parent == session_dir for p in session_dir.glob("*.json"))


def test_draft_post_uses_dedicated_sidecar_without_main_sidecar_rewrite(tmp_path, monkeypatch):
    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-large"
    models.Session(
        session_id=sid,
        title="Large",
        messages=[{"role": "assistant", "content": "x" * 300_000}],
    ).save(skip_index=True)
    main_sidecar = session_dir / f"{sid}.json"
    before_payload = main_sidecar.read_bytes()
    before_mtime_ns = main_sidecar.stat().st_mtime_ns

    save_calls = []
    real_save = models.Session.save

    def save_spy(*args, **kwargs):
        save_calls.append((args, kwargs))
        return real_save(*args, **kwargs)

    monkeypatch.setattr(models.Session, "save", save_spy)

    status, payload = _post_json(
        monkeypatch,
        routes,
        "/api/session/draft",
        {"session_id": sid, "text": "drafted payload", "files": []},
    )

    assert status == 200
    assert payload["draft"] == {"text": "drafted payload", "files": []}
    assert not save_calls
    assert (session_dir / ".drafts" / f"{sid}.json").exists()
    assert main_sidecar.stat().st_mtime_ns == before_mtime_ns
    assert main_sidecar.read_bytes() == before_payload


def test_draft_explicit_empty_stays_authoritative_after_reload(tmp_path, monkeypatch):
    models, routes, _ = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-empty"
    models.Session(session_id=sid, title="Session").save(
        touch_updated_at=False,
        skip_index=True,
    )

    status, payload = _post_json(
        monkeypatch,
        routes,
        "/api/session/draft",
        {"session_id": sid, "text": "", "files": []},
    )
    assert status == 200
    assert payload["draft"] == {"text": "", "files": []}

    loaded = models.Session.load(sid)
    loaded.title = "touch"
    loaded.save(touch_updated_at=False, skip_index=True)
    assert models.Session.load(sid).composer_draft == {"text": "", "files": []}


def test_session_delete_removes_dedicated_draft_file(tmp_path, monkeypatch):
    import api.routes as routes
    models, _, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-delete"
    models.Session(session_id=sid, title="Session").save(
        touch_updated_at=False,
        skip_index=True,
    )
    routes._save_session_draft(sid, {"text": "in-memory draft", "files": []})
    assert (session_dir / ".drafts" / f"{sid}.json").exists()

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda value: False)

    _, payload = _post_json(
        monkeypatch,
        routes,
        "/api/session/delete",
        {"session_id": sid},
    )

    assert payload["ok"] is True
    assert not (session_dir / f"{sid}.json").exists()
    assert not (session_dir / ".drafts" / f"{sid}.json").exists()


def test_route_session_delete_is_final_after_inflight_autosave(tmp_path, monkeypatch):
    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)
    sid = "sid-route-race"
    models.Session(
        session_id=sid,
        title="Route Race",
        messages=[{"role": "assistant", "content": "x"}],
    ).save(
        touch_updated_at=False,
        skip_index=True,
    )

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda value: False)

    real_delete_session_draft = routes._delete_session_draft

    def delete_session_draft_and_race(sid_to_delete):
        deleted = real_delete_session_draft(sid_to_delete)
        models._save_session_draft(sid_to_delete, {"text": "inflight", "files": []})
        return deleted

    monkeypatch.setattr(routes, "_delete_session_draft", delete_session_draft_and_race)

    delete_status = {}

    delete_status["status"], delete_status["payload"] = _post_json(
        monkeypatch,
        routes,
        "/api/session/delete",
        {"session_id": sid},
    )

    assert delete_status["status"] == 200
    assert delete_status["payload"]["ok"] is True
    assert not (session_dir / f"{sid}.json").exists()
    assert not (session_dir / ".drafts" / f"{sid}.json").exists()


@pytest.mark.parametrize(
    ("text", "files"),
    [
        ("non-empty", []),
        ("   ", []),
        ("", [{"name": "notes.txt"}]),
    ],
)
def test_draft_post_for_diskless_session_materializes_main_session_and_sidecar(
    tmp_path, monkeypatch, text, files
):
    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = models.new_session().session_id
    assert not (session_dir / f"{sid}.json").exists()

    status, payload = _post_json(
        monkeypatch,
        routes,
        "/api/session/draft",
        {"session_id": sid, "text": text, "files": files},
    )

    assert status == 200
    assert payload["draft"] == {"text": text, "files": files}
    assert (session_dir / f"{sid}.json").exists()
    assert (session_dir / ".drafts" / f"{sid}.json").exists()
    raw_session = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert raw_session.get("composer_draft") == {}
    assert raw_session.get("message_count") == 0

    models.SESSIONS.clear()
    loaded = models.Session.load(sid)
    assert loaded.composer_draft == {"text": text, "files": files}


def test_empty_draft_on_diskless_new_chat_does_not_create_draft_file(tmp_path, monkeypatch):
    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = models.new_session().session_id
    assert not (session_dir / f"{sid}.json").exists()

    status, payload = _post_json(
        monkeypatch,
        routes,
        "/api/session/draft",
        {"session_id": sid, "text": "", "files": []},
    )

    assert status == 200
    assert payload["draft"] == {"text": "", "files": []}
    assert not (session_dir / f"{sid}.json").exists()
    assert not (session_dir / ".drafts" / f"{sid}.json").exists()


def test_draft_post_for_stale_request_sid_does_not_write_under_old_sid(tmp_path, monkeypatch):
    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = models.new_session().session_id
    rotated_sid = f"{sid}-rotated"
    s = models.get_session(sid)
    s.composer_draft = {"text": "original", "files": []}

    class _RotateLock:
        def __enter__(self):
            s.session_id = rotated_sid
            with routes.LOCK:
                routes.SESSIONS[sid] = s
                routes.SESSIONS[rotated_sid] = s
                routes.SESSIONS.move_to_end(rotated_sid)
            assert s.session_id == rotated_sid
            assert routes.SESSIONS.get(rotated_sid) is s
            assert routes.SESSIONS.get(sid) is s

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: _RotateLock())

    status, payload = _post_json(
        monkeypatch,
        routes,
        "/api/session/draft",
        {"session_id": sid, "text": "rotating", "files": []},
    )

    assert status == 200
    assert payload["session_id"] == rotated_sid
    assert payload["draft"] == {"text": "rotating", "files": []}
    assert s.composer_draft == {"text": "rotating", "files": []}
    assert not (session_dir / f"{sid}.json").exists()
    assert (session_dir / f"{rotated_sid}.json").exists()
    assert not (session_dir / ".drafts" / f"{sid}.json").exists()
    assert (session_dir / ".drafts" / f"{rotated_sid}.json").exists()


def test_draft_post_for_stale_pre_compression_snapshot_writes_to_continuation(tmp_path, monkeypatch):
    models, routes, session_dir, old_sid, continuation_sid = _build_pre_compression_migration_state(
        tmp_path, monkeypatch
    )

    try:
        status, payload = _post_json(
            monkeypatch,
            routes,
            "/api/session/draft",
            {"session_id": old_sid, "text": "recover from archived snapshot", "files": []},
        )

        assert status == 200
        assert payload["session_id"] == continuation_sid
        assert payload["draft"] == {"text": "recover from archived snapshot", "files": []}

        assert not (session_dir / ".drafts" / f"{old_sid}.json").exists()
        assert (session_dir / ".drafts" / f"{continuation_sid}.json").exists()

        models.SESSIONS.clear()
        loaded = models.Session.load(continuation_sid)
        assert loaded.composer_draft == {"text": "recover from archived snapshot", "files": []}
        assert (session_dir / f"{old_sid}.json").exists()
    finally:
        with routes.SESSION_AGENT_LOCKS_LOCK:
            routes.SESSION_AGENT_LOCKS.pop(old_sid, None)
            routes.SESSION_AGENT_LOCKS.pop(continuation_sid, None)


def test_draft_post_for_stale_pre_compression_snapshot_profile_mismatch_rejected(
    tmp_path, monkeypatch
):
    models, routes, session_dir, old_sid, continuation_sid = _build_pre_compression_migration_state(
        tmp_path,
        monkeypatch,
    )

    continuation = models.Session.load(continuation_sid)
    assert continuation is not None
    continuation.save(touch_updated_at=False, skip_index=False)

    continuation_path = session_dir / f"{continuation_sid}.json"
    continuation_payload = json.loads(continuation_path.read_text(encoding="utf-8"))
    continuation_payload["profile"] = "mismatch-profile"
    continuation_path.write_text(json.dumps(continuation_payload, ensure_ascii=False), encoding="utf-8")

    with routes.LOCK:
        routes.SESSIONS.pop(continuation_sid, None)

    try:
        status, payload = _post_json(
            monkeypatch,
            routes,
            "/api/session/draft",
            {"session_id": old_sid, "text": "must fail by profile", "files": []},
        )

        assert status == 409
        assert payload["ok"] is False
        assert payload["error"] == "Session moved"
        assert payload["session_id"] == continuation_sid
        assert not (session_dir / ".drafts" / f"{old_sid}.json").exists()
        assert not (session_dir / ".drafts" / f"{continuation_sid}.json").exists()
    finally:
        with routes.SESSION_AGENT_LOCKS_LOCK:
            routes.SESSION_AGENT_LOCKS.pop(old_sid, None)
            routes.SESSION_AGENT_LOCKS.pop(continuation_sid, None)


def test_draft_post_waits_for_rotation_while_lock_waits_for_authority(tmp_path, monkeypatch):
    models, routes, session_dir, old_sid, continuation_sid = _build_pre_compression_migration_state(
        tmp_path,
        monkeypatch,
    )
    lock = routes._get_session_agent_lock(continuation_sid)
    lock.acquire()
    lock_waiting = threading.Event()

    result = {}
    started = threading.Event()
    done = threading.Event()

    original_get_session_agent_lock = routes._get_session_agent_lock

    class _TrackingLock:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __enter__(self):
            lock_waiting.set()
            self._wrapped.acquire()
            return self

        def __exit__(self, exc_type, exc, tb):
            self._wrapped.release()
            return None

    def _get_session_agent_lock(sid):
        if sid in (old_sid, continuation_sid):
            return _TrackingLock(lock)
        return original_get_session_agent_lock(sid)

    monkeypatch.setattr(routes, "_get_session_agent_lock", _get_session_agent_lock)

    def _call():
        started.set()
        status, payload = _post_json(
            monkeypatch,
            routes,
            "/api/session/draft",
            {"session_id": old_sid, "text": "rotating while waiting", "files": []},
        )
        result["status"] = status
        result["payload"] = payload
        done.set()

    thread = threading.Thread(target=_call)
    thread.start()
    assert started.wait(timeout=2), "draft request did not start"
    try:
        assert lock_waiting.wait(timeout=2), "draft request did not block on authority lock"
        assert not done.is_set()
        with routes.SESSION_AGENT_LOCKS_LOCK:
            routes.SESSION_AGENT_LOCKS[continuation_sid] = lock
            routes.SESSION_AGENT_LOCKS.pop(old_sid, None)
        lock.release()

        assert done.wait(timeout=5), "draft request did not complete after rotation lock handoff"
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        if lock.locked():
            lock.release()
        thread.join(timeout=1)
        with routes.SESSION_AGENT_LOCKS_LOCK:
            routes.SESSION_AGENT_LOCKS.pop(old_sid, None)
            routes.SESSION_AGENT_LOCKS.pop(continuation_sid, None)

    assert result["status"] == 200
    assert result["payload"]["session_id"] == continuation_sid
    models.SESSIONS.clear()
    loaded = models.Session.load(continuation_sid)
    assert loaded.composer_draft == {"text": "rotating while waiting", "files": []}
    assert not (session_dir / ".drafts" / f"{old_sid}.json").exists()


@pytest.mark.parametrize(("cached", "messageful"), [(False, False), (True, True)])
def test_draft_post_for_missing_owner_rejected(tmp_path, monkeypatch, cached, messageful):
    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-messageful-missing"
    messages = [{"role": "assistant", "content": "persisted"}] if messageful else []
    s = models.Session(
        session_id=sid,
        title="Route Race",
        messages=messages,
        composer_draft={"text": "original", "files": []},
    )
    s.save(touch_updated_at=False, skip_index=True)
    assert (session_dir / f"{sid}.json").exists()
    s = models.get_session(sid)
    s.composer_draft = {"text": "original", "files": []}
    (session_dir / f"{sid}.json").unlink(missing_ok=True)

    if not cached:
        models._record_webui_deleted_session_tombstone(sid)

    class _DeleteLock:
        def __enter__(self):
            if not cached:
                with routes.LOCK:
                    routes.SESSIONS.pop(sid, None)

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: _DeleteLock())

    status, payload = _post_json(
        monkeypatch,
        routes,
        "/api/session/draft",
        {"session_id": sid, "text": "autosave", "files": []},
    )

    assert status == 409
    assert payload["ok"] is False
    assert payload["error"] == "Session no longer active"
    assert payload["session_id"] == sid
    assert s.composer_draft == {"text": "original", "files": []}
    assert not (session_dir / f"{sid}.json").exists()
    assert not (session_dir / ".drafts" / f"{sid}.json").exists()
    if not cached:
        assert sid in models._load_webui_deleted_session_tombstone()


def test_resolve_session_does_not_cache_if_owner_vanishes_before_recache(
    tmp_path, monkeypatch
):
    models, _, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-cold-load-race"
    models.Session(
        session_id=sid,
        title="Load Race",
        messages=[{"role": "assistant", "content": "persisted"}],
    ).save(touch_updated_at=False, skip_index=True)
    real_load = models.Session.load

    def load_and_delete(loaded_sid):
        loaded = real_load(loaded_sid)
        (session_dir / f"{loaded_sid}.json").unlink(missing_ok=True)
        models.SESSIONS.clear()
        return loaded

    monkeypatch.setattr(models.Session, "load", load_and_delete)

    with pytest.raises(KeyError):
        models.get_session(sid)
    assert sid not in models.SESSIONS
    assert not (session_dir / f"{sid}.json").exists()


def test_sessions_cleanup_phase1_removes_dedicated_draft_file(tmp_path, monkeypatch):
    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-orphan"
    models.Session(session_id=sid, title="Untitled").save(
        touch_updated_at=False,
        skip_index=True,
    )
    routes._save_session_draft(sid, {"text": "draft", "files": []})

    status, result = _cleanup_route(monkeypatch, routes)

    assert status == 200
    assert result["cleaned"] == 1
    assert not (session_dir / f"{sid}.json").exists()
    assert not (session_dir / ".drafts" / f"{sid}.json").exists()


def test_sessions_cleanup_reports_draft_delete_failure(tmp_path, monkeypatch):
    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-cleanup-fail"
    models.Session(session_id=sid, title="Untitled").save(
        touch_updated_at=False,
        skip_index=True,
    )
    routes._save_session_draft(sid, {"text": "from-route", "files": []})

    monkeypatch.setattr(routes, "_delete_session_draft", lambda _sid: False)

    status, result = _cleanup_route(monkeypatch, routes)

    assert status == 500
    assert result["cleaned"] == 1
    assert result["ok"] is False
    assert result["error"] == "Failed to delete one or more session drafts"
    assert result["draft_delete_failed"] is True
    assert not (session_dir / f"{sid}.json").exists()
    assert (session_dir / ".drafts" / f"{sid}.json").exists()


def test_session_delete_reports_draft_delete_failure(tmp_path, monkeypatch):
    import api.routes as routes

    models, _, session_dir = _build_draft_env(tmp_path, monkeypatch)
    sid = "sid-delete-fail"
    models.Session(session_id=sid, title="Delete Fail").save(
        touch_updated_at=False,
        skip_index=True,
    )
    routes._save_session_draft(sid, {"text": "delete fail", "files": []})

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda value: False)
    monkeypatch.setattr(routes, "_delete_session_draft", lambda sid_value: False)

    status, payload = _post_json(
        monkeypatch,
        routes,
        "/api/session/delete",
        {"session_id": sid},
    )

    assert status == 500
    assert payload["ok"] is False
    assert payload["error"] == "Failed to delete session draft"
    assert payload["draft_delete_failed"] is True
    assert not (session_dir / f"{sid}.json").exists()
    assert (session_dir / ".drafts" / f"{sid}.json").exists()


def test_model_cleanup_removes_draft_artifact(tmp_path, monkeypatch):
    import api.profiles as profiles

    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)
    sid = "sid-model-delete"

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(tmp_path))

    _write_cli_db(tmp_path / "state.db")

    (session_dir / f"{sid}.json").write_text('{"session_id": "sid-model-delete"}', encoding="utf-8")
    draft_path = session_dir / ".drafts" / f"{sid}.json"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text('{"text": "x", "files": []}', encoding="utf-8")

    assert models.delete_cli_session(sid) is True
    assert not (session_dir / f"{sid}.json").exists()
    assert not draft_path.exists()


def test_autosave_and_delete_lock_step_does_not_recreate_draft(tmp_path, monkeypatch):
    models, _, session_dir = _build_draft_env(tmp_path, monkeypatch)
    sid = "sid-race"
    models.Session(session_id=sid, title="Race", messages=[{"role": "assistant", "content": "x"}]).save(
        touch_updated_at=False,
        skip_index=True,
    )

    saved_to_replace = threading.Event()
    allow_replace = threading.Event()
    original_safe_replace = models._safe_replace
    replace_error = {}

    def slow_safe_replace(src, dst):
        saved_to_replace.set()
        allow_replace.wait()
        try:
            return original_safe_replace(src, dst)
        except Exception as exc:
            replace_error["error"] = exc
            raise

    monkeypatch.setattr(models, "_safe_replace", slow_safe_replace)

    def autosave_worker(errors):
        try:
            models._save_session_draft(sid, {"text": "inflight", "files": []})
        except Exception as exc:
            errors["autosave"] = str(exc)

    def delete_worker(errors):
        try:
            (session_dir / f"{sid}.json").unlink(missing_ok=True)
            models._delete_session_draft(sid)
        except Exception as exc:
            errors["delete"] = str(exc)

    autosave_errors: dict[str, str] = {}
    delete_errors: dict[str, str] = {}
    autosave_thread = threading.Thread(target=autosave_worker, args=(autosave_errors,))
    delete_thread = threading.Thread(target=delete_worker, args=(delete_errors,))

    autosave_thread.start()
    assert saved_to_replace.wait(timeout=2), "autosave did not reach replace wait point"
    delete_thread.start()

    allow_replace.set()
    autosave_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    assert not autosave_thread.is_alive()
    assert not delete_thread.is_alive()
    assert not autosave_errors
    assert not delete_errors
    assert not replace_error
    assert not (session_dir / f"{sid}.json").exists()
    assert not (session_dir / ".drafts" / f"{sid}.json").exists()


def test_save_after_missing_main_session_does_not_create_dedicated_draft(tmp_path, monkeypatch):
    models, _, session_dir = _build_draft_env(tmp_path, monkeypatch)
    models._save_session_draft("sid-missing-main", {"text": "discarded", "files": []})
    assert not (session_dir / "sid-missing-main.json").exists()
    assert not (session_dir / ".drafts" / "sid-missing-main.json").exists()


def test_draft_path_error_shows_original_input(tmp_path, monkeypatch):
    models, _, _ = _build_draft_env(tmp_path, monkeypatch)

    with pytest.raises(ValueError) as exc:
        models._session_draft_path("../unsafe")
    assert "Unsafe session_id '../unsafe'" in str(exc.value)
