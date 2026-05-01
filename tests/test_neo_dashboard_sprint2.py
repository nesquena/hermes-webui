"""Neo WebUI Sprint 2: Dashboard panel registration and default panel."""

from pathlib import Path


REPO = Path(__file__).parent.parent
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")


def test_dashboard_panel_is_registered_in_rail_and_sidebar():
    assert 'class="rail-btn nav-tab' in INDEX_HTML
    assert 'data-panel="dashboard"' in INDEX_HTML
    assert 'onclick="switchPanel(\'dashboard\')"' in INDEX_HTML
    assert 'data-i18n-title="tab_dashboard"' in INDEX_HTML
    assert 'data-label="Dashboard"' in INDEX_HTML


def test_dashboard_has_panel_and_main_view_shell():
    assert 'id="panelDashboard"' in INDEX_HTML
    assert 'id="mainDashboard"' in INDEX_HTML
    assert 'static/dashboard.js' in INDEX_HTML
    assert 'loadDashboard()' in INDEX_HTML or 'function loadDashboard' in (REPO / "static" / "dashboard.js").read_text(encoding="utf-8")


def test_switch_panel_treats_dashboard_as_main_view_and_lazy_loads():
    assert "'dashboard'" in PANELS_JS
    assert "showing-dashboard" in PANELS_JS
    assert "nextPanel === 'dashboard'" in PANELS_JS
    assert "loadDashboard" in PANELS_JS


def test_boot_supports_dashboard_query_and_default_setting():
    assert "URLSearchParams(location.search).get('panel')" in BOOT_JS
    assert "default_panel" in BOOT_JS
    assert "dashboard" in BOOT_JS
    assert "switchPanel(_initialPanel" in BOOT_JS


def test_backend_default_panel_setting_uses_env_with_chat_fallback():
    assert "HERMES_WEBUI_DEFAULT_PANEL" in CONFIG_PY
    assert '"default_panel"' in CONFIG_PY
    assert '"chat"' in CONFIG_PY
