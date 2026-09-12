import json
import multiprocessing
import os
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _patch_store(monkeypatch, tmp_path):
    from api import models, routes, streaming

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    with models.LOCK:
        models.SESSIONS.clear()
    return session_dir


def _invoke_post(monkeypatch, routes, path, body):
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: dict(body))
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: (
            captured.update(payload=payload, status=status) or True
        ),
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400, **_kwargs: (
            captured.update(payload={"error": message}, status=status) or True
        ),
    )
    assert routes.handle_post(object(), SimpleNamespace(path=path)) is True
    return captured


def _prepare_delete_route(monkeypatch, models, routes):
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda _sid: False)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)


def _replay_rows():
    return [
        {
            "role": "assistant",
            "content": "",
            "id": "assistant-a",
            "finish_reason": "stop",
            "reasoning": "recoverable private transcript",
            "timestamp": 123,
        }
        for _ in range(2)
    ]


def _seed_replay_artifacts(sidecar):
    paths = [
        sidecar.with_name(f"{sidecar.name}.replay-v10.deadbeef.bak"),
        sidecar.with_name(
            f"_replay-v10.{sidecar.name}.deadbeef.manifest.json"
        ),
        sidecar.with_name(f".{sidecar.name}.replay-v10.tmp.probe"),
        sidecar.with_name(f".{sidecar.name}.replay-v10.restore.probe"),
        sidecar.with_name(
            f"._replay-v10.{sidecar.name}.deadbeef.manifest.json.tmp.probe"
        ),
    ]
    for path in paths:
        path.write_text("recoverable private transcript", encoding="utf-8")
    return paths


def _record_tombstone_process(session_dir, sid, peer_sid, result_queue):
    from api import models

    models.SESSION_DIR = Path(session_dir)
    models.SESSION_INDEX_FILE = Path(session_dir) / "_index.json"
    real_load = models._load_webui_deleted_session_tombstone

    def load_then_wait_for_peer(**kwargs):
        current = real_load(**kwargs)
        marker_dir = Path(session_dir) / "tombstone-read-markers"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / sid).write_text("read", encoding="utf-8")
        deadline = time.monotonic() + 0.5
        while not (marker_dir / peer_sid).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return current

    models._load_webui_deleted_session_tombstone = load_then_wait_for_peer
    try:
        models._record_webui_deleted_session_tombstone(sid)
    except Exception as exc:
        result_queue.put((sid, type(exc).__name__, str(exc)))
    else:
        result_queue.put((sid, "ok", ""))


def test_compactor_publish_cannot_resurrect_deleted_session(tmp_path, monkeypatch):
    from api import models, routes
    from scripts import compact_session_replays as compactor

    session_dir = _patch_store(monkeypatch, tmp_path)
    _prepare_delete_route(monkeypatch, models, routes)
    sid = "race-delete"
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": sid,
                "title": "Race",
                "messages": _replay_rows(),
                "context_messages": [],
                "_sidecar_generation_v1": 3,
            }
        ),
        encoding="utf-8",
    )

    delete_started = threading.Event()
    delete_done = threading.Event()
    delete_result = {}
    real_replace = compactor.os.replace
    delete_thread = None

    def delete_session():
        delete_started.set()
        delete_result.update(
            _invoke_post(
                monkeypatch,
                routes,
                "/api/session/delete",
                {"session_id": sid},
            )
        )
        delete_done.set()

    def interleave_delete(source, destination):
        nonlocal delete_thread
        if (
            Path(destination) == sidecar
            and ".replay-v10.tmp." in Path(source).name
            and delete_thread is None
        ):
            delete_thread = threading.Thread(target=delete_session)
            delete_thread.start()
            assert delete_started.wait(timeout=2)
            # Deletion must remain blocked until compact_sidecar returns from
            # this publication hook and eventually releases the SID authority.
            assert not delete_done.wait(timeout=0.25)
        return real_replace(source, destination)

    monkeypatch.setattr(compactor.os, "replace", interleave_delete)
    result = compactor.compact_sidecar(sidecar)
    assert result["status"] == "compacted"
    assert delete_thread is not None
    delete_thread.join(timeout=5)
    assert not delete_thread.is_alive()
    assert delete_result["status"] == 200
    assert delete_result["payload"]["ok"] is True
    assert sid in models._load_webui_deleted_session_tombstone()
    assert not sidecar.exists()
    assert not list(session_dir.glob(f"*replay-v10*{sid}*"))


