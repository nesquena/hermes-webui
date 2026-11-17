"""Regression coverage for notification clicks reusing an open WebUI tab (#4109)."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SW_SRC = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
ROUTES_SRC = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """This module only reads static source; it does not need the HTTP fixture."""


def _notification_click_handler() -> str:
    start = SW_SRC.index("self.addEventListener('notificationclick'")
    return SW_SRC[start:]


def test_notification_click_keeps_exact_path_focus_fast_path():
    """The focus() fast path must keep matching on pathname, exactly as before.

    A session deep link inherits the current page's query string, so requiring a
    query match would fail to find an already-open `/session/<sid>` tab and
    spawn a duplicate window. The search component only becomes part of the
    target's identity when the URL carries a panel intent (#7652 round 5)."""
    handler = _notification_click_handler()

    target_idx = handler.index("const targetClient = clientList.find")
    exact_focus_idx = handler.index("if (targetClient) return targetClient.focus();")
    reusable_idx = handler.index("const focusableClient = clientList.find")

    assert "samePath(client.url)" in handler
    assert "new URL(clientUrl).pathname === targetPath" in handler
    assert target_idx < exact_focus_idx < reusable_idx

    # #7652 round 5: the pathname+search match is conditional on the target
    # actually carrying a panel intent, so a plain session notification is
    # never routed by its query string.
    assert "const samePathAndSearch = (clientUrl) =>" in handler
    assert "c.pathname === targetPath && c.search === new URL(targetUrl).search" in handler
    assert "targetCarriesPanelIntent ? samePathAndSearch(client.url) : samePath(client.url)" in handler
    assert "const targetCarriesPanelIntent = (() =>" in handler
    assert "searchParams.get('panel')" in handler


def test_notification_click_navigates_reusable_client_before_opening_window():
    handler = _notification_click_handler()

    reusable_idx = handler.index("const focusableClient = clientList.find")
    navigate_idx = handler.index("focusableClient.navigate(targetUrl)")
    open_fallback_idx = handler.index("return openNotificationWindow();")

    assert "sameOrigin(client.url) && 'focus' in client && 'navigate' in client" in handler
    assert reusable_idx < navigate_idx < open_fallback_idx
    assert "if (self.clients.openWindow) return self.clients.openWindow(targetUrl)" not in handler

    # #7652 round 5: a same-pathname client is navigated ONLY for a panel
    # intent. For a session notification that reload would discard composer
    # text still inside the 400ms draft-save window, so the gate has to come
    # before the navigate, not merely prefer it.
    same_pathname_idx = handler.index("const samePathnameClient = clientList.find")
    assert "samePath(client.url) && 'navigate' in client" in handler
    assert "if (targetCarriesPanelIntent) {" in handler
    gate_idx = handler.index("if (targetCarriesPanelIntent) {")
    assert gate_idx < same_pathname_idx < reusable_idx, (
        "the panel-intent gate must precede the same-pathname navigate"
    )


def test_notification_click_focuses_after_navigation_or_navigation_failure():
    handler = _notification_click_handler()

    assert (
        ".then((client) => (client && 'focus' in client ? "
        "client.focus() : focusableClient.focus()))"
    ) in handler
    assert ".catch(() => focusableClient.focus())" in handler


def test_notification_click_open_window_remains_no_reusable_client_fallback():
    handler = _notification_click_handler()

    assert "const openNotificationWindow = () => (" in handler
    assert "self.clients.openWindow ? self.clients.openWindow(targetUrl) : undefined" in handler
    assert handler.index("return openNotificationWindow();") > handler.index(
        "focusableClient.navigate(targetUrl)"
    )


def test_test_notification_without_sid_still_targets_current_page_for_reuse():
    # #7652 review: a caller with no options at all (the Settings test button)
    # still falls back to the current session's URL; only an explicit
    # {sessionless:true} marker routes away from it.
    assert "const sessionless=!!(options&&options.sessionless);" in MESSAGES_SRC
    assert "sessionless?null:((options&&options.sid)||(S&&S.session&&S.session.session_id))" in MESSAGES_SRC
    # #7652 round 4: a sessionless target carries a panel intent so the click
    # opens the Tasks panel instead of the last chat boot would restore.
    assert "const url=sessionless?rootWithPanelIntent:(sid?`${location.origin}${_sessionUrlForSid(sid)}`:location.href);" in MESSAGES_SRC
    assert "const rootWithPanelIntent=panel" in MESSAGES_SRC
    assert "panel=${encodeURIComponent(panel)}" in MESSAGES_SRC
    assert "sendBrowserNotification('Hermes test','Notifications are ready.',{force:true});" in (
        ROOT / "static" / "index.html"
    ).read_text(encoding="utf-8")


def test_service_worker_update_delivery_keeps_versioned_no_store_route():
    assert "const CACHE_NAME = 'hermes-shell-__WEBUI_VERSION__';" in SW_SRC
    assert "self.skipWaiting();" in SW_SRC
    assert "self.clients.claim();" in SW_SRC

    route_idx = ROUTES_SRC.index('"/sw.js"')
    route_block = ROUTES_SRC[route_idx : route_idx + 1200]
    assert 'replace(\n                "__WEBUI_VERSION__", version_token\n            )' in route_block
    assert 'handler.send_header("Cache-Control", "no-store")' in route_block
