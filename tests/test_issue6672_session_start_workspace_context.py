"""Regression coverage for the session-start workspace prompt authority."""

import json
import queue
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse


def _legacy_payload(session_id, workspace, *, messages=None):
    return {
        "session_id": session_id,
        "title": "Legacy",
        "workspace": str(workspace),
        "model": "test-model",
        "created_at": 1.0,
        "updated_at": 2.0,
        "messages": messages or [{"role": "user", "content": "hello"}],
    }


def _isolate_session_store(tmp_path, monkeypatch):
    from api import models, routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sessions = OrderedDict()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    return session_dir, sessions


def _invoke_post_route(monkeypatch, path, body):
    from api import routes

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, **_kwargs: captured.update(
        payload=payload, status=status
    ) or payload)
    routes.handle_post(SimpleNamespace(command="POST"), SimpleNamespace(path=path))
    return captured


def test_reported_mid_session_switch_keeps_streaming_composer_prefix(tmp_path):
    from api import streaming
    from api.models import Session

    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    session = Session(session_id="issue6672prompt", workspace=initial)

    first = streaming._webui_session_workspace_prompts(
        session,
        workspace=session.workspace,
    )
    session.workspace = str(changed.resolve())
    second = streaming._webui_session_workspace_prompts(
        session,
        workspace=session.workspace,
    )

    assert f"Workspace: {initial.resolve()}" in first["ephemeral_system_prompt"]
    assert first["ephemeral_system_prompt"] == second["ephemeral_system_prompt"]
    assert first["system_prompt"] == second["system_prompt"]
    assert first["workspace_ctx"] != second["workspace_ctx"]
    assert f"Workspace: {changed.resolve()}" not in second["ephemeral_system_prompt"]


def test_session_start_workspace_round_trips_and_legacy_sidecar_freezes_once(tmp_path, monkeypatch):
    from api import models
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    session = Session(
        session_id="issue6672persist",
        workspace=initial,
        messages=[{"role": "user", "content": "hello"}],
    )
    session.save(skip_index=True)
    session.workspace = str(changed.resolve())
    session.save(skip_index=True)

    payload = json.loads(session.path.read_text(encoding="utf-8"))
    assert payload["session_start_workspace"] == str(initial.resolve())
    assert Session.load(session.session_id).session_start_workspace == str(initial.resolve())
    assert Session.load_metadata_only(session.session_id).session_start_workspace == str(initial.resolve())

    legacy_id = "issue6672legacy"
    legacy_payload = dict(payload)
    legacy_payload["session_id"] = legacy_id
    legacy_payload["workspace"] = str(initial.resolve())
    legacy_payload.pop("session_start_workspace")
    legacy_path = session_dir / f"{legacy_id}.json"
    legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    legacy = Session.load(legacy_id)
    assert legacy.session_start_workspace == str(initial.resolve())
    assert json.loads(legacy_path.read_text(encoding="utf-8"))["session_start_workspace"] == str(initial.resolve())
    legacy_meta = Session.load_metadata_only(legacy_id)
    assert legacy_meta.session_start_workspace == str(initial.resolve())
    legacy.workspace = str(changed.resolve())
    legacy.save(skip_index=True)
    assert Session.load(legacy_id).session_start_workspace == str(initial.resolve())


