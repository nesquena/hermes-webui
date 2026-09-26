"""A successful session delete is a terminal persistence boundary for old workers."""

import json
import sqlite3
import threading
import urllib.request
import urllib.error
import pytest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer

from api import config, models, profiles, routes, run_journal, streaming, upload
from tests.test_steer_worker_boundaries import worker_scene as worker_scene


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    import socket

    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in (
            "127.0.0.1",
            "::1",
            "localhost",
        ):
            raise RuntimeError("External connections are disabled in deletion tests")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.mark.parametrize("evict_cached_copy", [False, True])
@pytest.mark.parametrize("worker_outcome", ["success", "error"])
def test_http_delete_revokes_late_worker_persistence(
    worker_scene, monkeypatch, tmp_path, worker_outcome, evict_cached_copy
):
    import server

    scene = worker_scene
    # The external database API remains synthetic; its cleanup operation exists
    # so a real session GET failure cannot be hidden behind a broken fake close.
    import sys
    from types import SimpleNamespace

    monkeypatch.setattr(
        sys.modules["hermes_state"],
        "SessionDB",
        lambda *a, **kw: SimpleNamespace(close=lambda: None),
    )
    sid = scene.session.session_id
    session_dir = models.SESSION_DIR
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    with sqlite3.connect(home / "state.db") as db:
        db.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT, model_config TEXT, title TEXT, model TEXT, started_at REAL, ended_at REAL, end_reason TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, message_count INTEGER DEFAULT 1); CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT);"
        )
        db.execute(
            "INSERT INTO sessions (id,parent_session_id,source,model_config,started_at,title,model) VALUES (?,NULL,'webui','{}',1,'Deletion probe','test-model')",
            (sid,),
        )
        db.execute(
            "INSERT INTO messages VALUES (1,?,'user','isolated deletion probe')", (sid,)
        )
    monkeypatch.setattr(config, "SESSION_WRITEBACK_OWNERS", {})
    config.register_session_writeback_owner(sid, "run")
    for name in (
        "SESSIONS",
        "STREAMS",
        "CANCEL_FLAGS",
        "AGENT_INSTANCES",
        "STREAM_SESSION_OWNERS",
        "ACTIVE_RUNS",
        "SESSION_AGENT_LOCKS",
    ):
        if hasattr(routes, name):
            monkeypatch.setattr(routes, name, getattr(config, name))
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    # Undo the worker fixture's permissive get_session lambda: use real lifetime checks.
    monkeypatch.setattr(streaming, "get_session", models.get_session)
    monkeypatch.setattr(
        upload, "_session_attachment_dir", lambda item: tmp_path / "attachments" / item
    )

    entered, release = threading.Event(), threading.Event()
    base_agent = streaming._get_ai_agent()

    class PausedAgent(base_agent):
        def __init__(self, stream_delta_callback=None, **kwargs):
            super().__init__(**kwargs)
            self.emit_token = stream_delta_callback

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: PausedAgent)

    def pause_provider():
        assert callable(scene.agent.emit_token)
        scene.agent.emit_token("before deletion")
        entered.set()
        assert release.wait(15), "test must always release the synthetic provider"
        scene.agent.emit_token("LATE_AFTER_DELETE_MARKER")
        if worker_outcome == "error":
            raise RuntimeError("Synthetic late provider failure")

    scene.on_run = pause_provider
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    serving = threading.Thread(target=httpd.serve_forever, daemon=True)
    serving.start()
    base = "http://127.0.0.1:%s" % httpd.server_port
    report = {"worker_outcome": worker_outcome}
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(scene.run)
            try:
                assert entered.wait(10), (
                    "real worker never reached the provider barrier"
                )
                assert (session_dir / "_run_journal" / sid / "run.jsonl").exists()
                if evict_cached_copy:
                    replacement = models.Session.load(sid)
                    assert replacement is not scene.session
                    models.SESSIONS[sid] = replacement
                req = urllib.request.Request(
                    base + "/api/session/delete",
                    data=json.dumps({"session_id": sid}).encode(),
                    headers={"Content-Type": "application/json", "Origin": base},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    report["http_status"] = response.status
                    report["delete_response"] = json.load(response)
                report["immediately_after_delete"] = {
                    "sidecar_exists": (session_dir / f"{sid}.json").exists(),
                    "journal_exists": (session_dir / "_run_journal" / sid).exists(),
                    "in_session_cache": sid in models.SESSIONS,
                    "runtime_still_active": "run" in config.ACTIVE_RUNS,
                    "cancelled": config.CANCEL_FLAGS.get("run").is_set()
                    if config.CANCEL_FLAGS.get("run")
                    else False,
                }
                with sqlite3.connect(home / "state.db") as db:
                    report["state_db_rows_after_delete"] = db.execute(
                        "SELECT count(*) FROM sessions WHERE id=?", (sid,)
                    ).fetchone()[0]
                assert report["http_status"] == 200
                assert report["delete_response"]["ok"] is True
                assert report["delete_response"]["state_db_cleanup_failed"] is False
                assert report["immediately_after_delete"]["sidecar_exists"] is False
                assert report["immediately_after_delete"]["journal_exists"] is False
            finally:
                release.set()
            future.result(timeout=15)
        path = session_dir / "_run_journal" / sid / "run.jsonl"
        report["after_late_worker"] = {
            "journal_exists": path.exists(),
            "late_marker_on_disk": "LATE_AFTER_DELETE_MARKER" in path.read_text()
            if path.exists()
            else False,
            "sidecar_exists": (session_dir / f"{sid}.json").exists(),
            "in_session_cache": sid in models.SESSIONS,
            "tombstoned": sid in models._load_webui_deleted_session_tombstone(),
        }
        try:
            with urllib.request.urlopen(
                base + "/api/session?session_id=" + sid + "&resolve_model=0", timeout=10
            ) as response:
                restored = json.load(response)
                session_payload = restored.get("session", restored)
                report["http_readback"] = {
                    "status": response.status,
                    "session_id": session_payload.get("session_id"),
                    "message_count": len(
                        session_payload.get("messages", restored.get("messages", []))
                    ),
                }
        except urllib.error.HTTPError as error:
            report["http_readback"] = {"status": error.code}
            error.close()
        # The exact Session object held by the deleted worker stays revoked even
        # after its worker teardown drops the transient stream-revocation marker.
        with pytest.raises(RuntimeError, match="persistence authority was revoked"):
            scene.session.save()

        # A genuinely new session is a separate control, not a reused stale writer.
        fresh = models.Session(
            session_id="new-user-conversation",
            title="Fresh",
            workspace=str(tmp_path),
            messages=[{"role": "user", "content": "new work"}],
        )
        fresh.save()
        fresh_writer = run_journal.RunJournalWriter(
            fresh.session_id, "new-run", session_dir=session_dir
        )
        fresh_writer.append_sse_event("token", {"text": "fresh work"})
        report["new_session_works"] = bool(
            run_journal.read_run_events(
                fresh.session_id, "new-run", session_dir=session_dir
            )["events"]
        )

        assert report["after_late_worker"] == {
            "journal_exists": False,
            "late_marker_on_disk": False,
            "sidecar_exists": False,
            "in_session_cache": False,
            "tombstoned": True,
        }
        assert report["http_readback"] == {"status": 404}
        assert report["new_session_works"] is True
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        serving.join(timeout=5)


def test_revoked_writer_stays_revoked_after_worker_cleanup(monkeypatch, tmp_path):
    from api.session_persistence import (
        session_persistence_gate,
        revoke_session_persistence,
    )

    monkeypatch.setattr(config, "SESSION_WRITEBACK_OWNERS", {})
    sid, rid = "lifetime-deleted", "lifetime-old-run"
    writer = run_journal.RunJournalWriter(sid, rid, session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "before"})
    config.register_session_writeback_owner(sid, rid)
    gate = session_persistence_gate(sid, tmp_path)
    with gate.lock:
        revoke_session_persistence(gate)
    run_journal.delete_run_journal(sid, session_dir=tmp_path)
    config.clear_session_writeback_owner_if_owned(sid, rid)
    assert writer.append_sse_event("token", {"text": "late callback"}) is None
    assert not (tmp_path / "_run_journal" / sid).exists()


