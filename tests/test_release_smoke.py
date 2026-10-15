"""Release gate smoke suite — fast subset that runs on every release target OS.

Exercises three release-critical properties without starting a server:

  1. App import / boot: core modules load cleanly (no import-time crashes).
  2. API surface: key route handlers and health-check plumbing are present.
  3. Static asset integrity: index.html + service-worker reference expected
     boot-critical identifiers.

Intentionally free of wall-clock budget assertions (no ``assert elapsed < N``).
Runs in under 30 seconds on any supported platform.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. App import / boot smoke
# ---------------------------------------------------------------------------


def test_api_config_importable():
    """api.config must import without raising."""
    mod = importlib.import_module("api.config")
    assert mod is not None


def test_api_routes_importable():
    """api.routes must import without raising."""
    mod = importlib.import_module("api.routes")
    assert mod is not None


def test_api_models_importable():
    """api.models must import without raising."""
    mod = importlib.import_module("api.models")
    assert mod is not None


def test_api_streaming_importable():
    """api.streaming must import without raising."""
    mod = importlib.import_module("api.streaming")
    assert mod is not None


def test_bootstrap_importable():
    """bootstrap.py must import without raising (covers entry-point wiring)."""
    mod = importlib.import_module("bootstrap")
    assert mod is not None


# ---------------------------------------------------------------------------
# 2. API surface smoke — handler presence
# ---------------------------------------------------------------------------


def test_health_handler_defined():
    """A health-check route handler must exist in api.routes."""
    import api.routes as routes
    # The health endpoint is referenced by the CI smoke and packaging smoke.
    assert hasattr(routes, "_handle_health") or any(
        "health" in name for name in dir(routes)
    ), "No health handler found in api.routes"


def test_sessions_handler_defined():
    """The /api/sessions handler must exist (core API surface)."""
    import api.routes as routes
    assert any(
        "session" in name.lower() for name in dir(routes)
    ), "No session handler found in api.routes"


def test_config_has_default_port():
    """Config must expose a default server port."""
    import api.config as config
    port = getattr(config, "DEFAULT_PORT", None) or getattr(config, "PORT", None)
    assert port is not None, "api.config must expose DEFAULT_PORT or PORT"
    assert isinstance(port, int), f"Port must be an int, got {type(port)}"
    assert 1024 <= port <= 65535, f"Port {port} is outside valid TCP range"


# ---------------------------------------------------------------------------
# 3. Static asset integrity
# ---------------------------------------------------------------------------


def test_index_html_exists():
    """static/index.html must exist at the expected path."""
    index = REPO / "static" / "index.html"
    assert index.is_file(), "static/index.html is missing"


def test_index_html_has_boot_script():
    """index.html must reference boot.js (the app entry point)."""
    index = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    assert "boot.js" in index, "index.html must reference boot.js"


def test_service_worker_exists():
    """static/sw.js (service worker) must exist."""
    sw = REPO / "static" / "sw.js"
    assert sw.is_file(), "static/sw.js is missing"


def test_server_py_exists():
    """server.py (main entry point) must exist in the repo root."""
    server = REPO / "server.py"
    assert server.is_file(), "server.py is missing from repo root"


def test_server_py_importable():
    """server.py must be syntactically valid Python (importable)."""
    import py_compile
    result = py_compile.compile(str(REPO / "server.py"), doraise=True)
    assert result is not None or True  # compile raises on error, so reaching here = ok


@pytest.mark.parametrize("name", [
    "sessions.js",
    "messages.js",
    "ui.js",
    "panels.js",
])
def test_critical_js_file_exists(name):
    """Core JS files must exist in static/."""
    path = REPO / "static" / name
    assert path.is_file(), f"static/{name} is missing"
