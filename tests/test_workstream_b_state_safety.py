"""Focused Workstream B regressions for private state-file persistence."""

from __future__ import annotations

import json
import os
import stat
from collections import OrderedDict
from pathlib import Path

import pytest


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.workspace as workspace

    state_dir = tmp_path / "webui"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(config, "STATE_DIR", state_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(workspace, "_profile_state_dir", lambda: state_dir)
    return state_dir, session_dir, index_file


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _with_umask(mask: int):
    previous = os.umask(mask)

    class _Restore:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            os.umask(previous)

    return _Restore()


def test_session_create_replace_and_backup_are_private_under_permissive_umask(
    isolated_state,
):
    from api.models import Session

    _state_dir, session_dir, index_file = isolated_state
    sid = "private_state"
    with _with_umask(0o000):
        first = Session(
            session_id=sid,
            workspace="",
            model="test-model",
            messages=[{"role": "user", "content": "one"}],
        )
        first.save()

    session_path = session_dir / f"{sid}.json"
    assert _mode(session_path) == 0o600
    assert _mode(index_file) == 0o600

    session_path.chmod(0o644)
    index_file.chmod(0o644)
    replacement = Session(
        session_id=sid,
        workspace="",
        model="test-model",
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ],
    )
    replacement.save()
    assert _mode(session_path) == 0o600
    assert _mode(index_file) == 0o600

    session_path.chmod(0o644)
    shrinking = Session(
        session_id=sid,
        workspace="",
        model="test-model",
        messages=[{"role": "user", "content": "replacement"}],
    )
    shrinking.save()
    assert _mode(session_path) == 0o600
    assert _mode(session_path.with_suffix(".json.bak")) == 0o600


def test_workspace_metadata_create_and_replace_are_private_under_permissive_umask(
    isolated_state,
    tmp_path,
):
    import api.workspace as workspace

    state_dir, _session_dir, _index_file = isolated_state
    target_workspace = tmp_path / "workspace"
    target_workspace.mkdir()

    with _with_umask(0o000):
        workspace.save_workspaces([{"path": str(target_workspace), "name": "Home"}])
        workspace.set_last_workspace(str(target_workspace))

    workspaces_file = state_dir / "workspaces.json"
    last_workspace_file = state_dir / "last_workspace.txt"
    assert _mode(workspaces_file) == 0o600
    assert _mode(last_workspace_file) == 0o600

    workspaces_file.chmod(0o644)
    last_workspace_file.chmod(0o644)
    workspace.save_workspaces([{"path": str(target_workspace), "name": "Renamed"}])
    workspace.set_last_workspace(str(target_workspace))
    assert _mode(workspaces_file) == 0o600
    assert _mode(last_workspace_file) == 0o600


def test_recovery_replacement_is_private_under_permissive_umask(isolated_state):
    from api.models import Session
    from api.session_recovery import recover_all_sessions_on_startup, recover_session

    _state_dir, session_dir, index_file = isolated_state
    sid = "recover_private"
    live_path = session_dir / f"{sid}.json"
    backup_path = live_path.with_suffix(".json.bak")
    live = Session(
        session_id=sid,
        workspace="",
        model="test-model",
        messages=[{"role": "user", "content": "short"}],
    )
    live.save(skip_index=True)
    backup_path.write_text(
        json.dumps(
            {
                **json.loads(live_path.read_text(encoding="utf-8")),
                "messages": [
                    {"role": "user", "content": "long"},
                    {"role": "assistant", "content": "answer"},
                ],
            }
        ),
        encoding="utf-8",
    )
    live_path.chmod(0o644)
    backup_path.chmod(0o644)

    with _with_umask(0o000):
        result = recover_session(live_path)

    assert result["restored"] is True
    assert _mode(live_path) == 0o600
    assert _mode(backup_path) == 0o600

    recover_all_sessions_on_startup(session_dir, rebuild_index=True)
    assert _mode(index_file) == 0o600


def test_settings_create_is_private_under_permissive_umask(isolated_state):
    import api.config as config

    state_dir, _session_dir, _index_file = isolated_state
    settings_path = state_dir / "settings.json"

    with _with_umask(0o000):
        config._atomic_write_settings_text(settings_path, '{"theme": "dark"}')

    assert _mode(settings_path) == 0o600

    settings_path.chmod(0o644)
    config._atomic_write_settings_text(settings_path, '{"theme": "light"}')
    assert _mode(settings_path) == 0o600