def test_revocation_does_not_block_another_session_same_run_id(monkeypatch, tmp_path):
    from api.session_persistence import (
        session_persistence_gate,
        revoke_session_persistence,
    )

    monkeypatch.setattr(config, "SESSION_WRITEBACK_OWNERS", {})
    sid, rid = "lifetime-owner-a", "lifetime-shared-run"
    config.register_session_writeback_owner(sid, rid)
    try:
        gate = session_persistence_gate(sid, tmp_path)
        with gate.lock:
            revoke_session_persistence(gate)
        other = run_journal.RunJournalWriter(
            "lifetime-owner-b", rid, session_dir=tmp_path
        )
        event = other.append_sse_event("token", {"text": "unrelated"})
        assert event is not None, (
            "revocation must use full session identity, not run id alone"
        )
    finally:
        config.clear_session_writeback_owner_if_owned(sid, rid)


@pytest.fixture
def local_store(monkeypatch, tmp_path):
    from collections import OrderedDict
    from api import turn_journal
    from tests.test_file_manager_external_session import _DeleteJSONHandler
    from types import SimpleNamespace

    root = tmp_path / "local-store"
    root.mkdir()
    for module in (models, config, routes, streaming):
        monkeypatch.setattr(module, "SESSION_DIR", root)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", root / "_index.json")
    sessions = OrderedDict()
    for module in (models, config, routes, streaming):
        monkeypatch.setattr(module, "SESSIONS", sessions)
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", OrderedDict())
    monkeypatch.setattr(routes, "_check_csrf", lambda _: True)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _: {})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _: False)
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda _: False)
    monkeypatch.setattr(
        routes, "_worktree_retained_payload_for_session_id", lambda _: {}
    )
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(models, "delete_cli_session", lambda _: True)
    monkeypatch.setattr(models, "get_last_workspace", lambda **kw: str(tmp_path))
    monkeypatch.setattr(
        upload, "_session_attachment_dir", lambda sid: tmp_path / "attachments" / sid
    )

    def new(sid):
        session = models.Session(
            session_id=sid,
            title="Lifetime",
            workspace=str(tmp_path),
            messages=[{"role": "user", "content": "keep until delete"}],
        )
        session.save()
        sessions[sid] = session
        return session

    def delete(sid):
        handler = _DeleteJSONHandler({"session_id": sid})
        routes.handle_post(handler, SimpleNamespace(path="/api/session/delete"))
        return handler.status, json.loads(handler.wfile.getvalue())

    return SimpleNamespace(root=root, new=new, delete=delete, turns=turn_journal)


