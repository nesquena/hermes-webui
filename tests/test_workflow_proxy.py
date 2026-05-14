import json
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import urlparse


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self

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


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        if isinstance(self._payload, str):
            return self._payload.encode("utf-8")
        return json.dumps(self._payload).encode("utf-8")


def test_workflow_proxy_route_forwards_canonical_read_model_path(monkeypatch):
    from api import dashboard_probe, workflows
    from api.routes import handle_get

    monkeypatch.setattr(
        dashboard_probe,
        "get_dashboard_status",
        lambda: {"running": True, "url": "http://127.0.0.1:9119"},
    )
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout, dict(request.header_items())))
        if request.full_url == "http://127.0.0.1:9119/workflows":
            return _FakeResponse('<script>window.__HERMES_SESSION_TOKEN__="token-123";</script>')
        return _FakeResponse({"facts": {"workflows": [{"id": "wf_1"}]}, "insights": None})

    monkeypatch.setattr(workflows.urllib.request, "urlopen", fake_urlopen)
    handler = _FakeHandler()

    handled = handle_get(handler, urlparse("http://example.com/api/workflows?limit=10"))

    assert handled is True
    assert handler.status == 200
    assert handler.json_body()["facts"]["workflows"][0]["id"] == "wf_1"
    assert calls == [
        ("http://127.0.0.1:9119/workflows", 2.0, {"Accept": "text/html", "User-agent": "hermes-webui-workflow-proxy"}),
        (
            "http://127.0.0.1:9119/api/workflows?limit=10",
            2.0,
            {
                "Accept": "application/json",
                "User-agent": "hermes-webui-workflow-proxy",
                "X-hermes-session-token": "token-123",
            },
        ),
    ]


def test_workflow_proxy_maps_missing_backend_to_structured_unavailable(monkeypatch):
    from api import dashboard_probe
    from api.routes import handle_get

    monkeypatch.setattr(dashboard_probe, "get_dashboard_status", lambda: {"running": False, "enabled": "auto"})
    handler = _FakeHandler()

    handled = handle_get(handler, urlparse("http://example.com/api/workflows/wf_1/dag"))

    assert handled is True
    assert handler.status == 503
    body = handler.json_body()
    assert body["error"] == "Workflow API is not available on this Hermes backend."
    assert body["backend"]["running"] is False


def test_workflow_proxy_preserves_not_found_but_converts_upstream_unauthorized(monkeypatch):
    from api import dashboard_probe, workflows
    from api.routes import handle_get

    monkeypatch.setattr(dashboard_probe, "get_dashboard_status", lambda: {"running": True, "url": "http://127.0.0.1:9119"})

    def missing_urlopen(request, timeout):
        raise HTTPError(request.full_url, 404, "not found", Message(), None)

    monkeypatch.setattr(workflows.urllib.request, "urlopen", missing_urlopen)
    missing_handler = _FakeHandler()
    assert handle_get(missing_handler, urlparse("http://example.com/api/workflows/missing/dag")) is True
    assert missing_handler.status == 404

    def unauthorized_urlopen(request, timeout):
        raise HTTPError(request.full_url, 401, "unauthorized", Message(), None)

    monkeypatch.setattr(workflows.urllib.request, "urlopen", unauthorized_urlopen)
    unauthorized_handler = _FakeHandler()
    assert handle_get(unauthorized_handler, urlparse("http://example.com/api/workflows")) is True
    assert unauthorized_handler.status == 503
    assert "not available" in unauthorized_handler.json_body()["error"]


def test_workflow_proxy_rejects_noncanonical_workflow_paths():
    from api.workflows import is_workflow_proxy_path

    assert is_workflow_proxy_path("/api/workflows")
    assert is_workflow_proxy_path("/api/workflows/inbox")
    assert is_workflow_proxy_path("/api/workflows/inbox/inbox_1")
    assert is_workflow_proxy_path("/api/workflows/wf_1/dag")
    assert is_workflow_proxy_path("/api/workflows/wf_1/nodes/node-1")
    assert is_workflow_proxy_path("/api/workflows/wf_1/events")
    assert is_workflow_proxy_path("/api/workflows/wf_1/artifacts")
    assert not is_workflow_proxy_path("/api/workflows/wf_1/delete")
    assert not is_workflow_proxy_path("/api/workflows/../../secrets")


