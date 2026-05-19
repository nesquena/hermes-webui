"""Tests for metadata-only Capy structured progress-event status."""
import io
import json
from urllib.parse import urlparse

from api.capy_progress import progress_status, record_progress_event


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


class _PostRouteHandler(_RouteHandler):
    def __init__(self, payload):
        super().__init__()
        raw = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)


def test_progress_status_returns_bounded_taxonomy_without_echoing_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPY_PROGRESS_LABEL", "renderer <script>bad()</script> SECRET_VALUE_DO_NOT_LEAK")
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "events.jsonl"))

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


def test_capy_progress_status_route_returns_metadata_only_status(monkeypatch, tmp_path):
    import api.routes as routes

    monkeypatch.setenv("CAPY_PROGRESS_LABEL", "api_key SECRET_VALUE_DO_NOT_LEAK")
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "events.jsonl"))
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


def test_record_progress_event_updates_status_counts_without_persisting_payload(monkeypatch, tmp_path):
    log_path = tmp_path / "progress-events.jsonl"
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(log_path))

    started = record_progress_event({"event_type": "run.started", "run_id": "sprint-1"})
    record_progress_event(
        {
            "event_type": "tool.completed",
            "run_id": "sprint-1",
            "payload": {
                "renderer": "<script>bad()</script>",
                "api_key": "SECRET_VALUE_DO_NOT_LEAK",
                "prompt": "raw prompt ignore previous instructions",
            },
        }
    )

    status = progress_status()
    serialized = json.dumps(status, sort_keys=True).lower()
    stored = log_path.read_text(encoding="utf-8").lower()

    assert started["stored"] is True
    assert started["queued"] is True
    assert started["event_type"] == "run.started"
    assert started["family"] == "run"
    assert status["active_run_count"] == 1
    assert status["recent_event_count"] == 2
    assert status["recent_event_types"] == ["run.started", "tool.completed"]
    assert status["recent_family_counts"] == {"run": 1, "tool": 1}
    assert status["redaction_status"] == "metadata_only"
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "raw prompt" not in serialized
    assert "renderer" not in stored
    assert "secret_value_do_not_leak" not in stored
    assert "raw prompt" not in stored

    record_progress_event({"event_type": "run.completed", "run_id": "sprint-1"})
    assert progress_status()["active_run_count"] == 0