def test_delete_waits_for_admitted_save_and_removes_it(local_store, monkeypatch):
    session = local_store.new("save-before-delete")
    entered, release, deleting = threading.Event(), threading.Event(), threading.Event()
    original = models._safe_replace

    def paused_replace(source, target):
        if target == session.path:
            entered.set()
            assert release.wait(5)
        original(source, target)

    monkeypatch.setattr(models, "_safe_replace", paused_replace)

    def delete():
        deleting.set()
        return local_store.delete(session.session_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = pool.submit(session.save, skip_index=True)
        try:
            assert entered.wait(5)
            deleted = pool.submit(delete)
            assert deleting.wait(5)
            with pytest.raises(TimeoutError):
                deleted.result(timeout=0.05)
        finally:
            release.set()
        saved.result(timeout=5)
        assert deleted.result(timeout=5)[0] == 200
    assert not session.path.exists()
    assert not session.path.with_suffix(".json.bak").exists()
    assert all(
        row["session_id"] != session.session_id
        for row in json.loads(models.SESSION_INDEX_FILE.read_text())
    )
    with pytest.raises(RuntimeError, match="persistence authority was revoked"):
        session.save()


def test_delete_wins_while_old_save_serializes_payload(local_store, monkeypatch):
    session = local_store.new("delete-before-commit")
    entered, release = threading.Event(), threading.Event()
    original = models.json.dumps

    def paused_json(obj, *args, **kwargs):
        if (
            isinstance(obj, dict)
            and obj.get("session_id") == session.session_id
            and "messages" in obj
        ):
            entered.set()
            assert release.wait(5)
        return original(obj, *args, **kwargs)

    monkeypatch.setattr(models.json, "dumps", paused_json)
    with ThreadPoolExecutor(max_workers=1) as pool:
        saved = pool.submit(session.save)
        try:
            assert entered.wait(5)
            assert local_store.delete(session.session_id)[0] == 200
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="persistence authority was revoked"):
            saved.result(timeout=5)
    assert not session.path.exists()


