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
