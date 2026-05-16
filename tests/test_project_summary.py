import json
import pytest


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
            "status_map": {},
            "last_sync_at": "2026-05-14T21:00:00Z",
            "sync_status": "ok",
            "sync_error": None,
        }],
        "projects": [{
            "project_id": "prj_abc",
            "name": "Alpha",
            "description": "Test project",
            "domain": "projetos",
            "status": "ativo",
            "color": "#FF5722",
            "default_source_id": "jira_test",
            "refs": {"github": ["https://github.com/test/alpha"], "obsidian": [], "sessions": []},
            "created_at": 1700000000,
            "updated_at": 1700000000,
            "archived": False,
        }],
        "tasks": [
            {"task_id": "tsk_1", "project_id": "prj_abc", "title": "Task A", "description": "", "status": "backlog", "priority": "media", "category": "Backend", "owner": "jr", "progress": 0, "due_date": "", "external_ref": None, "refs": {"github": [], "obsidian": [], "sessions": []}, "created_at": 1700000100, "updated_at": 1700000200, "archived": False},
            {"task_id": "tsk_2", "project_id": "prj_abc", "title": "Task B", "description": "", "status": "em_andamento", "priority": "alta", "category": "Frontend", "owner": "jr", "progress": 50, "due_date": "", "external_ref": None, "refs": {"github": [], "obsidian": [], "sessions": []}, "created_at": 1700000300, "updated_at": 1700000400, "archived": False},
            {"task_id": "tsk_3", "project_id": "prj_abc", "title": "Task C", "description": "", "status": "concluido", "priority": "baixa", "category": "Docs", "owner": "jr", "progress": 100, "due_date": "", "external_ref": None, "refs": {"github": [], "obsidian": [], "sessions": []}, "created_at": 1700000500, "updated_at": 1700000600, "archived": False},
        ],
        "activity": [],
    }
    pf.write_text(json.dumps(store))
    monkeypatch.setattr("api.config.PROJECTS_FILE", pf)
    monkeypatch.setattr("api.projects.PROJECTS_FILE", pf)
    return pf


def test_project_summary_stats(tmp_projects_file):
    from api.projects import project_summary
    result = project_summary("prj_abc")
    assert result["project"]["name"] == "Alpha"
    assert result["stats"]["total"] == 3
    assert result["stats"]["by_status"]["backlog"] == 1
    assert result["stats"]["by_status"]["em_andamento"] == 1
    assert result["stats"]["by_status"]["concluido"] == 1
    assert result["stats"]["completion_pct"] == pytest.approx(33.3, abs=0.1)


def test_project_summary_recent_tasks(tmp_projects_file):
    from api.projects import project_summary
    result = project_summary("prj_abc")
    assert len(result["recent_tasks"]) == 3
    assert result["recent_tasks"][0]["task_id"] == "tsk_3"


def test_project_summary_not_found(tmp_projects_file):
    from api.projects import project_summary
    with pytest.raises(KeyError):
        project_summary("prj_nonexistent")