def test_explicit_path_legacy_load_persists_session_start_workspace(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    path = session_dir / "issue6672explicit.json"
    path.write_text(
        json.dumps(_legacy_payload("issue6672explicit", tmp_path / "imported-workspace")),
        encoding="utf-8",
    )

    loaded = models._load_session_from_path(path)

    assert loaded.session_start_workspace == str((tmp_path / "imported-workspace").resolve())
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["session_start_workspace"] == loaded.session_start_workspace

    keys = list(persisted)
    assert keys.index("session_start_workspace") < keys.index("messages")


def test_explicit_path_legacy_load_stays_bounded_while_session_lock_is_owned(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    sid = "issue6672explicit-lock"
    path = session_dir / f"{sid}.json"
    path.write_text(json.dumps(_legacy_payload(sid, tmp_path / "workspace")), encoding="utf-8")
    lock = models._get_session_agent_lock(sid)
    assert lock.acquire(blocking=False)
    try:
        loaded = models._load_session_from_path(path)
    finally:
        lock.release()
    assert loaded.session_start_workspace == str((tmp_path / "workspace").resolve())
    assert "session_start_workspace" not in json.loads(path.read_text(encoding="utf-8"))


def test_metadata_only_legacy_load_does_not_full_parse_while_session_lock_is_owned(tmp_path, monkeypatch):
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    sid = "issue6672metadata-lock"
    path = session_dir / f"{sid}.json"
    path.write_text(json.dumps(_legacy_payload(sid, tmp_path / "workspace")), encoding="utf-8")
    lock = models._get_session_agent_lock(sid)
    assert lock.acquire(blocking=False)
    try:
        loaded = models.Session.load_metadata_only(sid)
    finally:
        lock.release()
    assert loaded._loaded_metadata_only is True
    assert loaded.messages == []
    assert loaded.session_start_workspace == loaded.workspace
    assert "session_start_workspace" not in json.loads(path.read_text(encoding="utf-8"))


def test_legacy_migration_skips_a_changed_sidecar(tmp_path, monkeypatch):
    from api import models

    sid = "issue6672race"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    path = session_dir / f"{sid}.json"
    path.write_text(json.dumps(_legacy_payload(sid, tmp_path / "old-workspace")), encoding="utf-8")
    concurrent = _legacy_payload(sid, tmp_path / "concurrent-workspace")
    calls = {"count": 0}

    def signature_with_concurrent_write(candidate):
        calls["count"] += 1
        if calls["count"] == 2:
            candidate.write_text(json.dumps(concurrent), encoding="utf-8")
        return ("before",) if calls["count"] == 1 else ("after",)

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "_sidecar_stat_signature", signature_with_concurrent_write)

    loaded = models._load_session_from_path(path)

    assert loaded.workspace == str((tmp_path / "old-workspace").resolve())
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["workspace"] == str(tmp_path / "concurrent-workspace")
    assert "session_start_workspace" not in persisted


def test_current_turn_workspace_remains_authoritative_for_file_ops(tmp_path, monkeypatch):
    from api import models
    from api import routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    sessions = OrderedDict()
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    changed.mkdir()
    session = Session(session_id="issue6672fileops", workspace=initial)
    session.save(skip_index=True)
    sessions[session.session_id] = session
    session.workspace = str(changed.resolve())
    session.save(skip_index=True)

    marker = changed / "authorized-write.txt"
    marker.write_text("initial", encoding="utf-8")
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    result = routes._handle_file_save(
        object(),
        {
            "session_id": session.session_id,
            "path": marker.name,
            "content": "changed workspace",
        },
    )

    assert result["ok"] is True
    assert marker.read_text(encoding="utf-8") == "changed workspace"


def test_session_import_export_preserves_session_start_workspace(tmp_path, monkeypatch):
    from api import models, routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    sessions = OrderedDict()
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: Path(value).resolve())

    initial = tmp_path / "initial-workspace"
    current = tmp_path / "current-workspace"
    current.mkdir()
    source = Session(
        session_id="issue6672export",
        workspace=current,
        session_start_workspace=initial,
        messages=[{"role": "user", "content": "hello"}],
        profile="default",
    )
    source.save(skip_index=True)
    sessions[source.session_id] = source

    class ExportHandler:
        def __init__(self):
            self.headers = {}
            self.wfile = SimpleNamespace(write=self._write)
            self.body = b""

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.headers[name] = value

        def end_headers(self):
            pass

        def _write(self, data):
            self.body += data

    export_handler = ExportHandler()
    monkeypatch.setattr(routes, "get_session", lambda _sid: source)
    routes._handle_session_export(
        export_handler,
        urlparse(f"/api/session/export?session_id={source.session_id}"),
    )
    exported = json.loads(export_handler.body)
    assert exported["session_start_workspace"] == str(initial.resolve())

    imported_payload = {}

    def capture_json(_handler, payload, **_kwargs):
        imported_payload.update(payload)
        return payload

    monkeypatch.setattr(routes, "j", capture_json)
    routes._handle_session_import(object(), exported)
    imported = sessions[imported_payload["session"]["session_id"]]

    assert imported.session_start_workspace == str(initial.resolve())
    assert Session.load(imported.session_id).session_start_workspace == str(initial.resolve())


