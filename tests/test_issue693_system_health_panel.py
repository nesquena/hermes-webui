"""Regression coverage for #693 live VPS host resource health panel."""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from urllib.parse import urlparse


REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
AUTH_PY = (REPO_ROOT / "api" / "auth.py").read_text(encoding="utf-8")


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def test_system_health_payload_normalizes_safe_aggregate_metrics(monkeypatch):
    from api import system_health

    monkeypatch.setattr(system_health, "_cpu_percent", lambda: 17.345)
    monkeypatch.setattr(
        system_health,
        "_memory_usage",
        lambda: {"used_bytes": 4_000, "total_bytes": 10_000, "percent": 40.0},
    )
    monkeypatch.setattr(
        system_health,
        "_disk_usage",
        lambda: {"used_bytes": 55_500, "total_bytes": 100_000, "percent": 55.5},
    )

    payload = system_health.build_system_health_payload()

    assert payload["status"] == "ok"
    assert payload["available"] is True
    assert payload["cpu"] == {"percent": 17.3}
    assert payload["memory"] == {"used_bytes": 4000, "total_bytes": 10000, "percent": 40.0}
    assert payload["disk"] == {"used_bytes": 55500, "total_bytes": 100000, "percent": 55.5}
    assert payload["checked_at"]
    rendered = repr(payload)
    for private_fragment in ("/home/", "/Users/", "mount", "path", "argv", "command", "env", "token"):
        assert private_fragment not in rendered


def test_system_health_payload_partial_and_unavailable_are_graceful(monkeypatch):
    from api import system_health

    def boom():
        raise RuntimeError("private /home/user/path should not leak")

    monkeypatch.setattr(system_health, "_cpu_percent", boom)
    monkeypatch.setattr(system_health, "_memory_usage", boom)
    monkeypatch.setattr(
        system_health,
        "_disk_usage",
        lambda: {"used_bytes": 1, "total_bytes": 4, "percent": 25.0},
    )

    partial = system_health.build_system_health_payload()
    assert partial["status"] == "partial"
    assert partial["available"] is True
    assert partial["disk"]["percent"] == 25.0
    assert partial["cpu"] is None
    assert partial["memory"] is None
    assert {e["metric"] for e in partial["errors"]} == {"cpu", "memory"}
    assert "/home/user" not in repr(partial)

    monkeypatch.setattr(system_health, "_disk_usage", boom)
    unavailable = system_health.build_system_health_payload()
    assert unavailable["status"] == "unavailable"
    assert unavailable["available"] is False
    assert unavailable["cpu"] is None
    assert unavailable["memory"] is None
    assert unavailable["disk"] is None
    assert "/home/user" not in repr(unavailable)


def test_system_health_route_registered_and_auth_gated(monkeypatch):
    assert 'parsed.path == "/api/system/health"' in ROUTES_PY
    assert "build_system_health_payload()" in ROUTES_PY
    assert '"/api/system/health"' not in AUTH_PY, "system metrics must not be public"

    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")
    from api.auth import check_auth

    handler = _FakeHandler()
    assert check_auth(handler, SimpleNamespace(path="/api/system/health", query="")) is False
    assert handler.status in (302, 401)


def test_system_health_route_returns_only_sanitized_payload(monkeypatch):
    from api import routes

    monkeypatch.setattr(
        routes,
        "build_system_health_payload",
        lambda: {
            "status": "ok",
            "available": True,
            "checked_at": "2026-05-05T00:00:00+00:00",
            "cpu": {"percent": 12.0},
            "memory": {"used_bytes": 1, "total_bytes": 2, "percent": 50.0},
            "disk": {"used_bytes": 3, "total_bytes": 4, "percent": 75.0},
            "errors": [],
        },
    )
    handler = _FakeHandler()
    assert routes.handle_get(handler, urlparse("http://example.test/api/system/health")) is True
    payload = handler.json_body()
    assert payload["cpu"]["percent"] == 12.0
    assert set(payload) == {"status", "available", "checked_at", "cpu", "memory", "disk", "errors"}


def test_system_health_panel_markup_and_styles_exist():
    assert 'id="systemHealthPanel"' in INDEX_HTML
    assert 'aria-label="Host resource health"' in INDEX_HTML
    assert 'data-system-health-metric="cpu"' in INDEX_HTML
    assert 'data-system-health-metric="memory"' in INDEX_HTML
    assert 'data-system-health-metric="disk"' in INDEX_HTML
    assert ".system-health-panel" in STYLE_CSS
    assert ".system-health-bar-fill" in STYLE_CSS
    assert ".system-health-panel.unavailable" in STYLE_CSS
    assert "@media(max-width:640px)" in STYLE_CSS and ".system-health-panel" in STYLE_CSS


def test_system_health_frontend_polls_visible_and_renders_progress_labels():
    assert "const SYSTEM_HEALTH_INTERVAL_MS=5000" in UI_JS
    assert "api('/api/system/health')" in UI_JS
    assert "document.visibilityState !== 'visible'" in UI_JS
    assert "document.addEventListener('visibilitychange',_syncSystemHealthMonitorVisibility)" in UI_JS
    assert "function renderSystemHealth(payload)" in UI_JS
    assert "setSystemHealthUnavailable" in UI_JS
    assert "data-system-health-metric" in INDEX_HTML
    assert "CPU" in INDEX_HTML and "RAM" in INDEX_HTML and "Disk" in INDEX_HTML
    assert "aria-valuenow" in UI_JS
    assert "style.width=`${percent}%`" in UI_JS


def test_system_health_backend_uses_no_shell_or_private_process_sources():
    src = (REPO_ROOT / "api" / "system_health.py").read_text(encoding="utf-8")
    assert "import subprocess" not in src
    assert "import psutil" not in src
    assert "os.environ" not in src
    assert "ps aux" not in src
    assert "/proc/self/environ" not in src
    for private_field in ("argv", "cmdline", "username", "mountpoint"):
        assert private_field not in src