def test_workspace_recovery_patch_releases_waiting_canonical_save_without_lost_turn(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "raw-writer"
    session = models.Session(
        session_id=sid,
        title="Raw writer",
        workspace=str(tmp_path / "before"),
        messages=[{"role": "user", "content": "durable turn"}],
    )
    session.save(skip_index=True)
    sidecar = session_dir / f"{sid}.json"
    generation_before = json.loads(sidecar.read_text(encoding="utf-8"))[
        "_sidecar_generation_v1"
    ]
    canonical = models.Session.load(sid)
    assert canonical is not None
    canonical.messages.append(
        {"role": "assistant", "content": "new durable turn"}
    )

    real_replace = models._safe_replace
    raw_ready = threading.Event()
    release_raw = threading.Event()
    raw_outcome = []
    canonical_outcome = []

    def gate_raw_replace(source, destination):
        if (
            threading.current_thread().name == "workspace-patch"
            and Path(destination) == sidecar
        ):
            raw_ready.set()
            assert release_raw.wait(timeout=5)
        return real_replace(source, destination)

    monkeypatch.setattr(models, "_safe_replace", gate_raw_replace)

    def raw_writer():
        try:
            models.persist_recovered_workspace_binding(
                session,
                tmp_path / "after",
                expected_workspace=str((tmp_path / "before").resolve()),
            )
        except Exception as exc:
            raw_outcome.append(f"{type(exc).__name__}:{exc}")
        else:
            raw_outcome.append("saved")

    def canonical_writer():
        try:
            canonical.save(skip_index=True)
        except Exception as exc:
            canonical_outcome.append(f"stale:{type(exc).__name__}:{exc}")
        else:
            canonical_outcome.append("saved")

    raw_thread = threading.Thread(target=raw_writer, name="workspace-patch")
    canonical_thread = threading.Thread(target=canonical_writer)
    raw_thread.start()
    assert raw_ready.wait(timeout=5)
    canonical_thread.start()
    canonical_thread.join(timeout=0.25)
    assert canonical_thread.is_alive(), (
        "canonical save must wait while the workspace patch owns sidecar authority"
    )
    release_raw.set()
    raw_thread.join(timeout=5)
    canonical_thread.join(timeout=5)
    assert not raw_thread.is_alive()
    assert not canonical_thread.is_alive()
    assert raw_outcome == ["saved"]
    assert canonical_outcome == ["saved"]

    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    contents = [row.get("content") for row in payload.get("messages") or []]
    generation_after = payload["_sidecar_generation_v1"]
    assert generation_after >= generation_before + 2
    assert "new durable turn" in contents
    assert payload["workspace"] == str((tmp_path / "after").resolve())


def test_cleanup_zero_message_removes_all_plaintext_recovery_artifacts(
    tmp_path, monkeypatch
):
    from api import models, routes
    from scripts import compact_session_replays as compactor

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "cleanup-leak"
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": sid,
                "title": "Leak",
                "messages": _replay_rows(),
                "context_messages": [],
            }
        ),
        encoding="utf-8",
    )
    compacted = compactor.compact_sidecar(sidecar)
    assert compacted["status"] == "compacted"
    loaded = models.Session.load(sid)
    assert loaded is not None
    loaded.messages = []
    loaded.save(skip_index=True)

    captured = _invoke_post(
        monkeypatch, routes, "/api/sessions/cleanup_zero_message", {}
    )
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert not sidecar.exists()
    assert not sidecar.with_suffix(".json.bak").exists()
    assert not list(session_dir.glob(f"{sidecar.name}.bak.archive-*"))
    assert not [path for path in session_dir.iterdir() if "replay-v10" in path.name]
    assert sid in models._load_webui_deleted_session_tombstone()