def test_session_import_rejects_invalid_session_start_workspace_type(tmp_path, monkeypatch):
    from api import models, routes

    _isolate_session_store(tmp_path, monkeypatch)
    captured = {}
    monkeypatch.setattr(routes, "bad", lambda _handler, message: captured.update(message=message) or captured)
    result = routes._handle_session_import(
        object(),
        {
            "messages": [],
            "workspace": str(tmp_path),
            "session_start_workspace": 1234,
        },
    )
    assert result is captured
    assert "must be a path string" in captured["message"]
    assert not models.SESSIONS


def test_session_import_rejects_untrusted_session_start_workspace(tmp_path, monkeypatch):
    from api import routes

    _isolate_session_store(tmp_path, monkeypatch)
    captured = {}
    monkeypatch.setattr(routes, "bad", lambda _handler, message: captured.update(message=message) or captured)
    routes._handle_session_import(
        object(),
        {
            "messages": [],
            "workspace": str(tmp_path),
            "session_start_workspace": "C:\\Windows",
        },
    )
    assert "outside the user home directory" in captured["message"].lower()


def test_session_lineage_variants_inherit_the_session_start_workspace(tmp_path):
    from api import routes
    from api.models import Session

    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    variants = [
        Session(
            session_id="issue6672-imported-messaging",
            workspace=changed,
            session_start_workspace=initial,
            raw_source="telegram",
            session_source="messaging",
        ),
        Session(
            session_id="issue6672-duplicate",
            workspace=changed,
            session_start_workspace=initial,
            session_source="webui",
        ),
        Session(
            session_id="issue6672-fork",
            workspace=changed,
            session_start_workspace=initial,
            session_source="fork",
            parent_session_id="issue6672-parent",
        ),
        Session(
            session_id="issue6672-compressed",
            workspace=changed,
            session_start_workspace=initial,
            session_source="fork",
            parent_session_id="issue6672-parent",
            compression_recovery_source_session_id="issue6672-parent",
        ),
        Session(
            session_id="issue6672-cron",
            workspace=changed,
            session_start_workspace=initial,
            raw_source="cron",
            source_tag="cron",
            session_source="cron",
            read_only=True,
        ),
        Session(
            session_id="issue6672-background",
            workspace=changed,
            session_start_workspace=initial,
            raw_source="background",
            session_source="background",
            read_only=True,
        ),
    ]

    assert all(
        routes._session_start_workspace_for_child(variant) == str(initial.resolve())
        for variant in variants
    )


def test_duplicate_route_constructs_child_with_frozen_workspace(tmp_path, monkeypatch):
    from api import models, routes
    from api.models import Session

    session_dir, sessions = _isolate_session_store(tmp_path, monkeypatch)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    source = Session(
        session_id="issue6672-duplicate-route",
        workspace=changed,
        session_start_workspace=initial,
        messages=[{"role": "user", "content": "hello"}],
    )
    source.save(skip_index=True)
    sessions[source.session_id] = source

    captured = _invoke_post_route(
        monkeypatch,
        "/api/session/duplicate",
        {"session_id": source.session_id},
    )

    child_id = captured["payload"]["session"]["session_id"]
    child = models.Session.load(child_id)
    assert captured["status"] == 200
    assert child.session_id != source.session_id
    assert child.workspace == str(changed)
    assert child.session_start_workspace == str(initial)
    assert (session_dir / f"{child_id}.json").exists()


def test_fork_route_constructs_child_with_frozen_workspace(tmp_path, monkeypatch):
    from api import models, routes
    from api.models import Session

    session_dir, sessions = _isolate_session_store(tmp_path, monkeypatch)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    source = Session(
        session_id="issue6672-fork-route",
        workspace=changed,
        session_start_workspace=initial,
        messages=[{"role": "user", "content": "hello"}],
    )
    source.save(skip_index=True)
    sessions[source.session_id] = source

    captured = _invoke_post_route(
        monkeypatch,
        "/api/session/branch",
        {"session_id": source.session_id, "keep_count": 1},
    )

    child_id = captured["payload"]["session_id"]
    child = models.Session.load(child_id)
    assert captured["status"] == 200
    assert child.parent_session_id == source.session_id
    assert child.workspace == str(changed)
    assert child.session_start_workspace == str(initial)
    assert (session_dir / f"{child_id}.json").exists()


