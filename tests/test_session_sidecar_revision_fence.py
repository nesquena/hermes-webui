import builtins
import errno
import hashlib
import json
import logging
import multiprocessing
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import types
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest


def _patch_store(monkeypatch, models, session_dir: Path) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    with models.LOCK:
        models.SESSIONS.clear()


def _seed_sidecar_delete_artifacts(sidecar: Path) -> tuple[list[Path], Path]:
    artifacts = [
        sidecar,
        sidecar.with_suffix(".json.bak"),
        sidecar.with_name(f"{sidecar.name}.bak.archive-deadbeef"),
        sidecar.with_name(f"{sidecar.name}.replay-v10.deadbeef.bak"),
        sidecar.with_name(f"_replay-v10.{sidecar.name}.deadbeef.manifest.json"),
        sidecar.with_name(f".{sidecar.name}.replay-v10.tmp.probe"),
        sidecar.with_name(f".{sidecar.name}.replay-v10.restore.probe"),
        sidecar.with_name(
            f"._replay-v10.{sidecar.name}.deadbeef.manifest.json.tmp.probe"
        ),
    ]
    for artifact in artifacts[1:]:
        artifact.write_text("recoverable hidden transcript", encoding="utf-8")
    unrelated = sidecar.with_name("unrelated.json.replay-v10.deadbeef.bak")
    unrelated.write_text("must survive", encoding="utf-8")
    return artifacts, unrelated


def _process_writer(
    session_dir, sid, marker, start_event, ready_queue, result_queue
):
    from api import models

    models.SESSION_DIR = Path(session_dir)
    models.SESSION_INDEX_FILE = Path(session_dir) / "_index.json"
    session = models.Session.load(sid)
    assert session is not None
    session.messages.append({"role": "assistant", "content": marker})
    ready_queue.put(marker)
    start_event.wait(timeout=15)
    try:
        session.save(skip_index=True)
    except models.StaleSessionGenerationError:
        result_queue.put((marker, "stale"))
    else:
        result_queue.put((marker, "saved"))


def _process_cleanup_race_writer(
    session_dir,
    sid,
    marker,
    start_event,
    ready_event,
    done_event,
    result_queue,
):
    from api import models

    models.SESSION_DIR = Path(session_dir)
    models.SESSION_INDEX_FILE = Path(session_dir) / "_index.json"
    session = models.Session.load(sid)
    assert session is not None
    session.messages.append({"role": "assistant", "content": marker})
    ready_event.set()
    start_event.wait(timeout=15)
    try:
        session.save(skip_index=True)
    except models.StaleSessionGenerationError:
        result_queue.put("stale")
    except Exception as exc:
        result_queue.put(f"{type(exc).__name__}:{exc}")
    else:
        result_queue.put("saved")
    finally:
        done_event.set()


def _process_record_deleted_tombstone(
    session_dir,
    sid,
    peer_sid,
    result_queue,
):
    from api import models

    models.SESSION_DIR = Path(session_dir)
    models.SESSION_INDEX_FILE = Path(session_dir) / "_index.json"
    real_load = models._load_webui_deleted_session_tombstone

    def load_then_wait_for_peer():
        current = real_load()
        marker_dir = Path(session_dir) / "tombstone-read-markers"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / sid).write_text("read", encoding="utf-8")
        deadline = time.monotonic() + 3
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


def _process_save_session_for_lock_namespace(session_dir, sid, result_queue):
    from api import models

    models.SESSION_DIR = Path(session_dir)
    models.SESSION_INDEX_FILE = Path(session_dir) / "_index.json"
    try:
        session = models.Session(
            session_id=sid,
            workspace=str(session_dir),
            messages=[{"role": "user", "content": "lock namespace probe"}],
        )
        session.save(skip_index=True)
    except Exception as exc:
        result_queue.put((sid, type(exc).__name__, str(exc)))
    else:
        result_queue.put((sid, "ok", ""))


def test_deleted_session_tombstone_rmw_is_cross_process_serialized(
    tmp_path,
    monkeypatch,
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
    result_queue = context.Queue()
    session_ids = ("deleted-a", "deleted-b")
    processes = [
        context.Process(
            target=_process_record_deleted_tombstone,
            args=(session_dir, sid, session_ids[1 - index], result_queue),
        )
        for index, sid in enumerate(session_ids)
    ]
    try:
        for process in processes:
            process.start()
        results = [result_queue.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=15)
        assert all(not process.is_alive() for process in processes)
        assert all(process.exitcode == 0 for process in processes)
        assert {result[:2] for result in results} == {
            ("deleted-a", "ok"),
            ("deleted-b", "ok"),
        }
        assert models._load_webui_deleted_session_tombstone() == frozenset(
            session_ids
        )
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)


def test_global_tombstone_lock_namespace_cannot_alias_session_sid(
    tmp_path,
    monkeypatch,
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
    for sid in (
        "ordinary-lock-namespace",
        models._WEBUI_DELETED_SESSION_TOMBSTONE_LOCK_SID,
    ):
        result_queue = context.Queue()
        process = context.Process(
            target=_process_save_session_for_lock_namespace,
            args=(session_dir, sid, result_queue),
        )
        try:
            process.start()
            process.join(timeout=5)
            assert not process.is_alive(), f"save deadlocked for accepted SID {sid!r}"
            assert process.exitcode == 0
            assert result_queue.get(timeout=2)[:2] == (sid, "ok")
        finally:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)


