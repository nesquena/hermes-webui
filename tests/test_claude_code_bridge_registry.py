from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4


QWEN_WRAPPER = Path("/Users/mohameddarwiche/bin/claude-qwen")
ORNITH_WRAPPER = Path("/Users/mohameddarwiche/bin/claude-ornith")


def _write_registry(path: Path, stores: list[dict]) -> None:
    path.write_text(json.dumps({"stores": stores}), encoding="utf-8")
    path.chmod(0o600)


def _valid_store(tmp_path: Path) -> tuple[dict, Path]:
    config_dir = tmp_path / ".claude-local"
    projects_dir = config_dir / "projects"
    workspace = tmp_path / "workspace"
    projects_dir.mkdir(parents=True)
    workspace.mkdir()
    return {
        "id": "local-models",
        "label": "Claude Local",
        "config_dir": str(config_dir),
        "claude_bin": str(QWEN_WRAPPER),
        "workspace_roots": [str(workspace)],
        "models": {
            "anthropic.qwen-aeon": {
                "label": "Claude Qwen",
                "argv": [str(QWEN_WRAPPER)],
            },
            "anthropic.ornith": {
                "label": "Claude Local · Ornith",
                "argv": [str(ORNITH_WRAPPER)],
            },
        },
    }, config_dir


def test_registry_rejects_symlink_or_non_private_file(tmp_path, monkeypatch):
    from api.claude_code_bridge import invalidate_claude_session_cache, load_claude_stores

    store, _config_dir = _valid_store(tmp_path)
    registry = tmp_path / "stores.json"
    _write_registry(registry, [store])
    registry.chmod(0o644)
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_STORES_FILE", str(registry))
    invalidate_claude_session_cache()
    assert load_claude_stores() == ()

    registry.chmod(0o600)
    registry_link = tmp_path / "stores-link.json"
    registry_link.symlink_to(registry)
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_STORES_FILE", str(registry_link))
    invalidate_claude_session_cache()
    assert load_claude_stores() == ()


def test_registry_keeps_config_dir_distinct_from_projects_dir(tmp_path, monkeypatch):
    from api.claude_code_bridge import invalidate_claude_session_cache, load_claude_stores

    store, config_dir = _valid_store(tmp_path)
    registry = tmp_path / "stores.json"
    _write_registry(registry, [store])
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_STORES_FILE", str(registry))
    invalidate_claude_session_cache()

    (valid_store,) = load_claude_stores()
    assert valid_store.config_dir == config_dir
    assert valid_store.config_dir.name == ".claude-local"
    assert valid_store.projects_dir == valid_store.config_dir / "projects"


