"""Static regression tests for the Connectors settings panel."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_settings_menu_contains_gateway_surface_sections():
    html = read("static/index.html")
    assert 'data-settings-section="connectors"' not in html
    assert 'data-settings-section="messagingChannels"' in html
    assert 'data-settings-section="eventWebhooks"' in html
    assert 'data-settings-section="developerApi"' in html
    assert 'id="settingsPaneMessagingChannels"' in html
    assert 'id="settingsPaneEventWebhooks"' in html
    assert 'id="settingsPaneDeveloperApi"' in html
    assert 'id="messagingChannelsList"' in html
    assert 'id="eventWebhooksList"' in html
    assert 'id="developerApiList"' in html


def test_gateway_surface_panels_load_and_call_connector_api():
    js = read("static/panels.js")
    assert "function loadConnectorSurfacePanel" in js
    assert "function loadMessagingChannelsPanel" in js
    assert "function loadEventWebhooksPanel" in js
    assert "function loadDeveloperApiPanel" in js
    assert "function saveConnectorConfig" in js
    assert "function toggleConnector" in js
    assert "function testConnector" in js
    assert "api('/api/connectors'" in js
    assert "'/api/connectors/'+encodeURIComponent(id)" in js
    assert "connectors_status_not_configured" in js
    assert "_connectorsForCategory" in js
    assert "event_webhook" in js
    assert "developer_api" in js


def test_gateway_status_links_to_messaging_channels_configuration():
    js = read("static/panels.js")
    assert "function loadGatewayStatus" in js
    assert "messaging_channels_configure_link" in js
    assert "switchSettingsSection('messagingChannels')" in js


def test_connectors_i18n_keys_are_present():
    i18n = read("static/i18n.js")
    for key in [
        "messaging_channels_tab_title",
        "messaging_channels_section_title",
        "messaging_channels_configure_link",
        "event_webhooks_tab_title",
        "event_webhooks_section_title",
        "developer_api_tab_title",
        "developer_api_section_title",
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
