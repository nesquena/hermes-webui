"""Regression coverage for #1823 Kanban stale-client/board-pointer failures."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.parse import urlparse

from api import routes

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
ROUTES = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def body_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_unknown_kanban_endpoint_get_returns_stale_client_diagnostic():
    """Obsolete/stale JS should not collapse to a bare `not found` 404."""
    handler = _FakeHandler()
    handled = routes.handle_get(handler, urlparse("/api/kanban/obsolete-shape"))

    assert handled is True
    assert handler.status == 404
    error = handler.body_json()["error"]
    assert error != "not found"
    assert "unknown Kanban endpoint: GET /api/kanban/obsolete-shape" in error
    assert "stale cached bundle" in error
    assert "Hard refresh now" in error


def test_unknown_kanban_endpoint_routes_are_wrapped_for_all_methods():
    assert 'return _kanban_unknown_endpoint(handler, parsed, "GET")' in ROUTES
    assert 'return _kanban_unknown_endpoint(handler, parsed, "POST")' in ROUTES
    assert 'return _kanban_unknown_endpoint(handler, parsed, "PATCH")' in ROUTES
    assert 'return _kanban_unknown_endpoint(handler, parsed, "DELETE")' in ROUTES


def test_kanban_stale_client_error_renders_hard_refresh_escape_hatch():
    assert "function _kanbanLooksLikeStaleClientError(err)" in PANELS
    assert "err.status === 404" in PANELS
    assert "msg === 'not found'" in PANELS
    assert "msg.includes('unknown kanban endpoint')" in PANELS
    assert "Kanban needs a hard refresh" in PANELS
    assert "Hard refresh now" in PANELS
    assert "navigator.serviceWorker.getRegistrations()" in PANELS
    assert "caches.keys()" in PANELS
    assert "window.location.reload()" in PANELS


def test_inner_handler_bad_response_does_not_emit_double_404(monkeypatch):
    """Regression: when the kanban bridge already sent a response via bad()
    (returns None), the unknown-endpoint wrapper must not concatenate a second
    404 body on the wire. Only an explicit `False` from the bridge means the
    path was unmatched.
    """
    from api import kanban_bridge

    # Force the task-log payload helper to report "not found" so the bridge
    # calls bad() and returns None.
    monkeypatch.setattr(kanban_bridge, "_task_log_payload", lambda *a, **kw: None)

    handler = _FakeHandler()
    handled = routes.handle_get(handler, urlparse("/api/kanban/tasks/abc/log"))

    assert handled is True
    assert handler.status == 404
    body = handler.wfile.getvalue().decode("utf-8")
    # Exactly one JSON object should have been written. Two concatenated
    # objects would produce something like `}{` between them.
    assert body.count("}{") == 0, f"double response detected: {body!r}"
    payload = json.loads(body)
    assert payload["error"] == "task not found"


def test_kanban_load_resolves_board_before_board_scoped_requests():
    boards_pos = PANELS.find("await loadKanbanBoards();")
    config_pos = PANELS.find("api('/api/kanban/config' + _kanbanBoardQuery())")
    assert boards_pos != -1
    assert config_pos != -1
    assert boards_pos < config_pos
    assert "_kanbanSetSavedBoard('default');" in PANELS