def _bridge_session(store: dict, records: list[dict], *, project: str = "project-a") -> str:
    session_id = str(uuid4())
    path = Path(store["config_dir"]) / "projects" / project / f"{session_id}.jsonl"
    for record in records:
        record.setdefault("sessionId", session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return session_id


def _top_user(workspace: Path) -> dict:
    return {"cwd": str(workspace), "message": {"role": "user", "content": "hello"}}


def _assistant(model: object, **extra) -> dict:
    return {**extra, "message": {"role": "assistant", "model": model, "content": "answer"}}


def _configure_valid_store(tmp_path, monkeypatch) -> dict:
    from api.claude_code_bridge import invalidate_claude_session_cache

    store, _config_dir = _valid_store(tmp_path)
    registry = tmp_path / "stores.json"
    _write_registry(registry, [store])
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_STORES_FILE", str(registry))
    invalidate_claude_session_cache()
    return store


def test_latest_top_level_non_synthetic_model_selects_qwen(tmp_path, monkeypatch):
    from api.claude_code_bridge import list_public_sessions, resolve_session

    store = _configure_valid_store(tmp_path, monkeypatch)
    workspace = Path(store["workspace_roots"][0])
    _bridge_session(
        store,
        [
            _top_user(workspace),
            _assistant("anthropic.ornith"),
            _assistant("anthropic.qwen-aeon", isSidechain=True),
            _assistant("<synthetic>"),
            _assistant("anthropic.qwen-aeon"),
        ],
    )

    (row,) = list_public_sessions()
    descriptor = resolve_session(row["session_id"])
    assert descriptor is not None
    assert descriptor.profile is not None
    assert descriptor.profile.model_id == "anthropic.qwen-aeon"
    assert descriptor.can_remote_resume is True


def test_unknown_model_and_disallowed_cwd_are_not_resumable(tmp_path, monkeypatch):
    from api.claude_code_bridge import list_public_sessions, resolve_session

    store = _configure_valid_store(tmp_path, monkeypatch)
    workspace = Path(store["workspace_roots"][0])
    _bridge_session(store, [_top_user(workspace), _assistant("unmapped")], project="unknown")
    _bridge_session(
        store,
        [{"cwd": str(Path.home() / ".ssh"), "message": {"role": "user", "content": "hello"}}, _assistant("anthropic.qwen-aeon")],
        project="unsafe",
    )

    rows = list_public_sessions()
    assert len(rows) == 2
    assert all(resolve_session(row["session_id"]).can_remote_resume is False for row in rows)


def test_metadata_scan_continues_after_display_cap_and_public_rows_hide_internals(tmp_path, monkeypatch):
    from api.claude_code_bridge import list_public_sessions, resolve_session
    import api.claude_code_bridge as bridge

    store = _configure_valid_store(tmp_path, monkeypatch)
    workspace = Path(store["workspace_roots"][0])
    raw_uuid = _bridge_session(
        store,
        [_top_user(workspace), _assistant("anthropic.qwen-aeon")],
    )
    monkeypatch.setattr(bridge, "CLAUDE_CODE_MAX_MESSAGES_PER_FILE", 1)

    (row,) = list_public_sessions()
    descriptor = resolve_session(row["session_id"])
    assert descriptor is not None
    assert descriptor.profile is not None
    assert descriptor.profile.model_id == "anthropic.qwen-aeon"
    assert len(descriptor.messages) == 1
    assert row["profile"] == "qwen"
    assert row["label"] == "Claude Qwen"
    assert row["can_remote_resume"] is True
    assert row["workspace_label"] == "workspace"
    rendered = json.dumps(row)
    for secret in (raw_uuid, store["config_dir"], store["claude_bin"]):
        assert secret not in rendered


def test_duplicate_or_mismatched_transcript_uuid_is_not_resolved(tmp_path, monkeypatch):
    from api.claude_code_bridge import list_public_sessions

    store = _configure_valid_store(tmp_path, monkeypatch)
    workspace = Path(store["workspace_roots"][0])
    duplicate = _bridge_session(store, [_top_user(workspace), _assistant("anthropic.qwen-aeon")], project="one")
    duplicate_path = Path(store["config_dir"]) / "projects" / "two" / f"{duplicate}.jsonl"
    duplicate_path.parent.mkdir(parents=True)
    duplicate_path.write_text(
        json.dumps({"sessionId": duplicate, "cwd": str(workspace), "message": {"role": "user", "content": "other"}}) + "\n",
        encoding="utf-8",
    )
    mismatched = str(uuid4())
    wrong_name = str(uuid4())
    wrong_path = Path(store["config_dir"]) / "projects" / "three" / f"{wrong_name}.jsonl"
    wrong_path.parent.mkdir(parents=True)
    wrong_path.write_text(
        json.dumps({"sessionId": mismatched, "cwd": str(workspace), "message": {"role": "user", "content": "wrong"}}) + "\n",
        encoding="utf-8",
    )

    assert list_public_sessions() == []


def test_registry_requires_the_canonical_wrapper_for_each_model(tmp_path, monkeypatch):
    from api.claude_code_bridge import invalidate_claude_session_cache, load_claude_stores

    store, _config_dir = _valid_store(tmp_path)
    arbitrary_wrapper = tmp_path / "wrapper"
    arbitrary_wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    arbitrary_wrapper.chmod(0o700)
    store["models"]["anthropic.qwen-aeon"]["argv"] = [str(arbitrary_wrapper)]
    registry = tmp_path / "stores.json"
    _write_registry(registry, [store])
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_STORES_FILE", str(registry))
    invalidate_claude_session_cache()

    assert load_claude_stores() == ()


def test_registry_requires_owner_execute_permission(tmp_path):
    import api.claude_code_bridge as bridge

    wrapper = tmp_path / "owner-not-executable"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o001)

    assert bridge._safe_existing_path(str(wrapper), executable=True) is None


