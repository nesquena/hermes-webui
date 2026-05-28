import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class RecordingRunnerHandler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, format, *args):  # pragma: no cover - keep test output quiet
        return

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self._read_json()
        RecordingRunnerHandler.calls.append(("POST", self.path, body))
        if self.path == "/v1/runs":
            self._send_json({
                "run_id": "run-http-1",
                "session_id": body["session_id"],
                "stream_id": "run-http-1",
                "status": "running",
                "active_controls": ["cancel"],
                "received_workspace": body.get("workspace"),
                "received_profile": body.get("profile"),
            })
        elif self.path == "/v1/runs/run-http-1/cancel":
            self._send_json({"ok": True, "status": "accepted", "event_id": "run-http-1:3"})
        elif self.path == "/v1/runs/run-http-1/approval/approval-1":
            self._send_json({"ok": True, "status": body.get("choice")})
        elif self.path == "/v1/runs/run-http-1/clarify/clarify-1":
            self._send_json({"ok": True, "status": "answered"})
        elif self.path == "/v1/runs/run-http-1/queue":
            self._send_json({"ok": False, "status": "unsupported", "message": "Queue unavailable."})
        elif self.path == "/v1/sessions/session-1/goal":
            self._send_json({"ok": False, "status": "unsupported", "message": "Goal unavailable."})
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_GET(self):
        RecordingRunnerHandler.calls.append(("GET", self.path, {}))
        parsed = urlparse(self.path)
        if parsed.path == "/v1/runs/run-http-1/events":
            cursor = (parse_qs(parsed.query).get("cursor") or [None])[0]
            self._send_json({
                "run_id": "run-http-1",
                "cursor": "2",
                "last_event_id": "run-http-1:2",
                "events": [
                    {"event_id": "run-http-1:2", "seq": 2, "type": "done", "payload": {"ok": True}},
                ],
                "seen_cursor": cursor,
            })
        elif parsed.path == "/v1/runs/run-http-1":
            self._send_json({
                "run_id": "run-http-1",
                "session_id": "session-1",
                "status": "completed",
                "terminal_state": "completed",
                "last_event_id": "run-http-1:2",
                "active_controls": [],
            })
        else:
            self._send_json({"error": "not found"}, status=404)


def run_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingRunnerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_runner_client_factory_is_default_off_and_uses_explicit_base_url(monkeypatch):
    runner_client = importlib.import_module("api.runner_client")

    assert runner_client.runner_base_url({}) is None
    assert runner_client.runner_client_configured({}) is False

    try:
        runner_client.build_runner_client_from_env({})
    except NotImplementedError as exc:
        assert "runner-local chat backend is not configured" in str(exc)
    else:
        raise AssertionError("runner client factory must stay default-off")

    client = runner_client.build_runner_client_from_env({"HERMES_WEBUI_RUNNER_BASE_URL": " http://runner.local/ "})
    assert isinstance(client, runner_client.HttpRunnerClient)
    assert client.base_url == "http://runner.local"


