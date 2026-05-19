"""Regression coverage for Jira sync operational failures."""

import json
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch

import pytest


def _base_store(sync_enabled=True, tasks=None):
    return {
        "schema_version": 2,
        "sources": [{
            "source_id": "jira_test",
            "type": "jira",
            "name": "Test Jira",
            "base_url": "https://test.atlassian.net",
            "project_key": "TST",
            "sync_enabled": sync_enabled,
            "sync_mode": "read",
            "status_map": {"To Do": "backlog", "In Progress": "em_andamento", "Done": "concluido"},
            "last_sync_at": None,
            "sync_status": "idle",
            "sync_error": None,
        }],
        "projects": [{
            "project_id": "prj_test1",
            "name": "Test Project",
            "description": "",
            "domain": "projetos",
            "status": "ativo",
            "color": "#00E5FF",
            "default_source_id": "jira_test",
            "refs": {"github": [], "obsidian": [], "sessions": []},
            "created_at": 1700000000,
            "updated_at": 1700000000,
            "archived": False,
        }],
        "tasks": tasks or [],
        "activity": [],
    }


@pytest.fixture
def tmp_projects_file(tmp_path, monkeypatch):
    pf = tmp_path / "projects.json"
    pf.write_text(json.dumps(_base_store()), encoding="utf-8")
    monkeypatch.setattr("api.config.PROJECTS_FILE", pf)
    monkeypatch.setattr("api.projects.PROJECTS_FILE", pf)
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")
    return pf


def test_jira_sync_route_returns_http_error_when_sync_result_has_error(monkeypatch):
    from api import routes

    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status
        return True

    handler = SimpleNamespace(headers={"Content-Length": "0"}, rfile=BytesIO(b""))
    parsed = urlparse("/api/jira/sync/jira_test")

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(
        routes.neo_jira,
        "handle_jira_sync",
        lambda source_id: {"error": "Jira unavailable", "synced": 0, "created": 0, "updated": 0},
    )

    routes.handle_post(handler, parsed)

    assert captured["status"] >= 400
    assert captured["payload"]["error"] == "Jira unavailable"
    assert captured["payload"].get("ok") is not True


@patch("api.jira.JiraClient.search_issues")
def test_sync_source_refuses_disabled_source(mock_search, tmp_projects_file):
    tmp_projects_file.write_text(json.dumps(_base_store(sync_enabled=False)), encoding="utf-8")

    from api.jira import sync_source

    result = sync_source("jira_test")

    assert result["error"] == "Jira source jira_test is disabled"
    assert result["error_code"] == "source_disabled"
    assert result["synced"] == 0
    mock_search.assert_not_called()

    store = json.loads(tmp_projects_file.read_text(encoding="utf-8"))
    source = store["sources"][0]
    assert source["sync_status"] == "error"
    assert source["sync_error"] == "Jira source jira_test is disabled"


@patch("api.jira.JiraClient.search_issues")
def test_sync_source_updates_progress_when_existing_issue_becomes_done(mock_search, tmp_projects_file):
    existing_task = {
        "task_id": "tsk_existing",
        "project_id": "prj_test1",
        "title": "Old title",
        "description": "",
        "status": "backlog",
        "priority": "media",
        "category": "Backend",
        "owner": "jr",
        "progress": 0,
        "due_date": "",
        "external_ref": {
            "type": "jira",
            "source_id": "jira_test",
            "key": "TST-1",
            "url": "https://test.atlassian.net/browse/TST-1",
            "status": "To Do",
            "synced_at": None,
        },
        "refs": {"github": [], "obsidian": [], "sessions": []},
        "created_at": 1700000000,
        "updated_at": 1700000000,
        "archived": False,
    }
    tmp_projects_file.write_text(json.dumps(_base_store(tasks=[existing_task])), encoding="utf-8")
    mock_search.return_value = [{
        "key": "TST-1",
        "fields": {
            "summary": "Done issue",
            "status": {"name": "Done"},
            "assignee": None,
            "priority": {"name": "Medium"},
        },
    }]

    from api.jira import sync_source

    result = sync_source("jira_test")

    assert result["updated"] == 1
    store = json.loads(tmp_projects_file.read_text(encoding="utf-8"))
    task = store["tasks"][0]
    assert task["status"] == "concluido"
    assert task["progress"] == 100


