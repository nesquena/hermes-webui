"""Tests for project bindings (/api/projects/bind).

A project can pin a workspace / model / reasoning effort so the quick-create
(+) button opens a new session pre-configured with that project's context.

- Bind fields persist to projects.json.
- workspace binding auto-registers the path in the saved workspace list.
- Invalid effort / nonexistent workspace are rejected.
- null/'' unbinds a field.
"""

import json
import urllib.error
import urllib.request

from tests._pytest_port import BASE


def _get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {}


def _post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {}


def _create_project():
    res = _post("/api/projects/create", {"name": "bind-test", "color": "#50c878"})
    assert res.get("ok"), f"create failed: {res}"
    return res["project"]["project_id"]


def test_bind_workspace_model_effort_roundtrip():
    pid = _create_project()
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "workspace": "C:/Users/Admin/workspace",
        "model": "test-model-1",
        "model_provider": "custom:test",
        "reasoning_effort": "high",
    })
    assert res.get("ok"), f"bind failed: {res}"
    p = res["project"]
    assert p["workspace"] == "C:\\Users\\Admin\\workspace"
    assert p["model"] == "test-model-1"
    assert p["model_provider"] == "custom:test"
    assert p["reasoning_effort"] == "high"

    # Persisted on disk via GET /api/projects
    proj_res = _get("/api/projects")
    projects = proj_res.get("projects", [])
    saved = next((x for x in projects if x["project_id"] == pid), None)
    assert saved is not None, "bound project must appear in /api/projects"
    assert saved.get("reasoning_effort") == "high"


def test_bind_workspace_auto_registers_in_workspace_list(tmp_path):
    """A workspace outside the default saved list is auto-registered so an
    admin-style path (e.g. D:\\projects\\...) can be bound in one step."""
    pid = _create_project()
    import pathlib
    ws_dir = tmp_path / "bound-ws"
    ws_dir.mkdir()
    ws_str = str(ws_dir).replace("/", "\\")
    res = _post("/api/projects/bind", {"project_id": pid, "workspace": ws_str})
    assert res.get("ok"), f"bind failed: {res}"
    assert pathlib.Path(res["project"]["workspace"]).resolve() == ws_dir.resolve()

    # The path must now be in the saved workspace list.
    ws_res = _get("/api/workspaces")
    assert any(
        pathlib.Path(w["path"]).resolve() == ws_dir.resolve()
        for w in ws_res.get("workspaces", [])
    ), "bound workspace must be auto-registered in the workspace list"


def test_bind_rejects_invalid_effort():
    pid = _create_project()
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "reasoning_effort": "insane",
    })
    assert "error" in res, "invalid effort must be rejected"
    assert "reasoning_effort" in res["error"]


def test_bind_rejects_nonexistent_workspace():
    pid = _create_project()
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "workspace": "Z:/definitely/not/here",
    })
    assert "error" in res, "nonexistent workspace must be rejected"


def test_bind_null_unbinds_field():
    pid = _create_project()
    _post("/api/projects/bind", {
        "project_id": pid,
        "workspace": "C:/Users/Admin/workspace",
        "reasoning_effort": "medium",
    })
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "workspace": None,
        "reasoning_effort": None,
    })
    assert res.get("ok"), f"unbind failed: {res}"
    p = res["project"]
    assert "workspace" not in p
    assert "reasoning_effort" not in p


def test_bind_unknown_project_404():
    res = _post("/api/projects/bind", {
        "project_id": "deadbeef0000",
        "workspace": "C:/Users/Admin/workspace",
    })
    assert "error" in res, "unknown project must be rejected"


def test_bind_multiple_workspaces_with_default(tmp_path):
    """A project can bind several workspaces; the marked default drives new
    sessions, and default must always be a member of the workspaces list."""
    pid = _create_project()
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()
    a, b = str(ws_a).replace("/", "\\"), str(ws_b).replace("/", "\\")

    res = _post("/api/projects/bind", {
        "project_id": pid,
        "workspaces": [a, b],
        "default_workspace": b,
        "auto_assign": True,
    })
    assert res.get("ok"), f"bind failed: {res}"
    p = res["project"]
    assert set(p["workspaces"]) == {a, b}, p["workspaces"]
    assert p["default_workspace"] == b
    assert p.get("auto_assign") is True

    # Default can also auto-add itself to the list (invariant holds).
    res2 = _post("/api/projects/bind", {
        "project_id": pid,
        "default_workspace": a,
    })
    p2 = res2["project"]
    assert p2["default_workspace"] == a
    assert a in p2["workspaces"]


