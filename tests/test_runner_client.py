import json

import pytest

from api.runner_client import HttpRunnerClient, RunnerClientError, runner_client_configured
from api.runtime_adapter import StartRunRequest


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_runner_client_is_default_off_without_endpoint():
    assert runner_client_configured({}) is False
    with pytest.raises(NotImplementedError, match="runner-local chat backend is not configured"):
        HttpRunnerClient.from_env({})


def test_runner_client_start_run_posts_explicit_boundary_payload(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse({"run_id": "run-1", "stream_id": "run-1", "status": "running"})

    monkeypatch.setattr("api.runner_client.urllib.request.urlopen", fake_urlopen)
    client = HttpRunnerClient(base_url="http://runner.local/", api_key="secret")

    result = client.start_run(
        StartRunRequest(
            session_id="s1",
            message="hello",
            attachments=[{"path": "/tmp/a.png", "mime": "image/png"}],
            workspace="/workspace",
            profile="default",
            provider="openai-codex",
            model="gpt-5.5",
            toolsets=["terminal"],
            source="webui",
            metadata={"route": "/api/chat/start"},
        )
    )

    assert result["run_id"] == "run-1"
    assert captured["url"] == "http://runner.local/v1/runs"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["body"] == {
        "session_id": "s1",
        "message": "hello",
        "attachments": [{"path": "/tmp/a.png", "mime": "image/png"}],
        "workspace": "/workspace",
        "profile": "default",
        "provider": "openai-codex",
        "model": "gpt-5.5",
        "toolsets": ["terminal"],
        "source": "webui",
        "metadata": {"route": "/api/chat/start"},
    }


def test_runner_client_maps_observe_status_and_controls(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append((req.get_method(), req.full_url, json.loads(req.data.decode("utf-8")) if req.data else None))
        return FakeResponse({"ok": True, "status": "accepted"})

    monkeypatch.setattr("api.runner_client.urllib.request.urlopen", fake_urlopen)
    client = HttpRunnerClient(base_url="http://runner.local")

    client.observe_run("run/1", cursor="event:2")
    client.get_run("run/1")
    client.cancel_run("run/1")
    client.respond_approval("run/1", "approval/1", "once")
    client.respond_clarify("run/1", "clarify/1", "answer")
    client.queue_message("run/1", "next", mode="interrupt")
    client.update_goal("session/1", "set", "finish")

    assert calls == [
        ("GET", "http://runner.local/v1/runs/run%2F1/events?cursor=event%3A2", None),
        ("GET", "http://runner.local/v1/runs/run%2F1", None),
        ("POST", "http://runner.local/v1/runs/run%2F1/cancel", {}),
        ("POST", "http://runner.local/v1/runs/run%2F1/approvals/approval%2F1/respond", {"choice": "once"}),
        ("POST", "http://runner.local/v1/runs/run%2F1/clarifications/clarify%2F1/respond", {"response": "answer"}),
        ("POST", "http://runner.local/v1/runs/run%2F1/messages", {"message": "next", "mode": "interrupt"}),
        ("POST", "http://runner.local/v1/sessions/session%2F1/goal", {"action": "set", "text": "finish"}),
    ]


def test_runner_client_rejects_non_object_json(monkeypatch):
    class ArrayResponse(FakeResponse):
        def read(self):
            return b"[]"

    monkeypatch.setattr("api.runner_client.urllib.request.urlopen", lambda req, timeout=0: ArrayResponse({}))
    with pytest.raises(RunnerClientError, match="non-object"):
        HttpRunnerClient(base_url="http://runner.local").get_run("r1")
