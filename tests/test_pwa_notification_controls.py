"""Regression coverage for PWA-backed browser notifications (#3196)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

DESKTOP_BACKGROUND_NOTIFICATION_NAMES = (
    "_desktopBackgroundedForNotifications",
    "__hermesSetBackgrounded",
    "_isBackgroundedForBrowserNotification",
)


def _source_between(start_marker: str, end_marker: str) -> str:
    start = MESSAGES_JS.index(start_marker)
    end = MESSAGES_JS.index(end_marker, start)
    return MESSAGES_JS[start:end]


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
    assert "sendBrowserNotification('Response complete',assistantText?assistantText.slice(0,100):'Task finished',{forceHidden:_wasEverBackgrounded,sid:activeSid})" in MESSAGES_JS
    assert "sendBrowserNotification('Approval required',d.description||'Tool approval needed',{sid:activeSid})" in MESSAGES_JS
    assert "sendBrowserNotification('Clarification needed',d.question||'Tool clarification needed',{sid:activeSid})" in MESSAGES_JS


def test_completion_notification_fires_when_tab_was_hidden_during_stream():
    """#4416: a throttled background-tab SSE delivers `done` late (after the user
    returns, document.hidden=false), which silently dropped the completion
    notification. The done handler now passes forceHidden based on whether the
    tab was hidden at ANY point during the stream, and sendBrowserNotification
    bypasses ONLY the live visibility gate (not the user's enabled setting) on
    forceHidden — so a backgrounded stream notifies, a watched one stays silent."""
    # The per-stream hidden tracker exists and is wired at attach + done.
    assert "_STREAM_WAS_HIDDEN" in MESSAGES_JS
    assert "function _bindStreamHiddenTracker" in MESSAGES_JS
    # Entries are stream-owned ({streamId, wasHidden}) so a stale entry from a
    # non-`done` terminal path can't be mis-attributed to a later same-sid stream.
    assert "function _shouldForceCompletionNotification(sid, streamId){" in MESSAGES_JS
    assert "return wasHidden||wasBackgrounded;" in MESSAGES_JS
    assert "function _clearStreamHidden" in MESSAGES_JS
    assert "function _clearStreamNotificationBackground" in MESSAGES_JS
    # Done-path cleanup lives inside _shouldForceCompletionNotification(); the
    # activeSid call sites are the non-done terminal paths.
    assert "_clearStreamHidden(sid, streamId);" in MESSAGES_JS
    assert "_clearStreamNotificationBackground(sid, streamId);" in MESSAGES_JS
    assert MESSAGES_JS.count("_clearStreamHidden(activeSid, streamId)") >= 3
    assert MESSAGES_JS.count("_clearStreamNotificationBackground(activeSid, streamId)") >= 3
    # sendBrowserNotification honors forceHidden but still respects the
    # notifications-enabled setting (forceHidden is NOT the test-button force).
    assert "const forceHidden=!!(options&&options.forceHidden);" in MESSAGES_JS
    assert "if(!force&&!window._notificationsEnabled) return;" in MESSAGES_JS
    assert "function _isBackgroundedForBrowserNotification(){" in MESSAGES_JS
    assert "window.__hermesSetBackgrounded=(value)=>{" in MESSAGES_JS
    assert "if(!force&&!forceHidden&&!_isBackgroundedForBrowserNotification()) return;" in MESSAGES_JS


def test_desktop_background_notification_signal_stays_out_of_stream_visibility():
    stream_tracker = _source_between(
        "const LIVE_STREAMS={};",
        "function closeLiveStream(sessionId, streamId, source){",
    )
    deferred_recovery = _source_between(
        "function _reattachOrRestoreAfterDeferredStreamError(source){",
        "  // Bug A fix (#631):",
    )

    for name in DESKTOP_BACKGROUND_NOTIFICATION_NAMES:
        assert name not in stream_tracker
        assert name not in deferred_recovery


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
    assert 'id="notificationPermissionButtonWrap"' in INDEX_HTML
    assert 'id="notificationPermissionButton"' in INDEX_HTML
    assert "requestNotificationPermission()" in INDEX_HTML
    assert "sendBrowserNotification('Hermes test'" in INDEX_HTML
    assert "{force:true}" in INDEX_HTML
    assert "function updateNotificationPermissionStatus" in PANELS_JS
    assert "const btn=$('notificationPermissionButton');" in PANELS_JS
    assert "const btnWrap=$('notificationPermissionButtonWrap');" in PANELS_JS
    assert "btn.disabled=granted;" in PANELS_JS
    assert "btn.title=granted?'':label;" in PANELS_JS
    assert "if(btnWrap) btnWrap.title=label;" in PANELS_JS
    assert "notifications_permission_status" in PANELS_JS
    assert "btn.setAttribute('aria-label', label);" in PANELS_JS
    assert "btn.setAttribute('aria-disabled', granted?'true':'false');" in PANELS_JS
    assert "btn.setAttribute('aria-disabled','true');" in PANELS_JS


def test_granted_permission_branch_is_not_silent():
    fn = MESSAGES_JS[
        MESSAGES_JS.index("function requestNotificationPermission(){") :
        MESSAGES_JS.index("function sendBrowserNotification(", MESSAGES_JS.index("function requestNotificationPermission(){"))
    ]
    assert "if(Notification.permission==='granted'){" in fn
    granted_branch = fn[
        fn.index("if(Notification.permission==='granted'){") :
        fn.index("if(Notification.permission==='denied'){")
    ]
    assert "updateNotificationPermissionStatus()" in granted_branch
    assert "showToast(t('notifications_enabled_toast'),3000)" in granted_branch
    assert "return Promise.resolve('granted');" in granted_branch


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
    entry = next(
        line for line in CHANGELOG.splitlines()
        if "Notification permission controls now reflect the real browser state" in line
    )
    assert entry.count("#4118") == 1