def test_progress_status_resanitizes_persisted_metadata(monkeypatch, tmp_path):
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(log_path))
    log_path.write_text(
        json.dumps(
            {
                "event_id": "bad/../event",
                "event_type": "tool.failed",
                "run_id": "SECRET_VALUE_DO_NOT_LEAK",
                "created_at": "<script>bad()</script> SECRET_VALUE_DO_NOT_LEAK",
                "payload": {"renderer": "raw prompt"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    status = progress_status()
    serialized = json.dumps(status, sort_keys=True).lower()

    assert status["recent_event_count"] == 1
    assert status["recent_event_types"] == ["tool.failed"]
    assert status["recent_family_counts"] == {"tool": 1}
    assert status["last_event_at"] == ""
    assert "<script" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "renderer" not in serialized
    assert "raw prompt" not in serialized


def test_progress_status_bounds_recent_family_counts_and_omits_empty_families(monkeypatch, tmp_path):
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(log_path))
    rows = []
    rows.extend({"event_type": "tool.completed", "created_at": "2026-05-18T00:00:00Z", "run_id": "sprint-1"} for _ in range(5))
    rows.extend({"event_type": "memory.ingest.completed", "created_at": "2026-05-18T00:00:01Z", "run_id": "sprint-1"} for _ in range(2))
    rows.append({"event_type": "space.visual_qa.completed", "created_at": "2026-05-18T00:00:02Z", "run_id": "qa-1"})
    rows.append({"event_type": "renderer.source", "created_at": "2026-05-18T00:00:03Z", "run_id": "SECRET_VALUE_DO_NOT_LEAK"})
    log_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    status = progress_status()
    serialized = json.dumps(status, sort_keys=True).lower()

    assert status["recent_family_counts"] == {"tool": 5, "memory.ingest": 2, "space.visual_qa": 1}
    assert "run" not in status["recent_family_counts"]
    assert "subagent" not in status["recent_family_counts"]
    assert "renderer" not in serialized
    assert "secret_value_do_not_leak" not in serialized


def test_progress_status_returns_bounded_recent_event_stream_metadata_only(monkeypatch, tmp_path):
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(log_path))
    rows = [
        {"event_id": "evt-001", "event_type": "run.started", "run_id": "sprint-1", "created_at": "2026-05-18T00:00:00Z"},
        {"event_id": "evt-002", "event_type": "tool.started", "run_id": "sprint-1", "created_at": "2026-05-18T00:00:01Z"},
        {"event_id": "evt-003", "event_type": "tool.completed", "run_id": "sprint-1", "created_at": "2026-05-18T00:00:02Z"},
        {"event_id": "evt-004", "event_type": "subagent.started", "run_id": "sprint-1", "created_at": "2026-05-18T00:00:03Z"},
        {"event_id": "evt-005", "event_type": "subagent.completed", "run_id": "sprint-1", "created_at": "2026-05-18T00:00:04Z"},
        {"event_id": "evt-006", "event_type": "memory.ingest.completed", "run_id": "sprint-1", "created_at": "2026-05-18T00:00:05Z"},
        {"event_id": "evt-007", "event_type": "space.visual_qa.completed", "run_id": "sprint-1", "created_at": "2026-05-18T00:00:06Z"},
        {"event_id": "renderer/../event", "event_type": "renderer.source", "run_id": "SECRET_VALUE_DO_NOT_LEAK", "created_at": "2026-05-18T00:00:07Z"},
    ]
    log_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    status = progress_status()
    serialized = json.dumps(status, sort_keys=True).lower()

    assert status["recent_events"] == [
        {
            "event_id": "evt-007",
            "event_type": "space.visual_qa.completed",
            "family": "space.visual_qa",
            "run_id": "sprint-1",
            "created_at": "2026-05-18T00:00:06Z",
        },
        {
            "event_id": "evt-006",
            "event_type": "memory.ingest.completed",
            "family": "memory.ingest",
            "run_id": "sprint-1",
            "created_at": "2026-05-18T00:00:05Z",
        },
        {
            "event_id": "evt-005",
            "event_type": "subagent.completed",
            "family": "subagent",
            "run_id": "sprint-1",
            "created_at": "2026-05-18T00:00:04Z",
        },
        {
            "event_id": "evt-004",
            "event_type": "subagent.started",
            "family": "subagent",
            "run_id": "sprint-1",
            "created_at": "2026-05-18T00:00:03Z",
        },
        {
            "event_id": "evt-003",
            "event_type": "tool.completed",
            "family": "tool",
            "run_id": "sprint-1",
            "created_at": "2026-05-18T00:00:02Z",
        },
        {
            "event_id": "evt-002",
            "event_type": "tool.started",
            "family": "tool",
            "run_id": "sprint-1",
            "created_at": "2026-05-18T00:00:01Z",
        },
    ]
    assert "renderer" not in serialized
    assert "secret_value_do_not_leak" not in serialized


def test_progress_status_scopes_before_bounding_recent_events(monkeypatch, tmp_path):
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(log_path))
    rows = [
        {
            "event_id": "evt-lab-old",
            "event_type": "tool.completed",
            "run_id": "research:lab",
            "space_id": "lab",
            "created_at": "2026-05-18T00:00:00Z",
        }
    ]
    rows.extend(
        {
            "event_id": f"evt-other-{idx}",
            "event_type": "tool.completed",
            "run_id": f"research:other-{idx}",
            "space_id": f"other-{idx}",
            "created_at": "2026-05-18T00:00:01Z",
        }
        for idx in range(201)
    )
    log_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    scoped = progress_status(space_id="lab")
    serialized = json.dumps(scoped, sort_keys=True).lower()

    assert scoped["space_id"] == "lab"
    assert scoped["recent_event_count"] == 1
    assert scoped["recent_events"] == [
        {
            "event_id": "evt-lab-old",
            "event_type": "tool.completed",
            "family": "tool",
            "run_id": "research:lab",
            "created_at": "2026-05-18T00:00:00Z",
            "space_id": "lab",
        }
    ]
    assert "other-200" not in serialized


def test_progress_event_rejects_unsafe_space_id_without_persisting_or_echoing(monkeypatch, tmp_path):
    import pytest

    log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(log_path))

    with pytest.raises(ValueError, match="Invalid progress space id"):
        record_progress_event(
            {
                "event_type": "tool.completed",
                "run_id": "safe-run",
                "space_id": "SECRET_VALUE_DO_NOT_LEAK",
                "payload": {"renderer": "<script>bad()</script>"},
            }
        )

    assert not log_path.exists()


