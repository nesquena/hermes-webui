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


def test_dashboard_left_sidebar_matches_reference_spacing():
    for rule in [
        "body.dashboard-shell-mode .sidebar{width:220px!important;min-width:220px;max-width:220px;flex:0 0 220px;overflow:hidden;}",
        ".neo-dashboard-brand{display:flex;align-items:center;gap:10px;padding:16px 20px 28px;border-bottom:0;}",
        ".neo-dashboard-brand img{width:40px;height:40px;filter:drop-shadow(0 0 10px var(--accent));}",
        ".neo-dashboard-menu{flex:1 1 auto;min-height:0;display:flex;flex-direction:column;gap:3px;padding:10px;overflow-y:auto;overscroll-behavior:contain;}",
        ".neo-dashboard-menu-item{display:flex;align-items:center;gap:10px;width:100%;min-height:36px;padding:7px 10px;",
        ".neo-sidebar-status{margin:0 10px;padding:10px;border:1px solid rgba(0,229,255,.2);",
        ".neo-sidebar-status p{margin:8px 0 0;font-size:10px;line-height:1.4;color:var(--muted);}",
        ".neo-vps-card{margin:0 10px;padding:10px 12px;",
    ]:
        assert rule in STYLE_CSS


def test_neo_sidebar_matches_required_navigation_order_and_targets():
    block = INDEX_HTML.split('class="neo-dashboard-menu"', 1)[1].split('class="neo-dashboard-bottom"', 1)[0]
    found = re.findall(
        r'data-neo-menu-item\s+data-panel="([^"]+)".*?onclick="switchPanel\(\'([^\']+)\'\)".*?data-i18n="([^"]+)"',
        block,
        re.S,
    )
    assert found == [
        ("dashboard", "dashboard", "tab_dashboard"),
        # Conversas — added in Onda 9 so users can reach panelChat (the
        # historic session list) from the dashboard rail. Without it the only
        # way to browse old chats was through the legacy hermes nav, which
        # the Neo fork hides.
        ("chat", "chat", "tab_conversations"),
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


def test_automation_mounts_cron_jobs_inside_neo_shell():
    line = next(l for l in PANELS_JS.splitlines() if "NEO_SHELL_PANELS" in l and "new Set" in l)
    assert "tasks" in line, "Automation/tasks must stay inside the Neo shell"
    assert "NEO_DEVELOPMENT_PANELS" not in PANELS_JS
    for marker in [
        "restoreDashboardTasks",
        "mountDashboardTasks",
        "nextPanel === 'tasks'",
        "await loadCrons()",
    ]:
        assert marker in (PANELS_JS + DASHBOARD_JS)
    for marker in [
        'id="mainTasks"',
        'id="panelTasks"',
        'id="cronList"',
        'id="taskDetailBody"',
        'id="taskDetailEmpty"',
        'id="btnRunTaskDetail"',
        'id="btnPauseTaskDetail"',
        'id="btnResumeTaskDetail"',
        'id="btnEditTaskDetail"',
        'id="btnDuplicateTaskDetail"',
        'id="btnDeleteTaskDetail"',
        'id="btnSaveTaskDetail"',
    ]:
        assert marker in INDEX_HTML
    assert 'class="neo-development-panel"' not in INDEX_HTML


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
        ".dashboard-right{display:flex;flex-direction:column;gap:12px;min-height:0;height:100%;overflow-x:hidden;overflow-y:auto;",
        ".hero-card{position:relative;height:clamp(300px,34vh,330px);",
        ".hero-portrait{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;object-position:center 43%;",
        ".hero-status-pill{position:absolute;left:18px;right:18px;bottom:22px;",
        "min-height:30px;font-size:9.5px;",
        ".hero-status-dot{width:7px;height:7px;",
        "animation:hero-status-pulse 1.05s ease-in-out infinite;",
        ".dashboard-quick-actions{display:flex;flex:1 1 auto;min-height:0;overflow-y:auto;",
    ]:
        assert rule in STYLE_CSS
    assert "@media (max-height: 940px) and (min-width: 901px)" in STYLE_CSS
    assert ".hero-card{height:200px;}" in STYLE_CSS
    assert "@media (max-height: 760px) and (min-width: 901px)" in STYLE_CSS
    assert ".hero-card{height:160px;}" in STYLE_CSS
    assert "@keyframes hero-status-pulse" in STYLE_CSS


def test_dashboard_right_sidebar_matches_reference_vertical_rhythm():
    """The right rail should align its bottom edge while preserving compact rows."""
    for rule in [
        ".dashboard-right{display:flex;flex-direction:column;gap:12px;min-height:0;height:100%;overflow-x:hidden;overflow-y:auto;",
        ".dashboard-quick-actions{display:flex;flex:1 1 auto;min-height:0;overflow-y:auto;",
        ".dashboard-quick-actions h3{margin:0;font-size:12px;font-weight:700;letter-spacing:0;text-transform:none;",
        ".dashboard-quick-action-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}",
        ".dashboard-quick-action{min-height:46px;display:flex;align-items:center;gap:8px;padding:8px 10px;",
        ".hero-card{height:200px;}",
        ".dashboard-kpi-card{min-height:92px;padding:10px;}",
        ".dashboard-quick-actions{min-height:150px;padding:10px;overflow-y:auto;}",
        ".dashboard-quick-action{min-height:34px;padding:6px 8px;font-size:10px;line-height:1.12;}",
    ]:
        assert rule in STYLE_CSS


def test_dashboard_right_sidebar_scrolls_when_actions_exceed_viewport():
    """Regression guard: quick actions must remain reachable on 900px-ish screens."""

    assert ".dashboard-right{display:flex;flex-direction:column;gap:12px;min-height:0;height:100%;overflow-x:hidden;overflow-y:auto;" in STYLE_CSS
    assert ".dashboard-quick-actions{display:flex;flex:1 1 auto;min-height:0;overflow-y:auto;" in STYLE_CSS
    assert "@media (max-height: 940px) and (min-width: 901px)" in STYLE_CSS


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