def test_existing_deleted_session_tombstone_retries_directory_fsync(
    tmp_path,
    monkeypatch,
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    fsynced = []
    monkeypatch.setattr(
        models,
        "_fsync_sidecar_directory",
        lambda directory: fsynced.append(Path(directory)),
    )
    models._record_webui_deleted_session_tombstone("durable-retry")
    fsynced.clear()

    models._record_webui_deleted_session_tombstone("durable-retry")

    assert fsynced == [session_dir]


def test_sidecar_delete_fails_closed_when_current_tombstone_cannot_be_verified(
    tmp_path,
    monkeypatch,
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "unverifiable-current-tombstone"
    session = models.Session(
        session_id=sid,
        messages=[{"role": "user", "content": "must remain durable"}],
    )
    session.save(skip_index=True)
    artifacts, _unrelated = _seed_sidecar_delete_artifacts(session.path)
    real_load = models._load_webui_deleted_session_tombstone
    load_count = 0

    def hide_current_sid_on_verification():
        nonlocal load_count
        load_count += 1
        retained = real_load()
        if load_count > 1:
            return frozenset(candidate for candidate in retained if candidate != sid)
        return retained

    monkeypatch.setattr(
        models,
        "_load_webui_deleted_session_tombstone",
        hide_current_sid_on_verification,
    )

    with models._session_sidecar_authority(sid):
        with pytest.raises(models.SessionDeleteTombstoneError):
            models._delete_session_sidecar_artifacts_locked(sid)

    assert load_count == 2
    assert all(artifact.exists() for artifact in artifacts)


def test_import_clear_cannot_race_tombstone_verification_and_delete(
    tmp_path,
    monkeypatch,
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "import-delete-race"
    seed = models.Session(
        session_id=sid,
        messages=[{"role": "user", "content": "before delete"}],
    )
    seed.save(skip_index=True)

    recorded = threading.Event()
    allow_delete = threading.Event()
    import_ready_to_save = threading.Event()
    allow_import_save = threading.Event()
    errors: queue.Queue[BaseException] = queue.Queue()
    delete_result: list[bool] = []
    imported: list[models.Session] = []

    real_record = models._record_webui_deleted_session_tombstone
    real_save = models.Session.save

    def record_then_pause(target_sid):
        real_record(target_sid)
        recorded.set()
        assert allow_delete.wait(timeout=5)

    def save_after_delete(self, *args, **kwargs):
        import_ready_to_save.set()
        assert allow_import_save.wait(timeout=5)
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(
        models,
        "_record_webui_deleted_session_tombstone",
        record_then_pause,
    )
    monkeypatch.setattr(models.Session, "save", save_after_delete)

    def delete_worker():
        try:
            with models._session_sidecar_authority(sid):
                delete_result.append(
                    models._delete_session_sidecar_artifacts_locked(sid)
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.put(exc)

    def import_worker():
        try:
            imported.append(
                models.import_cli_session(
                    sid,
                    "Imported",
                    [{"role": "user", "content": "after delete"}],
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.put(exc)

    delete_thread = threading.Thread(target=delete_worker, name="delete-worker")
    import_thread = threading.Thread(target=import_worker, name="import-worker")
    delete_thread.start()
    assert recorded.wait(timeout=5)
    import_thread.start()
    assert import_ready_to_save.wait(timeout=5)

    allow_delete.set()
    delete_thread.join(timeout=5)
    try:
        assert not delete_thread.is_alive()
        assert errors.empty(), list(errors.queue)
        assert delete_result == [True]
        assert sid in models._load_webui_deleted_session_tombstone()
        assert not (session_dir / f"{sid}.json").exists()
    finally:
        allow_import_save.set()
        import_thread.join(timeout=5)

    assert not import_thread.is_alive()
    assert errors.empty(), list(errors.queue)
    assert len(imported) == 1
    assert (session_dir / f"{sid}.json").exists()
    assert sid not in models._load_webui_deleted_session_tombstone()


def test_hidden_background_cleanup_uses_agent_and_sidecar_authorities(
    tmp_path,
    monkeypatch,
):
    from api import models, routes

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)
    sid = "hidden-background-cleanup"
    seed = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "assistant", "content": "background result"}],
    )
    seed.save(skip_index=True)
    stale_alias = models.Session.load(sid)
    assert stale_alias is not None
    with models.LOCK:
        models.SESSIONS[sid] = stale_alias
    backup = session_dir / f"{sid}.json.bak"
    archive = session_dir / f"{sid}.json.bak.archive-deadbeef"
    backup.write_text("backup", encoding="utf-8")
    archive.write_text("archive", encoding="utf-8")
    real_authority = models._session_sidecar_authority
    authority_entered = threading.Event()

    @contextmanager
    def observed_authority(session_id, *, session_dir=None):
        authority_entered.set()
        with real_authority(session_id, session_dir=session_dir):
            yield

    monkeypatch.setattr(models, "_session_sidecar_authority", observed_authority)
    fsynced = []
    monkeypatch.setattr(
        models,
        "_fsync_sidecar_directory",
        lambda directory: fsynced.append(Path(directory)),
    )
    agent_lock = routes._get_session_agent_lock(sid)
    assert agent_lock.acquire(timeout=1)
    started = threading.Event()
    failures = []

    def cleanup():
        started.set()
        try:
            routes._delete_hidden_background_session_sidecar(sid)
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=cleanup)
    try:
        thread.start()
        assert started.wait(timeout=1)
        assert not authority_entered.wait(timeout=0.2)
        assert (session_dir / f"{sid}.json").exists()
    finally:
        agent_lock.release()
    assert authority_entered.wait(timeout=2)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert failures == []
    assert not (session_dir / f"{sid}.json").exists()
    assert not backup.exists()
    assert not archive.exists()
    assert fsynced == [session_dir, session_dir]
    with models.LOCK:
        assert sid not in models.SESSIONS
    with pytest.raises(models.StaleSessionGenerationError):
        stale_alias.save(skip_index=True)


def test_hidden_ephemeral_cancel_cleanup_uses_durable_sidecar_delete_protocol(
    tmp_path,
    monkeypatch,
):
    from api import models, streaming

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    sid = "hidden-ephemeral-cancel"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "private side question"}],
    )
    session.active_stream_id = "ephemeral-stream"
    session.pending_user_message = "private side question"
    session.pending_attachments = ["secret.txt"]
    session.pending_started_at = time.time()
    session.pending_user_source = "webui"
    session.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS[sid] = session
    artifacts, unrelated = _seed_sidecar_delete_artifacts(session.path)

    with streaming._get_session_agent_lock(sid):
        streaming._finalize_cancelled_turn(session, ephemeral=True)

    assert session.active_stream_id is None
    assert session.pending_user_message is None
    assert session.pending_attachments == []
    assert session.pending_started_at is None
    assert session.pending_user_source is None
    assert all(not artifact.exists() for artifact in artifacts)
    assert unrelated.read_text(encoding="utf-8") == "must survive"
    assert sid in models._load_webui_deleted_session_tombstone()
    with models.LOCK:
        assert sid not in models.SESSIONS
    with pytest.raises(models.StaleSessionGenerationError):
        session.save(skip_index=True)


