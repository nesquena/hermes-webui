"""Neo Dashboard visual shell: topbar, sidebar status and VPS resources."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
DASHBOARD_JS = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def test_dashboard_topbar_shell_present():
    for marker in [
        'class="dashboard-topbar"',
        'id="dashboardSystemStatus"',
        'id="dashboardSystemUptime"',
        'id="dashboardSystemRegion"',
        'id="dashboardSystemVersion"',
        'id="dashboardTerminalBtn"',
    ]:
        assert marker in INDEX_HTML


def test_dashboard_sidebar_status_and_vps_present():
    for marker in [
        'class="neo-dashboard-brand"',
        'class="neo-sidebar-status"',
        'id="neoSidebarTalkNow"',
        'class="neo-vps-card"',
        'data-vps-metric="cpu"',
        'data-vps-metric="ram"',
        'data-vps-metric="disk"',
        'data-vps-metric="network"',
    ]:
        assert marker in INDEX_HTML


def test_health_routes_and_dashboard_polling_present():
    assert 'parsed.path == "/api/health/system"' in ROUTES_PY
    assert 'parsed.path == "/api/health/vps"' in ROUTES_PY
    assert "/api/health/system" in DASHBOARD_JS
    assert "/api/health/vps" in DASHBOARD_JS
    assert "renderDashboardSystemHealth" in DASHBOARD_JS
    assert "renderDashboardVpsHealth" in DASHBOARD_JS
    assert "focusDashboardComposer" in DASHBOARD_JS


def test_dashboard_visual_shell_css_present():
    for selector in [
        ".dashboard-topbar",
        ".dashboard-topbar-status",
        ".dashboard-shell-mode",
        ".neo-dashboard-brand",
        ".neo-sidebar-status",
        ".neo-sidebar-talk",
        ".neo-vps-card",
        ".neo-vps-bar",
    ]:
        assert selector in STYLE_CSS


def test_dashboard_uses_exclusive_desktop_shell():
    assert "dashboard-shell-mode" in PANELS_JS
    assert "document.body.classList.toggle" in PANELS_JS
    for selector in [
        "body.dashboard-shell-mode .app-titlebar",
        "body.dashboard-shell-mode .rail",
        "body.dashboard-shell-mode .sidebar",
        "body.dashboard-shell-mode #sidebarResize",
        "body.dashboard-shell-mode .sidebar-nav",
    ]:
        assert selector in STYLE_CSS
