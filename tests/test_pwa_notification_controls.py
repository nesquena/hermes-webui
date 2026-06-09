"""Regression coverage for PWA-backed browser notifications (#3196)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_browser_notifications_use_service_worker_when_available():
    assert "function _showPwaNotification" in MESSAGES_JS
    assert "navigator.serviceWorker.ready" in MESSAGES_JS
    assert "reg.showNotification" in MESSAGES_JS
    assert "new Notification" in MESSAGES_JS
    assert "function sendBrowserNotification" in MESSAGES_JS


def test_notification_payload_uses_completion_session_when_provided():
    assert "function _notificationOptions" in MESSAGES_JS
    assert "const sid=(options&&options.sid)||(S&&S.session&&S.session.session_id);" in MESSAGES_JS
    assert "_sessionUrlForSid(sid)" in MESSAGES_JS
    assert "data:{url}" in MESSAGES_JS
    assert "tag:sid?`hermes-${sid}`" in MESSAGES_JS
    assert "sendBrowserNotification('Response complete',assistantText?assistantText.slice(0,100):'Task finished',{sid:activeSid})" in MESSAGES_JS
    assert "sendBrowserNotification('Approval required',d.description||'Tool approval needed',{sid:activeSid})" in MESSAGES_JS
    assert "sendBrowserNotification('Clarification needed',d.question||'Tool clarification needed',{sid:activeSid})" in MESSAGES_JS


def test_service_worker_handles_notification_clicks_without_hijacking_other_sessions():
    assert "notificationclick" in SW_JS
    assert "event.notification.close()" in SW_JS
    assert "clients.matchAll" in SW_JS
    assert "clients.openWindow" in SW_JS
    # Match the open tab on pathname, not the full href (query/hash differ).
    assert "samePath(client.url)" in SW_JS
    assert "new URL(clientUrl).pathname === targetPath" in SW_JS
    assert "targetClient.focus()" in SW_JS
    exact_idx = SW_JS.index("targetClient.focus()")
    open_idx = SW_JS.index("self.clients.openWindow(targetUrl)")
    navigate_idx = SW_JS.index("focusableClient.navigate(targetUrl)")
    assert exact_idx < open_idx < navigate_idx


def test_settings_expose_permission_and_test_controls():
    assert "notificationPermissionStatus" in INDEX_HTML
    assert "requestNotificationPermission()" in INDEX_HTML
    assert "sendBrowserNotification('Hermes test'" in INDEX_HTML
    assert "{force:true}" in INDEX_HTML
    assert "function updateNotificationPermissionStatus" in PANELS_JS
    assert "notifications_permission_status" in PANELS_JS


def test_notification_i18n_and_changelog_entries_exist():
    for key in [
        "notifications_enable_btn",
        "notifications_test_btn",
        "notifications_permission_status",
        "notifications_enabled_toast",
        "notifications_denied",
        "notifications_unsupported",
    ]:
        assert key in I18N_JS
    assert "PWA notifications now use the service worker" in CHANGELOG
    assert "#3196" in CHANGELOG