def test_delete_fails_closed_when_required_sidecar_unlink_fails(
    tmp_path, monkeypatch
):
    from api import models, routes

    session_dir = _patch_store(monkeypatch, tmp_path)
    _prepare_delete_route(monkeypatch, models, routes)
    sid = "failed-unlink"
    session = models.Session(
        session_id=sid,
        messages=[{"role": "user", "content": "must remain fenced"}],
    )
    session.save(skip_index=True)
    sidecar = session_dir / f"{sid}.json"
    real_unlink = Path.unlink

    def fail_sidecar_unlink(path, *args, **kwargs):
        if Path(path) == sidecar:
            raise PermissionError("injected unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_sidecar_unlink)
    captured = _invoke_post(
        monkeypatch, routes, "/api/session/delete", {"session_id": sid}
    )
    assert captured["status"] == 500
    assert sidecar.exists()
    assert sid in models._load_webui_deleted_session_tombstone()


def test_delete_fails_closed_when_tombstone_cannot_be_persisted(
    tmp_path, monkeypatch
):
    from api import models, routes

    session_dir = _patch_store(monkeypatch, tmp_path)
    _prepare_delete_route(monkeypatch, models, routes)
    sid = "failed-tombstone"
    session = models.Session(
        session_id=sid,
        messages=[{"role": "user", "content": "must not be unlinked"}],
    )
    session.save(skip_index=True)
    sidecar = session_dir / f"{sid}.json"

    def fail_tombstone(_sid):
        raise OSError("injected tombstone persistence failure")

    monkeypatch.setattr(
        models, "_record_webui_deleted_session_tombstone", fail_tombstone
    )
    captured = _invoke_post(
        monkeypatch, routes, "/api/session/delete", {"session_id": sid}
    )
    assert captured["status"] == 500
    assert sidecar.exists()


@pytest.mark.parametrize("operation", ["record", "clear"])
def test_deleted_tombstone_rmw_fails_closed_on_corrupt_fence(
    tmp_path, monkeypatch, operation
):
    from api import models

    _patch_store(monkeypatch, tmp_path)
    existing_sid = "existing-deleted"
    models._record_webui_deleted_session_tombstone(existing_sid)
    tombstone = models._webui_deleted_session_tombstone_file()
    corrupt_bytes = b'{"version": 1, "ids": ["existing-deleted"'
    tombstone.write_bytes(corrupt_bytes)

    with pytest.raises(json.JSONDecodeError):
        if operation == "record":
            models._record_webui_deleted_session_tombstone("new-deleted")
        else:
            models._clear_webui_deleted_session_tombstone(existing_sid)

    assert tombstone.read_bytes() == corrupt_bytes


def test_delete_route_fails_closed_without_unlinking_when_tombstone_is_corrupt(
    tmp_path, monkeypatch
):
    from api import models, routes

    session_dir = _patch_store(monkeypatch, tmp_path)
    _prepare_delete_route(monkeypatch, models, routes)
    existing_sid = "existing-deleted-route"
    target_sid = "target-delete-route"
    models._record_webui_deleted_session_tombstone(existing_sid)
    tombstone = models._webui_deleted_session_tombstone_file()
    corrupt_bytes = b'{"version": 1, "ids": ["existing-deleted-route"'
    tombstone.write_bytes(corrupt_bytes)

    target = models.Session(
        session_id=target_sid,
        messages=[{"role": "user", "content": "must remain after refused delete"}],
    )
    target.save(skip_index=True)
    sidecar = session_dir / f"{target_sid}.json"

    captured = _invoke_post(
        monkeypatch, routes, "/api/session/delete", {"session_id": target_sid}
    )

    assert captured["status"] == 500
    assert sidecar.exists()
    assert tombstone.read_bytes() == corrupt_bytes


def test_new_tombstone_survives_capacity_trim(tmp_path, monkeypatch):
    from api import models

    _patch_store(monkeypatch, tmp_path)
    cap = models.WEBUI_DELETED_SESSION_TOMBSTONE_CAP
    for index in range(cap):
        models._record_webui_deleted_session_tombstone(f"z{index:05d}")
    target = "a-target-sid"
    models._record_webui_deleted_session_tombstone(target)
    loaded = models._load_webui_deleted_session_tombstone()
    assert len(loaded) == cap
    assert target in loaded


def test_deleted_tombstone_rmw_is_cross_process_serialized(tmp_path, monkeypatch):
    from api import models

    session_dir = _patch_store(monkeypatch, tmp_path)
    context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
    result_queue = context.Queue()
    sids = ("deleted-a", "deleted-b")
    processes = [
        context.Process(
            target=_record_tombstone_process,
            args=(session_dir, sid, sids[1 - index], result_queue),
        )
        for index, sid in enumerate(sids)
    ]
    try:
        for process in processes:
            process.start()
        results = [result_queue.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)
        assert all(not process.is_alive() for process in processes)
        assert all(process.exitcode == 0 for process in processes)
        assert {result[:2] for result in results} == {
            ("deleted-a", "ok"),
            ("deleted-b", "ok"),
        }
        assert models._load_webui_deleted_session_tombstone() == frozenset(sids)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)


def test_zero_message_cleanup_cannot_delete_a_concurrent_new_message(
    tmp_path, monkeypatch
):
    from api import models, routes

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "cleanup-writer-race"
    seed = models.Session(session_id=sid, title="Untitled", messages=[])
    seed.save(skip_index=True)
    sidecar = session_dir / f"{sid}.json"
    writer = models.Session.load(sid)
    assert writer is not None
    writer.messages.append({"role": "user", "content": "must survive or go stale"})
    writer_outcome = []
    real_unlink = Path.unlink
    started = threading.Event()
    writer_threads = []

    def writer_save():
        try:
            writer.save(skip_index=True)
        except Exception as exc:
            writer_outcome.append(f"stale:{type(exc).__name__}:{exc}")
        else:
            writer_outcome.append("saved")

    def gate_cleanup_unlink(path, *args, **kwargs):
        if (
            threading.current_thread().name == "cleanup"
            and Path(path) == sidecar
            and not started.is_set()
        ):
            started.set()
            thread = threading.Thread(target=writer_save)
            thread.start()
            thread.join(timeout=0.25)
            writer_threads.append(thread)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", gate_cleanup_unlink)
    captured = {}

    def cleanup():
        captured.update(
            _invoke_post(
                monkeypatch,
                routes,
                "/api/sessions/cleanup_zero_message",
                {},
            )
        )

    cleanup_thread = threading.Thread(target=cleanup, name="cleanup")
    cleanup_thread.start()
    cleanup_thread.join(timeout=5)
    assert not cleanup_thread.is_alive()
    assert started.is_set()
    assert len(writer_threads) == 1
    writer_thread = writer_threads[0]
    writer_thread.join(timeout=5)
    assert not writer_thread.is_alive()
    assert captured["status"] == 200
    assert writer_outcome
    if writer_outcome == ["saved"]:
        assert sidecar.exists()
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["messages"][-1]["content"] == "must survive or go stale"
    else:
        assert writer_outcome[0].startswith("stale:")
        assert not sidecar.exists()
        assert sid in models._load_webui_deleted_session_tombstone()


