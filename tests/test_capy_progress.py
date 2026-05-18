"""Tests for metadata-only Capy structured progress-event status."""
import io
import json
from urllib.parse import urlparse

from api.capy_progress import progress_status


class _RouteHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.sent_headers = []
        self.body = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    @property
    def wfile(self):
        return self.body


def test_progress_status_returns_bounded_taxonomy_without_echoing_env(monkeypatch):
    monkeypatch.setenv("CAPY_PROGRESS_LABEL", "renderer <script>bad()</script> SECRET_VALUE_DO_NOT_LEAK")

    status = progress_status()
    serialized = json.dumps(status, sort_keys=True)
    lowered = serialized.lower()

    assert status["available"] is True
    assert status["local_only"] is True
    assert status["status"] == "ready"
    assert status["recent_event_count"] == 0
    assert status["active_run_count"] == 0
    assert status["event_families"] == [
        "run",
        "tool",
        "subagent",
        "taskboard",
        "memory.ingest",
        "space.visual_qa",
    ]
    assert "run.started" in status["supported_event_types"]
    assert "tool.started" in status["supported_event_types"]
    assert "subagent.completed" in status["supported_event_types"]
    assert "space.visual_qa.completed" in status["supported_event_types"]
    assert status["redaction_status"] == "metadata_only"
    assert "renderer" not in lowered
    assert "<script" not in lowered
    assert "secret_value_do_not_leak" not in lowered
    assert "raw prompt" not in lowered


def test_capy_progress_status_route_returns_metadata_only_status(monkeypatch):
    import api.routes as routes

    monkeypatch.setenv("CAPY_PROGRESS_LABEL", "api_key SECRET_VALUE_DO_NOT_LEAK")
    handler = _RouteHandler()

    handled = routes.handle_get(handler, urlparse("/api/capy-progress/status"))

    assert handled is True
    assert handler.status == 200
    data = json.loads(handler.body.getvalue().decode("utf-8"))
    serialized = json.dumps(data, sort_keys=True).lower()
    assert data["available"] is True
    assert data["status"] == "ready"
    assert "tool.completed" in data["supported_event_types"]
    assert data["redaction_status"] == "metadata_only"
    assert "api_key" not in serialized
    assert "secret_value_do_not_leak" not in serialized