def test_hidden_ephemeral_cleanup_logs_tombstone_failure_without_unlinking(
    tmp_path,
    monkeypatch,
    caplog,
):
    from api import models, streaming

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    sid = "hidden-ephemeral-tombstone-failure"
    session = models.Session(session_id=sid, workspace=str(tmp_path))
    session.active_stream_id = "ephemeral-stream"
    session.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS[sid] = session
    artifacts, _unrelated = _seed_sidecar_delete_artifacts(session.path)

    def reject_tombstone(_sid):
        raise OSError("synthetic tombstone persistence failure")

    monkeypatch.setattr(
        models,
        "_record_webui_deleted_session_tombstone",
        reject_tombstone,
    )
    with caplog.at_level(logging.WARNING, logger="api.streaming"):
        with streaming._get_session_agent_lock(sid):
            streaming._finalize_cancelled_turn(session, ephemeral=True)

    assert all(artifact.exists() for artifact in artifacts)
    assert sid not in models._load_webui_deleted_session_tombstone()
    with models.LOCK:
        assert models.SESSIONS.get(sid) is session
    assert f"cancelled ephemeral session {sid}" in caplog.text
    assert "synthetic tombstone persistence failure" in caplog.text


def test_hidden_ephemeral_cleanup_rejects_noncanonical_session_path(
    tmp_path,
    monkeypatch,
    caplog,
):
    from api import models, streaming

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    sid = "hidden-ephemeral-path-check"
    session = models.Session(session_id=sid, workspace=str(tmp_path))
    session.save(skip_index=True)
    outside = tmp_path / "outside.json"
    outside.write_text("do not unlink", encoding="utf-8")
    noncanonical = types.SimpleNamespace(
        session_id=sid,
        path=outside,
        _sidecar_revisions=session._sidecar_revisions,
    )

    with caplog.at_level(logging.WARNING, logger="api.streaming"):
        with streaming._get_session_agent_lock(sid):
            deleted = streaming._cleanup_ephemeral_session_sidecar_locked(
                noncanonical,
                outcome="cancelled",
            )

    assert deleted is False
    assert session.path.exists()
    assert outside.read_text(encoding="utf-8") == "do not unlink"
    assert sid not in models._load_webui_deleted_session_tombstone()
    assert "does not match SID" in caplog.text


def test_hidden_ephemeral_normal_completion_uses_durable_sidecar_delete_protocol(
    tmp_path,
    monkeypatch,
    caplog,
):
    from api import config, models, streaming

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    sid = "hidden-ephemeral-complete"
    stream_id = "hidden-ephemeral-stream"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        title="btw: private side question",
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "private side question"
    session.pending_started_at = time.time()
    session.pending_user_source = "webui"
    session.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS[sid] = session
    artifacts, unrelated = _seed_sidecar_delete_artifacts(session.path)
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue

    class SuccessfulEphemeralAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self.context_compressor = None
            self.session_prompt_tokens = 3
            self.session_completion_tokens = 2
            self.session_estimated_cost_usd = 0.001
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "private answer"},
                ]
            }

        def interrupt(self, _message):
            pass

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = mock.Mock(
        return_value={
            "provider": "openai",
            "api_key": "synthetic-key",
            "base_url": None,
        }
    )
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=None)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.runtime_provider",
        fake_runtime_module,
    )
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(
        streaming,
        "_get_ai_agent",
        lambda: SuccessfulEphemeralAgent,
    )
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_args, **_kwargs: ("test-model", "openai", None),
    )
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda _cfg: [])

    with caplog.at_level(logging.ERROR, logger="api.streaming"):
        streaming._run_agent_streaming(
            session_id=sid,
            msg_text="private side question",
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
            ephemeral=True,
        )

    assert any(
        event == "done" and payload.get("answer") == "private answer"
        for event, payload in list(event_queue.queue)
    )
    assert all(not artifact.exists() for artifact in artifacts)
    assert unrelated.read_text(encoding="utf-8") == "must survive"
    assert sid in models._load_webui_deleted_session_tombstone()
    assert "_last_resort_sync_from_core failed" not in caplog.text
    with models.LOCK:
        assert sid not in models.SESSIONS


@pytest.mark.parametrize(
    ("zero_only", "title"),
    [(False, "Untitled"), (True, "Named empty session")],
)
def test_empty_session_cleanup_fences_writer_between_check_and_delete(
    tmp_path,
    monkeypatch,
    zero_only,
    title,
):
    from api import models, routes

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)
    monkeypatch.setattr(routes, "LOCK", models.LOCK)
    sid = f"cleanup-race-{'zero' if zero_only else 'normal'}"
    marker = "UNIQUE DURABLE TURN"
    models.Session(
        session_id=sid,
        title=title,
        workspace=str(tmp_path),
        messages=[],
    ).save(skip_index=True)
    sidecar = session_dir / f"{sid}.json"
    backup = sidecar.with_suffix(".json.bak")
    archive = backup.with_name(f"{backup.name}.archive-review-probe")
    backup.write_text("offline backup", encoding="utf-8")
    archive.write_text("offline archive", encoding="utf-8")

    context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
    start_event = context.Event()
    ready_event = context.Event()
    done_event = context.Event()
    result_queue = context.Queue()
    writer = context.Process(
        target=_process_cleanup_race_writer,
        args=(
            session_dir,
            sid,
            marker,
            start_event,
            ready_event,
            done_event,
            result_queue,
        ),
    )
    writer.start()
    assert ready_event.wait(timeout=15)

    original_load = models.Session.load.__func__

    def load_then_commit_writer(cls, candidate_sid):
        loaded = original_load(cls, candidate_sid)
        if candidate_sid == sid:
            start_event.set()
            assert done_event.wait(timeout=15), "writer blocked after cleanup check"
        return loaded

    monkeypatch.setattr(models.Session, "load", classmethod(load_then_commit_writer))
    monkeypatch.setattr(routes, "j", lambda _handler, payload: payload)
    try:
        result = routes._handle_sessions_cleanup(
            object(),
            {},
            zero_only=zero_only,
        )
        # The fixed cleanup reads directly while holding SID authority, so the
        # legacy Session.load interposition is not reached. Release the writer
        # after deletion to prove its stale generation remains fenced.
        start_event.set()
        writer.join(timeout=20)
        assert not writer.is_alive()
        assert writer.exitcode == 0
        writer_result = result_queue.get(timeout=5)
    finally:
        if writer.is_alive():
            writer.terminate()
        writer.join(timeout=5)

    assert result == {"ok": True, "cleaned": 1}
    assert writer_result == "stale"
    assert not sidecar.exists()
    assert not backup.exists()
    assert not archive.exists()
    assert sid in models._load_webui_deleted_session_tombstone()


