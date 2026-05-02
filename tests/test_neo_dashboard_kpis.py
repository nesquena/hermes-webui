"""Neo HU-03.4: Dashboard KPI summary and cards."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
DASHBOARD_JS = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def test_dashboard_summary_counts_projects_by_status(monkeypatch):
    import api.dashboard as dashboard

    now = 1_705_363_200
    projects = [
        {"status": "backlog", "created_at": now - 60},
        {"status": "em_andamento", "created_at": now - 30, "updated_at": now - 120},
        {"status": "em_andamento", "created_at": now - 3_000_000, "updated_at": now - 90_000},
        {"status": "concluido", "created_at": now - 3_000_000, "completed_at": now - 300},
        {"status": "arquivado", "created_at": now - 10},
    ]
    monkeypatch.setattr(dashboard, "load_projects", lambda: projects)
    monkeypatch.setattr(dashboard.time, "time", lambda: now)

    summary = dashboard.build_dashboard_summary()
    cards = {card["id"]: card for card in summary["cards"]}

    assert cards["active_projects"]["value"] == 4
    assert cards["active_projects"]["delta_value"] == 2
    assert cards["tasks_in_progress"]["value"] == 2
    assert cards["tasks_in_progress"]["delta_value"] == 1
    assert cards["completed"]["value"] == 1
    assert cards["completed"]["delta_value"] == 1
    assert cards["agents_online"]["value"] == 1


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
