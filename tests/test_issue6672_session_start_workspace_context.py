"""Regression coverage for the session-start workspace prompt authority."""

import json
from collections import OrderedDict
from pathlib import Path


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

    path = tmp_path / "imported-session.json"
    path.write_text(
        json.dumps(_legacy_payload("issue6672explicit", tmp_path / "imported-workspace")),
        encoding="utf-8",
    )

    loaded = models._load_session_from_path(path)

    assert loaded.session_start_workspace == str((tmp_path / "imported-workspace").resolve())
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["session_start_workspace"] == loaded.session_start_workspace


def test_legacy_migration_skips_a_changed_sidecar(tmp_path, monkeypatch):
    from api import models

    sid = "issue6672race"
    path = tmp_path / f"{sid}.json"
    path.write_text(json.dumps(_legacy_payload(sid, tmp_path / "old-workspace")), encoding="utf-8")
    concurrent = _legacy_payload(sid, tmp_path / "concurrent-workspace")
    calls = {"count": 0}

    def signature_with_concurrent_write(candidate):
        calls["count"] += 1
        if calls["count"] == 2:
            candidate.write_text(json.dumps(concurrent), encoding="utf-8")
        return ("before",) if calls["count"] == 1 else ("after",)

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "_sidecar_stat_signature", signature_with_concurrent_write)

    loaded = models.Session.load(sid)

    assert loaded.workspace == str((tmp_path / "old-workspace").resolve())
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["workspace"] == str(tmp_path / "concurrent-workspace")
    assert "session_start_workspace" not in persisted


def test_current_turn_workspace_remains_authoritative_for_file_ops(tmp_path, monkeypatch):
    from api import models
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    changed.mkdir()
    session = Session(session_id="issue6672fileops", workspace=initial)
    session.save(skip_index=True)
    session.workspace = str(changed.resolve())
    session.save(skip_index=True)

    authorized = models.get_session_for_file_ops(session.session_id)
    marker = Path(authorized.workspace) / "authorized-write.txt"
    marker.write_text("changed workspace", encoding="utf-8")

    assert authorized.workspace == str(changed.resolve())
    assert marker.read_text(encoding="utf-8") == "changed workspace"


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


def test_sync_chat_consumer_uses_frozen_context_and_current_turn_prefix(tmp_path, monkeypatch):
    import api.config
    import api.oauth
    from api import routes
    from api import streaming
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(api.config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(api.config, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(api.config, "SESSIONS", {})
    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    session = Session(session_id="issue6672sync", workspace=initial, messages=[])
    session.save(skip_index=True)
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