def test_registry_rejects_paths_not_owned_by_the_webui_uid(tmp_path, monkeypatch):
    import api.claude_code_bridge as bridge

    owned_file = tmp_path / "owned-by-current-user"
    owned_file.write_text("safe", encoding="utf-8")
    current_uid = os.getuid()
    monkeypatch.setattr(bridge.os, "getuid", lambda: current_uid + 1)

    assert bridge._safe_existing_path(str(owned_file)) is None


def test_registry_rejects_relative_parent_symlink_and_overlapping_store_paths(tmp_path, monkeypatch):
    from api.claude_code_bridge import invalidate_claude_session_cache, load_claude_stores

    store, config_dir = _valid_store(tmp_path)
    registry = tmp_path / "stores.json"
    store["config_dir"] = ".claude-local"
    _write_registry(registry, [store])
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_STORES_FILE", str(registry))
    invalidate_claude_session_cache()
    assert load_claude_stores() == ()

    store, _config_dir = _valid_store(tmp_path / "parent-link")
    target = tmp_path / "target"
    target.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(target, target_is_directory=True)
    store["workspace_roots"] = [str(linked_parent / "workspace")]
    _write_registry(registry, [store])
    invalidate_claude_session_cache()
    assert load_claude_stores() == ()

    first, _first_config = _valid_store(tmp_path / "first")
    second, _second_config = _valid_store(tmp_path / "second")
    second["id"] = "second"
    second["config_dir"] = first["config_dir"]
    _write_registry(registry, [first, second])
    invalidate_claude_session_cache()
    assert load_claude_stores() == ()


def test_unknown_model_before_latest_valid_model_remains_resumable(tmp_path, monkeypatch):
    from api.claude_code_bridge import list_public_sessions, resolve_session

    store = _configure_valid_store(tmp_path, monkeypatch)
    workspace = Path(store["workspace_roots"][0])
    _bridge_session(store, [_top_user(workspace), _assistant("unmapped"), _assistant("anthropic.qwen-aeon")])

    (row,) = list_public_sessions()
    descriptor = resolve_session(row["session_id"])
    assert descriptor is not None
    assert descriptor.can_remote_resume is True
    assert descriptor.profile is not None
    assert descriptor.profile.model_id == "anthropic.qwen-aeon"


def test_duplicate_after_display_limit_is_rejected_and_candidate_limit_fails_closed(tmp_path, monkeypatch):
    import api.claude_code_bridge as bridge

    store = _configure_valid_store(tmp_path, monkeypatch)
    workspace = Path(store["workspace_roots"][0])
    first = _bridge_session(store, [_top_user(workspace), _assistant("anthropic.qwen-aeon")], project="a")
    duplicate_path = Path(store["config_dir"]) / "projects" / "b" / f"{first}.jsonl"
    duplicate_path.parent.mkdir(parents=True)
    duplicate_path.write_text(
        json.dumps({"sessionId": first, "cwd": str(workspace), "message": {"role": "user", "content": "duplicate"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge, "CLAUDE_CODE_MAX_FILES", 1)

    assert bridge.list_public_sessions() == []

    monkeypatch.setattr(bridge, "CLAUDE_CODE_MAX_CANDIDATES", 1)
    assert bridge.list_public_sessions() == []


def test_cross_store_same_uuid_has_distinct_store_qualified_public_ids(tmp_path, monkeypatch):
    import api.claude_code_bridge as bridge

    first, _first_config = _valid_store(tmp_path / "first")
    second, _second_config = _valid_store(tmp_path / "second")
    second["id"] = "second-store"
    registry = tmp_path / "stores.json"
    _write_registry(registry, [first, second])
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_STORES_FILE", str(registry))
    session_id = str(uuid4())
    for store in (first, second):
        path = Path(store["config_dir"]) / "projects" / "project" / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"sessionId": session_id, "cwd": store["workspace_roots"][0], "message": {"role": "user", "content": "same UUID"}}) + "\n"
            + json.dumps({"sessionId": session_id, "message": {"role": "assistant", "model": "anthropic.qwen-aeon", "content": "answer"}}) + "\n",
            encoding="utf-8",
        )

    rows = bridge.list_public_sessions()
    assert len(rows) == 2
    assert len({row["session_id"] for row in rows}) == 2