def test_bind_workspaces_clear_with_null():
    pid = _create_project()
    _post("/api/projects/bind", {
        "project_id": pid,
        "workspaces": ["C:/Users/Admin/workspace"],
        "default_workspace": "C:/Users/Admin/workspace",
    })
    res = _post("/api/projects/bind", {"project_id": pid, "workspaces": None})
    assert res.get("ok"), f"unbind failed: {res}"
    p = res["project"]
    assert "workspaces" not in p
    assert "default_workspace" not in p


def test_bind_auto_assign_off():
    pid = _create_project()
    _post("/api/projects/bind", {
        "project_id": pid,
        "workspaces": ["C:/Users/Admin/workspace"],
        "auto_assign": True,
    })
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "auto_assign": False,
    })
    assert res.get("ok")
    assert "auto_assign" not in res["project"]


def test_auto_assign_project_for_workspace(tmp_path, monkeypatch):
    """_auto_assign_project_for_workspace picks the owning project in list order."""
    import api.routes as routes

    pid1 = _create_project()
    pid2 = _create_project()
    ws = tmp_path / "shared"
    ws.mkdir()
    ws_str = str(ws).replace("/", "\\")
    _post("/api/projects/bind", {
        "project_id": pid1,
        "workspaces": [ws_str],
        "auto_assign": True,
    })
    _post("/api/projects/bind", {
        "project_id": pid2,
        "workspaces": [ws_str],
        "auto_assign": True,
    })

    # First match in on-disk list order wins; reload from disk to avoid cache.
    assert routes._auto_assign_project_for_workspace(ws_str) in (pid1, pid2)

    # Unclaimed workspace → None
    assert routes._auto_assign_project_for_workspace("Z:/not/claimed") is None
    # Disabled flag → None
    _post("/api/projects/bind", {"project_id": pid1, "auto_assign": False})
    _post("/api/projects/bind", {"project_id": pid2, "auto_assign": False})
    assert routes._auto_assign_project_for_workspace(ws_str) is None


def test_apply_project_auto_assign_files_existing_sessions(tmp_path, monkeypatch):
    """_apply_project_auto_assign re-files existing sessions whose workspace
    is bound, skipping cross-profile rows and already-owned sessions."""
    import api.routes as routes

    # Point the index at a temp file with three sessions: two in the bound
    # workspace, one in another workspace.
    ws = tmp_path / "bound-ws"
    ws.mkdir()
    ws_str = str(ws).replace("/", "\\")
    index_file = tmp_path / "_index.json"
    index_file.write_text(json.dumps([
        {"session_id": "sess_aaa", "workspace": ws_str, "profile": "default",
         "project_id": None, "message_count": 3},
        {"session_id": "sess_bbb", "workspace": ws_str, "profile": "default",
         "project_id": "already-owned", "message_count": 1},
        {"session_id": "sess_ccc", "workspace": "C:/Users/Admin/workspace",
         "profile": "default", "project_id": None, "message_count": 2},
        {"session_id": "sess_ddd", "workspace": ws_str, "profile": "other",
         "project_id": None, "message_count": 4},
    ]))
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)

    saved = {}
    class _FakeSession:
        def __init__(self, sid):
            self.session_id = sid
            self.project_id = None
        def save(self):
            saved[self.session_id] = self.project_id

    monkeypatch.setattr(routes, "get_session",
                        lambda sid: _FakeSession(sid) if sid in (
                            "sess_aaa", "sess_bbb", "sess_ccc", "sess_ddd") else None)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    proj = {"project_id": "proj_xyz", "profile": "default",
            "workspaces": [ws_str], "auto_assign": True}
    changed = routes._apply_project_auto_assign(proj)

    # sess_aaa: bound ws, unowned → re-filed.
    assert saved.get("sess_aaa") == "proj_xyz"
    # sess_bbb: already owned by another project → untouched.
    assert "sess_bbb" not in saved
    # sess_ccc: different workspace → untouched.
    assert "sess_ccc" not in saved
    # sess_ddd: other profile → untouched.
    assert "sess_ddd" not in saved
    assert changed == 1
