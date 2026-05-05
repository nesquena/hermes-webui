"""Neo Dashboard visual shell: topbar, sidebar status and VPS resources."""

from pathlib import Path
import re


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
        'class="neo-vps-card"',
        'data-vps-metric="cpu"',
        'data-vps-metric="ram"',
        'data-vps-metric="disk"',
        'data-vps-metric="network"',
    ]:
        assert marker in INDEX_HTML
    # "Conversar agora" button removed in visual refinement (HU-03.8 DoD update)
    assert 'id="neoSidebarTalkNow"' not in INDEX_HTML


def test_neo_sidebar_matches_required_navigation_order_and_targets():
    block = INDEX_HTML.split('class="neo-dashboard-menu"', 1)[1].split('class="neo-dashboard-bottom"', 1)[0]
    found = re.findall(
        r'data-neo-menu-item\s+data-panel="([^"]+)".*?onclick="switchPanel\(\'([^\']+)\'\)".*?data-i18n="([^"]+)"',
        block,
        re.S,
    )
    assert found == [
        ("dashboard", "dashboard", "tab_dashboard"),
        ("projects", "projects", "tab_projects"),
        ("todos", "todos", "tab_tasks"),
        ("profiles", "profiles", "tab_profiles"),
        ("finance", "finance", "tab_finance"),
        ("agents", "agents", "tab_agents"),
        ("skills", "skills", "tab_skills"),
        ("tasks", "tasks", "tab_automation"),
        ("settings", "settings", "tab_settings"),
    ]


def test_neo_sidebar_bottom_blocks_and_footer_are_anchored():
    assert 'class="neo-dashboard-bottom"' in INDEX_HTML
    assert 'class="neo-sidebar-footer"' in INDEX_HTML
    assert 'data-i18n="sidebar_docs"' in INDEX_HTML
    assert 'data-i18n="sidebar_support"' in INDEX_HTML
    for selector in [
        ".neo-dashboard-bottom",
        "margin-top:auto",
        ".neo-dashboard-menu{flex:1 1 auto",
        "overflow-y:auto",
        ".neo-dashboard-bottom{margin-top:auto;display:flex;flex-shrink:0",
        ".neo-sidebar-footer",
    ]:
        assert selector in STYLE_CSS


def test_neo_placeholder_panels_are_routable_from_sidebar():
    for marker in [
        'id="mainProjects"',
        'id="mainTodos"',
        'id="mainFinance"',
        'id="mainAgents"',
        "projects: 'showing-projects'",
        "todos: 'showing-todos'",
        "finance: 'showing-finance'",
        "agents: 'showing-agents'",
        "NEO_SHELL_PANELS",
        "panelDashboard",
    ]:
        assert marker in (INDEX_HTML + PANELS_JS)
    for selector in [
        "main.main.showing-projects > #mainProjects",
        "main.main.showing-todos > #mainTodos",
        "main.main.showing-finance > #mainFinance",
        "main.main.showing-agents > #mainAgents",
        ".neo-placeholder-panel",
    ]:
        assert selector in STYLE_CSS


def test_automation_uses_neo_development_escape_page():
    line = next(l for l in PANELS_JS.splitlines() if "NEO_SHELL_PANELS" in l and "new Set" in l)
    assert "tasks" in line, "Automation/tasks must stay inside the Neo shell"
    assert "NEO_DEVELOPMENT_PANELS" in PANELS_JS
    assert "NEO_DEVELOPMENT_PANELS.has(nextPanel)" in PANELS_JS
    assert "nextPanel === 'tasks' && !NEO_DEVELOPMENT_PANELS.has(nextPanel)" in PANELS_JS
    for marker in [
        'id="mainTasks"',
        'class="neo-development-panel"',
        'data-i18n="automation_development_title"',
        'data-i18n="automation_development_sub"',
        'data-i18n="development_badge"',
    ]:
        assert marker in INDEX_HTML
    for key in [
        "automation_development_title",
        "automation_development_sub",
        "automation_development_note",
        "development_badge",
    ]:
        assert key in (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_health_routes_and_dashboard_polling_present():
    assert 'parsed.path == "/api/health/system"' in ROUTES_PY
    assert 'parsed.path == "/api/health/vps"' in ROUTES_PY
    assert "/api/health/system" in DASHBOARD_JS
    assert "/api/health/vps" in DASHBOARD_JS
    assert "renderDashboardSystemHealth" in DASHBOARD_JS
    assert "renderDashboardVpsHealth" in DASHBOARD_JS
    assert "focusDashboardComposer" in DASHBOARD_JS


def test_dashboard_topbar_actions_have_final_behaviors():
    for marker in [
        "openDashboardSearch",
        "openDashboardNotifications",
        "openDashboardHelp",
        "/api/sessions/search",
        "loadSession(session.session_id)",
        "Notification.requestPermission",
        "notifications_enabled",
        "cmdHelp",
    ]:
        assert marker in DASHBOARD_JS
    assert "dashboard_topbar_placeholder" not in DASHBOARD_JS


def test_dashboard_visual_shell_css_present():
    for selector in [
        ".dashboard-topbar",
        ".dashboard-topbar-status",
        ".dashboard-topbar-panel",
        ".dashboard-search-input",
        ".dashboard-notification-status",
        ".dashboard-help-actions",
        ".dashboard-shell-mode",
        ".neo-dashboard-brand",
        ".neo-sidebar-status",
        ".neo-vps-card",
        ".neo-vps-bar",
    ]:
        assert selector in STYLE_CSS
    # .neo-sidebar-talk removed — "Conversar agora" button dropped in visual refinement
    assert ".neo-sidebar-talk" not in STYLE_CSS


def test_dashboard_right_sidebar_hero_has_reference_proportions():
    for rule in [
        ".dashboard-right{display:flex;flex-direction:column;gap:12px;min-height:0;height:100%;overflow:hidden;}",
        ".hero-card{position:relative;height:clamp(300px,34vh,330px);",
        ".hero-portrait{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;object-position:center 43%;",
        ".hero-status-pill{position:absolute;left:18px;right:18px;bottom:22px;",
        "min-height:30px;font-size:9.5px;",
        ".hero-status-dot{width:7px;height:7px;",
        "animation:hero-status-pulse 1.05s ease-in-out infinite;",
        ".dashboard-quick-actions{display:flex;flex:1 1 auto;min-height:0;",
    ]:
        assert rule in STYLE_CSS
    assert ".hero-card{height:300px;}" in STYLE_CSS
    assert "@media (max-height: 760px) and (min-width: 901px)" in STYLE_CSS
    assert ".hero-card{height:250px;}" in STYLE_CSS
    assert "@keyframes hero-status-pulse" in STYLE_CSS


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
