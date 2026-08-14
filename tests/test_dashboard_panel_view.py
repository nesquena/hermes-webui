"""In-app dashboard view: registration, markup, CSS wiring and i18n keys."""

import re
from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")

NEW_KEYS = [
    "dashboard_title", "dashboard_desc", "dashboard_open_external",
    "dashboard_kpi_cron_jobs", "dashboard_kpi_skills", "dashboard_kpi_memory",
    "dashboard_system_health", "dashboard_recent_activity", "dashboard_no_activity",
    "dashboard_log_tail", "dashboard_memory_recall",
    "dashboard_memory_search_placeholder", "dashboard_memory_no_results",
    "dashboard_view_all", "dashboard_unavailable", "dashboard_health_unavailable",
]


def _extract_main_view_panels():
    m = re.search(r"const MAIN_VIEW_PANELS = \[([^\]]*)\]", PANELS_JS)
    assert m, "MAIN_VIEW_PANELS literal not found"
    return [p.strip().strip("'\"") for p in m.group(1).split(",")]


def test_dashboard_is_a_registered_main_view_panel():
    assert "dashboard" in _extract_main_view_panels()
    assert "dashboard: 'tab_dashboard'" in PANELS_JS
    assert "nextPanel === 'dashboard'" in PANELS_JS


def test_dashboard_loader_and_poll_lifecycle_exist():
    assert "async function loadDashboard" in PANELS_JS
    assert "function _dashboardStartPolling" in PANELS_JS
    assert "function _dashboardStopPolling" in PANELS_JS
    # Leaving the panel must stop the poll timer (kanban precedent).
    assert re.search(
        r"prevPanel === 'dashboard' && nextPanel !== 'dashboard'", PANELS_JS
    ), "missing dashboard leave-cleanup in switchPanel"


def test_dashboard_loader_bails_when_navigated_away():
    # Rapid sidebar clicks: the 7-endpoint fetch batch may resolve after the
    # user has already moved to another panel. The loader must bail before
    # rendering (main-thread thrash into a hidden view) and before starting the
    # poll timer (which would leak past switchPanel's leave-cleanup).
    m = re.search(r"async function loadDashboard.*?\n\}\n", PANELS_JS, re.DOTALL)
    assert m, "loadDashboard body not found"
    body = m.group(0)
    guard = body.find("_currentPanel !== 'dashboard'")
    assert guard != -1, "missing stale-load guard in loadDashboard"
    assert guard < body.index("_renderDashboard(box"), "guard must precede render"
    assert guard < body.index("_dashboardStartPolling();"), "guard must precede poll start"


def test_dashboard_markup_is_present():
    assert 'id="panelDashboard"' in INDEX_HTML
    assert 'id="mainDashboard"' in INDEX_HTML
    assert 'id="dashboardContent"' in INDEX_HTML
    # External-dashboard link lives inside the view header and stays driven
    # by _applyDashboardStatus via the data-dashboard-link hook.
    m = re.search(r'id="dashboardExternalBtn"[^>]*>', INDEX_HTML)
    assert m, "dashboardExternalBtn missing"
    btn_tag = INDEX_HTML[INDEX_HTML.rfind("<", 0, m.start()):m.end()]
    assert "data-dashboard-link" in btn_tag
    assert 'onclick="openHermesDashboard(event)"' in btn_tag


def test_dashboard_health_panel_does_not_reuse_insights_ids():
    # The insights monitor polls #systemHealthPanel scoped to showing-insights;
    # a duplicate id from the dashboard copy would shadow it.
    assert 'id="dashboardHealthPanel"' in PANELS_JS
    assert "data-dash-health-metric" in PANELS_JS
    assert PANELS_JS.count('id="systemHealthPanel"') == 1


def test_dashboard_css_wiring():
    assert "main.main.showing-dashboard > #mainDashboard{display:flex" in CSS
    assert "main.main > #mainDashboard" in CSS
    # The chat fallback rule must exclude the dashboard state, otherwise
    # #mainChat renders underneath the dashboard view.
    fallback = re.search(r"main\.main:not\([^{]+> #mainChat\{display:flex;\}", CSS)
    assert fallback, "chat fallback rule not found"
    assert ":not(.showing-dashboard)" in fallback.group(0)


def test_dashboard_styles_use_theme_variables():
    assert ".dash-kpi-grid{" in CSS
    assert ".dash-log-tail{" in CSS
    kpi_rule = re.search(r"\.dash-kpi\{[^}]*\}", CSS).group(0)
    assert "var(--surface)" in kpi_rule and "var(--border)" in kpi_rule


def test_dashboard_i18n_keys_exist_in_english_and_german():
    for locale_start, next_start in (("\n  en: {", "\n  it: {"), ("\n  de: {", "\n  zh: {")):
        start = I18N_JS.index(locale_start)
        end = I18N_JS.index(next_start, start)
        block = I18N_JS[start:end]
        for key in NEW_KEYS:
            assert f"{key}:" in block, f"{key} missing in block starting {locale_start!r}"


def test_dashboard_i18n_keys_exist_in_every_locale():
    for key in NEW_KEYS:
        assert I18N_JS.count(f"    {key}: ") == 15, f"{key} not in all 15 locales"