def test_jira_client_search_issues_follows_next_page_token(monkeypatch):
    from api.jira import JiraClient

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    requests = []
    responses = iter([
        FakeResponse({"issues": [{"key": "TST-1", "fields": {}}], "nextPageToken": "page-2"}),
        FakeResponse({"issues": [{"key": "TST-2", "fields": {}}]}),
    ])

    def fake_urlopen(req, timeout):
        requests.append(json.loads(req.data.decode("utf-8")))
        return next(responses)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")

    client = JiraClient("https://test.atlassian.net")
    issues = client.search_issues("project = TST", max_results=50)

    assert [issue["key"] for issue in issues] == ["TST-1", "TST-2"]
    assert requests[0].get("nextPageToken") is None
    assert requests[1]["nextPageToken"] == "page-2"


def test_jira_client_rate_limit_retry_is_bounded(monkeypatch):
    import email.message
    import urllib.error
    from io import BytesIO

    import api.jira as jira
    from api.jira import JiraClient

    calls = 0

    def fake_urlopen(req, timeout):
        nonlocal calls
        calls += 1
        headers = email.message.Message()
        headers["Retry-After"] = "0"
        raise urllib.error.HTTPError(
            req.full_url,
            429,
            "Too Many Requests",
            headers,
            BytesIO(b"rate limited"),
        )

    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")
    monkeypatch.setattr(jira, "_MAX_RATE_LIMIT_RETRIES", 2)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    client = JiraClient("https://test.atlassian.net")

    with pytest.raises(RuntimeError, match="rate limit persisted after 2 retries"):
        client._request("GET", "/myself")

    assert calls == 3


@patch("api.jira.JiraClient.search_issues")
def test_sync_source_discovers_projects_from_epics_and_routes_children(mock_search, tmp_projects_file):
    mock_search.return_value = [
        {
            "key": "TST-100",
            "fields": {
                "summary": "Obreiro Virtual",
                "issuetype": {"name": "Epic"},
                "status": {"name": "In Progress"},
                "assignee": None,
                "priority": {"name": "High"},
            },
        },
        {
            "key": "TST-101",
            "fields": {
                "summary": "Cadastro de membros",
                "issuetype": {"name": "Task"},
                "parent": {"key": "TST-100", "fields": {"summary": "Obreiro Virtual", "issuetype": {"name": "Epic"}}},
                "status": {"name": "To Do"},
                "assignee": {"displayName": "Junior"},
                "priority": {"name": "Medium"},
            },
        },
        {
            "key": "TST-200",
            "fields": {
                "summary": "Abratens",
                "issuetype": {"name": "Epic"},
                "status": {"name": "To Do"},
                "assignee": None,
                "priority": {"name": "Medium"},
            },
        },
        {
            "key": "TST-201",
            "fields": {
                "summary": "Cobrança de associados",
                "issuetype": {"name": "Story"},
                "parent": {"key": "TST-200", "fields": {"summary": "Abratens", "issuetype": {"name": "Epic"}}},
                "status": {"name": "Done"},
                "assignee": None,
                "priority": {"name": "High"},
            },
        },
    ]

    from api.jira import sync_source

    result = sync_source("jira_test")

    assert result["synced"] == 4
    assert result["projects_created"] == 2
    assert result["created"] == 2  # epics viram projetos, não tasks

    store = json.loads(tmp_projects_file.read_text(encoding="utf-8"))
    projects_by_key = {
        p.get("external_ref", {}).get("key"): p
        for p in store["projects"]
        if (p.get("external_ref") or {}).get("type") == "jira"
    }
    assert projects_by_key["TST-100"]["name"] == "Obreiro Virtual"
    assert projects_by_key["TST-200"]["name"] == "Abratens"

    task_by_key = {t["external_ref"]["key"]: t for t in store["tasks"]}
    assert task_by_key["TST-101"]["project_id"] == projects_by_key["TST-100"]["project_id"]
    assert task_by_key["TST-201"]["project_id"] == projects_by_key["TST-200"]["project_id"]
    assert task_by_key["TST-201"]["progress"] == 100