def test_transcript_hardlinks_nested_subagents_and_nonfinite_timestamps_are_rejected(tmp_path, monkeypatch):
    import api.claude_code_bridge as bridge

    store = _configure_valid_store(tmp_path, monkeypatch)
    workspace = Path(store["workspace_roots"][0])
    hardlinked = _bridge_session(store, [_top_user(workspace), _assistant("anthropic.qwen-aeon")], project="hardlinked")
    original = Path(store["config_dir"]) / "projects" / "hardlinked" / f"{hardlinked}.jsonl"
    os.link(original, original.with_name(f"copy-{hardlinked}.jsonl"))
    _bridge_session(store, [_top_user(workspace), _assistant("anthropic.qwen-aeon")], project="main/subagents")

    assert bridge._parse_timestamp("NaN") is None
    assert bridge._parse_timestamp("Infinity") is None
    assert bridge.list_public_sessions() == []


def test_transcript_mutated_while_parsing_is_rejected(tmp_path, monkeypatch):
    import api.claude_code_bridge as bridge

    store = _configure_valid_store(tmp_path, monkeypatch)
    workspace = Path(store["workspace_roots"][0])
    session_id = _bridge_session(store, [_top_user(workspace), _assistant("anthropic.qwen-aeon")])
    transcript = Path(store["config_dir"]) / "projects" / "project-a" / f"{session_id}.jsonl"
    real_loads = bridge.json.loads
    calls = 0

    def append_after_first_record(value):
        nonlocal calls
        parsed = real_loads(value)
        if isinstance(value, str) and '"sessionId"' in value:
            calls += 1
        if calls == 1 and isinstance(value, str) and '"sessionId"' in value:
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"sessionId": session_id, "message": {"role": "assistant", "model": "anthropic.qwen-aeon", "content": "late"}}) + "\n")
        return parsed

    monkeypatch.setattr(bridge.json, "loads", append_after_first_record)

    assert bridge.list_public_sessions() == []


def test_registry_mutated_while_parsing_is_rejected(tmp_path, monkeypatch):
    import api.claude_code_bridge as bridge

    store, _config_dir = _valid_store(tmp_path)
    registry = tmp_path / "stores.json"
    _write_registry(registry, [store])
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_STORES_FILE", str(registry))
    real_loads = bridge.json.loads

    def mutate_registry(value):
        parsed = real_loads(value)
        if isinstance(value, str) and '"stores"' in value:
            with registry.open("a", encoding="utf-8") as handle:
                handle.write(" ")
        return parsed

    monkeypatch.setattr(bridge.json, "loads", mutate_registry)

    assert bridge.load_claude_stores() == ()


def test_transcript_file_and_line_limits_fail_closed(tmp_path, monkeypatch):
    import api.claude_code_bridge as bridge

    store = _configure_valid_store(tmp_path, monkeypatch)
    workspace = Path(store["workspace_roots"][0])
    _bridge_session(store, [_top_user(workspace), _assistant("anthropic.qwen-aeon")])
    monkeypatch.setattr(bridge, "CLAUDE_CODE_MAX_LINES_PER_FILE", 1)
    assert bridge.list_public_sessions() == []

    monkeypatch.setattr(bridge, "CLAUDE_CODE_MAX_LINES_PER_FILE", 100)
    monkeypatch.setattr(bridge, "CLAUDE_CODE_MAX_FILE_BYTES", 1)
    assert bridge.list_public_sessions() == []
