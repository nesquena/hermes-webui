"""Neo Sprint 5: local-first Projects Command Center API."""

import importlib
import json
import urllib.error
import urllib.request

from tests._pytest_port import BASE


def _json_request(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def get(path):
    return _json_request("GET", path)


def post(path, body=None):
    return _json_request("POST", path, body or {})


def patch(path, body=None):
    return _json_request("PATCH", path, body or {})


def test_project_store_migrates_legacy_list(tmp_path, monkeypatch):
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(
        json.dumps([
            {"project_id": "legacy123456", "name": "Legacy", "color": "#00E5FF", "created_at": 10}
        ]),
        encoding="utf-8",
    )
    module = importlib.import_module("api.projects")
    monkeypatch.setattr(module, "PROJECTS_FILE", projects_file)

    data = module.load_project_store()

    assert data["schema_version"] == 2
    assert data["projects"][0]["project_id"] == "legacy123456"
    assert data["projects"][0]["name"] == "Legacy"
    assert data["tasks"] == []
    assert data["sources"] == []


def test_projects_snapshot_starts_empty():
    data, status = get("/api/projects")

    assert status == 200
    assert set(["projects", "tasks", "sources", "counts"]).issubset(data)
    assert isinstance(data["projects"], list)
    assert isinstance(data["tasks"], list)


def test_create_project_and_task_with_external_ref_then_update_status():
    project_payload = {
        "name": "Brabus Performance Store",
        "description": "E-commerce de peças automotivas",
        "domain": "projetos",
        "color": "#00E5FF",
        "default_source_id": "jira_300",
    }
    project_data, project_status = post("/api/projects", project_payload)

    assert project_status == 200
    project = project_data["project"]
    assert project["project_id"].startswith("prj_")
    assert project["name"] == "Brabus Performance Store"
    assert project["default_source_id"] == "jira_300"

    external_ref = {
        "type": "jira",
        "source_id": "jira_300",
        "key": "KAN-123",
        "url": "https://jira.example/browse/KAN-123",
        "status": "To Do",
    }
    task_data, task_status = post("/api/project-tasks", {
        "project_id": project["project_id"],
        "title": "Implementar checkout",
        "status": "backlog",
        "priority": "alta",
        "category": "Backend",
        "owner": "jr",
        "progress": 10,
        "due_date": "2026-05-15",
        "external_ref": external_ref,
        "refs": {"github": ["https://github.com/melojrx/brabus"], "obsidian": [], "sessions": []},
    })

    assert task_status == 200
    task = task_data["task"]
    assert task["task_id"].startswith("tsk_")
    assert task["external_ref"]["key"] == "KAN-123"
    assert task["external_ref"]["synced_at"] is None

    updated_data, updated_status = patch(f"/api/project-tasks/{task['task_id']}", {
        "status": "em_andamento",
        "progress": 65,
    })

    assert updated_status == 200
    assert updated_data["task"]["status"] == "em_andamento"
    assert updated_data["task"]["progress"] == 65
    assert updated_data["task"]["external_ref"]["key"] == "KAN-123"

    snapshot, status = get("/api/projects")
    assert status == 200
    counts = snapshot["counts"]["by_status"]
    assert counts["em_andamento"] >= 1
    assert any(t["task_id"] == task["task_id"] for t in snapshot["tasks"])


def test_project_task_validation_rejects_unknown_status():
    data, status = post("/api/project-tasks", {
        "project_id": "missing",
        "title": "Invalid",
        "status": "doing",
    })

    assert status == 400
    assert "status" in data["error"].lower()


def test_project_task_requires_existing_project_id():
    data, status = post("/api/project-tasks", {
        "title": "Task without project",
        "status": "backlog",
    })

    assert status == 400
    assert "project_id" in data["error"].lower()

    data, status = post("/api/project-tasks", {
        "project_id": "missing",
        "title": "Task with missing project",
        "status": "backlog",
    })

    assert status == 400
    assert "project_id" in data["error"].lower()


def test_project_task_update_rejects_unknown_project_id():
    project_data, _ = post("/api/projects", {"name": "Validation Project"})
    project_id = project_data["project"]["project_id"]
    task_data, task_status = post("/api/project-tasks", {
        "project_id": project_id,
        "title": "Move validation",
        "status": "backlog",
    })
    assert task_status == 200

    data, status = patch(f"/api/project-tasks/{task_data['task']['task_id']}", {
        "project_id": "missing",
    })

    assert status == 400
    assert "project_id" in data["error"].lower()