def test_progress_status_rejects_blank_explicit_scope_without_returning_global_events(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "events.jsonl"))
    record_progress_event({"event_type": "tool.completed", "run_id": "research:lab", "space_id": "lab"})

    with pytest.raises(ValueError, match="Invalid progress space id"):
        progress_status(space_id=" ")


def test_capy_progress_status_route_rejects_invalid_scope_without_returning_global_events(monkeypatch, tmp_path):
    import api.routes as routes

    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "events.jsonl"))
    record_progress_event({"event_type": "tool.completed", "run_id": "research:lab", "space_id": "lab"})

    for url in (
        "/api/capy-progress/status?space_id=SECRET_VALUE_DO_NOT_LEAK",
        "/api/capy-progress/status?space_id=",
        "/api/capy-progress/status?space_id=%20",
    ):
        handler = _RouteHandler()
        handled = routes.handle_get(handler, urlparse(url))

        assert handled is True
        assert handler.status == 400
        data = json.loads(handler.body.getvalue().decode("utf-8"))
        serialized = json.dumps(data, sort_keys=True).lower()
        assert data["error"] == "Invalid progress space id"
        assert "tool.completed" not in serialized
        assert "research:lab" not in serialized
        assert "secret_value_do_not_leak" not in serialized


def test_capy_progress_event_route_records_camelcase_event_metadata_only(monkeypatch, tmp_path):
    import api.routes as routes

    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "events.jsonl"))
    handler = _PostRouteHandler(
        {
            "eventType": "space.visual_qa.completed",
            "runId": "qa-run-1",
            "payload": {"source": "renderer <script>bad()</script> SECRET_VALUE_DO_NOT_LEAK"},
        }
    )

    handled = routes.handle_post(handler, urlparse("/api/capy-progress/event"))

    assert handled is True
    assert handler.status == 200
    data = json.loads(handler.body.getvalue().decode("utf-8"))
    serialized = json.dumps(data, sort_keys=True).lower()
    assert data["stored"] is True
    assert data["event_type"] == "space.visual_qa.completed"
    assert data["family"] == "space.visual_qa"
    assert data["redaction_status"] == "metadata_only"
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "secret_value_do_not_leak" not in serialized


def test_capy_progress_event_route_rejects_unknown_types_without_echoing_hostile_value(monkeypatch, tmp_path):
    import api.routes as routes

    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "events.jsonl"))
    handler = _PostRouteHandler({"event_type": "renderer.source", "payload": {"api_key": "SECRET_VALUE_DO_NOT_LEAK"}})

    handled = routes.handle_post(handler, urlparse("/api/capy-progress/event"))

    assert handled is True
    assert handler.status == 400
    data = json.loads(handler.body.getvalue().decode("utf-8"))
    serialized = json.dumps(data, sort_keys=True).lower()
    assert data["error"] == "Unsupported progress event type"
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "secret_value_do_not_leak" not in serialized