def test_empty_session_cleanup_does_not_count_partial_delete_as_index_ghost(
    tmp_path,
    monkeypatch,
):
    from api import models, routes

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    index_path = session_dir / "_index.json"
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)
    monkeypatch.setattr(routes, "LOCK", models.LOCK)
    failed_sid = "cleanup-partial-delete"
    healthy_sid = "cleanup-best-effort-peer"
    for sid in (failed_sid, healthy_sid):
        models.Session(
            session_id=sid,
            title="Untitled",
            workspace=str(tmp_path),
            messages=[],
        ).save(skip_index=True)
    failed_backup = session_dir / f"{failed_sid}.json.bak"
    failed_backup.write_text("required offline backup", encoding="utf-8")
    index_path.write_text(
        json.dumps(
            [
                {"session_id": failed_sid, "title": "Untitled", "message_count": 0},
                {"session_id": healthy_sid, "title": "Untitled", "message_count": 0},
            ]
        ),
        encoding="utf-8",
    )
    real_unlink = Path.unlink

    def fail_required_backup_unlink(path, *args, **kwargs):
        if path == failed_backup:
            raise PermissionError("required cleanup backup is locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_required_backup_unlink)
    monkeypatch.setattr(routes, "j", lambda _handler, payload: payload)

    result = routes._handle_sessions_cleanup(object(), {})

    assert result == {"ok": True, "cleaned": 1}
    assert not (session_dir / f"{failed_sid}.json").exists()
    assert failed_backup.exists()
    assert not (session_dir / f"{healthy_sid}.json").exists()
    assert json.loads(index_path.read_text(encoding="utf-8")) == []
    assert failed_sid in models._load_webui_deleted_session_tombstone()


def test_stale_loaded_instance_cannot_overwrite_newer_sidecar(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "revision-cas"
    seed = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "seed"}],
    )
    seed.save(skip_index=True)
    first = models.Session.load(sid)
    with models.LOCK:
        models.SESSIONS.pop(sid, None)
    second = models.Session.load(sid)
    assert first is not None and second is not None

    second.messages.append({"role": "assistant", "content": "newer"})
    second.save(skip_index=True)
    first.title = "stale mutation"
    with pytest.raises(models.StaleSessionGenerationError):
        first.save(skip_index=True)

    persisted = json.loads(
        (session_dir / f"{sid}.json").read_text(encoding="utf-8")
    )
    assert persisted["messages"][-1]["content"] == "newer"


def test_native_windows_newlines_do_not_invalidate_second_save(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "windows-newline-revision"
    real_open = builtins.open

    def windows_text_open(file, mode="r", *args, **kwargs):
        if mode == "w" and ".tmp." in str(file) and kwargs.get("newline") is None:
            kwargs["newline"] = "\r\n"
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", windows_text_open)
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "first"}],
    )
    session.save(skip_index=True)
    session.messages.append({"role": "assistant", "content": "second"})

    session.save(skip_index=True)

    persisted = json.loads(
        (session_dir / f"{sid}.json").read_text(encoding="utf-8")
    )
    assert persisted["_sidecar_generation_v1"] == 2
    assert persisted["messages"][-1]["content"] == "second"


def test_new_instance_expected_absent_never_overwrites_existing_sid(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "create-only"
    first = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "first"}],
    )
    stale = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "stale"}],
    )

    first.save(skip_index=True)
    with pytest.raises(models.StaleSessionGenerationError):
        stale.save(skip_index=True)
    persisted = json.loads(
        (session_dir / f"{sid}.json").read_text(encoding="utf-8")
    )
    assert persisted["messages"] == first.messages


def test_create_only_publish_fails_closed_without_atomic_primitive(
    tmp_path, monkeypatch
):
    from api import models

    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b'{"complete": true}')

    def unsupported_link(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "hard links unsupported")

    monkeypatch.setattr(models.os, "link", unsupported_link)

    with pytest.raises(OSError, match="hard links unsupported"):
        models._publish_sidecar_no_replace(source, destination)

    assert not destination.exists()
    assert source.read_bytes() == b'{"complete": true}'


def test_generation_is_scoped_per_sid_across_rotation(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    old_sid = "parent"
    new_sid = "continuation"
    session = models.Session(
        session_id=old_sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "parent"}],
    )
    session.save(skip_index=True)

    session.session_id = new_sid
    session.parent_session_id = old_sid
    session.messages.append({"role": "assistant", "content": "continuation"})
    session.save(skip_index=True)
    session.session_id = old_sid
    session.title = "archived parent"
    session.save(skip_index=True)

    parent = json.loads(
        (session_dir / f"{old_sid}.json").read_text(encoding="utf-8")
    )
    continuation = json.loads(
        (session_dir / f"{new_sid}.json").read_text(encoding="utf-8")
    )
    assert parent["_sidecar_generation_v1"] == 2
    assert continuation["_sidecar_generation_v1"] == 1


@pytest.mark.skipif(os.name == "nt", reason="fork-based multiprocess CAS probe")
def test_two_process_writers_have_exactly_one_cas_winner(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "multiprocess-cas"
    models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "base"}],
    ).save(skip_index=True)

    context = multiprocessing.get_context("fork")
    start_event = context.Event()
    ready_queue = context.Queue()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_process_writer,
            args=(
                session_dir,
                sid,
                marker,
                start_event,
                ready_queue,
                result_queue,
            ),
        )
        for marker in ("writer-a", "writer-b")
    ]
    for process in processes:
        process.start()
    assert {ready_queue.get(timeout=15), ready_queue.get(timeout=15)} == {
        "writer-a",
        "writer-b",
    }
    start_event.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    results = dict(result_queue.get(timeout=5) for _ in processes)
    assert sorted(results.values()) == ["saved", "stale"]
    persisted = json.loads(
        (session_dir / f"{sid}.json").read_text(encoding="utf-8")
    )
    committed = {
        message.get("content")
        for message in persisted["messages"]
        if message.get("content") in {"writer-a", "writer-b"}
    }
    assert len(committed) == 1
    assert persisted["_sidecar_generation_v1"] == 2


