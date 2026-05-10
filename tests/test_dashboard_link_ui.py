import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_dashboard_nav_buttons_are_hidden_by_default_and_subpath_safe():
    assert 'id="dashboardRailBtn"' in INDEX_HTML
    assert 'id="dashboardMobileBtn"' in INDEX_HTML
    assert "data-dashboard-link" in INDEX_HTML
    assert 'data-i18n-title="tab_dashboard"' in INDEX_HTML
    assert "display:none" in INDEX_HTML
    assert "Dashboard" in INDEX_HTML
    assert 'href="/' not in INDEX_HTML


def test_dashboard_rail_item_sits_between_insights_and_settings_spacer():
    rail = re.search(r'<nav class="rail".*?</nav>', INDEX_HTML, re.DOTALL).group(0)
    assert (
        rail.index('data-panel="insights"')
        < rail.index('id="dashboardRailBtn"')
        < rail.index("rail-spacer")
    )


def test_dashboard_frontend_fetches_status_with_sixty_second_cache():
    assert "DASHBOARD_STATUS_TTL_MS=60000" in UI_JS
    assert "function refreshDashboardStatus" in UI_JS
    assert "api('/api/dashboard/status')" in UI_JS
    assert "setInterval(refreshDashboardStatus,DASHBOARD_STATUS_TTL_MS)" in UI_JS
    assert 'fetch("/api/dashboard/status"' not in UI_JS
    assert "fetch('/api/dashboard/status'" not in UI_JS


def test_dashboard_probe_initializes_after_shared_api_helper_is_loaded():
    assert "function _initDashboardLinkProbe" in UI_JS
    assert (
        "document.addEventListener('DOMContentLoaded',_initDashboardLinkProbe,{once:true})"
        in UI_JS
    )
    assert "else _initDashboardLinkProbe();" not in UI_JS


def test_dashboard_frontend_opens_external_tab_safely_and_derives_browser_host_url():
    assert "function openHermesDashboard" in UI_JS
    assert "window.open" in UI_JS
    assert "noopener,noreferrer" in UI_JS
    assert "window.location.hostname" in UI_JS
    assert "_dashboardBrowserUrl" in UI_JS
    assert 'id="dashboardRailBtn"' in INDEX_HTML
    assert re.search(
        r'id="dashboardRailBtn"[^>]*onclick="openHermesDashboard\(event\)"', INDEX_HTML
    )


def test_dashboard_loopback_warning_and_external_badge_are_present():
    assert "dashboard_loopback_warning" in UI_JS
    assert "dashboard-external-badge" in INDEX_HTML
    assert ".dashboard-external-badge" in STYLE_CSS
    assert "dashboard-link-visible" in STYLE_CSS


def test_dashboard_settings_controls_live_in_system_panel():
    assert 'id="settingsDashboardMode"' in INDEX_HTML
    assert 'id="settingsDashboardUrl"' in INDEX_HTML
    assert "function saveDashboardSettings" in UI_JS
    assert "api('/api/dashboard/config'" in UI_JS
