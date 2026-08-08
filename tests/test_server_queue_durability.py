"""Behavioral coverage for the server-owned WebUI queue."""

from __future__ import annotations

import io
import json
import copy
import threading
import time
import zipfile
from pathlib import Path

from api import models, routes
from api.models import Session


_REAL_THREAD = threading.Thread


class _JSONHandler:
    headers = {}

    def __init__(self):
        self.status = None
        self.headers = {}
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()
        self.headers_sent = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers_sent[key] = value

    def end_headers(self):
        pass


def _payload(handler):
    raw = handler.wfile.getvalue().decode("utf-8")
    return json.loads(raw) if raw else {}


def _setup(monkeypatch, tmp_path, *, active_profile="default"):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    models.SESSIONS.clear()
    routes.SESSIONS.clear()
    routes.STREAMS.clear()

    monkeypatch.setattr(
        routes,
        "_session_visible_to_active_profile",
        lambda profile, handler=None: handler is None or profile == active_profile,
    )
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda _sid, session: session)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_args, **_kwargs: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_repair_foreign_session_model_provider",
        lambda _session, **kwargs: kwargs["resolved_provider"],
    )
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _config: False)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "register_stream_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "unregister_stream_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "_fanout_server_turn_started", lambda *_args, **_kwargs: None)

    from api import config, upload

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    attachment_root = tmp_path / "attachments"
    monkeypatch.setattr(upload, "_attachment_root", lambda: attachment_root)
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **_kwargs: (model or "gpt-4o", provider or "openai", False),
    )
    return session_dir, attachment_root


def _new_session(sid, workspace, *, profile="default", queue=None):
    session = Session(
        session_id=sid,
        title="Queue test",
        workspace=str(workspace),
        profile=profile,
        model="gpt-4o",
        model_provider="openai",
        messages=[{"role": "user", "content": "seed"}],
        queue=queue,
    )
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session
    return session


def _queue_action(body):
    handler = _JSONHandler()
    routes._handle_chat_queue(handler, body)
    return handler.status, _payload(handler)


def _upload_action(monkeypatch, handler_fn, session_id, filename, content, *, patch_workspace=True):
    from api import upload

    monkeypatch.setattr(
        upload,
        "parse_multipart",
        lambda *_args: ({"session_id": session_id}, {"file": (filename, content)}),
    )
    monkeypatch.setattr(upload, "_get_active_profile_name", lambda: "default")
    if patch_workspace:
        monkeypatch.setattr(upload, "resolve_trusted_workspace", lambda path: Path(path).resolve())
    handler = _JSONHandler()
    handler_fn(handler)
    return handler.status, _payload(handler)


def _uploaded_file(root, sid, name="notes.txt"):
    destination = root / sid
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / name
    path.write_text("uploaded", encoding="utf-8")
    return {
        "name": name,
        "path": str(path),
        "mime": "text/plain",
        "size": path.stat().st_size,
        "is_image": False,
    }


def _install_start_stubs(monkeypatch, *, thread_start=None):
    class _Worker:
        starts = []

        def __init__(self, target, args, kwargs, daemon):
            self.target = target
            self.args = args
            self.kwargs = kwargs
            self.daemon = daemon

        def start(self):
            self.starts.append((self.target, self.args, self.kwargs))
            if thread_start is not None:
                thread_start()

    monkeypatch.setattr(routes.threading, "Thread", _Worker)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: object())
    monkeypatch.setattr(
        routes,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: {},
        raising=False,
    )
    return _Worker


def _disable_enqueue_drain(monkeypatch):
    monkeypatch.setattr(
        routes,
        "drain_queued_session_turn",
        lambda _sid: {"accepted": False, "reason": "test"},
    )


def _archived_queue_target(monkeypatch, tmp_path, *, active_run_age):
    _setup(monkeypatch, tmp_path)
    from api import config

    archived = _new_session("queue-archived-a", tmp_path)
    archived.pre_compression_snapshot = True
    archived.queue = []
    archived.save()
    live = _new_session("queue-live-b", tmp_path)
    live.parent_session_id = archived.session_id
    live.active_stream_id = "compression-stream"
    live.pending_user_message = "current turn"
    live.save()
    routes.STREAMS[live.active_stream_id] = object()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS["archived-run"] = {
            "session_id": archived.session_id,
            "stream_id": "archived-run",
            "started_at": time.time() - active_run_age,
        }
    worker = _install_start_stubs(monkeypatch)
    return archived, live, worker


def _settle_session(session):
    settled = models.Session.load(session.session_id)
    settled.active_stream_id = None
    settled.pending_user_message = None
    settled.pending_attachments = []
    settled.pending_started_at = None
    settled.pending_user_source = None
    settled.save()
    models.SESSIONS[session.session_id] = settled
    routes.SESSIONS[session.session_id] = settled


def test_session_queue_survives_sidecar_reload(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    session = _new_session("queue-reload", tmp_path)
    session.queue = [{"id": "item-1", "text": "continue", "files": []}]
    session.save()

    reloaded = models.Session.load(session.session_id)

    assert reloaded is not None
    assert reloaded.queue == session.queue


def test_enqueue_idle_session_immediately_starts_one_worker_and_claims_item(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session("queue-idle-enqueue", tmp_path)
    worker = _install_start_stubs(monkeypatch)

    status, response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "start now",
            "model": "queued-model",
            "model_provider": "queued-provider",
        }
    )

    assert status == 200
    assert response["accepted"] is True
    assert response["item"]["text"] == "start now"
    assert response["queue"] == []
    persisted = models.Session.load(session.session_id)
    assert persisted.queue == []
    assert persisted.pending_user_message == "start now"
    assert persisted.active_stream_id
    assert len(worker.starts) == 1