def test_recovery_expected_absent_uses_create_or_fail(tmp_path, monkeypatch):
    from api import session_recovery

    session_path = tmp_path / "absent.json"
    backup_path = session_path.with_suffix(".json.bak")
    backup_path.write_text(
        json.dumps(
            {
                "session_id": "absent",
                "messages": [{"role": "user", "content": "backup"}],
            }
        ),
        encoding="utf-8",
    )
    competing = {
        "messages": [{"role": "user", "content": "competing"}]
    }
    real_link = session_recovery.os.link

    def competing_link(src, dst):
        Path(dst).write_text(json.dumps(competing), encoding="utf-8")
        return real_link(src, dst)

    monkeypatch.setattr(session_recovery.os, "link", competing_link)
    result = session_recovery.recover_session(session_path)
    assert result["restored"] is False
    assert result["stale_generation"] is True
    assert json.loads(session_path.read_text(encoding="utf-8")) == competing


def test_recovery_invalidates_cached_alias_and_publishes_generation(
    tmp_path, monkeypatch
):
    from api import models, session_recovery

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "recovery-alias"
    session_path = session_dir / f"{sid}.json"
    backup_path = session_path.with_suffix(".json.bak")
    live = {
        "session_id": sid,
        "workspace": str(tmp_path),
        "messages": [{"role": "user", "content": "live"}],
    }
    backup = {
        "session_id": sid,
        "workspace": str(tmp_path),
        "messages": [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    }
    session_path.write_text(json.dumps(live), encoding="utf-8")
    backup_path.write_text(json.dumps(backup), encoding="utf-8")
    alias = models.Session.load(sid)
    assert alias is not None
    with models.LOCK:
        models.SESSIONS[sid] = alias
    fsynced = []
    monkeypatch.setattr(
        models,
        "_fsync_sidecar_directory",
        lambda directory: fsynced.append(Path(directory)),
    )

    result = session_recovery.recover_session(session_path)
    assert result["restored"] is True
    assert fsynced == [session_dir]
    with models.LOCK:
        assert sid not in models.SESSIONS
    restored = json.loads(session_path.read_text(encoding="utf-8"))
    assert restored["_sidecar_generation_v1"] == 1
    alias.title = "stale alias"
    with pytest.raises(RuntimeError, match="stale|generation"):
        alias.save(skip_index=True)


def test_backup_is_monotone_across_later_poorer_shrinks(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "monotone-backup"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": str(index)} for index in range(10)
        ],
    )
    session.save(skip_index=True)
    session.messages = session.messages[:2]
    session.save(skip_index=True)
    session.messages = [
        {"role": "user", "content": str(index)} for index in range(5)
    ]
    session.save(skip_index=True)
    session.messages = session.messages[:4]
    session.save(skip_index=True)
    backup = json.loads(
        (session_dir / f"{sid}.json.bak").read_text(encoding="utf-8")
    )
    assert len(backup["messages"]) == 10


