import json

from api import workspace


def test_workspaces_file_uses_global_state_across_profiles(tmp_path, monkeypatch):
    global_state = tmp_path / "webui"
    global_state.mkdir()
    global_ws_file = global_state / "workspaces.json"
    global_last_file = global_state / "last_workspace.txt"
    profile_state = tmp_path / "profiles" / "coder" / "webui_state"
    profile_state.mkdir(parents=True)

    monkeypatch.setattr(workspace, "_GLOBAL_WS_FILE", global_ws_file)
    monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", global_last_file)
    monkeypatch.setattr(workspace, "_legacy_profile_state_dirs", lambda: [profile_state])

    workspace.save_workspaces([{"path": str(tmp_path / "shared"), "name": "Shared"}])
    workspace.set_last_workspace(str(tmp_path))

    assert workspace._workspaces_file() == global_ws_file
    assert workspace._last_workspace_file() == global_last_file
    assert json.loads(global_ws_file.read_text(encoding="utf-8")) == [
        {"path": str(tmp_path / "shared"), "name": "Shared"}
    ]
    assert not (profile_state / "workspaces.json").exists()
    assert global_last_file.read_text(encoding="utf-8") == str(tmp_path)


def test_load_workspaces_merges_legacy_profile_spaces_into_global(tmp_path, monkeypatch):
    global_state = tmp_path / "webui"
    global_state.mkdir()
    global_ws_file = global_state / "workspaces.json"
    global_last_file = global_state / "last_workspace.txt"
    profile_state = tmp_path / "profiles" / "coder" / "webui_state"
    profile_state.mkdir(parents=True)
    home = tmp_path / "home"
    coder = tmp_path / "coder"
    home.mkdir()
    coder.mkdir()

    global_ws_file.write_text(
        json.dumps([{"path": str(home), "name": "Home"}]),
        encoding="utf-8",
    )
    (profile_state / "workspaces.json").write_text(
        json.dumps([
            "stale-entry",
            {"name": "Missing path"},
            {"path": str(coder), "name": "Coder"},
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(workspace, "_GLOBAL_WS_FILE", global_ws_file)
    monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", global_last_file)
    monkeypatch.setattr(workspace, "_legacy_profile_state_dirs", lambda: [profile_state])

    loaded = workspace.load_workspaces()

    assert loaded == [
        {"path": str(home.resolve()), "name": "Home"},
        {"path": str(coder.resolve()), "name": "Coder"},
    ]
    assert json.loads(global_ws_file.read_text(encoding="utf-8")) == loaded


def test_profile_workspace_migration_marker_waits_for_global_write_success(tmp_path, monkeypatch):
    global_state = tmp_path / "webui"
    global_state.mkdir()
    blocking_directory = global_state / "workspaces.json"
    blocking_directory.mkdir()
    profile_state = tmp_path / "profiles" / "coder" / "webui_state"
    profile_state.mkdir(parents=True)
    coder = tmp_path / "coder"
    coder.mkdir()
    (profile_state / "workspaces.json").write_text(
        json.dumps([{"path": str(coder), "name": "Coder"}]),
        encoding="utf-8",
    )

    monkeypatch.setattr(workspace, "_GLOBAL_WS_FILE", blocking_directory)
    monkeypatch.setattr(workspace, "_legacy_profile_state_dirs", lambda: [profile_state])

    loaded = workspace._migrate_profile_workspaces_into_global([])

    assert loaded == [{"path": str(coder.resolve()), "name": "Coder"}]
    assert not workspace._profile_workspace_migration_marker().exists()


def test_global_last_workspace_wins_over_legacy_profile_last_workspace(tmp_path, monkeypatch):
    global_state = tmp_path / "webui"
    global_state.mkdir()
    global_ws_file = global_state / "workspaces.json"
    global_last_file = global_state / "last_workspace.txt"
    profile_state = tmp_path / "profiles" / "coder" / "webui_state"
    profile_state.mkdir(parents=True)
    global_last = tmp_path / "global-last"
    profile_last = tmp_path / "profile-last"
    global_last.mkdir()
    profile_last.mkdir()
    global_last_file.write_text(str(global_last), encoding="utf-8")
    (profile_state / "last_workspace.txt").write_text(str(profile_last), encoding="utf-8")

    monkeypatch.setattr(workspace, "_GLOBAL_WS_FILE", global_ws_file)
    monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", global_last_file)
    monkeypatch.setattr(workspace, "_legacy_profile_state_dirs", lambda: [profile_state])

    assert workspace.get_last_workspace() == str(global_last)