def test_enqueue_repairs_teardown_empty_check_gap(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session("queue-empty-gap", tmp_path)
    worker = _install_start_stubs(monkeypatch)
    real_drain = routes.drain_queued_session_turn
    empty_checked = threading.Event()
    release_empty_check = threading.Event()
    enqueue_done = threading.Event()
    enqueue_result = {}

    def teardown_drain(sid):
        if not empty_checked.is_set():
            assert not models.Session.load(sid).queue
            empty_checked.set()
            assert release_empty_check.wait(timeout=5)
            return {"accepted": False, "reason": "empty", "session_id": sid}
        return real_drain(sid)

    monkeypatch.setattr(routes, "drain_queued_session_turn", teardown_drain)
    teardown = _REAL_THREAD(
        target=lambda: routes.drain_queued_session_turn(session.session_id),
    )
    teardown.start()
    assert empty_checked.wait(timeout=5)

    def enqueue():
        enqueue_result["value"] = _queue_action(
            {
                "session_id": session.session_id,
                "action": "enqueue",
                "text": "arrived after empty check",
            }
        )
        enqueue_done.set()

    enqueue_thread = _REAL_THREAD(target=enqueue)
    enqueue_thread.start()
    assert enqueue_done.wait(timeout=5)
    release_empty_check.set()
    teardown.join(timeout=5)
    enqueue_thread.join(timeout=5)

    assert not teardown.is_alive() and not enqueue_thread.is_alive()
    assert enqueue_result["value"][0] == 200
    assert len(worker.starts) == 1
    persisted = models.Session.load(session.session_id)
    assert persisted.queue == []
    assert persisted.pending_user_message == "arrived after empty check"


def test_immediate_thread_failure_keeps_accepted_intent_recoverable(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session("queue-immediate-failure", tmp_path)

    def fail_thread_start():
        raise RuntimeError("thread start failed")

    worker = _install_start_stubs(monkeypatch, thread_start=fail_thread_start)
    status, response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "recover this",
        }
    )

    assert status == 200
    assert response["accepted"] is True
    persisted = models.Session.load(session.session_id)
    assert persisted.queue or persisted.active_stream_id or persisted.pending_user_message
    assert persisted.queue == [] or persisted.queue[0]["text"] == "recover this"
    assert len(worker.starts) == 1


def test_active_enqueue_waits_for_later_teardown_drain(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session("queue-active-enqueue", tmp_path)
    session.active_stream_id = "active-stream"
    session.pending_user_message = "current turn"
    session.save()
    routes.STREAMS["active-stream"] = object()
    worker = _install_start_stubs(monkeypatch)
    real_drain = routes.drain_queued_session_turn
    drain_results = []

    def capture_drain(sid):
        result = real_drain(sid)
        drain_results.append(result)
        return result

    monkeypatch.setattr(routes, "drain_queued_session_turn", capture_drain)

    status, response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "next turn",
        }
    )

    assert status == 200
    item_id = response["item"]["id"]
    assert drain_results[0]["_status"] == 409
    assert [entry["id"] for entry in response["queue"]] == [item_id]
    assert not worker.starts

    routes.STREAMS.pop("active-stream")
    settled = models.Session.load(session.session_id)
    settled.active_stream_id = None
    settled.pending_user_message = None
    settled.pending_attachments = []
    settled.pending_started_at = None
    settled.pending_user_source = None
    settled.save()
    models.SESSIONS[session.session_id] = settled
    routes.SESSIONS[session.session_id] = settled

    started = routes.drain_queued_session_turn(session.session_id)

    assert started["stream_id"]
    assert len(worker.starts) == 1
    assert models.Session.load(session.session_id).queue == []
    assert models.Session.load(session.session_id).pending_user_message == "next turn"


def test_concurrent_enqueue_and_teardown_drains_start_one_worker(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session("queue-immediate-teardown-race", tmp_path)
    worker = _install_start_stubs(monkeypatch)
    real_drain = routes.drain_queued_session_turn
    both_entered = threading.Barrier(2)
    enqueue_result = {}
    teardown_result = {}

    def concurrent_drain(sid):
        both_entered.wait(timeout=5)
        return real_drain(sid)

    monkeypatch.setattr(routes, "drain_queued_session_turn", concurrent_drain)

    teardown = _REAL_THREAD(
        target=lambda: teardown_result.setdefault(
            "value", routes.drain_queued_session_turn(session.session_id)
        ),
    )

    def enqueue():
        enqueue_result["value"] = _queue_action(
            {
                "session_id": session.session_id,
                "action": "enqueue",
                "text": "one concurrent turn",
            }
        )

    enqueue_thread = _REAL_THREAD(target=enqueue)
    teardown.start()
    enqueue_thread.start()
    teardown.join(timeout=5)
    enqueue_thread.join(timeout=5)

    assert not teardown.is_alive() and not enqueue_thread.is_alive()
    assert enqueue_result["value"][0] == 200
    assert teardown_result["value"].get("_status", 200) in {200, 409}
    assert len(worker.starts) == 1
    persisted = models.Session.load(session.session_id)
    assert persisted.queue == []
    assert persisted.pending_user_message == "one concurrent turn"


def test_queue_actions_are_server_authoritative_and_persist(monkeypatch, tmp_path):
    session_dir, attachment_root = _setup(monkeypatch, tmp_path)
    _disable_enqueue_drain(monkeypatch)
    session = _new_session("queue-actions", tmp_path)
    first_file = _uploaded_file(attachment_root, session.session_id, "first.txt")

    status, first_response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "first",
            "files": [first_file],
            "model": "gpt-4o",
            "model_provider": "openai",
            "profile": "wrong-client-profile",
        }
    )
    assert status == 200
    first = first_response["item"]
    assert first["id"]
    assert first["files"] == [first_file | {"path": str(Path(first_file["path"]).resolve())}]
    assert first["model"] == "gpt-4o"
    assert first["model_provider"] == "openai"
    assert "profile" not in first

    status, second_response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "second",
            "model": "gpt-4o-mini",
            "model_provider": "openai",
        }
    )
    assert status == 200
    second = second_response["item"]
    ids = [first["id"], second["id"]]

    status, edited = _queue_action(
        {
            "session_id": session.session_id,
            "action": "edit",
            "item_id": first["id"],
            "text": "edited",
        }
    )
    assert status == 200
    assert edited["item"]["text"] == "edited"
    assert edited["item"]["files"] == first["files"]
    assert edited["item"]["model"] == first["model"]

    status, _ = _queue_action(
        {
            "session_id": session.session_id,
            "action": "edit",
            "item_id": first["id"],
            "text": "must reject metadata mutation",
            "files": [],
        }
    )
    assert status == 400
    assert models.Session.load(session.session_id).queue[0]["files"] == first["files"]

    status, reordered = _queue_action(
        {
            "session_id": session.session_id,
            "action": "reorder",
            "item_ids": list(reversed(ids)),
        }
    )
    assert status == 200
    assert [entry["id"] for entry in reordered["queue"]] == [second["id"], first["id"]]

    reloaded = models.Session.load(session.session_id)
    assert [entry["id"] for entry in reloaded.queue] == [second["id"], first["id"]]

    status, deleted = _queue_action(
        {"session_id": session.session_id, "action": "delete", "item_id": second["id"]}
    )
    assert status == 200
    assert [entry["id"] for entry in deleted["queue"]] == [first["id"]]

    status, cleared = _queue_action({"session_id": session.session_id, "action": "clear"})
    assert status == 200
    assert cleared["queue"] == []
    assert models.Session.load(session.session_id).queue == []
    assert (session_dir / f"{session.session_id}.json").exists()


