"""Neo Dashboard KPI tests — validates real task/project data from schema v2."""

from pathlib import Path

import api.dashboard as dashboard

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
DASHBOARD_JS = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def _make_store(now):
    """Schema v2 fixture with projects and tasks."""
    return {
        "schema_version": 2,
        "sources": [],
        "projects": [
            {"id": "prj_001", "name": "Projeto A", "status": "ativo", "created_at": now - 60},
            {"id": "prj_002", "name": "Projeto B", "status": "ativo", "created_at": now - 3_000_000},
            {"id": "prj_003", "name": "Projeto C", "status": "arquivado", "created_at": now - 10, "archived": True},
        ],
        "tasks": [
            {"id": "tsk_001", "status": "em_andamento", "created_at": now - 30, "updated_at": now - 100},
            {"id": "tsk_002", "status": "em_andamento", "created_at": now - 3_000_000, "updated_at": now - 200_000},
            {"id": "tsk_003", "status": "em_andamento", "created_at": now - 50, "updated_at": now - 50, "archived": True},
            {"id": "tsk_004", "status": "concluido", "created_at": now - 3_000_000, "completed_at": now - 300},
            {"id": "tsk_005", "status": "concluido", "created_at": now - 3_000_000, "updated_at": now - 1_000_000},
        ],
        "activity": [],
    }


def test_dashboard_kpis_use_real_task_data(monkeypatch):
    """KPIs must count tasks (not projects) for in_progress and completed."""
    now = 1_705_363_200

    monkeypatch.setattr(dashboard, "load_project_store", lambda: _make_store(now))
    monkeypatch.setattr(dashboard.time, "time", lambda: now)

    summary = dashboard.build_dashboard_summary()
    cards = {card["id"]: card for card in summary["cards"]}

    # Projetos Ativos: 2 non-archived projects
    assert cards["active_projects"]["value"] == 2
    # Delta: only prj_001 created this month
    assert cards["active_projects"]["delta_value"] == 1

    # Tarefas em Andamento: 2 non-archived tasks with status em_andamento
    assert cards["tasks_in_progress"]["value"] == 2
    # Delta since yesterday: tsk_001 updated recently, tsk_002 updated 200k sec ago (>1 day)
    assert cards["tasks_in_progress"]["delta_value"] == 1

    # Concluídas esta semana: tsk_004 completed 300s ago (recent), tsk_005 updated 1M sec ago (old)
    assert cards["completed"]["value"] == 1
    assert cards["completed"]["delta_value"] == 1


def test_dashboard_agents_online_not_hardcoded(monkeypatch):
    """Agents online must come from health check, not hardcoded 1."""
    now = 1_705_363_200

    monkeypatch.setattr(dashboard, "load_project_store", lambda: _make_store(now))
    monkeypatch.setattr(dashboard.time, "time", lambda: now)
    monkeypatch.setattr(dashboard, "_process_cmdlines", lambda: [
        "python -m api.server hermes webui",
        "python gateway/run.py hermes gateway",
        "python -m hermes cron scheduler",
        "python -m hermes subagent worker",
    ])

    summary = dashboard.build_dashboard_summary()
    cards = {card["id"]: card for card in summary["cards"]}

    assert cards["agents_online"]["value"] == 4
    assert "agents" in summary
    assert summary["agents"]["online"] == 4
    assert summary["agents"]["total"] == 4


def test_agents_health_partial(monkeypatch):
    """When only webui is running, agents online = 1."""
    monkeypatch.setattr(dashboard, "_process_cmdlines", lambda: [
        "python -m api.server hermes webui",
    ])

    agents = dashboard.build_agents_health_summary()

    assert agents["online"] == 1
    assert agents["total"] == 4
    components_by_id = {c["id"]: c for c in agents["components"]}
    assert components_by_id["webui"]["online"] is True
    assert components_by_id["gateway"]["online"] is False
    assert components_by_id["cron"]["online"] is False
    assert components_by_id["subagents"]["online"] is False


def test_dashboard_summary_route_registered():
    assert 'parsed.path == "/api/dashboard/summary"' in ROUTES_PY
    assert "build_dashboard_summary" in ROUTES_PY


def test_dashboard_kpi_shell_and_frontend_rendering_present():
    assert 'id="dashboardKpiGrid"' in INDEX_HTML
    assert 'class="dashboard-kpi-grid"' in INDEX_HTML
    assert "/api/dashboard/summary" in DASHBOARD_JS
    assert "renderDashboardKpis" in DASHBOARD_JS
    assert "data-kpi-id" in DASHBOARD_JS
    assert "switchPanel(card.panel" in DASHBOARD_JS


def test_dashboard_kpi_css_and_i18n_present():
    for selector in [
        ".dashboard-kpi-grid",
        ".dashboard-kpi-card",
        ".dashboard-kpi-label",
        ".dashboard-kpi-value",
        ".dashboard-kpi-delta",
    ]:
        assert selector in STYLE_CSS
    for key in [
        "kpi_active_projects",
        "kpi_tasks_in_progress",
        "kpi_completed",
        "kpi_agents_online",
        "kpi_delta_this_month",
        "kpi_delta_since_yesterday",
        "kpi_delta_this_week",
        "kpi_all_operational",
    ]:
        assert key in I18N_JS
