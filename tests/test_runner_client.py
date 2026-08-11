import json
import urllib.request

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


class _FakeOpener:
    """Stand-in for the no-redirect opener: routes .open() to a fake urlopen."""

    def __init__(self, fake_urlopen):
        self._fake = fake_urlopen

    def open(self, req, timeout=0):
        return self._fake(req, timeout=timeout)


def _patch_opener(monkeypatch, fake_urlopen):
    monkeypatch.setattr(
        HttpRunnerClient, "_opener", lambda self: _FakeOpener(fake_urlopen)
    )


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
        return FakeResponse({
            "run_id": "run-1",
            "stream_id": "run-1",
            "status": "running",
            # #6327 receiver compare-and-accept: the runner echoes the claimed
            # SID + generation to acknowledge the owner fence.
            "owner_fence": {
                "session_id": "s1",
                "generation": "fingerprint-1",
                "accepted": True,
            },
        })

    _patch_opener(monkeypatch, fake_urlopen)
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
            # #6327: complete JSON-safe generation/route fence claimed under
            # the canonical owner's AGENT lock immediately before the call.
            owner_fence={
                "session_id": "s1",
                "profile": "default",
                "profile_home": "/home/test/.hermes",
                "generation": "fingerprint-1",
                "version": "claim-1",
                "route": {
                    "workspace": "/workspace",
                    "model": "gpt-5.5",
                    "provider": "openai-codex",
                    "normalized_model": False,
                },
            },
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
        "owner_fence": {
            "session_id": "s1",
            "profile": "default",
            "profile_home": "/home/test/.hermes",
            "generation": "fingerprint-1",
            "version": "claim-1",
            "route": {
                "workspace": "/workspace",
                "model": "gpt-5.5",
                "provider": "openai-codex",
                "normalized_model": False,
            },
        },
    }


def test_runner_client_refuses_run_without_fence_acceptance(monkeypatch):
    """#6327: a runner response without the owner-fence echo is NOT an
    accepted run — the run must never be treated as started until the
    receiver compare-and-accepts the claimed SID + generation."""

    def fake_urlopen(req, timeout=0):
        return FakeResponse({
            "run_id": "run-1",
            "stream_id": "run-1",
            "status": "running",
        })

    _patch_opener(monkeypatch, fake_urlopen)
    client = HttpRunnerClient(base_url="http://runner.local/", api_key="secret")

    with pytest.raises(RunnerClientError, match="compare-and-accept"):
        client.start_run(
            StartRunRequest(
                session_id="s1",
                message="hello",
                workspace="/workspace",
                profile="default",
                provider="openai-codex",
                model="gpt-5.5",
                owner_fence={
                    "session_id": "s1",
                    "profile": "default",
                    "profile_home": "/home/test/.hermes",
                    "generation": "fingerprint-1",
                    "version": "claim-1",
                    "route": {
                        "workspace": "/workspace",
                        "model": "gpt-5.5",
                        "provider": "openai-codex",
                        "normalized_model": False,
                    },
                },
            )
        )


def test_runner_client_refuses_run_with_partial_fence_schema(monkeypatch):
    """#6327: a non-empty dict is transport, not acceptance — an incomplete
    fence schema (missing generation / route lane / claim version) is refused
    before any POST."""

    def fake_urlopen(req, timeout=0):
        raise AssertionError("start_run must not POST an incomplete owner fence")

    _patch_opener(monkeypatch, fake_urlopen)
    client = HttpRunnerClient(base_url="http://runner.local/", api_key="secret")

    for bad_fence in (
        {"session_id": "s1"},
        {"session_id": "s1", "generation": "g1"},
        {
            "session_id": "s1",
            "profile": "default",
            "profile_home": "/home/test/.hermes",
            "generation": "g1",
            "version": "v1",
            "route": {"workspace": "/workspace"},
        },
    ):
        with pytest.raises(RunnerClientError, match="owner_fence"):
            client.start_run(
                StartRunRequest(
                    session_id="s1",
                    message="hello",
                    workspace="/workspace",
                    profile="default",
                    owner_fence=bad_fence,
                )
            )


def test_runner_client_start_run_refuses_empty_owner_fence(monkeypatch):
    """#6327 fail-closed acceptance: a run without a non-empty owner fence is
    never POSTed — an unowned run must never be acknowledged by the runner."""

    def fake_urlopen(req, timeout=0):
        raise AssertionError("start_run must not POST without a valid owner fence")

    _patch_opener(monkeypatch, fake_urlopen)
    client = HttpRunnerClient(base_url="http://runner.local/", api_key="secret")

    for bad_fence in (None, {}, {"session_id": ""}):
        try:
            client.start_run(
                StartRunRequest(
                    session_id="s1",
                    message="hello",
                    workspace="/workspace",
                    profile="default",
                    owner_fence=bad_fence,
                )
            )
        except RunnerClientError as exc:
            assert "owner_fence" in str(exc)
        else:
            raise AssertionError(f"start_run accepted empty fence {bad_fence!r}")


def test_runner_client_maps_observe_status_and_controls(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append((req.get_method(), req.full_url, json.loads(req.data.decode("utf-8")) if req.data else None))
        return FakeResponse({"ok": True, "status": "accepted"})

    _patch_opener(monkeypatch, fake_urlopen)
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
        ("POST", "http://runner.local/v1/runs/run%2F1/approval", {"choice": "once", "approval_id": "approval/1"}),
        ("POST", "http://runner.local/v1/runs/run%2F1/clarifications/clarify%2F1/respond", {"response": "answer"}),
        ("POST", "http://runner.local/v1/runs/run%2F1/messages", {"message": "next", "mode": "interrupt"}),
        ("POST", "http://runner.local/v1/sessions/session%2F1/goal", {"action": "set", "text": "finish"}),
    ]


def test_runner_client_rejects_non_object_json(monkeypatch):
    class ArrayResponse(FakeResponse):
        def read(self):
            return b"[]"

    _patch_opener(monkeypatch, lambda req, timeout=0: ArrayResponse({}))
    with pytest.raises(RunnerClientError, match="non-object"):
        HttpRunnerClient(base_url="http://runner.local").get_run("r1")


def test_runner_client_rejects_non_http_scheme():
    """Hardening: a misconfigured base_url with a non-http(s) scheme must be
    rejected at construction so it can never reach urlopen (e.g. file://)."""
    for bad in ("file:///etc/passwd", "ftp://runner.local/x", "/no/scheme"):
        with pytest.raises(ValueError, match="http"):
            HttpRunnerClient(base_url=bad)
    # http and https are accepted.
    assert HttpRunnerClient(base_url="http://runner.local").base_url == "http://runner.local"
    assert HttpRunnerClient(base_url="https://runner.local/").base_url == "https://runner.local"


def test_runner_client_opener_does_not_follow_redirects():
    """Hardening: the request opener must NOT follow 3xx redirects, so a
    misbehaving runner cannot smuggle the Bearer token to another host."""
    opener = HttpRunnerClient(base_url="http://runner.local")._opener()
    redirect_handlers = [
        h for h in opener.handlers
        if isinstance(h, urllib.request.HTTPRedirectHandler)
    ]
    assert redirect_handlers, "expected a redirect handler on the opener"
    # The overridden handler returns None from redirect_request → urllib raises
    # instead of following the redirect.
    assert all(
        h.redirect_request(None, None, 302, "Found", {}, "http://evil.example") is None
        for h in redirect_handlers
    )