def test_combine_drops_all_attachments_and_keeps_order(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _disable_enqueue_drain(monkeypatch)
    session = _new_session("queue-combine", tmp_path)
    first_status, first_response = _queue_action(
        {"session_id": session.session_id, "action": "enqueue", "text": "one", "model": "gpt-4o", "model_provider": "openai"}
    )
    assert first_status == 200
    attachment = _uploaded_file(tmp_path / "attachments", session.session_id)
    second_status, second_response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "two",
            "files": [attachment],
            "model": "gpt-4o",
            "model_provider": "openai",
        }
    )
    assert second_status == 200
    assert first_response["item"]["id"] != second_response["item"]["id"]

    status, response = _queue_action({"session_id": session.session_id, "action": "combine"})

    assert status == 200
    assert len(response["queue"]) == 1
    combined = response["queue"][0]
    assert combined["text"] == "one\n\ntwo"
    assert combined["files"] == []
    assert models.Session.load(session.session_id).queue == response["queue"]


def test_queue_rejects_unuploaded_attachment_metadata(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session("queue-upload-boundary", tmp_path)

    status, response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "outside",
            "files": [{"name": "secret.txt", "path": str(tmp_path / "secret.txt")}],
            "model": "gpt-4o",
            "model_provider": "openai",
        }
    )

    assert status == 400
    assert "uploaded" in response["error"].lower() or "session" in response["error"].lower()
    assert models.Session.load(session.session_id).queue == []


def test_enqueue_uses_compatible_model_resolver_and_captures_choice(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _disable_enqueue_drain(monkeypatch)
    session = _new_session("queue-model-choice", tmp_path)
    calls = []

    def resolve(model, provider, **kwargs):
        calls.append((model, provider, kwargs))
        return "resolved-model", "resolved-provider", True

    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", resolve)
    status, response = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "preserve route",
            "model": "client-model",
            "model_provider": "client-provider",
        }
    )

    assert status == 200
    assert calls and calls[0][0:2] == ("client-model", "client-provider")
    assert response["item"]["model"] == "resolved-model"
    assert response["item"]["model_provider"] == "resolved-provider"


def test_queue_profile_scope_rejects_cross_profile_without_retagging(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, active_profile="default")
    _disable_enqueue_drain(monkeypatch)
    own = _new_session("queue-own-profile", tmp_path, profile="default")
    other = _new_session("queue-other-profile", tmp_path, profile="other")

    own_status, _ = _queue_action(
        {"session_id": own.session_id, "action": "enqueue", "text": "allowed", "profile": "other"}
    )
    other_status, other_response = _queue_action(
        {"session_id": other.session_id, "action": "enqueue", "text": "blocked", "profile": "default"}
    )

    assert own_status == 200
    assert other_status == 404
    assert other_response["error"] == "Session not found"
    assert models.Session.load(other.session_id).queue == []
    assert models.Session.load(other.session_id).profile == "other"


def test_snapshot_enqueue_resolves_to_live_continuation_with_young_archived_run(monkeypatch, tmp_path):
    archived, live, worker = _archived_queue_target(monkeypatch, tmp_path, active_run_age=30)

    status, response = _queue_action(
        {"session_id": archived.session_id, "action": "enqueue", "text": "after compression"}
    )

    assert status == 200
    assert response["session_id"] == live.session_id
    assert [entry["text"] for entry in response["queue"]] == ["after compression"]
    assert models.Session.load(archived.session_id).queue == []
    assert not worker.starts

    routes.STREAMS.pop(live.active_stream_id)
    _settle_session(live)
    started = routes.drain_queued_session_turn(live.session_id)

    assert started["stream_id"]
    assert len(worker.starts) == 1
    assert routes.drain_queued_session_turn(archived.session_id)["_status"] == 409
    assert len(worker.starts) == 1
    assert models.Session.load(archived.session_id).queue == []


def test_snapshot_enqueue_resolves_to_live_continuation_with_stale_archived_run(monkeypatch, tmp_path):
    archived, live, worker = _archived_queue_target(monkeypatch, tmp_path, active_run_age=181)

    status, response = _queue_action(
        {"session_id": archived.session_id, "action": "enqueue", "text": "still live only"}
    )

    assert status == 200
    assert response["session_id"] == live.session_id
    assert [entry["text"] for entry in response["queue"]] == ["still live only"]
    assert models.Session.load(archived.session_id).queue == []
    assert not worker.starts

    routes.STREAMS.pop(live.active_stream_id)
    _settle_session(live)
    assert routes.drain_queued_session_turn(live.session_id)["stream_id"]
    assert len(worker.starts) == 1
    assert routes.drain_queued_session_turn(archived.session_id)["_status"] == 409
    assert len(worker.starts) == 1