@patch("api.jira.JiraClient.search_issues")
def test_sync_source_routes_unparented_issue_to_component_project(mock_search, tmp_projects_file):
    mock_search.return_value = [{
        "key": "TST-50",
        "fields": {
            "summary": "Ajustar dashboard",
            "issuetype": {"name": "Task"},
            "components": [{"name": "Neo WebUI"}],
            "labels": ["backend"],
            "status": {"name": "In Progress"},
            "assignee": None,
            "priority": {"name": "Medium"},
        },
    }]

    from api.jira import sync_source

    result = sync_source("jira_test")

    assert result["projects_created"] == 1
    store = json.loads(tmp_projects_file.read_text(encoding="utf-8"))
    project = next(p for p in store["projects"] if p["name"] == "Neo WebUI")
    task = next(t for t in store["tasks"] if t["external_ref"]["key"] == "TST-50")
    assert task["project_id"] == project["project_id"]
    assert project["external_ref"]["grouping"] == "component"


def test_jira_client_search_issues_requests_grouping_fields(monkeypatch):
    from api.jira import JiraClient

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def read(self):
            return json.dumps({"issues": []}).encode("utf-8")

    captured = {}
    def fake_urlopen(req, timeout):
        captured.update(json.loads(req.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")

    JiraClient("https://test.atlassian.net").search_issues("project = TST")

    for field in ["issuetype", "parent", "components", "labels"]:
        assert field in captured["fields"]


@patch("api.jira.JiraClient.search_issues")
def test_sync_source_routes_subtask_through_parent_task_to_epic(mock_search, tmp_projects_file):
    mock_search.return_value = [
        {
            "key": "TST-10",
            "fields": {
                "summary": "Obreiro Virtual",
                "issuetype": {"name": "Épico"},
                "status": {"name": "In Progress"},
                "assignee": None,
                "priority": {"name": "High"},
            },
        },
        {
            "key": "TST-20",
            "fields": {
                "summary": "API pública",
                "issuetype": {"name": "Tarefa"},
                "parent": {"key": "TST-10", "fields": {"summary": "Obreiro Virtual", "issuetype": {"name": "Épico"}}},
                "status": {"name": "In Progress"},
                "assignee": None,
                "priority": {"name": "Medium"},
            },
        },
        {
            "key": "TST-21",
            "fields": {
                "summary": "Subtarefa da API",
                "issuetype": {"name": "Subtask"},
                "parent": {"key": "TST-20", "fields": {"summary": "API pública", "issuetype": {"name": "Tarefa"}}},
                "status": {"name": "To Do"},
                "assignee": None,
                "priority": {"name": "Medium"},
            },
        },
    ]

    from api.jira import sync_source

    result = sync_source("jira_test")

    assert result["projects_created"] == 1
    store = json.loads(tmp_projects_file.read_text(encoding="utf-8"))
    epic_project = next(p for p in store["projects"] if (p.get("external_ref") or {}).get("key") == "TST-10")
    task = next(t for t in store["tasks"] if t["external_ref"]["key"] == "TST-21")
    assert task["project_id"] == epic_project["project_id"]


@patch("api.jira.JiraClient.search_issues")
def test_sync_source_ignores_labels_as_project_grouping_by_default(mock_search, tmp_projects_file):
    mock_search.return_value = [{
        "key": "TST-70",
        "fields": {
            "summary": "Tarefa administrativa",
            "issuetype": {"name": "Task"},
            "labels": ["ADM"],
            "status": {"name": "To Do"},
            "assignee": None,
            "priority": {"name": "Medium"},
        },
    }]

    from api.jira import sync_source

    result = sync_source("jira_test")

    assert result["projects_created"] == 0
    store = json.loads(tmp_projects_file.read_text(encoding="utf-8"))
    assert not any((p.get("external_ref") or {}).get("key") == "label:ADM" for p in store["projects"])
    task = next(t for t in store["tasks"] if t["external_ref"]["key"] == "TST-70")
    assert task["project_id"] == "prj_test1"


@patch("api.jira.JiraClient.search_issues")
def test_sync_source_can_collapse_multiple_epics_into_configured_project_group(mock_search, tmp_projects_file):
    store = json.loads(tmp_projects_file.read_text(encoding="utf-8"))
    store["sources"][0]["project_groups"] = [
        {
            "key": "obreiro-virtual",
            "name": "Obreiro Virtual",
            "name_contains": ["[obreiro]"],
        }
    ]
    tmp_projects_file.write_text(json.dumps(store), encoding="utf-8")
    mock_search.return_value = [
        {
            "key": "TST-40",
            "fields": {
                "summary": "[obreiro] - Equipes",
                "issuetype": {"name": "Épico"},
                "status": {"name": "In Progress"},
                "assignee": None,
                "priority": {"name": "High"},
            },
        },
        {
            "key": "TST-41",
            "fields": {
                "summary": "[obreiro] - Cadastro de membros",
                "issuetype": {"name": "Épico"},
                "status": {"name": "In Progress"},
                "assignee": None,
                "priority": {"name": "High"},
            },
        },
        {
            "key": "TST-42",
            "fields": {
                "summary": "Criar tela de equipes",
                "issuetype": {"name": "Task"},
                "parent": {"key": "TST-40", "fields": {"summary": "[obreiro] - Equipes", "issuetype": {"name": "Épico"}}},
                "status": {"name": "To Do"},
                "assignee": None,
                "priority": {"name": "Medium"},
            },
        },
    ]

    from api.jira import sync_source

    result = sync_source("jira_test")

    assert result["projects_created"] == 1
    store = json.loads(tmp_projects_file.read_text(encoding="utf-8"))
    projects = [p for p in store["projects"] if (p.get("external_ref") or {}).get("key") == "obreiro-virtual"]
    assert len(projects) == 1
    assert projects[0]["name"] == "Obreiro Virtual"
    assert (projects[0].get("external_ref") or {}).get("grouping") == "group"
    task = next(t for t in store["tasks"] if t["external_ref"]["key"] == "TST-42")
    assert task["project_id"] == projects[0]["project_id"]


@patch("api.jira.JiraClient.search_issues")
def test_sync_source_can_group_issues_linked_to_configured_root_key(mock_search, tmp_projects_file):
    store = json.loads(tmp_projects_file.read_text(encoding="utf-8"))
    store["sources"][0]["project_groups"] = [
        {
            "key": "abratens",
            "name": "Abratens",
            "project_id": "prj_test1",
            "keys": ["TST-101"],
            "linked_keys": ["TST-101"],
        }
    ]
    tmp_projects_file.write_text(json.dumps(store), encoding="utf-8")
    mock_search.return_value = [
        {
            "key": "TST-101",
            "fields": {
                "summary": "Abratens",
                "issuetype": {"name": "Épico"},
                "status": {"name": "To Do"},
                "assignee": None,
                "priority": {"name": "High"},
                "issuelinks": [],
            },
        },
        {
            "key": "TST-116",
            "fields": {
                "summary": "Implementações Pré-Deploy",
                "issuetype": {"name": "Task"},
                "status": {"name": "In Progress"},
                "assignee": None,
                "priority": {"name": "High"},
                "issuelinks": [{"type": {"name": "Relates"}, "outwardIssue": {"key": "TST-101"}}],
            },
        },
    ]

    from api.jira import sync_source

    result = sync_source("jira_test")

    assert "error" not in result
    store = json.loads(tmp_projects_file.read_text(encoding="utf-8"))
    task = next(t for t in store["tasks"] if t["external_ref"]["key"] == "TST-116")
    assert task["project_id"] == "prj_test1"
    project = next(p for p in store["projects"] if p["project_id"] == "prj_test1")
    assert (project.get("external_ref") or {}).get("key") == "abratens"