def test_backup_retirement_requires_matching_receipt(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "cleanup"
    backup_path = session_dir / f"{sid}.json.bak"
    live_path = session_dir / f"{sid}.json"
    live_path.write_text("committed live", encoding="utf-8")
    live_receipt = models._read_sidecar_revision(live_path, sid)
    backup_path.write_text("first backup", encoding="utf-8")

    first_receipt = models._read_sidecar_revision(backup_path, sid)
    backup_path.write_text("newer foreign backup", encoding="utf-8")

    assert models._retire_backup_if_owned(
        sid, backup_path, None, live_receipt
    ) is False
    assert (
        models._retire_backup_if_owned(
            sid, backup_path, first_receipt, live_receipt
        )
        is False
    )
    assert backup_path.read_text(encoding="utf-8") == "newer foreign backup"

    current_receipt = models._read_sidecar_revision(backup_path, sid)
    archive_path = backup_path.with_name(f"{backup_path.name}.archive-test")
    archive_path.write_text('{"archived": true}', encoding="utf-8")
    assert (
        models._retire_backup_if_owned(
            sid, backup_path, current_receipt, live_receipt
        )
        is True
    )
    assert not backup_path.exists()
    assert not archive_path.exists()


def test_shrinking_save_fails_closed_when_backup_publish_fails(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "backup-fail-closed"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    )
    session.save(skip_index=True)
    original = json.loads(session.path.read_text(encoding="utf-8"))
    real_replace = models._safe_replace

    def fail_backup_publish(source, destination):
        if Path(destination).suffix == ".bak":
            raise OSError("simulated backup publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(models, "_safe_replace", fail_backup_publish)
    session.messages = session.messages[:1]
    with pytest.raises(RuntimeError, match="backup"):
        session.save(skip_index=True)
    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    assert persisted["messages"] == original["messages"]
    assert (
        persisted["_sidecar_generation_v1"]
        == original["_sidecar_generation_v1"]
    )


def test_malformed_backup_is_archived_before_live_snapshot_promotion(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "malformed-backup-recovery"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    )
    session.save(skip_index=True)
    before_shrink = session.path.read_bytes()
    malformed = b'{"broken":'
    backup_path = session.path.with_suffix(".json.bak")
    backup_path.write_bytes(malformed)
    session.messages = session.messages[:1]

    session.save(skip_index=True)

    archive = backup_path.with_name(
        f"{backup_path.name}.archive-{hashlib.sha256(malformed).hexdigest()}"
    )
    assert archive.read_bytes() == malformed
    assert backup_path.read_bytes() == before_shrink
    assert len(json.loads(session.path.read_text(encoding="utf-8"))["messages"]) == 1


def test_malformed_backup_archive_does_not_require_hard_links(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "malformed-backup-no-hardlinks"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    )
    session.save(skip_index=True)
    before_shrink = session.path.read_bytes()
    malformed = b'{"broken":'
    backup_path = session.path.with_suffix(".json.bak")
    backup_path.write_bytes(malformed)
    session.messages = session.messages[:1]

    def unsupported_link(*_args, **_kwargs):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    monkeypatch.setattr(models.os, "link", unsupported_link)

    session.save(skip_index=True)

    archive = backup_path.with_name(
        f"{backup_path.name}.archive-{hashlib.sha256(malformed).hexdigest()}"
    )
    assert archive.read_bytes() == malformed
    assert backup_path.read_bytes() == before_shrink
    assert len(json.loads(session.path.read_text(encoding="utf-8"))["messages"]) == 1


def test_archive_temporary_name_handles_maximum_valid_session_id(tmp_path):
    from api import models

    sid = "s" * 150
    backup_path = tmp_path / f"{sid}.json.bak"
    backup_path.write_bytes(b'{"broken":')
    receipt = models._read_sidecar_revision(backup_path, sid)

    archive_path = models._archive_incomparable_backup(
        sid,
        backup_path,
        receipt,
    )

    assert archive_path.read_bytes() == b'{"broken":'
    assert len(os.fsencode(archive_path.name)) <= 255


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract")
def test_first_sidecar_publication_fsyncs_parent_directory(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    fsynced = []
    monkeypatch.setattr(
        models,
        "_fsync_sidecar_directory",
        lambda directory: fsynced.append(Path(directory)),
        raising=False,
    )
    session = models.Session(
        session_id="durable-first-publication",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "first"}],
    )

    session.save(skip_index=True)

    assert fsynced == [session_dir]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract")
def test_sidecar_directory_fsync_tolerates_only_unsupported_filesystems(
    tmp_path,
    monkeypatch,
):
    from api import models

    def unsupported(_fd):
        raise OSError(errno.EINVAL, "directory fsync unsupported")

    monkeypatch.setattr(models.os, "fsync", unsupported)
    models._fsync_sidecar_directory(tmp_path)

    def real_failure(_fd):
        raise OSError(errno.EIO, "durability failure")

    monkeypatch.setattr(models.os, "fsync", real_failure)
    with pytest.raises(OSError, match="durability failure"):
        models._fsync_sidecar_directory(tmp_path)

    def permission_failure(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "directory open denied")

    monkeypatch.setattr(models.os, "open", permission_failure)
    with pytest.raises(PermissionError, match="directory open denied"):
        models._fsync_sidecar_directory(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract")
def test_shrink_publications_fsync_archive_backup_then_live(
    tmp_path,
    monkeypatch,
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    session = models.Session(
        session_id="durable-shrink-publication",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    )
    session.save(skip_index=True)
    backup_path = session.path.with_suffix(".json.bak")
    malformed = b'{"broken":'
    backup_path.write_bytes(malformed)
    session.messages = session.messages[:1]
    events = []
    real_replace = models._safe_replace

    def record_replace(source, destination):
        events.append(("replace", Path(destination).name))
        return real_replace(source, destination)

    monkeypatch.setattr(models, "_safe_replace", record_replace)
    monkeypatch.setattr(
        models,
        "_fsync_sidecar_directory",
        lambda directory: events.append(("fsync", Path(directory).name)),
        raising=False,
    )

    session.save(skip_index=True)

    archive_name = (
        f"{backup_path.name}.archive-{hashlib.sha256(malformed).hexdigest()}"
    )
    assert events == [
        ("replace", archive_name),
        ("fsync", session_dir.name),
        ("replace", backup_path.name),
        ("fsync", session_dir.name),
        ("replace", session.path.name),
        ("fsync", session_dir.name),
    ]


def test_non_object_backup_is_archived_before_live_snapshot_promotion(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "non-object-backup-recovery"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ],
    )
    session.save(skip_index=True)
    before_shrink = session.path.read_bytes()
    unusable = b"[]"
    backup_path = session.path.with_suffix(".json.bak")
    backup_path.write_bytes(unusable)
    session.messages = session.messages[:1]

    session.save(skip_index=True)

    archive = backup_path.with_name(
        f"{backup_path.name}.archive-{hashlib.sha256(unusable).hexdigest()}"
    )
    assert archive.read_bytes() == unusable
    assert backup_path.read_bytes() == before_shrink


def test_foreign_sid_backup_is_archived_and_replaced_before_shrink(
    tmp_path, monkeypatch
):
    from api import models, session_recovery

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "foreign-backup-owner"
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=messages,
    )
    session.save(skip_index=True)
    foreign_bytes = json.dumps(
        {"session_id": "foreign-owner", "messages": messages},
        ensure_ascii=False,
    ).encode("utf-8")
    backup_path = session.path.with_suffix(".json.bak")
    backup_path.write_bytes(foreign_bytes)
    session.messages = messages[:1]

    session.save(skip_index=True)

    archive = backup_path.with_name(
        f"{backup_path.name}.archive-{hashlib.sha256(foreign_bytes).hexdigest()}"
    )
    assert archive.read_bytes() == foreign_bytes
    primary_backup = json.loads(backup_path.read_text(encoding="utf-8"))
    assert primary_backup["session_id"] == sid
    result = session_recovery.recover_session(session.path)
    assert result["restored"] is True
    restored = json.loads(session.path.read_text(encoding="utf-8"))
    assert restored["messages"] == messages


def test_workspace_patch_does_not_grant_stale_alias_new_revision(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "workspace-stale-alias"
    original_workspace = tmp_path / "before"
    recovered_workspace = tmp_path / "after"
    seed = models.Session(
        session_id=sid,
        workspace=str(original_workspace),
        messages=[{"role": "user", "content": "seed"}],
    )
    seed.save(skip_index=True)
    stale = models.Session.load(sid)
    newer = models.Session.load(sid)
    assert stale is not None and newer is not None
    newer.messages.append({"role": "assistant", "content": "newer"})
    newer.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS[sid] = stale

    current = models.persist_recovered_workspace_binding(
        stale,
        recovered_workspace,
        expected_workspace=str(original_workspace.resolve()),
    )

    assert current is not stale
    assert current.messages[-1]["content"] == "newer"
    assert current.workspace == str(recovered_workspace.resolve())
    stale.title = "must not own current revision"
    with pytest.raises(models.StaleSessionGenerationError):
        stale.save(skip_index=True)
    persisted = json.loads(seed.path.read_text(encoding="utf-8"))
    assert persisted["messages"][-1]["content"] == "newer"


def test_incomparable_backup_is_archived_before_latest_snapshot_promotion(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "incomparable-backup"
    a = {"role": "user", "content": "A"}
    unique = {"role": "assistant", "content": "UNIQUE-U"}
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[a, unique],
    )
    session.save(skip_index=True)
    session.messages = [a]
    session.save(skip_index=True)
    session.messages = [
        a,
        {"role": "assistant", "content": "D1"},
        {"role": "user", "content": "D2"},
    ]
    session.save(skip_index=True)

    session.messages = [a]
    session.save(skip_index=True)

    live = json.loads(session.path.read_text(encoding="utf-8"))
    backup_path = session.path.with_suffix(".json.bak")
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    archives = list(session_dir.glob(f"{backup_path.name}.archive-*"))
    assert [row["content"] for row in live["messages"]] == ["A"]
    assert [row["content"] for row in backup["messages"]] == ["A", "D1", "D2"]
    assert len(archives) == 1
    archived = json.loads(archives[0].read_text(encoding="utf-8"))
    assert [row["content"] for row in archived["messages"]] == ["A", "UNIQUE-U"]


def test_backup_dominance_preserves_complete_legacy_snapshot_payload(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "backup-complete-snapshot"
    first = {"role": "user", "content": "first"}
    second = {"role": "assistant", "content": "second"}
    third = {"role": "user", "content": "third"}
    unique_tool_call = {"id": "unique-old-tool-call", "name": "must-survive"}
    unique_context = {"role": "system", "content": "unique old model context"}
    unique_draft = {"text": "unique old draft", "attachments": ["draft.txt"]}
    unique_future_metadata = {"opaque": ["future", "must-survive"]}
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[first, second],
        context_messages=[first, unique_context],
        tool_calls=[unique_tool_call],
        composer_draft=unique_draft,
    )
    session.save(touch_updated_at=False, skip_index=True)

    session.messages = [first]
    session.context_messages = [first]
    session.tool_calls = []
    session.composer_draft = {}
    session.save(touch_updated_at=False, skip_index=True)
    backup_path = session.path.with_suffix(".json.bak")

    # Legacy backups predate the generation/count/fingerprint metadata. Their
    # remaining durable payload must participate in exactly the same dominance
    # decision as a current sidecar.
    legacy_backup = json.loads(backup_path.read_text(encoding="utf-8"))
    for derived_key in (
        "_sidecar_generation_v1",
        "message_count",
        "anchor_scene_index",
    ):
        legacy_backup.pop(derived_key, None)
    legacy_backup["future_recovery_metadata"] = unique_future_metadata
    backup_path.write_text(
        json.dumps(legacy_backup, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Message-only dominance considers this live snapshot richer, although it
    # lacks all three uniquely recoverable non-transcript artifacts.
    session.messages = [first, second, third]
    session.context_messages = [first, third]
    session.save(touch_updated_at=False, skip_index=True)
    session.messages = [first]
    session.context_messages = [first]
    session.save(touch_updated_at=False, skip_index=True)

    recoverable_payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in [backup_path, *session_dir.glob(f"{backup_path.name}.archive-*")]
    ]
    assert any(
        unique_tool_call in (payload.get("tool_calls") or [])
        and unique_context in (payload.get("context_messages") or [])
        and payload.get("composer_draft") == unique_draft
        and payload.get("future_recovery_metadata") == unique_future_metadata
        for payload in recoverable_payloads
    )


def test_backup_dominance_preserves_message_order():
    from api.models import _ordered_json_rows_cover

    first = {"role": "user", "content": "first"}
    second = {"role": "assistant", "content": "second"}

    assert _ordered_json_rows_cover([first, second], [first]) is True
    assert _ordered_json_rows_cover([second, first], [first, second]) is False


def test_backup_snapshot_dominance_budget_exhaustion_fails_closed(monkeypatch):
    from api import models

    baseline = {
        "session_id": "bounded-backup",
        "messages": [{"role": "user", "content": "x" * 128}],
        "future_metadata": {"opaque": "y" * 128},
    }
    candidate = {
        **baseline,
        "messages": [
            *baseline["messages"],
            {"role": "assistant", "content": "new"},
        ],
    }

    assert models._session_snapshot_covers(
        candidate,
        baseline,
        max_canonical_bytes=64,
    ) is False

    boundary = {"future_metadata": "x" * 7}
    assert models._session_snapshot_covers(
        boundary,
        boundary,
        max_canonical_bytes=18,
    ) is True

    real_dumps = models.json.dumps
    dumps_calls = []

    def counted_dumps(value, *args, **kwargs):
        dumps_calls.append(value)
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(models.json, "dumps", counted_dumps)
    assert models._session_snapshot_covers(
        boundary,
        boundary,
        max_canonical_bytes=17,
    ) is False
    assert len(dumps_calls) == 1

    oversized = "z" * (4 * 1024 * 1024)
    deeply_nested = None
    # Top-level list-valued fields compare their rows independently, so one
    # layer is consumed by the ordered-subsequence traversal before preflight.
    for _ in range(66):
        deeply_nested = [deeply_nested]
    cyclic = []
    cyclic.append(cyclic)
    too_many_items = {"one": 1, "two": 2, "three": 3}
    lone_surrogate = "\ud800"

    def reject_unproven_serialization(value, *args, **kwargs):
        if (
            value is oversized
            or value is deeply_nested
            or value is cyclic
            or value is too_many_items
            or value is lone_surrogate
            or value == b"opaque"
        ):
            pytest.fail("out-of-budget or non-canonical value reached json.dumps")
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(models.json, "dumps", reject_unproven_serialization)
    assert models._session_snapshot_covers(
        {"future_metadata": oversized},
        {"future_metadata": oversized},
        max_canonical_bytes=1024 * 1024,
    ) is False
    assert models._session_snapshot_covers(
        {"future_metadata": deeply_nested},
        {"future_metadata": deeply_nested},
    ) is False
    assert models._session_snapshot_covers(
        {"future_metadata": b"opaque"},
        {"future_metadata": b"opaque"},
    ) is False
    assert models._session_snapshot_covers(
        {"future_metadata": {"cycle": cyclic}},
        {"future_metadata": {"cycle": cyclic}},
    ) is False
    assert models._session_snapshot_covers(
        {"future_metadata": lone_surrogate},
        {"future_metadata": lone_surrogate},
    ) is False

    monkeypatch.setattr(models, "_BACKUP_SNAPSHOT_DOMINANCE_MAX_ITEMS", 2)
    assert models._session_snapshot_covers(
        {"future_metadata": too_many_items},
        {"future_metadata": too_many_items},
    ) is False

    def memory_error(*_args, **_kwargs):
        raise MemoryError

    monkeypatch.setattr(models.json, "dumps", memory_error)
    assert models._session_snapshot_covers(
        {"future_metadata": "small"},
        {"future_metadata": "small"},
    ) is False


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux RSS and RLIMIT_AS")
def test_backup_snapshot_dominance_oversize_preflight_bounds_memory():
    repo_root = Path(__file__).resolve().parents[1]
    probe = r'''import json
import resource

from api.models import _session_snapshot_covers

payload = "x" * (80 * 1024 * 1024)
snapshot = {"future_unknown_metadata": payload}
rss_before_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
with open("/proc/self/status", encoding="utf-8") as status_file:
    vm_size_kib = next(
        int(line.split()[1])
        for line in status_file
        if line.startswith("VmSize:")
    )
headroom = 96 * 1024 * 1024
_, hard_limit = resource.getrlimit(resource.RLIMIT_AS)
soft_limit = vm_size_kib * 1024 + headroom
if hard_limit != resource.RLIM_INFINITY:
    soft_limit = min(soft_limit, hard_limit)
resource.setrlimit(
    resource.RLIMIT_AS,
    (soft_limit, hard_limit),
)
try:
    result = _session_snapshot_covers(snapshot, snapshot)
except BaseException as exc:
    outcome = type(exc).__name__
else:
    outcome = repr(result)
rss_after_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({
    "outcome": outcome,
    "rss_peak_delta_mib": (rss_after_kib - rss_before_kib) / 1024,
}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["outcome"] == "False"
    assert report["rss_peak_delta_mib"] < 16


def test_backup_retirement_requires_live_revision_to_still_match(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "cleanup-live-race"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ],
    )
    session.save(skip_index=True)
    session.messages = session.messages[:1]
    backup_receipt = session.save(skip_index=True)
    committed_receipt = models._read_sidecar_revision(session.path, sid)
    session.messages.append({"role": "assistant", "content": "new generation"})
    session.save(skip_index=True)

    assert models._retire_backup_if_owned(
        sid,
        session.path.with_suffix(".json.bak"),
        backup_receipt,
        committed_receipt,
    ) is False
    assert session.path.with_suffix(".json.bak").exists()


def test_same_count_external_metadata_update_reloads_cached_owner(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "same-count-metadata"
    seed = models.Session(
        session_id=sid,
        title="before",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "same transcript"}],
    )
    seed.save(skip_index=True)
    cached = models.Session.load(sid)
    external = models.Session.load(sid)
    assert cached is not None and external is not None
    with models.LOCK:
        models.SESSIONS[sid] = cached
    external.title = "after"
    external.save(skip_index=True)

    loaded = models.get_session(sid)

    assert loaded is not cached
    assert loaded.title == "after"
    assert loaded.messages == cached.messages


def test_modern_cache_freshness_uses_prefix_generation_not_full_digest(
    tmp_path, monkeypatch
):
    from api import models

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    session = models.Session(
        session_id="prefix-generation",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "large sidecar proxy"}],
    )
    session.save(skip_index=True)
    cached = models.Session.load(session.session_id)
    assert cached is not None

    monkeypatch.setattr(
        models,
        "_read_sidecar_revision",
        lambda *_args, **_kwargs: pytest.fail("cache hit hashed the full sidecar"),
    )

    assert models._cached_session_lags_disk(cached) is False


def test_recovery_rejects_backup_with_foreign_embedded_sid(tmp_path, monkeypatch):
    from api import models, session_recovery

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "expected-sid"
    session_path = session_dir / f"{sid}.json"
    live = {
        "session_id": sid,
        "messages": [{"role": "user", "content": "live"}],
    }
    foreign = {
        "session_id": "foreign-sid",
        "messages": [
            {"role": "user", "content": "foreign one"},
            {"role": "assistant", "content": "foreign two"},
        ],
    }
    session_path.write_text(json.dumps(live), encoding="utf-8")
    session_path.with_suffix(".json.bak").write_text(
        json.dumps(foreign), encoding="utf-8"
    )

    result = session_recovery.recover_session(session_path)

    assert result["restored"] is False
    assert "session id" in result["error"].lower()
    assert json.loads(session_path.read_text(encoding="utf-8")) == live


def test_create_only_publish_uses_native_windows_rename_without_hardlinks(
    tmp_path, monkeypatch
):
    from api import models

    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b'{"complete": true}')

    def unsupported_link(_source, _destination):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    real_rename = models.os.rename
    rename_calls = []

    def create_only_rename(rename_source, rename_destination):
        rename_calls.append((rename_source, rename_destination))
        assert not destination.exists()
        real_rename(rename_source, rename_destination)

    monkeypatch.setattr(models.os, "link", unsupported_link)
    monkeypatch.setattr(models.os, "rename", create_only_rename)
    monkeypatch.setattr(models.os, "name", "nt")

    models._publish_sidecar_no_replace(source, destination)

    assert rename_calls == [(source, destination)]
    assert destination.read_bytes() == b'{"complete": true}'
    assert not source.exists()


def test_orphan_recovery_rechecks_delete_tombstone_under_authority(
    tmp_path, monkeypatch
):
    from api import models, session_recovery

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "deleted-before-recovery"
    session_path = session_dir / f"{sid}.json"
    session_path.with_suffix(".json.bak").write_text(
        json.dumps(
            {
                "session_id": sid,
                "messages": [{"role": "user", "content": "deleted"}],
            }
        ),
        encoding="utf-8",
    )
    models._record_webui_deleted_session_tombstone(sid)

    result = session_recovery.recover_session(session_path)

    assert result["restored"] is False
    assert result.get("deleted") is True
    assert not session_path.exists()


def test_state_db_materialization_rechecks_delete_inside_authority(
    tmp_path, monkeypatch
):
    from api import models, session_recovery

    session_dir = tmp_path / "sessions"
    _patch_store(monkeypatch, models, session_dir)
    sid = "deleted-during-reconcile"
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
            "model TEXT, started_at REAL, message_count INTEGER)"
        )
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
            "role TEXT, content TEXT, timestamp REAL)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, 'webui', 'deleted', 'model', 1, 1)",
            (sid,),
        )
        conn.execute(
            "INSERT INTO messages VALUES (1, ?, 'user', 'deleted', 1)",
            (sid,),
        )

    real_authority = models._session_sidecar_authority

    @contextmanager
    def delete_before_authority_yields(session_id, *, session_dir=None):
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        models._record_webui_deleted_session_tombstone(session_id)
        with real_authority(session_id, session_dir=session_dir):
            yield

    monkeypatch.setattr(
        models, "_session_sidecar_authority", delete_before_authority_yields
    )

    result = session_recovery.recover_missing_sidecars_from_state_db(
        session_dir, db_path
    )

    assert result["materialized"] == 0
    assert not (session_dir / f"{sid}.json").exists()