def test_http_runner_client_matches_runtime_adapter_contract():
    runtime = importlib.import_module("api.runtime_adapter")
    runner_client = importlib.import_module("api.runner_client")
    server, base_url = run_server()
    RecordingRunnerHandler.calls.clear()
    try:
        client = runner_client.HttpRunnerClient(base_url=base_url, timeout=2)
        request = runtime.StartRunRequest(
            session_id="session-1",
            message="hello runner",
            attachments=[{"name": "photo.png", "path": "/uploads/photo.png", "mime": "image/png"}],
            workspace="/workspace/project",
            profile="research",
            provider="openai-codex",
            model="gpt-5.5",
            toolsets=["terminal", "file"],
            source="webui",
            metadata={"route": "/api/chat/start"},
        )

        started = client.start_run(request)
        replay = client.observe_run("run-http-1", cursor="1")
        status = client.get_run("run-http-1")
        cancel = client.cancel_run("run-http-1")
        approval = client.respond_approval("run-http-1", "approval-1", "once")
        clarify = client.respond_clarify("run-http-1", "clarify-1", "answer")
        queued = client.queue_message("run-http-1", "next", mode="queue")
        goal = client.update_goal("session-1", "status", "")
    finally:
        server.shutdown()
        server.server_close()

    assert started["run_id"] == "run-http-1"
    assert started["session_id"] == "session-1"
    assert started["received_workspace"] == "/workspace/project"
    assert started["received_profile"] == "research"
    assert replay["last_event_id"] == "run-http-1:2"
    assert replay["seen_cursor"] == "1"
    assert status["terminal_state"] == "completed"
    assert cancel["event_id"] == "run-http-1:3"
    assert approval["status"] == "once"
    assert clarify["status"] == "answered"
    assert queued["status"] == "unsupported"
    assert goal["status"] == "unsupported"

    start_call = RecordingRunnerHandler.calls[0]
    assert start_call[0:2] == ("POST", "/v1/runs")
    assert start_call[2]["session_id"] == "session-1"
    assert start_call[2]["workspace"] == "/workspace/project"
    assert start_call[2]["profile"] == "research"
    assert start_call[2]["provider"] == "openai-codex"
    assert start_call[2]["model"] == "gpt-5.5"
    assert start_call[2]["toolsets"] == ["terminal", "file"]
    assert start_call[2]["source"] == "webui"
    assert start_call[2]["metadata"] == {"route": "/api/chat/start"}


def test_runtime_runner_factory_uses_http_client_only_when_base_url_configured(monkeypatch):
    routes = importlib.import_module("api.routes")
    runner_client = importlib.import_module("api.runner_client")

    monkeypatch.delenv("HERMES_WEBUI_RUNNER_BASE_URL", raising=False)
    try:
        routes._runtime_runner_client_factory()
    except NotImplementedError as exc:
        assert "runner-local chat backend is not configured" in str(exc)
    else:
        raise AssertionError("runner-local must still return bounded 501 when no runner URL is configured")

    monkeypatch.setenv("HERMES_WEBUI_RUNNER_BASE_URL", "http://127.0.0.1:65535")
    client = routes._runtime_runner_client_factory()
    assert isinstance(client, runner_client.HttpRunnerClient)


def test_in_memory_runner_backend_provides_minimal_supervised_contract():
    runner_backend = importlib.import_module("api.runner_backend")
    runtime = importlib.import_module("api.runtime_adapter")

    backend = runner_backend.InMemoryRunnerBackend()
    request = runtime.StartRunRequest(
        session_id="session-1",
        message="hello",
        workspace="/workspace/project",
        profile="default",
    )

    started = backend.start_run(request)
    status = backend.get_run(started["run_id"])
    replay = backend.observe_run(started["run_id"], cursor="0")
    cancel = backend.cancel_run(started["run_id"])
    second_status = backend.get_run(started["run_id"])

    assert started["session_id"] == "session-1"
    assert started["stream_id"] == started["run_id"]
    assert started["active_controls"] == ["cancel"]
    assert status["status"] == "running"
    assert [event["type"] for event in replay["events"]] == ["run.started"]
    assert cancel["status"] == "cancelled"
    assert second_status["terminal_state"] == "cancelled"


def test_runner_backend_http_handler_matches_runner_client_endpoints():
    runner_backend = importlib.import_module("api.runner_backend")
    runner_client = importlib.import_module("api.runner_client")
    runtime = importlib.import_module("api.runtime_adapter")

    server = runner_backend.make_runner_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        client = runner_client.HttpRunnerClient(base_url=base_url, timeout=2)
        started = client.start_run(runtime.StartRunRequest(session_id="session-1", message="hello"))
        status = client.get_run(started["run_id"])
        latest = client.latest_run_for_session("session-1")
        replay = client.observe_run(started["run_id"], cursor="0")
        cancel = client.cancel_run(started["run_id"])
        second_replay = client.observe_run(started["run_id"], cursor="1")
    finally:
        server.shutdown()
        server.server_close()

    assert started["run_id"].startswith("runner-local-")
    assert status["status"] == "running"
    assert latest["run_id"] == started["run_id"]
    assert [event["type"] for event in replay["events"]] == ["run.started"]
    assert cancel["status"] == "cancelled"
    assert [event["type"] for event in second_replay["events"]] == ["cancelled"]