def test_snapshot_edit_mutates_live_continuation_not_archived_snapshot(monkeypatch, tmp_path):
    archived, live, worker = _archived_queue_target(monkeypatch, tmp_path, active_run_age=30)
    item = {"id": "live-item", "text": "before", "files": [], "model": "gpt-4o", "model_provider": "openai"}
    live.queue = [item]
    live.save()

    status, response = _queue_action(
        {
            "session_id": archived.session_id,
            "action": "edit",
            "item_id": item["id"],
            "text": "after",
        }
    )

    assert status == 200
    assert response["session_id"] == live.session_id
    assert response["item"]["text"] == "after"
    assert models.Session.load(live.session_id).queue[0]["text"] == "after"
    assert models.Session.load(archived.session_id).queue == []
    assert not worker.starts


def test_snapshot_enqueue_validates_attachment_path_against_live_continuation(monkeypatch, tmp_path):
    archived, live, _worker = _archived_queue_target(monkeypatch, tmp_path, active_run_age=30)
    live_file = _uploaded_file(tmp_path / "attachments", live.session_id, "live.txt")

    status, response = _queue_action(
        {
            "session_id": archived.session_id,
            "action": "enqueue",
            "text": "inspect live upload",
            "files": [live_file],
        }
    )

    assert status == 200
    assert response["session_id"] == live.session_id
    assert response["item"]["files"][0]["path"] == str(Path(live_file["path"]).resolve())
    archived_file = _uploaded_file(tmp_path / "attachments", archived.session_id, "archive.txt")
    rejected_status, _ = _queue_action(
        {
            "session_id": archived.session_id,
            "action": "enqueue",
            "text": "wrong owner",
            "files": [archived_file],
        }
    )

    assert rejected_status == 400
    assert models.Session.load(archived.session_id).queue == []
    assert [entry["text"] for entry in models.Session.load(live.session_id).queue] == [
        "inspect live upload"
    ]


def test_drain_refuses_legacy_work_on_hidden_compression_snapshot(monkeypatch, tmp_path):
    archived, _live, worker = _archived_queue_target(monkeypatch, tmp_path, active_run_age=181)
    archived.queue = [{"id": "hidden-item", "text": "must stay hidden", "files": []}]
    archived.save()

    response = routes.drain_queued_session_turn(archived.session_id)

    assert response["_status"] == 409
    assert models.Session.load(archived.session_id).queue[0]["id"] == "hidden-item"
    assert not worker.starts


def test_archived_plain_upload_lands_on_continuation_and_queues_via_archive(monkeypatch, tmp_path):
    archived, live, _worker = _archived_queue_target(monkeypatch, tmp_path, active_run_age=30)
    from api import upload

    status, uploaded = _upload_action(
        monkeypatch,
        upload.handle_upload,
        archived.session_id,
        "note.txt",
        b"uploaded for the live continuation",
    )

    assert status == 200
    uploaded_path = Path(uploaded["path"])
    assert uploaded_path.is_file()
    assert uploaded_path.parent == upload._attachment_root() / live.session_id

    queue_status, queued = _queue_action(
        {
            "session_id": archived.session_id,
            "action": "enqueue",
            "text": "inspect note",
            "files": [uploaded],
        }
    )

    assert queue_status == 200
    assert queued["session_id"] == live.session_id
    assert queued["item"]["files"][0]["path"] == str(uploaded_path.resolve())


def test_extracted_archive_destination_uses_live_attachment_inbox_and_queues(monkeypatch, tmp_path):
    from api import upload

    real_attachment_root = upload._attachment_root
    archived, live, _worker = _archived_queue_target(monkeypatch, tmp_path, active_run_age=30)
    inbox = tmp_path.parent / f"configured-attachment-inbox-{tmp_path.name}"
    monkeypatch.setattr(upload, "_attachment_root", real_attachment_root)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(inbox))
    monkeypatch.setattr(
        upload,
        "resolve_trusted_workspace",
        lambda _path: (_ for _ in ()).throw(AssertionError("archive upload must not resolve workspace")),
    )

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("inside.txt", "archive payload")
    status, extracted = _upload_action(
        monkeypatch,
        upload.handle_upload_extract,
        archived.session_id,
        "bundle.zip",
        archive.getvalue(),
        patch_workspace=False,
    )

    assert status == 200
    destination = Path(extracted["dest"])
    assert destination.is_dir()
    attachment_root = upload._session_attachment_dir(live.session_id)
    assert destination.parent == attachment_root
    assert destination.is_relative_to(attachment_root)
    assert not destination.is_relative_to(Path(live.workspace).resolve())

    queue_status, queued = _queue_action(
        {
            "session_id": archived.session_id,
            "action": "enqueue",
            "text": "inspect archive",
            "files": [{"name": str(destination), "path": str(destination), "extracted": extracted["extracted"]}],
        }
    )

    assert queue_status == 200
    assert queued["session_id"] == live.session_id
    assert queued["item"]["files"][0]["path"] == str(destination)
    assert queued["item"]["files"][0]["extracted"] == extracted["extracted"]


def test_extracted_queue_metadata_rejects_attachment_root_escape_and_symlink_escape(monkeypatch, tmp_path):
    archived, live, _worker = _archived_queue_target(monkeypatch, tmp_path, active_run_age=30)
    outside = tmp_path.parent / "queue-forged-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    outside_dir = tmp_path.parent / "queue-forged-dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")
    from api import upload

    attachment_root = upload._session_attachment_dir(live.session_id)
    attachment_root.mkdir(parents=True, exist_ok=True)
    link = attachment_root / "queue-escape-link"
    link.symlink_to(outside_dir, target_is_directory=True)

    outside_status, _ = _queue_action(
        {
            "session_id": archived.session_id,
            "action": "enqueue",
            "text": "forged outside",
            "files": [{"name": "outside", "path": str(outside), "extracted": 1}],
        }
    )
    symlink_status, _ = _queue_action(
        {
            "session_id": archived.session_id,
            "action": "enqueue",
            "text": "forged symlink",
            "files": [{"name": "secret", "path": str(link / "secret.txt"), "extracted": 1}],
        }
    )

    assert outside_status == 400
    assert symlink_status == 400
    assert models.Session.load(live.session_id).queue == []


