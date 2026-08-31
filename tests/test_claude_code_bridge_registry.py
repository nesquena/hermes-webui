from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4


def _write_registry(path: Path, stores: list[dict]) -> None:
    path.write_text(json.dumps({"stores": stores}), encoding="utf-8")
    path.chmod(0o600)


def _valid_store(tmp_path: Path) -> tuple[dict, Path]:
    config_dir = tmp_path / ".claude-local"
    projects_dir = config_dir / "projects"
    workspace = tmp_path / "workspace"
    projects_dir.mkdir(parents=True)
    workspace.mkdir()
    claude_bin = tmp_path / "claude"
    qwen_wrapper = tmp_path / "claude-qwen"
    ornith_wrapper = tmp_path / "claude-ornith"
    for executable in (claude_bin, qwen_wrapper, ornith_wrapper):
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
    return {
        "id": "local-models",
        "label": "Claude Local",
        "config_dir": str(config_dir),
        "claude_bin": str(claude_bin),
        "workspace_roots": [str(workspace)],
        "models": {
            "anthropic.qwen-aeon": {
                "label": "Claude Qwen",
                "argv": [str(qwen_wrapper)],
            },
            "anthropic.ornith": {
                "label": "Claude Local · Ornith",
                "argv": [str(ornith_wrapper)],
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
