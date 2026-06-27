"""Coverage for dashboard plugin backend mounting (plugin_api.py).

A dashboard plugin may ship an optional ``plugin_api.py`` exposing a FastAPI
``APIRouter`` named ``router``.  ``dispatch_plugin_api`` serves that router's GET
routes (read-only) under ``/api/plugins/<name>/<route>`` so the plugin's built
frontend can fetch its own data instead of receiving a 404 (blank panel).

The core dispatch/coercion logic is tested with a dependency-free duck-typed
router; a separate fastapi-gated test exercises real APIRouter introspection
including ``Query(...)`` default unwrapping.
"""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

import api.plugins as plugins


def _seed_module(monkeypatch, name, router):
    """Pre-seed the import cache so dispatch uses our fake module (no file I/O)."""
    mod = ModuleType(f"_fake_plugin_api_{name}")
    mod.router = router
    cache = dict(plugins._PLUGIN_API_MODULES)
    cache[name] = mod
    monkeypatch.setattr(plugins, "_PLUGIN_API_MODULES", cache)


def _route(path, methods, endpoint):
    return SimpleNamespace(path=path, methods=set(methods), endpoint=endpoint)


def test_dispatch_matches_get_route_and_coerces_query(monkeypatch):
    def summary(hours: int = 24):
        return {"hours": hours}

    _seed_module(monkeypatch, "demo", SimpleNamespace(routes=[_route("/summary", ["GET"], summary)]))

    assert plugins.dispatch_plugin_api("demo", "summary", {"hours": ["48"]}) == (200, {"hours": 48})


def test_dispatch_uses_endpoint_default_when_param_absent(monkeypatch):
    def summary(hours: int = 24):
        return {"hours": hours}

    _seed_module(monkeypatch, "demo", SimpleNamespace(routes=[_route("/summary", ["GET"], summary)]))

    assert plugins.dispatch_plugin_api("demo", "summary", {}) == (200, {"hours": 24})


def test_dispatch_unknown_route_returns_none(monkeypatch):
    _seed_module(monkeypatch, "demo", SimpleNamespace(routes=[_route("/summary", ["GET"], lambda: {})]))

    assert plugins.dispatch_plugin_api("demo", "missing", {}) is None


def test_dispatch_ignores_non_get_routes(monkeypatch):
    _seed_module(monkeypatch, "demo", SimpleNamespace(routes=[_route("/summary", ["POST"], lambda: {})]))

    assert plugins.dispatch_plugin_api("demo", "summary", {}) is None


def test_dispatch_rejects_invalid_plugin_name(monkeypatch):
    _seed_module(monkeypatch, "demo", SimpleNamespace(routes=[_route("/summary", ["GET"], lambda: {})]))

    assert plugins.dispatch_plugin_api("../etc", "summary", {}) is None
    assert plugins.dispatch_plugin_api("Bad Name", "summary", {}) is None


def test_dispatch_wraps_endpoint_errors_as_500(monkeypatch):
    def boom():
        raise RuntimeError("kaboom")

    _seed_module(monkeypatch, "demo", SimpleNamespace(routes=[_route("/summary", ["GET"], boom)]))

    status, payload = plugins.dispatch_plugin_api("demo", "summary", {})
    assert status == 500 and "error" in payload


def test_dispatch_no_router_returns_none(monkeypatch):
    _seed_module(monkeypatch, "demo", SimpleNamespace())  # module without .router

    assert plugins.dispatch_plugin_api("demo", "summary", {}) is None


def test_dispatch_with_real_fastapi_router(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    router = fastapi.APIRouter()

    @router.get("/summary")
    def summary(hours: int = fastapi.Query(24, ge=1, le=720)):
        return {"hours": hours}

    _seed_module(monkeypatch, "demo", SimpleNamespace(routes=router.routes))

    # Query(...) default object must be unwrapped to its plain default (24).
    assert plugins.dispatch_plugin_api("demo", "summary", {}) == (200, {"hours": 24})
    assert plugins.dispatch_plugin_api("demo", "summary", {"hours": ["12"]}) == (200, {"hours": 12})


# silence unused-import lint without changing behaviour
_ = (sys, ModuleType)
