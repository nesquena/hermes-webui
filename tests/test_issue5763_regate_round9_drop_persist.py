"""Round 9 (#5763): saved rows must not carry project_id.

Persisting the native id made the picker send it when the store is disabled
or the project is gone. The id lanes then require a DB write, so
remove/rename/reorder fail or silently ignore the requested order.

Load and fallback merge keep path/name/project_mirror only. The picker then
sends path-only actions, which stay local when sync is off.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import api.projects_bridge as bridge
from api.workspace import load_workspaces, save_workspaces


def _make_handler():
    h = MagicMock()
    h._headers_buffer = []
    h.wfile = MagicMock()
    captured = {}
    h.send_response.side_effect = lambda code: captured.__setitem__("code", code)
    h.end_headers.side_effect = lambda: None
    h.wfile.write.side_effect = lambda data: captured.__setitem__(
        "body", json.loads(data.decode("utf-8")))
    h._captured = captured
    return h


def _code(h):
    return h.send_response.call_args[0][0]


def _response(h):
    return h._captured.get("body", {})


def _picker_action(row):
    body = {"path": row["path"]}
    pid = str(row.get("project_id") or "").strip()
    if pid:
        body["project_id"] = pid
    kind = str(row.get("workspace_kind") or "").strip()
    if kind:
        body["workspace_kind"] = kind
    return body


def _setup_kill_switch(tmp_path, monkeypatch, rows):
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "0")
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr("api.workspace._remote_terminal_cwd", lambda profile=None: None)
    bridge._cache.clear()
    save_workspaces(rows, profile="default")
    loaded = load_workspaces(profile="default")
    return loaded, bridge.merge_hermes_projects(
        loaded, profile_home=tmp_path, profile="default")


def test_load_and_fallback_merge_drop_saved_project_id(tmp_path, monkeypatch):
    host = tmp_path / "proj"
    host.mkdir()
    loaded, projected = _setup_kill_switch(tmp_path, monkeypatch, [
        {"path": str(host), "name": "A", "project_mirror": True, "project_id": "proj-a"},
    ])
    assert loaded and "project_id" not in loaded[0], loaded
    assert loaded[0].get("project_mirror") is True
    assert len(projected) == 1
    assert "project_id" not in projected[0], projected
    assert "project_id" not in _picker_action(projected[0])


def test_kill_switch_picker_can_remove_saved_mirror(tmp_path, monkeypatch):
    host = tmp_path / "proj"
    host.mkdir()
    _loaded, projected = _setup_kill_switch(tmp_path, monkeypatch, [
        {"path": str(host), "name": "A", "project_mirror": True, "project_id": "proj-a"},
    ])
    from api.routes import _handle_workspace_remove
    h = _make_handler()
    _handle_workspace_remove(h, _picker_action(projected[0]))
    assert _code(h) == 200, _response(h)
    leftover = load_workspaces(profile="default")
    host_key = str(host.resolve())
    assert all(Path(w["path"]).resolve() != Path(host_key) for w in leftover), leftover


def test_kill_switch_picker_can_rename_saved_mirror(tmp_path, monkeypatch):
    host = tmp_path / "proj"
    host.mkdir()
    _loaded, projected = _setup_kill_switch(tmp_path, monkeypatch, [
        {"path": str(host), "name": "A", "project_mirror": True, "project_id": "proj-a"},
    ])
    from api.routes import _handle_workspace_rename
    h = _make_handler()
    body = _picker_action(projected[0])
    body["name"] = "Renamed"
    _handle_workspace_rename(h, body)
    assert _code(h) == 200, _response(h)
    saved = load_workspaces(profile="default")
    assert any(w.get("name") == "Renamed" for w in saved), saved


def test_kill_switch_picker_can_reorder_saved_mirrors(tmp_path, monkeypatch):
    dirs = {}
    for name in ("A", "B", "C"):
        d = tmp_path / name
        d.mkdir()
        dirs[name] = str(d)
    _loaded, projected = _setup_kill_switch(tmp_path, monkeypatch, [
        {"path": dirs["A"], "name": "A", "project_mirror": True, "project_id": "id-a"},
        {"path": dirs["B"], "name": "B", "project_mirror": True, "project_id": "id-b"},
        {"path": dirs["C"], "name": "C", "project_mirror": True, "project_id": "id-c"},
    ])
    by_name = {w["name"]: w for w in projected}
    requested = [by_name["A"], by_name["C"], by_name["B"]]
    from api.routes import _handle_workspace_reorder
    h = _make_handler()
    _handle_workspace_reorder(h, {"paths": [_picker_action(w) for w in requested]})
    assert _code(h) == 200, _response(h)
    saved = [w for w in load_workspaces(profile="default") if w.get("name") in ("A", "B", "C")]
    assert [w.get("name") for w in saved] == ["A", "C", "B"], saved
