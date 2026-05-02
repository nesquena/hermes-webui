"""Neo HU-03.9/HU-03.10: admin dropdown and personal panel shell."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
DASHBOARD_JS = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
TASKS_MD = (ROOT / "docs" / "neo" / "TASKS.md").read_text(encoding="utf-8")


def test_admin_dropdown_shell_and_actions_present():
    for marker in [
        'id="dashboardAdminBtn"',
        'aria-haspopup="menu"',
        'id="dashboardAdminMenu"',
        'data-admin-action="profiles"',
        'data-admin-action="settings"',
        'data-admin-action="logout"',
    ]:
        assert marker in INDEX_HTML


def test_admin_dropdown_reuses_existing_panel_and_logout_handlers():
    assert "toggleDashboardAdminMenu" in DASHBOARD_JS
    assert "handleDashboardAdminMenu" in DASHBOARD_JS
    assert "switchPanel('profiles')" in DASHBOARD_JS
    assert "switchPanel('settings')" in DASHBOARD_JS
    assert "signOut()" in DASHBOARD_JS
    assert "dashboardAdminMenu" in DASHBOARD_JS


def test_personal_panel_overview_and_settings_link_present():
    for marker in [
        'id="neoPersonalOverview"',
        'id="neoPersonalProfileName"',
        'id="neoPersonalLanguage"',
        'id="neoPersonalDefaultPanel"',
        'id="neoPersonalThemeSkin"',
        'onclick="openNeoPersonalSettings()"',
    ]:
        assert marker in INDEX_HTML
    assert "renderNeoPersonalPanel" in DASHBOARD_JS
    assert "openNeoPersonalSettings" in DASHBOARD_JS
    assert "switchSettingsSection('preferences')" in DASHBOARD_JS
    assert "renderNeoPersonalPanel" in PANELS_JS


def test_admin_and_personal_css_and_task_tracking_present():
    for selector in [
        ".dashboard-admin-menu",
        ".dashboard-admin-menu-item",
        ".neo-personal-overview",
        ".neo-personal-grid",
        ".neo-personal-settings-link",
    ]:
        assert selector in STYLE_CSS

    hu_03_9 = TASKS_MD.split("### HU-03.9", 1)[1].split("### HU-03.10", 1)[0]
    assert "**Status:** concluída" in hu_03_9
    assert "- [x] Menu Perfil / Configurações / Logout." in hu_03_9

    hu_03_10 = TASKS_MD.split("### HU-03.10", 1)[1].split("### HU-03.11", 1)[0]
    assert "**Status:** concluída" in hu_03_10
    assert "- [x] Criar placeholder útil com perfil + preferências." in hu_03_10
