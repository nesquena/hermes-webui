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