def test_compression_recovery_constructs_child_with_frozen_workspace(tmp_path, monkeypatch):
    from api import models, routes
    from api.compression_recovery import stamp_compression_exhausted_recovery
    from api.models import Session

    session_dir, sessions = _isolate_session_store(tmp_path, monkeypatch)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    source = Session(
        session_id="issue6672-compression-route",
        workspace=changed,
        session_start_workspace=initial,
        profile="default",
        messages=[{"role": "user", "content": "hello"}],
    )
    stamp_compression_exhausted_recovery(source, message="Context length exceeded.")
    source.save(skip_index=True)
    sessions[source.session_id] = source

    handler = SimpleNamespace()
    captured = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: captured.update(
            payload=payload, status=status
        ) or payload,
    )
    routes._handle_session_compression_recovery_start(handler, {"session_id": source.session_id})

    child_id = captured["payload"]["session"]["session_id"]
    child = models.Session.load(child_id)
    assert captured["status"] == 200
    assert child.parent_session_id == source.session_id
    assert child.compression_recovery_source_session_id == source.session_id
    assert child.workspace == str(changed)
    assert child.session_start_workspace == str(initial)
    assert (session_dir / f"{child_id}.json").exists()


def test_workspace_prompt_helper_keeps_sync_and_gateway_consumers_on_same_authority(tmp_path):
    from api import streaming
    from api.models import Session

    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    session = Session(session_id="issue6672consumers", workspace=initial)
    session.workspace = str(changed.resolve())
    prompts = streaming._webui_session_workspace_prompts(session, workspace=session.workspace)

    assert f"Active workspace at session start: {initial.resolve()}" in prompts["system_prompt"]
    assert f"Workspace: {initial.resolve()}" in prompts["ephemeral_system_prompt"]
    assert str(changed.resolve()) not in prompts["system_prompt"]
    assert str(changed.resolve()) not in prompts["ephemeral_system_prompt"]
    assert str(changed.resolve()).replace("\\", "\\\\") in prompts["workspace_ctx"]


def test_child_workspace_fallback_is_lazy(tmp_path, monkeypatch):
    from api import routes

    source = SimpleNamespace(
        workspace=str(tmp_path / "current"),
        session_start_workspace=str(tmp_path / "initial"),
    )
    monkeypatch.setattr(routes, "get_last_workspace", lambda: (_ for _ in ()).throw(
        AssertionError("fallback workspace should not be evaluated")
    ))
    assert routes._session_start_workspace_for_child(source) == source.session_start_workspace


