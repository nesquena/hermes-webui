"""Integration test: full Jira sync → summary → verify data flow."""
import json
import pytest
from unittest.mock import patch
from pathlib import Path


@pytest.fixture
def full_store(tmp_path, monkeypatch):
    pf = tmp_path / "projects.json"
    store = {
        "schema_version": 2,
        "sources": [{
            "source_id": "jira_cotin",
            "type": "jira",
            "name": "Jira COTIN/DELOG",
            "base_url": "https://cotin5.atlassian.net",
            "project_key": "KAN",
            "sync_enabled": True,
            "sync_mode": "read",
            "status_map": {
                "A fazer": "backlog",
                "Em andamento": "em_andamento",
                "SUSPENSA": "em_revisao",
                "Concluído": "concluido",
            },
            "last_sync_at": None,
            "sync_status": "idle",
            "sync_error": None,
        }],
        "projects": [{
            "project_id": "prj_cotin",
            "name": "COTIN/DELOG",
            "description": "MGI project",
            "domain": "governo",
            "status": "ativo",
            "color": "#4CAF50",
            "default_source_id": "jira_cotin",
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


@patch("api.jira.JiraClient.search_issues")
def test_full_sync_then_summary(mock_search, full_store):
    mock_search.return_value = [
        {"key": "KAN-315", "fields": {"summary": "Deploy staging", "status": {"name": "A fazer"}, "assignee": None, "priority": {"name": "Medium"}, "updated": "2026-05-14T10:00:00.000+0000", "created": "2026-05-10T08:00:00.000+0000"}, "self": "https://cotin5.atlassian.net/rest/api/3/issue/1"},
        {"key": "KAN-316", "fields": {"summary": "Fix auth flow", "status": {"name": "Concluído"}, "assignee": {"displayName": "Junior"}, "priority": {"name": "High"}, "updated": "2026-05-14T12:00:00.000+0000", "created": "2026-05-11T09:00:00.000+0000"}, "self": "https://cotin5.atlassian.net/rest/api/3/issue/2"},
        {"key": "KAN-317", "fields": {"summary": "Preparar iframes", "status": {"name": "Em andamento"}, "assignee": {"displayName": "Junior"}, "priority": {"name": "High"}, "updated": "2026-05-14T14:00:00.000+0000", "created": "2026-05-12T10:00:00.000+0000"}, "self": "https://cotin5.atlassian.net/rest/api/3/issue/3"},
    ]

    from api.jira import sync_source
    result = sync_source("jira_cotin")
    assert result["synced"] == 3
    assert result["created"] == 3

    from api.projects import project_summary
    summary = project_summary("prj_cotin")
    assert summary["stats"]["total"] == 3
    assert summary["stats"]["by_status"]["backlog"] == 1
    assert summary["stats"]["by_status"]["em_andamento"] == 1
    assert summary["stats"]["by_status"]["concluido"] == 1
    assert summary["stats"]["completion_pct"] == pytest.approx(33.3, abs=0.1)
    assert summary["source"]["sync_status"] == "ok"
    assert len(summary["recent_tasks"]) == 3
