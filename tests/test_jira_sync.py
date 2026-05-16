import json
import pytest
from unittest.mock import patch


@pytest.fixture
def tmp_projects_file(tmp_path, monkeypatch):
    pf = tmp_path / "projects.json"
    store = {
        "schema_version": 2,
        "sources": [{
            "source_id": "jira_test",
            "type": "jira",
            "name": "Test Jira",
            "base_url": "https://test.atlassian.net",
            "project_key": "TST",
            "sync_enabled": True,
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
        "tasks": [],
        "activity": [],
    }
    pf.write_text(json.dumps(store))
    monkeypatch.setattr("api.config.PROJECTS_FILE", pf)
    monkeypatch.setattr("api.projects.PROJECTS_FILE", pf)
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")
    return pf


def _fake_issues():
    return [
        {
            "key": "TST-1",
            "fields": {
                "summary": "First issue",
                "status": {"name": "To Do"},
                "assignee": None,
                "priority": {"name": "Medium"},
                "updated": "2026-05-14T10:00:00.000+0000",
                "created": "2026-05-10T08:00:00.000+0000",
            },
            "self": "https://test.atlassian.net/rest/api/3/issue/10001",
        },
        {
            "key": "TST-2",
            "fields": {
                "summary": "Second issue",
                "status": {"name": "In Progress"},
                "assignee": {"displayName": "Junior"},
                "priority": {"name": "High"},
                "updated": "2026-05-14T12:00:00.000+0000",
                "created": "2026-05-11T09:00:00.000+0000",
            },
            "self": "https://test.atlassian.net/rest/api/3/issue/10002",
        },
    ]


@patch("api.jira.JiraClient.search_issues")
def test_sync_source_creates_tasks(mock_search, tmp_projects_file):
    mock_search.return_value = _fake_issues()
    from api.jira import sync_source
    result = sync_source("jira_test")
    assert result["synced"] == 2
    assert result["created"] == 2
    assert result["updated"] == 0

    store = json.loads(tmp_projects_file.read_text())
    assert len(store["tasks"]) == 2
    assert store["tasks"][0]["external_ref"]["key"] == "TST-1"
    assert store["tasks"][0]["status"] == "backlog"
    assert store["tasks"][1]["status"] == "em_andamento"


@patch("api.jira.JiraClient.search_issues")
def test_sync_source_updates_existing(mock_search, tmp_projects_file):
    mock_search.return_value = _fake_issues()
    from api.jira import sync_source
    sync_source("jira_test")

    updated_issues = _fake_issues()
    updated_issues[0]["fields"]["status"]["name"] = "Done"
    mock_search.return_value = updated_issues
    result = sync_source("jira_test")
    assert result["updated"] == 2

    store = json.loads(tmp_projects_file.read_text())
    done_task = next(t for t in store["tasks"] if t["external_ref"]["key"] == "TST-1")
    assert done_task["status"] == "concluido"
