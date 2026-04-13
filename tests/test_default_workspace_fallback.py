import json
from pathlib import Path

import api.config as config


def test_resolve_default_workspace_falls_back_to_existing_home_work(monkeypatch, tmp_path):
    preferred = tmp_path / "work"
    preferred.mkdir()
    state_dir = tmp_path / "state"

    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)

    resolved = config.resolve_default_workspace("/definitely/not/usable")

    assert resolved == preferred.resolve()



def test_save_settings_rewrites_bad_default_workspace_to_fallback(monkeypatch, tmp_path):
    preferred = tmp_path / "work"
    preferred.mkdir()
    state_dir = tmp_path / "state"
    settings_file = tmp_path / "settings.json"

    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", preferred)

    saved = config.save_settings({"default_workspace": "/definitely/not/usable"})
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))

    assert saved["default_workspace"] == str(preferred.resolve())
    assert on_disk["default_workspace"] == str(preferred.resolve())


def test_resolve_default_workspace_creates_home_workspace_when_missing(monkeypatch, tmp_path):
    """When no preferred dir exists, resolve falls back to creating ~/workspace."""
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    # Neither ~/work nor ~/workspace exists yet
    resolved = config.resolve_default_workspace(None)
    assert resolved == (tmp_path / "workspace").resolve()
    assert resolved.is_dir()


def test_resolve_default_workspace_raises_when_all_candidates_fail(monkeypatch, tmp_path):
    """RuntimeError is raised when every candidate is unwritable."""
    import stat, pytest
    # Make tmp_path read-only so mkdir inside it fails
    tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.delenv("HERMES_WEBUI_DEFAULT_WORKSPACE", raising=False)
    try:
        with pytest.raises(RuntimeError, match="Could not create or access"):
            config.resolve_default_workspace(None)
    finally:
        tmp_path.chmod(stat.S_IRWXU)  # restore for cleanup


def test_workspace_candidates_deduplicates_home_workspace(monkeypatch, tmp_path):
    """~/workspace must appear at most once in the candidates list even if it exists."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.delenv("HERMES_WEBUI_DEFAULT_WORKSPACE", raising=False)
    candidates = config._workspace_candidates(None)
    paths = [str(p) for p in candidates]
    assert paths.count(str(ws.resolve())) <= 1, "~/workspace must not appear twice"


def test_env_var_workspace_takes_priority_over_passed_raw(monkeypatch, tmp_path):
    """HERMES_WEBUI_DEFAULT_WORKSPACE env var overrides a None raw arg but not a valid one."""
    env_ws = tmp_path / "env_workspace"
    env_ws.mkdir()
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setenv("HERMES_WEBUI_DEFAULT_WORKSPACE", str(env_ws))
    # When raw is None, env var should be used
    resolved = config.resolve_default_workspace(None)
    assert resolved == env_ws.resolve()


def test_ensure_workspace_dir_returns_false_for_unwritable_path(monkeypatch, tmp_path):
    """_ensure_workspace_dir returns False for a path that can't be created."""
    import stat
    # Make parent read-only so mkdir fails
    parent = tmp_path / "ro_parent"
    parent.mkdir()
    parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        result = config._ensure_workspace_dir(parent / "child")
        assert result is False
    finally:
        parent.chmod(stat.S_IRWXU)
