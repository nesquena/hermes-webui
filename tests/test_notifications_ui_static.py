"""Static coverage for the Hermex/WebUI cron notification inbox UI."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
NOTIFICATIONS = (ROOT / "static" / "notifications.js").read_text(encoding="utf-8")
ROUTES = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_notifications_script_and_nav_are_wired():
    assert 'static/notifications.js?v=__WEBUI_VERSION__' in INDEX
    assert 'data-panel="notifications"' in INDEX
    assert 'id="notificationsBadge"' in INDEX
    assert 'id="notificationsBadgeMobile"' in INDEX
    assert 'id="panelNotifications"' in INDEX
    assert 'id="mainNotifications"' in INDEX


def test_notifications_panel_is_part_of_main_view_switching():
    assert "notifications: 'Hermex Inbox'" in PANELS
    assert "'notifications'" in PANELS
    assert "nextPanel === 'notifications'" in PANELS
    assert "loadNotifications" in PANELS
    assert "showing-notifications" in STYLE
    assert "#mainNotifications" in STYLE


def test_notifications_js_uses_aggregate_api_sse_and_badge_without_duplicate_toast():
    assert "all_profiles: '1'" in NOTIFICATIONS
    assert "/api/notifications" in NOTIFICATIONS
    assert "api/notifications/events" in NOTIFICATIONS
    assert "new EventSource" in NOTIFICATIONS
    assert "_setNotificationBadge" in NOTIFICATIONS
    assert "showToast('Cron finished:" not in NOTIFICATIONS
    assert "markSelectedNotificationRead" in NOTIFICATIONS
    assert "markAllNotificationsRead" in NOTIFICATIONS
    assert "openNotificationCronJob" in NOTIFICATIONS


def test_notification_ids_never_enter_inline_javascript_string_literals():
    assert "openNotificationDetail(this.dataset.notificationKey)" in NOTIFICATIONS
    assert "openNotificationCronJob(this.dataset.jobId)" in NOTIFICATIONS
    assert "data-notification-key" in NOTIFICATIONS
    assert "data-notification-id" not in NOTIFICATIONS
    assert "openNotificationDetail('${esc(id)}')" not in NOTIFICATIONS
    assert "openNotificationCronJob('${esc(String(row.job_id))}')" not in NOTIFICATIONS


def test_notification_identity_is_profile_scoped_end_to_end():
    assert "JSON.stringify([String(row && row.profile || ''), id])" in NOTIFICATIONS
    assert "_notificationsSelectedKey" in NOTIFICATIONS
    assert "_notificationsSeenKeys" in NOTIFICATIONS
    assert "payload.profile = row.profile" in NOTIFICATIONS


def test_sse_connection_snapshot_precedes_once_exit():
    snapshot = 'handler.wfile.write(sse_event("snapshot", initial))'
    assert snapshot in ROUTES
    assert ROUTES.index(snapshot) < ROUTES.index("if once:", ROUTES.index(snapshot))