def test_workflow_proxy_unavailable_payload_includes_actionable_capability_reason(monkeypatch):
    from api import dashboard_probe, workflows
    from api.routes import handle_get

    monkeypatch.setattr(dashboard_probe, "get_dashboard_status", lambda: {"running": False, "enabled": "auto"})
    handler = _FakeHandler()

    assert handle_get(handler, urlparse("http://example.com/api/workflows")) is True
    body = handler.json_body()

    assert handler.status == 503
    assert body["reason"] == "dashboard_unavailable"
    assert body["capability"] == "workflows"
    assert "Start or restart Hermes dashboard" in body["recovery"]
    assert body["backend"]["running"] is False

    def unauthorized_urlopen(request, timeout):
        if request.full_url == "http://127.0.0.1:9119/workflows":
            return _FakeResponse("<html></html>")
        raise HTTPError(request.full_url, 401, "unauthorized", Message(), None)

    monkeypatch.setattr(dashboard_probe, "get_dashboard_status", lambda: {"running": True, "url": "http://127.0.0.1:9119"})
    monkeypatch.setattr(workflows.urllib.request, "urlopen", unauthorized_urlopen)
    auth_handler = _FakeHandler()

    assert handle_get(auth_handler, urlparse("http://example.com/api/workflows")) is True
    auth_body = auth_handler.json_body()
    assert auth_handler.status == 503
    assert auth_body["reason"] == "dashboard_auth_failed"
    assert auth_body["backend"]["status"] == 401


def test_workflow_proxy_posts_inbox_items_to_core_dashboard(monkeypatch):
    from api import dashboard_probe, workflows
    from api.routes import handle_post

    monkeypatch.setattr(dashboard_probe, "get_dashboard_status", lambda: {"running": True, "url": "http://127.0.0.1:9119"})
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, request.get_method(), dict(request.header_items()), request.data))
        if request.full_url == "http://127.0.0.1:9119/workflows":
            return _FakeResponse('<script>window.__HERMES_SESSION_TOKEN__="token-123";</script>')
        return _FakeResponse({"facts": {"inboxItem": {"id": "inbox_1"}}, "insights": None})

    monkeypatch.setattr(workflows.urllib.request, "urlopen", fake_urlopen)
    raw = b'{"title":"Build inbox","source":"webui_chat"}'
    handler = _FakeHandler()
    handler.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json"}
    handler.rfile = __import__("io").BytesIO(raw)

    assert handle_post(handler, urlparse("http://example.com/api/workflows/inbox")) is True

    assert handler.status == 200
    assert handler.json_body()["facts"]["inboxItem"]["id"] == "inbox_1"
    assert calls[1][0] == "http://127.0.0.1:9119/api/workflows/inbox"
    assert calls[1][1] == "POST"
    assert calls[1][3] == raw


def test_workflow_proxy_patches_inbox_items_to_core_dashboard(monkeypatch):
    from api import dashboard_probe, workflows
    from api.routes import handle_patch

    monkeypatch.setattr(dashboard_probe, "get_dashboard_status", lambda: {"running": True, "url": "http://127.0.0.1:9119"})
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, request.get_method(), dict(request.header_items()), request.data))
        if request.full_url == "http://127.0.0.1:9119/workflows":
            return _FakeResponse('<script>window.__HERMES_SESSION_TOKEN__="token-123";</script>')
        return _FakeResponse({"facts": {"inboxItem": {"id": "inbox_1", "status": "triaged"}}, "insights": None})

    monkeypatch.setattr(workflows.urllib.request, "urlopen", fake_urlopen)
    raw = b'{"status":"triaged","classification":"decomposition_worthy"}'
    handler = _FakeHandler()
    handler.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json"}
    handler.rfile = __import__("io").BytesIO(raw)

    assert handle_patch(handler, urlparse("http://example.com/api/workflows/inbox/inbox_1")) is True

    assert handler.status == 200
    assert handler.json_body()["facts"]["inboxItem"]["status"] == "triaged"
    assert calls[1][0] == "http://127.0.0.1:9119/api/workflows/inbox/inbox_1"
    assert calls[1][1] == "PATCH"
    assert calls[1][2]["X-hermes-session-token"] == "token-123"
    assert calls[1][3] == raw