def test_run_agent_streaming_composes_frozen_context_through_production_worker(tmp_path, monkeypatch):
    from api import streaming
    from api.models import Session

    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    session = Session(session_id="issue6672worker", workspace=initial, messages=[])
    session.workspace = str(changed.resolve())
    session.active_stream_id = "issue6672worker-stream"
    session.pending_started_at = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.profile = None
    session.personality = None
    session.save = lambda *args, **kwargs: None

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.tool_progress_callback = kwargs.get("tool_progress_callback")
            self.reasoning_callback = kwargs.get("reasoning_callback")
            self.clarify_callback = kwargs.get("clarify_callback")

        def run_conversation(self, **kwargs):
            captured.update(kwargs)
            return {"messages": [], "final_response": "done", "completed": True}

    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *_args, **_kwargs: ("test-model", "test-provider", None))
    monkeypatch.setattr(streaming, "_maybe_schedule_title_refresh", lambda *args, **kwargs: None)
    monkeypatch.setattr(streaming, "get_state_db_session_messages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(streaming, "reconciled_state_db_messages_for_session", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(streaming, "_new_turn_context_from_messages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(streaming, "_deduplicate_context_messages", lambda messages: messages)
    monkeypatch.setattr(streaming, "_sanitize_messages_for_api", lambda messages, **_kwargs: messages)
    monkeypatch.setattr(streaming, "_drain_webui_process_notifications", lambda *args, **kwargs: [])
    monkeypatch.setattr(streaming, "_build_native_multimodal_message", lambda prefix, text, *_args, **_kwargs: prefix + text)
    monkeypatch.setattr(streaming, "_persistent_state_snapshot", lambda *_args: {})
    monkeypatch.setattr(streaming, "_accept_pending_async_delegations", lambda *args, **kwargs: [])
    monkeypatch.setattr(streaming, "_merge_display_messages_after_agent_result", lambda *args, **kwargs: [])
    monkeypatch.setattr(streaming, "_compact_session_image_parts_for_persistence", lambda *_args: None)
    monkeypatch.setattr(streaming, "_restore_reasoning_metadata", lambda *_args: None)
    monkeypatch.setattr(streaming, "_assign_stable_message_ids", lambda *_args: None)
    monkeypatch.setattr(streaming, "_restore_display_reasoning_metadata", lambda *_args: None)
    monkeypatch.setattr(streaming, "_dedupe_replayed_context_messages", lambda *_args: [])
    monkeypatch.setattr(streaming, "_active_turn_identity", None, raising=False)
    monkeypatch.setattr(streaming, "load_settings", lambda: {})
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda *_args: {})
    monkeypatch.setattr(streaming, "_save_streaming_checkpoint", lambda *_args: None)
    monkeypatch.setattr(streaming, "_maybe_run_auto_compression", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(streaming, "_finalize_stream_session", lambda *_args, **_kwargs: None, raising=False)
    streaming.STREAMS[session.active_stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text="hello",
            model="test-model",
            model_provider="test-provider",
            workspace=str(changed),
            stream_id=session.active_stream_id,
        )
    finally:
        streaming.STREAMS.pop(session.active_stream_id, None)
    assert captured["system_message"] == streaming._webui_workspace_system_prompt(str(initial.resolve()))
    assert captured["user_message"] == streaming._workspace_context_prefix(str(changed)) + "hello"


def test_sync_chat_consumer_uses_frozen_context_and_current_turn_prefix(tmp_path, monkeypatch):
    import api.config
    import api.oauth
    from api import routes
    from api import streaming
    from api import models
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    sessions = OrderedDict()
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    session = Session(session_id="issue6672sync", workspace=initial, messages=[])
    session.save(skip_index=True)
    sessions[session.session_id] = session
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, **kwargs):
            captured.update(kwargs)
            result = {
                "messages": [
                    {"role": "user", "content": kwargs["user_message"]},
                    {"role": "assistant", "content": "done"},
                ],
                "final_response": "done",
                "completed": True,
            }
            captured["run_result"] = result
            return result

    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: str(changed.resolve()))
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_args: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider"),
    )
    monkeypatch.setattr(routes, "require_ai_agent_class", lambda: FakeAgent)
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])
    monkeypatch.setattr(streaming, "_context_messages_for_new_turn", lambda *_args: [])
    monkeypatch.setattr(streaming, "_sanitize_messages_for_api", lambda messages, **_kwargs: messages)
    monkeypatch.setattr(streaming, "_restore_reasoning_metadata", lambda _old, new: new)
    monkeypatch.setattr(streaming, "_assign_stable_message_ids", lambda *_args: None)
    monkeypatch.setattr(streaming, "_dedupe_replayed_context_messages", lambda _old, new, _msg: new)
    monkeypatch.setattr(streaming, "_restore_display_reasoning_metadata", lambda _old, new: new)
    monkeypatch.setattr(
        streaming,
        "_merge_display_messages_after_agent_result",
        lambda *_args, **_kwargs: captured["run_result"]["messages"],
    )
    monkeypatch.setattr(streaming, "_compact_session_image_parts_for_persistence", lambda _session: None)
    monkeypatch.setattr(routes, "title_from", lambda messages, fallback: fallback)
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    monkeypatch.setattr(api.config, "resolve_model_provider", lambda *_args: ("test-model", "test-provider", None))
    monkeypatch.setattr(api.config, "resolve_custom_provider_connection", lambda _provider: (None, None))
    monkeypatch.setattr(api.oauth, "resolve_runtime_provider_with_anthropic_env_lock", lambda *_args, **_kwargs: {})

    routes._handle_chat_sync(
        object(),
        {"session_id": session.session_id, "message": "hello", "workspace": str(changed)},
    )
    assert f"Active workspace at session start: {initial.resolve()}" in captured["system_message"]
    assert str(changed.resolve()) not in captured["system_message"]
    assert captured["user_message"] == (
        streaming._webui_session_workspace_prompts(session, workspace=str(changed))["workspace_ctx"]
        + "hello"
    )
