"""Regression tests for service worker API cache exclusion under subpath mounts.

The WebUI can be served at /hermes/. In that deployment API requests look like
/hermes/api/sessions, not /api/sessions. The service worker must treat those as
network-only; otherwise cache-first handling can serve a stale sidebar session
list until the browser cache/service-worker cache is cleared.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SW_SRC = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")


def test_service_worker_excludes_subpath_mounted_api_routes_from_cache():
    assert "url.pathname.includes('/api/')" in SW_SRC, (
        "service worker must bypass cache for subpath-mounted API routes like "
        "/hermes/api/sessions, not only root-mounted /api/*"
    )


def test_service_worker_excludes_subpath_mounted_health_routes_from_cache():
    assert "url.pathname.includes('/health')" in SW_SRC, (
        "service worker must bypass cache for subpath-mounted health routes like "
        "/hermes/health, not only root-mounted /health"
    )


def test_service_worker_documents_api_routes_are_never_cached():
    assert "API and streaming endpoints" in SW_SRC
    assert "always go to network" in SW_SRC


def test_service_worker_does_not_intercept_its_own_script():
    assert "url.pathname.endsWith('/sw.js')" in SW_SRC, (
        "service worker must bypass /sw.js so a stale cached worker cannot block cache-version updates"
    )


def test_service_worker_uses_network_first_for_page_navigation():
    """Page navigations must hit the server before cache so expired auth redirects work."""
    navigate_idx = SW_SRC.find("event.request.mode === 'navigate'")
    assert navigate_idx != -1, "service worker must special-case page navigations"
    fetch_idx = SW_SRC.find("fetch(new Request(event.request, { cache: 'no-store' }))", navigate_idx)
    cache_idx = SW_SRC.find("caches.match", navigate_idx)
    assert fetch_idx != -1, "navigation branch must try the live server first while bypassing HTTP cache"
    assert cache_idx != -1, "navigation branch may use cached shell only as offline fallback"
    assert fetch_idx < cache_idx, (
        "navigation requests must be network-first, not cache-first, so auth redirects "
        "and freshly set login cookies are honored without a manual refresh"
    )


def test_service_worker_navigation_retries_plain_url_fetch_before_offline():
    """Cloned navigate-mode fetches can reject behind proxies while the origin is up.

    The navigate branch must retry with event.request.url + same-origin credentials
    before serving the synthetic offline HTML (which would otherwise trap the tab).
    """
    navigate_idx = SW_SRC.find("event.request.mode === 'navigate'")
    assert navigate_idx != -1, "service worker must special-case page navigations"
    # End of navigate respondWith block before the shell-asset path.
    navigate_end = SW_SRC.find("Shell assets: network-first", navigate_idx)
    assert navigate_end != -1, "expected shell-assets block after navigate branch"
    navigate_block = SW_SRC[navigate_idx:navigate_end]
    first_fetch = navigate_block.find(
        "fetch(new Request(event.request, { cache: 'no-store' }))"
    )
    retry_fetch = navigate_block.find(
        "fetch(event.request.url, { cache: 'no-store', credentials: 'same-origin'"
    )
    offline_html = navigate_block.find("You are offline")
    assert first_fetch != -1, "navigation branch must keep the cloned Request no-store fetch"
    assert retry_fetch != -1, (
        "navigation branch must retry with event.request.url and credentials: 'same-origin' "
        "after the cloned Request fetch rejects"
    )
    assert "redirect: 'follow'" in navigate_block, (
        "navigation retry must use redirect: 'follow'"
    )
    assert offline_html != -1, "navigation branch must still have offline HTML as last resort"
    assert first_fetch < retry_fetch < offline_html, (
        "order must be: cloned Request fetch → plain URL retry → offline HTML"
    )


def test_service_worker_navigation_offline_html_self_heals_to_login():
    """Synthetic offline HTML recovers via scope-relative login after health, not root /login."""
    navigate_idx = SW_SRC.find("event.request.mode === 'navigate'")
    assert navigate_idx != -1
    navigate_end = SW_SRC.find("Shell assets: network-first", navigate_idx)
    navigate_block = SW_SRC[navigate_idx:navigate_end]
    assert "You are offline" in navigate_block
    assert "Hermes requires a server connection" in navigate_block
    assert "new URL('login', self.registration.scope)" in navigate_block, (
        "offline fallback must resolve login against the SW scope so a /hermes/ "
        "mount recovers to /hermes/login, never origin /login"
    )
    assert "new URL('health', self.registration.scope)" in navigate_block, (
        "offline fallback must probe health against the SW scope"
    )
    assert "getRegistration(" in navigate_block, (
        "offline fallback must unregister only this SW via getRegistration(scope)"
    )
    assert "getRegistrations()" not in navigate_block, (
        "offline fallback must not iterate every service worker on the origin"
    )
    online_idx = navigate_block.find("navigator.onLine")
    assert online_idx != -1, "auto-recover must consult navigator.onLine"
    health_fetch_idx = navigate_block.find("fetch(healthUrl")
    assert health_fetch_idx != -1, "auto-recover must probe fetch(healthUrl)"
    assert online_idx < health_fetch_idx, (
        "navigator.onLine must be consulted before the health probe auto-recover"
    )
    assert "Retry" in navigate_block, (
        "offline fallback must keep Retry as a user control"
    )


def test_service_worker_does_not_precache_page_shell_under_auth():
    """Do not cache './' during install; it may be the authenticated app or login redirect."""
    shell_block = SW_SRC[SW_SRC.find("const SHELL_ASSETS"):SW_SRC.find("];", SW_SRC.find("const SHELL_ASSETS"))]
    assert "'./'" not in shell_block and '"./"' not in shell_block, (
        "pre-caching './' can serve a stale authenticated app shell while logged out; "
        "navigation should populate shell cache only after a successful non-redirect network load"
    )


def test_service_worker_never_caches_login_page_or_login_script():
    assert "url.pathname.endsWith('/login')" in SW_SRC or "url.pathname.includes('/login')" in SW_SRC, (
        "service worker must bypass the login page so stale auth UI cannot survive until cache clear"
    )
    assert "url.pathname.endsWith('/static/login.js')" in SW_SRC, (
        "service worker must bypass static/login.js so stale login handlers cannot block password submit"
    )


def test_service_worker_only_cache_puts_shell_assets_or_valid_navigation_shell():
    assert "SHELL_ASSETS.includes(shellPath)" in SW_SRC, (
        "non-navigation cache puts must be limited to the explicit app shell asset allowlist; "
        "a generic cache-first handler can trap stale login.js until users clear cache"
    )
