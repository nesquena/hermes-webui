import json
from urllib.parse import urlparse


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}
        self.client_address = ("127.0.0.1", 0)

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


def test_run_graph_endpoint_returns_read_only_projection(monkeypatch):
    import api.routes as routes

    captured = {}

    def fake_build_run_graph(session_id, run_id, *, after_seq=None):
        captured.update({"session_id": session_id, "run_id": run_id, "after_seq": after_seq})
        return {
            "version": 1,
            "session_id": session_id,
            "run_id": run_id,
            "status": "succeeded",
            "event_count": 2,
            "nodes": [{"id": f"run:{run_id}", "kind": "run", "label": "Run"}],
            "edges": [],
        }

    monkeypatch.setattr(routes, "build_run_graph", fake_build_run_graph, raising=False)

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/run/graph?session_id=session_1&run_id=run_1&after_seq=7")

    routes.handle_get(handler, parsed)

    assert handler.status == 200
    assert captured == {"session_id": "session_1", "run_id": "run_1", "after_seq": 7}
    assert handler.json_body()["nodes"][0]["kind"] == "run"


def test_run_graph_endpoint_requires_session_id_and_run_id(monkeypatch):
    import api.routes as routes

    def fail_if_called(*args, **kwargs):
        raise AssertionError("build_run_graph must not run without required identifiers")

    monkeypatch.setattr(routes, "build_run_graph", fail_if_called, raising=False)

    missing_session = _FakeHandler()
    routes.handle_get(missing_session, urlparse("http://example.com/api/run/graph?run_id=run_1"))
    assert missing_session.status == 400
    assert "session_id required" in missing_session.json_body()["error"]

    missing_run = _FakeHandler()
    routes.handle_get(missing_run, urlparse("http://example.com/api/run/graph?session_id=session_1"))
    assert missing_run.status == 400
    assert "run_id required" in missing_run.json_body()["error"]


def test_run_latest_endpoint_returns_latest_journal_for_session(monkeypatch):
    import api.routes as routes

    captured = {}

    def fake_latest_run_summary_for_session(session_id):
        captured["session_id"] = session_id
        return {
            "session_id": session_id,
            "run_id": "run_new",
            "last_seq": 4,
            "last_event_id": "run_new:4",
            "last_event": "done",
            "terminal": True,
            "terminal_state": "completed",
        }

    monkeypatch.setattr(routes, "latest_run_summary_for_session", fake_latest_run_summary_for_session, raising=False)

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("http://example.com/api/run/latest?session_id=session_1"))

    body = handler.json_body()
    assert handler.status == 200
    assert captured == {"session_id": "session_1"}
    assert body["found"] is True
    assert body["run_id"] == "run_new"
    assert body["journal"]["terminal_state"] == "completed"


def test_run_latest_endpoint_requires_session_id_and_reports_empty(monkeypatch):
    import api.routes as routes

    def fake_latest_run_summary_for_session(session_id):
        assert session_id == "session_1"
        return None

    monkeypatch.setattr(routes, "latest_run_summary_for_session", fake_latest_run_summary_for_session, raising=False)

    missing_session = _FakeHandler()
    routes.handle_get(missing_session, urlparse("http://example.com/api/run/latest"))
    assert missing_session.status == 400
    assert "session_id required" in missing_session.json_body()["error"]

    empty = _FakeHandler()
    routes.handle_get(empty, urlparse("http://example.com/api/run/latest?session_id=session_1"))
    body = empty.json_body()
    assert empty.status == 200
    assert body == {"found": False, "session_id": "session_1", "run_id": None, "journal": None}