def test_archived_upload_rejects_cross_profile_continuation(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, active_profile="default")
    from api import upload

    archived = _new_session("queue-cross-profile-archive", tmp_path, profile="default")
    archived.pre_compression_snapshot = True
    archived.queue = []
    archived.save()
    foreign = _new_session("queue-cross-profile-live", tmp_path, profile="other")
    foreign.parent_session_id = archived.session_id
    foreign.save()

    status, response = _upload_action(
        monkeypatch,
        upload.handle_upload,
        archived.session_id,
        "foreign.txt",
        b"must not cross profile",
    )

    assert status == 409
    assert "continuation" in response["error"]
    assert not (upload._attachment_root() / archived.session_id).exists()


def test_runner_local_queue_endpoint_and_drain_fail_closed(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session("queue-runner-local", tmp_path)
    session.queue = [{"id": "item", "text": "keep", "files": []}]
    session.save()
    from api import runtime_adapter

    monkeypatch.setattr(runtime_adapter, "runtime_adapter_runner_enabled", lambda: True)
    status, response = _queue_action(
        {"session_id": session.session_id, "action": "enqueue", "text": "unsupported"}
    )

    assert status == 501
    assert "unsupported" in response["error"]
    drain_response = routes.drain_queued_session_turn(session.session_id)
    assert drain_response["_status"] == 501
    assert models.Session.load(session.session_id).queue[0]["id"] == "item"


def test_queued_prompt_is_composed_at_drain_and_metadata_is_unchanged(monkeypatch, tmp_path):
    _session_dir, attachment_root = _setup(monkeypatch, tmp_path)
    real_drain = routes.drain_queued_session_turn
    _disable_enqueue_drain(monkeypatch)
    session = _new_session("queue-prompt", tmp_path)
    attachment = _uploaded_file(attachment_root, session.session_id, "report.pdf")
    status, queued = _queue_action(
        {
            "session_id": session.session_id,
            "action": "enqueue",
            "text": "inspect this",
            "files": [attachment],
            "model": "queued-model",
            "model_provider": "queued-provider",
        }
    )
    assert status == 200
    item = queued["item"]
    captured = {}

    def fake_start(run_session, **kwargs):
        captured["session"] = run_session
        captured.update(kwargs)
        return {"stream_id": "queued-stream", "session_id": run_session.session_id, "_status": 200}

    monkeypatch.setattr(routes, "_start_run", fake_start)
    monkeypatch.setattr(routes, "drain_queued_session_turn", real_drain)

    response = routes.drain_queued_session_turn(session.session_id)

    assert response["stream_id"] == "queued-stream"
    assert captured["msg"] == "inspect this\n\n[Attached files: " + attachment["path"] + "]"
    assert captured["attachments"] == [attachment]
    assert captured["model"] == "queued-model"
    assert captured["model_provider"] == "queued-provider"
    assert captured["queue_item_id"] == item["id"]
    assert captured["session"].queue == [item]
    assert routes._compose_queued_turn_message("", [attachment]) == "I've uploaded 1 file(s): " + attachment["path"]


def test_atomic_queue_claim_removes_item_in_same_save(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session(
        "queue-atomic",
        tmp_path,
        queue=[{"id": "atomic-item", "text": "queued", "files": []}],
    )
    saves = []
    original_save = Session.save

    def save(current, *args, **kwargs):
        if current.session_id == session.session_id:
            saves.append((list(current.queue), current.active_stream_id, current.pending_user_message))
        return original_save(current, *args, **kwargs)

    monkeypatch.setattr(Session, "save", save)
    worker = _install_start_stubs(monkeypatch)

    response = routes._start_chat_stream_for_session(
        session,
        msg="stale input is replaced by the queue item",
        attachments=[],
        workspace=str(tmp_path),
        model="wrong-model",
        model_provider="wrong-provider",
        external_runtime_owned=False,
        queue_item_id="atomic-item",
    )

    assert response["stream_id"]
    assert len(saves) == 1
    assert saves[0][0] == []
    assert saves[0][2] == "queued"
    persisted = models.Session.load(session.session_id)
    assert persisted.queue == []
    assert persisted.active_stream_id == response["stream_id"]
    assert persisted.pending_user_message == "queued"
    assert len(worker.starts) == 1


def test_rejected_queue_claim_leaves_item_queued(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session(
        "queue-reject",
        tmp_path,
        queue=[{"id": "real-item", "text": "keep", "files": []}],
    )
    worker = _install_start_stubs(monkeypatch)

    response = routes._start_chat_stream_for_session(
        session,
        msg="wrong",
        attachments=[],
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider="openai",
        external_runtime_owned=False,
        queue_item_id="wrong-item",
    )

    assert response["_status"] == 409
    assert models.Session.load(session.session_id).queue[0]["id"] == "real-item"
    assert session.active_stream_id is None
    assert not worker.starts


def test_start_reloads_authoritative_queue_head_after_cache_replacement(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    authoritative = _new_session(
        "queue-authoritative-head",
        tmp_path,
        queue=[{"id": "disk", "text": "old disk head", "files": []}],
    )
    # The object currently installed in the locked cache is the mutation owner.
    # A stale caller object must not replace it, and the start path must not
    # silently downgrade to an older sidecar snapshot.
    authoritative.queue = [{"id": "authoritative", "text": "use this", "files": []}]
    stale = Session(
        session_id=authoritative.session_id,
        title=authoritative.title,
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider="openai",
        messages=list(authoritative.messages),
        queue=[{"id": "stale", "text": "never start this", "files": []}],
    )
    models.SESSIONS[authoritative.session_id] = authoritative
    routes.SESSIONS[authoritative.session_id] = authoritative
    worker = _install_start_stubs(monkeypatch)

    response = routes._start_chat_stream_for_session(
        stale,
        msg="stale caller prompt",
        attachments=[],
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider="openai",
        claim_queue_head=True,
        external_runtime_owned=False,
    )

    assert response["stream_id"]
    assert worker.starts[0][1][1] == "use this"
    persisted = models.Session.load(authoritative.session_id)
    assert persisted.queue == []
    assert persisted.pending_queue_item["id"] == "authoritative"
    metadata = models.Session.load_metadata_only(authoritative.session_id)
    assert metadata.pending_queue_item["id"] == "authoritative"


def test_cross_source_start_preserves_claimed_head_and_incoming_tail_sources(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    head = {
        "id": "human-head",
        "text": "human turn",
        "display_text": "human turn",
        "files": [],
        "model": "human-model",
        "model_provider": "human-provider",
        "source": "webui",
    }
    session = _new_session("queue-cross-source", tmp_path, queue=[head])
    worker = _install_start_stubs(monkeypatch)

    response = routes._start_chat_stream_for_session(
        session,
        msg="[IMPORTANT: process completed]",
        attachments=[],
        workspace=str(tmp_path),
        model="wakeup-model",
        model_provider="wakeup-provider",
        source="process_wakeup",
        display_text="[IMPORTANT: process completed]",
        external_runtime_owned=False,
    )

    persisted = models.Session.load(session.session_id)
    assert persisted is not None
    assert response["source"] == "webui"
    assert response["queue_item_id"] == "human-head"
    assert persisted.pending_user_source == "webui"
    assert persisted.pending_queue_item["source"] == "webui"
    assert persisted.queue[0]["source"] == "process_wakeup"
    assert worker.starts[0][1][1] == "human turn"


def test_direct_start_appends_retained_queue_tail_then_claims_head(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    head = {"id": "head", "text": "head turn", "files": [], "model": "head-model", "model_provider": "head-provider"}
    tail = {"id": "tail", "text": "tail turn", "files": [], "model": "tail-model", "model_provider": "tail-provider"}
    session = _new_session("queue-direct-order", tmp_path, queue=[head, tail])
    worker = _install_start_stubs(monkeypatch)

    response = routes._start_chat_stream_for_session(
        session,
        msg="new direct turn\n\n[Attached files: /attachments/new.txt]",
        attachments=[{"name": "new.txt", "path": "/attachments/new.txt"}],
        workspace=str(tmp_path),
        model="direct-model",
        model_provider="direct-provider",
        source="webui",
        display_text="new direct turn",
        external_runtime_owned=False,
    )

    assert response["queue_item_id"] == "head"
    assert response["queue_item"]["text"] == "head turn"
    assert response["queue"] and [item["id"] for item in response["queue"]] == ["tail", response["queue"][1]["id"]]
    persisted = models.Session.load(session.session_id)
    assert [item["id"] for item in persisted.queue] == ["tail", response["queue"][1]["id"]]
    incoming = persisted.queue[1]
    assert incoming["text"] == "new direct turn"
    assert incoming["display_text"] == "new direct turn"
    assert incoming["files"] == [{"name": "new.txt", "path": "/attachments/new.txt"}]
    assert incoming["model"] == "direct-model"
    assert incoming["model_provider"] == "direct-provider"
    assert incoming["source"] == "webui"
    assert incoming["id"] and incoming["created_at"] and incoming["_queued_at"]
    assert persisted.pending_user_message == "head turn"
    assert len(worker.starts) == 1


def test_direct_start_at_queue_cap_fails_closed_without_claim(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    queue = [{"id": str(i), "text": f"turn-{i}", "files": []} for i in range(100)]
    session = _new_session("queue-direct-cap", tmp_path, queue=queue)
    worker = _install_start_stubs(monkeypatch)

    response = routes._start_chat_stream_for_session(
        session,
        msg="must remain unsent",
        attachments=[],
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider="openai",
        external_runtime_owned=False,
    )

    assert response["_status"] == 409
    assert models.Session.load(session.session_id).queue == queue
    assert not worker.starts


def test_thread_start_failure_restores_deferred_session_state(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session("queue-deferred-rollback", tmp_path)
    session.workspace = str(tmp_path / "before")
    session.model = "before-model"
    session.model_provider = "before-provider"
    session.truncation_watermark = 321.0
    session.save()
    before = json.loads(session.path.read_text(encoding="utf-8"))
    _install_start_stubs(monkeypatch, thread_start=lambda: (_ for _ in ()).throw(RuntimeError("start failed")))

    try:
        routes._start_chat_stream_for_session(
            session,
            msg="never started",
            attachments=[{"name": "file.txt"}],
            workspace=str(tmp_path / "after"),
            model="after-model",
            model_provider="after-provider",
            external_runtime_owned=False,
        )
    except RuntimeError as exc:
        assert str(exc) == "start failed"
    else:
        raise AssertionError("thread start failure must be surfaced")

    after = json.loads(session.path.read_text(encoding="utf-8"))
    for key in ("messages", "queue", "workspace", "model", "model_provider", "active_stream_id",
                "pending_user_message", "pending_attachments", "pending_started_at",
                "pending_user_source", "truncation_watermark"):
        assert after.get(key) == before.get(key), key
    assert routes.STREAMS == {}


def test_eager_thread_start_failure_removes_checkpoint_and_preserves_tail_enqueue(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "eager")
    head = {"id": "eager-head", "text": "queued head", "files": [], "model": "m", "model_provider": "p"}
    session = _new_session("queue-eager-rollback", tmp_path, queue=[head])
    before_messages = copy.deepcopy(session.messages)
    _disable_enqueue_drain(monkeypatch)
    new_item = {}

    def enqueue_before_failure():
        _status, response = _queue_action(
            {
                "session_id": session.session_id,
                "action": "enqueue",
                "text": "concurrent tail",
                "model": "tail-model",
                "model_provider": "tail-provider",
            }
        )
        new_item.update(response["item"])
        raise RuntimeError("start failed")

    _install_start_stubs(monkeypatch, thread_start=enqueue_before_failure)

    try:
        routes._start_chat_stream_for_session(
            session,
            msg="ignored direct prompt",
            attachments=[],
            workspace=str(tmp_path),
            model="gpt-4o",
            model_provider="openai",
            external_runtime_owned=False,
        )
    except RuntimeError as exc:
        assert str(exc) == "start failed"
    else:
        raise AssertionError("thread start failure must be surfaced")

    persisted = models.Session.load(session.session_id)
    assert persisted.messages == before_messages
    assert [item["id"] for item in persisted.queue] == ["eager-head", new_item["id"]]
    assert persisted.pending_queue_item is None
    assert persisted.active_stream_id is None
    assert persisted.pending_user_message is None
    assert persisted.pending_attachments == []
    assert persisted.pending_started_at is None
    assert persisted.pending_user_source is None
    assert not session.path.with_suffix(".json.bak").exists()


def test_empty_eager_thread_start_failure_restores_pre_start_sidecar(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "eager")
    session = _new_session("queue-empty-eager-rollback", tmp_path)
    session.messages = []
    session.save()
    session.path.with_suffix(".json.bak").unlink(missing_ok=True)
    _install_start_stubs(
        monkeypatch,
        thread_start=lambda: (_ for _ in ()).throw(RuntimeError("start failed")),
    )

    try:
        routes._start_chat_stream_for_session(
            session,
            msg="never started",
            attachments=[],
            workspace=str(tmp_path),
            model="gpt-4o",
            model_provider="openai",
            external_runtime_owned=False,
        )
    except RuntimeError as exc:
        assert str(exc) == "start failed"
    else:
        raise AssertionError("thread start failure must be surfaced")

    persisted = models.Session.load(session.session_id)
    assert persisted is not None
    assert persisted.messages == []
    assert persisted.active_stream_id is None
    assert persisted.pending_user_message is None
    assert persisted.pending_queue_item is None
    assert not session.path.with_suffix(".json.bak").exists()


def test_thread_start_failure_preserves_concurrent_session_updates(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session(
        "queue-concurrent-session-update",
        tmp_path,
        queue=[{"id": "head", "text": "queued head", "files": []}],
    )
    concurrent_workspace = str(tmp_path / "concurrent-workspace")

    def update_session_then_fail():
        with routes._get_session_agent_lock(session.session_id):
            current = routes._authoritative_session_locked(session.session_id, fallback=session)
            current.title = "Concurrent rename"
            current.workspace = concurrent_workspace
            current.model = "concurrent-model"
            current.model_provider = "concurrent-provider"
            current.save()
        raise RuntimeError("start failed")

    _install_start_stubs(monkeypatch, thread_start=update_session_then_fail)

    try:
        routes._start_chat_stream_for_session(
            session,
            msg="ignored",
            attachments=[],
            workspace=str(tmp_path / "admitted-workspace"),
            model="admitted-model",
            model_provider="admitted-provider",
            external_runtime_owned=False,
            queue_item_id="head",
        )
    except RuntimeError as exc:
        assert str(exc) == "start failed"
    else:
        raise AssertionError("thread start failure must be surfaced")

    persisted = models.Session.load(session.session_id)
    assert persisted is not None
    assert persisted.title == "Concurrent rename"
    assert persisted.workspace == concurrent_workspace
    assert persisted.model == "concurrent-model"
    assert persisted.model_provider == "concurrent-provider"
    assert [item["id"] for item in persisted.queue] == ["head"]
    assert persisted.active_stream_id is None
    assert persisted.pending_queue_item is None


def test_thread_start_failure_restores_exact_claimed_item_in_order(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    first = {
        "id": "thread-item",
        "text": "recover me",
        "files": [{"name": "photo.png", "path": "/attachments/photo.png", "mime": "image/png"}],
        "model": "queued-model",
        "model_provider": "queued-provider",
        "created_at": 123.0,
        "_queued_at": 456,
    }
    second = {
        "id": "later-item",
        "text": "run me next",
        "files": [],
        "model": "later-model",
        "model_provider": "later-provider",
    }
    session = _new_session(
        "queue-thread-failure",
        tmp_path,
        queue=[first, second],
    )
    _install_start_stubs(monkeypatch, thread_start=lambda: (_ for _ in ()).throw(RuntimeError("start failed")))

    try:
        routes._start_chat_stream_for_session(
            session,
            msg="ignored",
            attachments=[],
            workspace=str(tmp_path),
            model="gpt-4o",
            model_provider="openai",
            external_runtime_owned=False,
            queue_item_id="thread-item",
        )
    except RuntimeError as exc:
        assert str(exc) == "start failed"
    else:
        raise AssertionError("thread start failure must be surfaced")

    persisted = models.Session.load(session.session_id)
    assert persisted.queue == [first, second]
    assert [item["id"] for item in persisted.queue] == ["thread-item", "later-item"]
    assert persisted.active_stream_id is None
    assert persisted.pending_user_message is None
    assert persisted.pending_attachments == []
    assert persisted.pending_started_at is None
    assert persisted.pending_user_source is None


def test_drain_starts_one_item_per_teardown_in_order(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session(
        "queue-order",
        tmp_path,
        queue=[
            {"id": "one", "text": "one", "files": [], "model": "m1", "model_provider": "p1"},
            {"id": "two", "text": "two", "files": [], "model": "m2", "model_provider": "p2"},
        ],
    )
    worker = _install_start_stubs(monkeypatch)

    first = routes.drain_queued_session_turn(session.session_id)
    assert first["_status"] if "_status" in first else first["stream_id"]
    assert models.Session.load(session.session_id).queue[0]["id"] == "two"
    assert len(worker.starts) == 1

    settled = models.Session.load(session.session_id)
    settled.active_stream_id = None
    settled.pending_user_message = None
    settled.pending_attachments = []
    settled.pending_started_at = None
    settled.pending_user_source = None
    settled.save()
    models.SESSIONS[session.session_id] = settled
    routes.SESSIONS[session.session_id] = settled

    second = routes.drain_queued_session_turn(session.session_id)
    assert second["stream_id"]
    assert models.Session.load(session.session_id).queue == []
    assert [call[1][1] for call in worker.starts] == ["one", "two"]


def test_concurrent_duplicate_drains_start_at_most_one_worker(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session(
        "queue-duplicate",
        tmp_path,
        queue=[{"id": "only", "text": "only once", "files": []}],
    )
    worker = _install_start_stubs(monkeypatch)
    barrier = threading.Barrier(2)
    original_start_run = routes._start_run

    def start_run_with_barrier(run_session, **kwargs):
        barrier.wait(timeout=5)
        return original_start_run(run_session, **kwargs)

    monkeypatch.setattr(routes, "_start_run", start_run_with_barrier)
    results = []
    real_thread = _REAL_THREAD

    def drain():
        results.append(routes.drain_queued_session_turn(session.session_id))

    first = real_thread(target=drain)
    second = real_thread(target=drain)
    first.start()
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert sorted(result.get("_status", 200) for result in results) == [200, 409]
    assert len(worker.starts) == 1
    assert models.Session.load(session.session_id).queue == []


def test_enqueue_during_claim_is_not_lost(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    session = _new_session(
        "queue-enqueue-race",
        tmp_path,
        queue=[{"id": "old", "text": "old", "files": []}],
    )
    lock = routes._get_session_agent_lock(session.session_id)
    claim_entered = threading.Event()
    release_claim = threading.Event()
    enqueue_finished = threading.Event()

    def claim(run_session, **_kwargs):
        with lock:
            if run_session.active_stream_id:
                return {
                    "error": "session already has an active stream",
                    "_status": 409,
                }
            claim_entered.set()
            assert release_claim.wait(timeout=5)
            run_session.queue = list(run_session.queue)[1:]
            run_session.active_stream_id = "claimed"
            run_session.pending_user_message = "old"
            routes.STREAMS["claimed"] = object()
            run_session.save()
        return {"stream_id": "claimed", "session_id": run_session.session_id, "_status": 200}

    monkeypatch.setattr(routes, "_start_run", claim)
    real_thread = _REAL_THREAD
    drain_thread = real_thread(
        target=lambda: routes.drain_queued_session_turn(session.session_id),
    )
    drain_thread.start()
    assert claim_entered.wait(timeout=5)

    def enqueue():
        _queue_action(
            {
                "session_id": session.session_id,
                "action": "enqueue",
                "text": "new during teardown",
            }
        )
        enqueue_finished.set()

    enqueue_thread = real_thread(target=enqueue)
    enqueue_thread.start()
    assert not enqueue_finished.wait(timeout=0.1)
    release_claim.set()
    drain_thread.join(timeout=5)
    enqueue_thread.join(timeout=5)

    assert not drain_thread.is_alive() and not enqueue_thread.is_alive()
    persisted = models.Session.load(session.session_id)
    assert [item["text"] for item in persisted.queue] == ["new during teardown"]


def test_local_and_gateway_teardown_helpers_call_shared_drain_for_all_outcomes(monkeypatch):
    import inspect

    from api import gateway_chat, streaming

    for module, helper, worker, sid in (
        (streaming, streaming._drain_queued_session_turn_after_teardown, streaming._run_agent_streaming, "local"),
        (gateway_chat, gateway_chat._drain_queued_session_turn_after_teardown, gateway_chat._run_gateway_chat_streaming, "gateway"),
    ):
        worker_source = inspect.getsource(worker)
        assert "finally:" in worker_source
        if module is streaming:
            assert 'getattr(s, "session_id", session_id) or session_id' in worker_source
        else:
            assert "_drain_queued_session_turn_after_teardown(session_id)" in worker_source
        for outcome in ({"stream_id": "ok"}, {"_status": 500}, {"cancelled": True}):
            monkeypatch.setattr(routes, "drain_queued_session_turn", lambda _sid, result=outcome: result)
            assert helper(sid) is outcome


def test_compression_rotation_keeps_queue_on_live_continuation_only(monkeypatch, tmp_path):
    _session_dir, _attachment_root = _setup(monkeypatch, tmp_path)
    from api import streaming

    monkeypatch.setattr(streaming, "SESSION_DIR", _session_dir)
    old = Session(
        session_id="queue-compression-old",
        title="Queue compression",
        workspace=str(tmp_path),
        profile="default",
        model="gpt-4o",
        model_provider="openai",
        messages=[],
    )
    old.save()
    item = {
        "id": "continuation-item",
        "text": "continue after compression",
        "files": [],
        "model": "gpt-4o",
        "model_provider": "openai",
    }
    continuation = _new_session(
        "queue-compression-new",
        tmp_path,
        queue=[item],
    )

    streaming._preserve_pre_compression_snapshot(continuation, old.session_id)

    archived_payload = json.loads(
        (_session_dir / f"{old.session_id}.json").read_text(encoding="utf-8")
    )
    assert archived_payload.get("queue") == []
    assert Session.load(old.session_id).queue == []
    assert continuation.queue == [item]

    worker = _install_start_stubs(monkeypatch)
    started = routes.drain_queued_session_turn(continuation.session_id)

    assert started["stream_id"]
    assert len(worker.starts) == 1
    live = Session.load(continuation.session_id)
    assert live.queue == []
    assert live.pending_user_message == item["text"]

    archived_drain = routes.drain_queued_session_turn(old.session_id)
    assert archived_drain["_status"] == 409
    assert len(worker.starts) == 1



def test_frontend_queue_is_server_hydrated_and_does_not_drain_on_busy_reset():
    ui = Path("static/ui.js").read_text(encoding="utf-8")
    sessions = Path("static/sessions.js").read_text(encoding="utf-8")
    messages = Path("static/messages.js").read_text(encoding="utf-8")
    commands = Path("static/commands.js").read_text(encoding="utf-8")

    assert "function hydrateSessionQueue" in ui
    assert "api('/api/chat/queue'" in ui
    assert "shiftQueuedSessionMessage" not in ui
    assert "_queueDrainSid" not in ui
    busy_body = ui[ui.index("function setBusy(v)"):ui.index("// ── Queue chip display", ui.index("function setBusy(v)"))]
    assert "queueSessionMessage" not in busy_body
    assert "hydrateSessionQueue(S.session.session_id,data.session.queue)" in sessions
    assert "review and send when ready" in sessions
    assert "await queueSessionMessage" in messages
    assert "await queueSessionMessage" in commands
    interrupt_start = commands.index("async function cmdInterrupt(")
    interrupt_end = commands.index("async function cmdSteer(", interrupt_start)
    interrupt_body = commands[interrupt_start:interrupt_end]
    assert interrupt_body.index("await queueSessionMessage") < interrupt_body.index("cancelStream")