def test_direct_backup_restore_refuses_a_deleted_sid(tmp_path, monkeypatch):
    from api import models, session_recovery

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "deleted-restore"
    live_path = session_dir / f"{sid}.json"
    live_payload = {
        "session_id": sid,
        "messages": [{"role": "user", "content": "live"}],
        "_sidecar_generation_v1": 2,
        "_sidecar_epoch_v1": "a" * 32,
    }
    backup_payload = {
        "session_id": sid,
        "messages": [
            {"role": "user", "content": "live"},
            {"role": "assistant", "content": "deleted backup"},
        ],
        "_sidecar_generation_v1": 1,
        "_sidecar_epoch_v1": "a" * 32,
    }
    live_path.write_text(json.dumps(live_payload), encoding="utf-8")
    live_path.with_suffix(".json.bak").write_text(
        json.dumps(backup_payload), encoding="utf-8"
    )
    models._record_webui_deleted_session_tombstone(sid)

    result = session_recovery.recover_session(live_path)
    assert result["restored"] is False
    assert result.get("deleted") is True
    assert json.loads(live_path.read_text(encoding="utf-8")) == live_payload


def test_state_db_materialization_rechecks_tombstone_before_publish(
    tmp_path, monkeypatch
):
    from api import models, session_recovery

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "deleted-during-reconcile"
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
            "model TEXT, started_at REAL, message_count INTEGER)"
        )
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
            "role TEXT, content TEXT, timestamp REAL)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, 'webui', 'deleted', 'model', 1, 1)",
            (sid,),
        )
        connection.execute(
            "INSERT INTO messages VALUES (1, ?, 'user', 'deleted', 1)",
            (sid,),
        )

    real_read = session_recovery._read_state_db_missing_sidecar_rows

    def rows_then_delete(*args, **kwargs):
        rows = real_read(*args, **kwargs)
        models._record_webui_deleted_session_tombstone(sid)
        return rows

    monkeypatch.setattr(
        session_recovery, "_read_state_db_missing_sidecar_rows", rows_then_delete
    )
    result = session_recovery.recover_missing_sidecars_from_state_db(
        session_dir, db_path
    )
    assert result["materialized"] == 0
    assert not (session_dir / f"{sid}.json").exists()


def test_hidden_ephemeral_cleanup_uses_complete_durable_delete_protocol(
    tmp_path, monkeypatch
):
    from api import models, streaming

    _patch_store(monkeypatch, tmp_path)
    sid = "hidden-ephemeral"
    session = models.Session(
        session_id=sid,
        messages=[{"role": "user", "content": "private side question"}],
    )
    session.active_stream_id = "ephemeral-stream"
    session.pending_user_message = "private side question"
    session.save(skip_index=True)
    replay_artifacts = _seed_replay_artifacts(session.path)
    backup = session.path.with_suffix(".json.bak")
    backup.write_text("recoverable private transcript", encoding="utf-8")

    streaming._cleanup_ephemeral_cancelled_turn(session)

    assert not session.path.exists()
    assert not backup.exists()
    assert all(not path.exists() for path in replay_artifacts)
    assert sid in models._load_webui_deleted_session_tombstone()


def test_compactor_restore_refuses_tombstoned_sid(tmp_path, monkeypatch):
    from api import models
    from scripts import compact_session_replays as compactor

    session_dir = _patch_store(monkeypatch, tmp_path)
    sid = "tombstoned-rollback"
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": sid,
                "messages": _replay_rows(),
                "context_messages": [],
            }
        ),
        encoding="utf-8",
    )
    compacted = compactor.compact_sidecar(sidecar)
    models._record_webui_deleted_session_tombstone(sid)

    with pytest.raises(compactor.StreamJSONError, match="deleted|tombstone"):
        compactor.restore_manifest(Path(compacted["manifest"]))
