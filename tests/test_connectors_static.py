"""Static regression tests for the Connectors settings panel."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_settings_menu_contains_connectors_section():
    html = read("static/index.html")
    assert 'data-settings-section="connectors"' in html
    assert 'id="settingsPaneConnectors"' in html
    assert 'id="connectorsList"' in html
    assert 'id="connectorDetail"' in html


def test_connectors_panel_loads_and_calls_connector_api():
    js = read("static/panels.js")
    assert "function loadConnectorsPanel" in js
    assert "function saveConnectorConfig" in js
    assert "function toggleConnector" in js
    assert "function testConnector" in js
    assert "api('/api/connectors'" in js
    assert "'/api/connectors/'+encodeURIComponent(id)" in js
    assert "connectors_status_not_configured" in js
    assert "connector-category-group" in js
    assert "connectors_category_messaging" in js


def test_gateway_status_links_to_connectors_configuration():
    js = read("static/panels.js")
    assert "function loadGatewayStatus" in js
    assert "connectors_configure_link" in js
    assert "switchSettingsSection('connectors')" in js


def test_connectors_i18n_keys_are_present():
    i18n = read("static/i18n.js")
    for key in [
        "connectors_tab_title",
        "connectors_section_title",
        "connectors_configure_link",
        "connectors_category_messaging",
        "connectors_category_event_webhook",
        "connectors_category_developer_api",
        "connectors_status_not_configured",
        "connectors_status_configured",
        "connectors_status_enabled",
        "connectors_status_error",
        "connectors_status_unknown",
        "connectors_saved",
        "connectors_test_failed",
    ]:
        assert key in i18n


def test_connectors_routes_are_registered():
    routes = read("api/routes.py")
    assert 'parsed.path == "/api/connectors"' in routes
    assert 'parsed.path.startswith("/api/connectors/")' in routes
    assert "save_connector" in routes
    assert "toggle_connector" in routes
    assert "test_connector" in routes