def test_delete_between_sidecar_commit_and_index_update(local_store, monkeypatch):
    session = local_store.new("delete-before-index")
    entered, release = threading.Event(), threading.Event()
    original = models._write_session_index

    def paused_index(updates=None, **kwargs):
        if updates and any(row is session for row in updates):
            entered.set()
            assert release.wait(5)
        return original(updates, **kwargs)

    monkeypatch.setattr(models, "_write_session_index", paused_index)
    with ThreadPoolExecutor(max_workers=1) as pool:
        saved = pool.submit(session.save)
        try:
            assert entered.wait(5)
            assert local_store.delete(session.session_id)[0] == 200
        finally:
            release.set()
        saved.result(timeout=5)
    assert not session.path.exists()
    assert all(
        row["session_id"] != session.session_id
        for row in json.loads(models.SESSION_INDEX_FILE.read_text())
    )
    assert session.session_id in models._load_webui_deleted_session_tombstone()


def test_delete_waits_for_admitted_journal_append(local_store, monkeypatch):
    session = local_store.new("append-before-delete")
    writer = run_journal.RunJournalWriter(
        session.session_id, "run", session_dir=local_store.root
    )
    entered, release, deleting = threading.Event(), threading.Event(), threading.Event()
    original = run_journal._reserve_next_seq

    def paused_seq(path):
        entered.set()
        assert release.wait(5)
        return original(path)

    monkeypatch.setattr(run_journal, "_reserve_next_seq", paused_seq)

    def delete():
        deleting.set()
        return local_store.delete(session.session_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        appended = pool.submit(writer.append_sse_event, "token", {"text": "before"})
        try:
            assert entered.wait(5)
            deleted = pool.submit(delete)
            assert deleting.wait(5)
            with pytest.raises(TimeoutError):
                deleted.result(timeout=0.05)
        finally:
            release.set()
        assert appended.result(timeout=5)
        assert deleted.result(timeout=5)[0] == 200
    assert writer.append_sse_event("stream_end", {}) is None
    assert not (local_store.root / "_run_journal" / session.session_id).exists()


def test_delete_does_not_recreate_turn_journal(local_store):
    session = local_store.new("turn-delete")
    local_store.turns.append_turn_journal_event(
        session.session_id, {"event": "submitted", "stream_id": "run"}
    )
    assert local_store.delete(session.session_id)[0] == 200
    assert (
        local_store.turns.append_turn_journal_event_for_stream(
            session.session_id, "run", {"event": "interrupted"}
        )
        is None
    )
    assert local_store.turns.read_turn_journal(session.session_id)["events"] == []


def test_busy_persistence_fence_returns_without_deletion(local_store, monkeypatch):
    from api.session_persistence import session_persistence_gate

    session = local_store.new("busy-persistence")
    gate = session_persistence_gate(session.session_id, local_store.root)
    calls = []

    class BusyLock:
        def acquire(self, timeout=None):
            calls.append(timeout)
            assert timeout is not None and 0 <= timeout <= 5
            return False

        def release(self):
            raise AssertionError("not acquired")

    monkeypatch.setattr(gate, "lock", BusyLock())
    assert local_store.delete(session.session_id)[0] == 503
    assert calls and session.path.exists() and session._persistence_handle().valid
    assert models.SESSIONS[session.session_id] is session
    assert session.session_id not in models._load_webui_deleted_session_tombstone()
    lock = config._get_session_agent_lock(session.session_id)
    assert lock.acquire(blocking=False), "failed delete leaked the mutation lock"
    lock.release()


def test_explicit_import_does_not_restore_old_handles(local_store):
    import copy

    old = local_store.new("same-id-import")
    copied = copy.deepcopy(old)
    writer = run_journal.RunJournalWriter(
        old.session_id, "old-run", session_dir=local_store.root
    )
    assert local_store.delete(old.session_id)[0] == 200
    fresh = models.import_cli_session(
        old.session_id, "Imported", [{"role": "user", "content": "fresh"}]
    )
    fresh.save()
    for stale in (old, copied):
        with pytest.raises(RuntimeError, match="persistence authority was revoked"):
            stale.save()
    assert writer.append_sse_event("token", {"text": "old"}) is None
    assert (
        local_store.turns.append_turn_journal_event_for_stream(
            old.session_id,
            "old-run",
            {"event": "interrupted"},
            _persistence=writer._persistence,
        )
        is None
    )
    assert local_store.turns.read_turn_journal(old.session_id)["events"] == []
    assert json.loads(fresh.path.read_text())["messages"][0]["content"] == "fresh"
    assert run_journal.RunJournalWriter(
        fresh.session_id, "fresh-run", session_dir=local_store.root
    ).append_sse_event("token", {})


def test_other_store_same_identifiers_remains_writable(tmp_path):
    from api.session_persistence import (
        session_persistence_gate,
        revoke_session_persistence,
    )

    first, other = tmp_path / "first", tmp_path / "other"
    old = run_journal.RunJournalWriter("same", "same-run", session_dir=first)
    fresh = run_journal.RunJournalWriter("same", "same-run", session_dir=other)
    gate = session_persistence_gate("same", first)
    with gate.lock:
        revoke_session_persistence(gate)
    assert old.append_sse_event("token", {}) is None
    assert fresh.append_sse_event("token", {})


def test_reclaimed_gate_recovers_durable_deletion(local_store):
    import gc
    import weakref

    session = local_store.new("released-handles")
    gate = weakref.ref(session._persistence_handle().gate)
    assert local_store.delete(session.session_id)[0] == 200
    del session
    gc.collect()
    assert gate() is None
    writer = run_journal.RunJournalWriter(
        "released-handles", "new-holder", session_dir=local_store.root
    )
    assert writer.append_sse_event("token", {}) is None
    assert not (local_store.root / "_run_journal" / "released-handles").exists()


@pytest.mark.parametrize(
    "record",
    ["not-json", "[]", '{"version": 2, "ids": []}', '{"version": 1, "ids": 2}'],
)
def test_unknown_deleted_record_does_not_grant_write_permission(tmp_path, record):
    (tmp_path / "_deleted_webui_sessions.json").write_text(record)
    with pytest.raises((ValueError, RuntimeError)):
        run_journal.RunJournalWriter("unknown", "run", session_dir=tmp_path)
    assert not (tmp_path / "_run_journal").exists()


@pytest.mark.parametrize("which", ["session", "store"])
def test_journal_rejects_mismatched_captured_identity(tmp_path, which):
    from api.session_persistence import SessionPersistenceHandle
    from api import turn_journal

    handle = SessionPersistenceHandle("owner", tmp_path)
    sid = "other" if which == "session" else "owner"
    root = tmp_path if which == "session" else tmp_path / "other"
    with pytest.raises(ValueError, match="Mismatched"):
        run_journal.append_run_event(
            sid, "run", "token", {}, session_dir=root, _persistence=handle
        )
    with pytest.raises(ValueError, match="Mismatched"):
        turn_journal.append_turn_journal_event(
            sid, {"event": "submitted"}, session_dir=root, _persistence=handle
        )


def test_worker_without_captured_turn_authority_fails_closed(local_store):
    session = local_store.new("missing-captured-authority")
    assert (
        local_store.turns.append_turn_journal_event_for_stream(
            session.session_id, "run", {"event": "completed"}, _persistence=None
        )
        is None
    )
    assert local_store.turns.read_turn_journal(session.session_id)["events"] == []


def test_invalid_delete_target_does_not_revoke_local_owner(local_store, tmp_path):
    session = local_store.new("invalid-target")
    outside = tmp_path / "outside.json"
    outside.write_text("outside-preserved")
    session.path.unlink()
    session.path.symlink_to(outside)
    assert local_store.delete(session.session_id)[0] == 400
    assert session._persistence_handle().valid
    assert models.SESSIONS[session.session_id] is session
    assert outside.read_text() == "outside-preserved"


def test_boolean_tombstone_version_is_not_write_authority(tmp_path):
    (tmp_path / "_deleted_webui_sessions.json").write_text(
        '{"version": true, "ids": []}'
    )
    with pytest.raises(RuntimeError, match="Unrecognized"):
        run_journal.RunJournalWriter("unknown", "run", session_dir=tmp_path)
