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


def test_remote_terminal_workspace_paths_with_parent_escape_still_reject(monkeypatch):
    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config())

    escaped = f"{REMOTE_CWD}/../other/projects/demo"

    with pytest.raises(ValueError, match="Path does not exist"):
        workspace.validate_workspace_to_add(escaped)

    with pytest.raises(ValueError, match="Path does not exist"):
        workspace.resolve_trusted_workspace(escaped)


@pytest.mark.parametrize("workspace_path", ["/etc", "/etc/ssh"])
def test_remote_terminal_workspace_system_roots_still_reject(monkeypatch, workspace_path):
    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config(terminal={"backend": "ssh", "cwd": "/etc"}))

    with pytest.raises(ValueError, match="Path points to a system directory"):
        workspace.validate_workspace_to_add(workspace_path)

    with pytest.raises(ValueError, match="Path points to a system directory"):
        workspace.resolve_trusted_workspace(workspace_path)


@pytest.mark.parametrize("validator", [workspace.resolve_trusted_workspace, workspace.validate_workspace_to_add])
def test_var_home_workspaces_stay_allowed_before_system_root_blocklist(monkeypatch, validator):
    home = Path("/var/home/joeyshiue")
    candidate = home / "projects/demo"

    monkeypatch.setattr(workspace, "_resolve_path", lambda raw: candidate if str(raw) == str(candidate) else Path(raw))
    monkeypatch.setattr(workspace, "_home_path", lambda: home)
    monkeypatch.setattr(workspace, "_workspace_access_error", lambda _candidate: None)

    assert validator(str(candidate)) == candidate
