import json
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
        return _FakeResponse({"facts": {"workflows": [{"id": "wf_1"}]}, "insights": None})

    monkeypatch.setattr(workflows.urllib.request, "urlopen", fake_urlopen)
    handler = _FakeHandler()

    handled = handle_get(handler, urlparse("http://example.com/api/workflows?limit=10"))

    assert handled is True
    assert handler.status == 200
    assert handler.json_body()["facts"]["workflows"][0]["id"] == "wf_1"
    assert calls == [("http://127.0.0.1:9119/api/workflows?limit=10", 2.0, {"Accept": "application/json", "User-agent": "hermes-webui-workflow-proxy"})]


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
        raise HTTPError(request.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(workflows.urllib.request, "urlopen", missing_urlopen)
    missing_handler = _FakeHandler()
    assert handle_get(missing_handler, urlparse("http://example.com/api/workflows/missing/dag")) is True
    assert missing_handler.status == 404

    def unauthorized_urlopen(request, timeout):
        raise HTTPError(request.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr(workflows.urllib.request, "urlopen", unauthorized_urlopen)
    unauthorized_handler = _FakeHandler()
    assert handle_get(unauthorized_handler, urlparse("http://example.com/api/workflows")) is True
    assert unauthorized_handler.status == 503
    assert "not available" in unauthorized_handler.json_body()["error"]


def test_workflow_proxy_rejects_noncanonical_workflow_paths():
    from api.workflows import is_workflow_proxy_path

    assert is_workflow_proxy_path("/api/workflows")
    assert is_workflow_proxy_path("/api/workflows/wf_1/dag")
    assert is_workflow_proxy_path("/api/workflows/wf_1/nodes/node-1")
    assert is_workflow_proxy_path("/api/workflows/wf_1/events")
    assert is_workflow_proxy_path("/api/workflows/wf_1/artifacts")
    assert not is_workflow_proxy_path("/api/workflows/wf_1/delete")
    assert not is_workflow_proxy_path("/api/workflows/../../secrets")
