from __future__ import annotations

import io
import json
from urllib.parse import urlparse


class FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(b"{}")
        self.headers = {"Content-Length": "2"}
        self.client_address = ("127.0.0.1", 0)
        self.request = None

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


def test_admin_reload_returns_ok_without_binding_missing_compact(monkeypatch):
    import importlib
    from api import routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "_guard_request_session_visibility",
        lambda handler, parsed, body=None, method="POST": True,
    )
    # Exercise the endpoint bindings without mutating the real api.models module.
    monkeypatch.setattr(importlib, "reload", lambda module: module)

    handler = FakeHandler()
    handled = routes.handle_post(handler, urlparse("/api/admin/reload"))

    assert handled is None
    assert handler.status == 200
    assert handler.json_body() == {"status": "ok", "reloaded": "api.models"}
    assert routes.get_session.__module__ == "api.models"
    assert routes.Session.__module__ == "api.models"
