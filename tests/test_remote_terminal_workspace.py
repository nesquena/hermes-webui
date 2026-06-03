from pathlib import Path

import pytest

from api import config as api_config
from api import workspace


REMOTE_CWD = "/Users/joeyshiue"


def _remote_config(**overrides):
    cfg = {"terminal": {"backend": "ssh", "cwd": REMOTE_CWD}}
    cfg.update(overrides)
    return cfg


def test_remote_terminal_cwd_is_profile_default_without_local_stat(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    fallback.mkdir()

    monkeypatch.setattr(api_config, "DEFAULT_WORKSPACE", fallback)
    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config())

    assert workspace._profile_default_workspace() == REMOTE_CWD


def test_remote_terminal_last_workspace_ignores_stale_local_path(monkeypatch, tmp_path):
    stale_local = tmp_path / "stale-local"
    stale_local.mkdir()
    last_workspace = tmp_path / "last_workspace.txt"
    last_workspace.write_text(str(stale_local), encoding="utf-8")

    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config())
    monkeypatch.setattr(workspace, "_last_workspace_file", lambda: last_workspace)
    monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", tmp_path / "missing-global-last-workspace.txt")

    assert workspace.get_last_workspace() == REMOTE_CWD


def test_remote_terminal_workspace_paths_under_cwd_do_not_require_local_existence(monkeypatch):
    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config())

    target_side_project = f"{REMOTE_CWD}/projects/demo"

    assert workspace.validate_workspace_to_add(target_side_project) == Path(target_side_project).resolve()
    assert workspace.resolve_trusted_workspace(target_side_project) == Path(target_side_project).resolve()


def test_remote_terminal_workspace_paths_outside_cwd_still_reject(monkeypatch):
    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config())

    with pytest.raises(ValueError, match="Path does not exist"):
        workspace.validate_workspace_to_add("/Users/other/projects/demo")

    with pytest.raises(ValueError, match="Path does not exist"):
        workspace.resolve_trusted_workspace("/Users/other/projects/demo")
