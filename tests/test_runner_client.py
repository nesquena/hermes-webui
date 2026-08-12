import json
import urllib.request

import pytest

from api.runner_client import (
    HttpRunnerClient,
    RunnerClientError,
    RunnerFenceRefused,
    runner_client_configured,
)
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
            # #6327 receiver compare-and-accept: the runner echoes the
            # COMPLETE claimed fence (accepted:true + SID + profile/home +
            # generation + per-run claim version + route lane + lease), not a
            # weak SID+generation reflection.
            "owner_fence": {
                "session_id": "s1",
                "profile": "default",
                "profile_home": "/home/test/.hermes",
                "generation": "fingerprint-1",
                "version": "claim-1",
                "lease": "lease-1",
                "route": {
                    "workspace": "/workspace",
                    "model": "gpt-5.5",
                    "provider": "openai-codex",
                    "normalized_model": False,
                },
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
                "lease": "lease-1",
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
            "lease": "lease-1",
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
    receiver compare-and-accepts the complete claimed fence (accepted:true +
    SID + profile/home + generation + version + route + lease).  The refusal
    is RunnerFenceRefused (retryable, never ambiguous) so the route requeues
    instead of treating the run as started."""

    def fake_urlopen(req, timeout=0):
        return FakeResponse({
            "run_id": "run-1",
            "stream_id": "run-1",
            "status": "running",
        })

    _patch_opener(monkeypatch, fake_urlopen)
    client = HttpRunnerClient(base_url="http://runner.local/", api_key="secret")

    with pytest.raises(RunnerFenceRefused, match="owner_fence"):
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


def test_runner_client_rejects_weak_sid_generation_echo(monkeypatch):
    """#6327: the historical success oracle — a reflected owner_fence that
    only matches session_id + generation — is TRANSPORT, not acceptance, and
    must now be refused (the canonical validator requires the complete fence:
    accepted:true, nonce/version, profile/home, route, lease)."""

    claimed = {
        "session_id": "s1",
        "profile": "default",
        "profile_home": "/home/test/.hermes",
        "generation": "fingerprint-1",
        "version": "claim-1",
        "lease": "lease-1",
        "route": {
            "workspace": "/workspace",
            "model": "gpt-5.5",
            "provider": "openai-codex",
            "normalized_model": False,
        },
    }

    def fake_urlopen(req, timeout=0):
        return FakeResponse({
            "run_id": "run-1",
            "status": "running",
            # Weak reflection: only SID + generation + accepted:true.
            "owner_fence": {
                "session_id": "s1",
                "generation": "fingerprint-1",
                "accepted": True,
            },
        })

    _patch_opener(monkeypatch, fake_urlopen)
    client = HttpRunnerClient(base_url="http://runner.local/", api_key="secret")
    with pytest.raises(RunnerFenceRefused, match="owner_fence"):
        client.start_run(
            StartRunRequest(
                session_id="s1",
                message="hello",
                workspace="/workspace",
                profile="default",
                provider="openai-codex",
                model="gpt-5.5",
                owner_fence=claimed,
            )
        )


def _complete_runner_echo(**overrides):
    echo = {
        "accepted": True,
        "session_id": "s1",
        "profile": "default",
        "profile_home": "/home/test/.hermes",
        "generation": "fingerprint-1",
        "version": "claim-1",
        "lease": "lease-1",
        "route": {
            "workspace": "/workspace",
            "model": "gpt-5.5",
            "provider": "openai-codex",
            "normalized_model": False,
        },
    }
    for key, value in overrides.items():
        if key == "route" and isinstance(value, dict):
            echo["route"] = dict(echo["route"])
            echo["route"].update(value)
        else:
            echo[key] = value
    return echo


def _claimed_fence():
    return {
        "session_id": "s1",
        "profile": "default",
        "profile_home": "/home/test/.hermes",
        "generation": "fingerprint-1",
        "version": "claim-1",
        "lease": "lease-1",
        "route": {
            "workspace": "/workspace",
            "model": "gpt-5.5",
            "provider": "openai-codex",
            "normalized_model": False,
        },
    }


@pytest.mark.parametrize(
    "mutation,expected",
    [
        # accepted:true is required.
        ({"accepted": False}, "accepted:true"),
        ({"accepted": None}, "accepted:true"),
        # Every identity field must match exactly.
        ({"session_id": "other"}, "owner_fence.session_id"),
        ({"profile": "other"}, "owner_fence.profile"),
        ({"profile_home": "/other/home"}, "owner_fence.profile_home"),
        ({"generation": "other-fingerprint"}, "owner_fence.generation"),
        ({"version": "other-claim"}, "owner_fence.version"),
        ({"lease": "other-lease"}, "owner_fence.lease"),
        # The full route lane must match.
        ({"route": {"workspace": "/other"}}, "route.workspace"),
        ({"route": {"model": "other-model"}}, "route.model"),
        ({"route": {"provider": "other-provider"}}, "route.provider"),
        ({"route": {"normalized_model": True}}, "route.normalized_model"),
        # A MISSING normalized_model echo must never equal a claimed false
        # value (bool() coercion would treat absent == false).
        ({"route": {"normalized_model": None}}, "route.normalized_model"),
    ],
)
def test_runner_client_rejects_fence_mismatch_per_field(monkeypatch, mutation, expected):
    """#6327: one rejection test per fence field — a runner echo that
    mismatches ANY field of the claimed generation/route fence (or misses
    accepted:true) must raise RunnerFenceRefused, never a generic
    RunnerClientError, so the retryable 409/requeue path stays reachable."""

    def fake_urlopen(req, timeout=0):
        return FakeResponse({
            "run_id": "run-1",
            "status": "running",
            "owner_fence": _complete_runner_echo(**mutation),
        })

    _patch_opener(monkeypatch, fake_urlopen)
    client = HttpRunnerClient(base_url="http://runner.local/", api_key="secret")
    with pytest.raises(RunnerFenceRefused, match=expected):
        client.start_run(
            StartRunRequest(
                session_id="s1",
                message="hello",
                workspace="/workspace",
                profile="default",
                provider="openai-codex",
                model="gpt-5.5",
                owner_fence=_claimed_fence(),
            )
        )


def test_runner_client_refuses_run_with_partial_fence_schema(monkeypatch):
    """#6327: a non-empty dict is transport, not acceptance — an incomplete
    fence schema (missing lease, missing generation / route lane / claim
    version, or a missing/malformed ``route.normalized_model``) is refused
    BEFORE any POST, and the refusal is the typed RunnerFenceRefused
    (retryable, never ambiguous) so the route requeues instead of reaching
    the generic 502 path."""

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
        # Missing the REQUIRED per-session lease.
        {
            "session_id": "s1",
            "profile": "default",
            "profile_home": "/home/test/.hermes",
            "generation": "g1",
            "version": "v1",
            "route": {
                "workspace": "/workspace",
                "model": "gpt-5.5",
                "provider": "openai-codex",
                "normalized_model": False,
            },
        },
        # Missing the required route.normalized_model flag.
        {
            "session_id": "s1",
            "profile": "default",
            "profile_home": "/home/test/.hermes",
            "generation": "g1",
            "version": "v1",
            "lease": "lease-1",
            "route": {
                "workspace": "/workspace",
                "model": "gpt-5.5",
                "provider": "openai-codex",
            },
        },
        # Malformed normalized_model (not type-checked as a bool).
        {
            "session_id": "s1",
            "profile": "default",
            "profile_home": "/home/test/.hermes",
            "generation": "g1",
            "version": "v1",
            "lease": "lease-1",
            "route": {
                "workspace": "/workspace",
                "model": "gpt-5.5",
                "provider": "openai-codex",
                "normalized_model": "false",
            },
        },
    ):
        with pytest.raises(RunnerFenceRefused, match="owner_fence"):
            client.start_run(
                StartRunRequest(
                    session_id="s1",
                    message="hello",
                    workspace="/workspace",
                    profile="default",
                    owner_fence=bad_fence,
                )
            )


def test_runner_client_rejects_request_lane_divergence(monkeypatch):
    """#6327: the top-level request route is cross-bound to the fence route
    BEFORE the POST — a request whose SID/profile/workspace/model/provider
    diverges from the claimed fence lane is refused (typed RunnerFenceRefused)
    with zero POSTs, so the runner can never create a run for a lane the
    fence did not authorize."""

    def fake_urlopen(req, timeout=0):
        raise AssertionError("start_run must not POST a divergent request lane")

    _patch_opener(monkeypatch, fake_urlopen)
    client = HttpRunnerClient(base_url="http://runner.local/", api_key="secret")

    fence = _claimed_fence()
    base = dict(
        session_id="s1",
        message="hello",
        workspace="/workspace",
        profile="default",
        provider="openai-codex",
        model="gpt-5.5",
        owner_fence=fence,
    )
    for request_mutation in (
        {"session_id": "other-sid"},
        {"profile": "other-profile"},
        {"workspace": "/other-workspace"},
        {"model": "other-model"},
        {"provider": "other-provider"},
    ):
        req = StartRunRequest(**{**base, **request_mutation})
        with pytest.raises(RunnerFenceRefused, match="diverges from the owner_fence lane"):
            client.start_run(req)


def test_runner_client_canonicalizes_root_profile_wire_identity(monkeypatch):
    """#6327: a valid ROOT session (profile None) serializes an empty
    profile; the client canonicalizes it to the 'default' wire identity so it
    never falls into the generic schema-error path, and the POST body carries
    the canonical profile bound to the fence lane."""

    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse({
            "run_id": "run-1",
            "stream_id": "run-1",
            "status": "running",
            "owner_fence": {
                "accepted": True,
                "session_id": "s1",
                "profile": "default",
                "profile_home": "/home/test/.hermes",
                "generation": "fingerprint-1",
                "version": "claim-1",
                "lease": "lease-1",
                "route": {
                    "workspace": "/workspace",
                    "model": "gpt-5.5",
                    "provider": "openai-codex",
                    "normalized_model": False,
                },
            },
        })

    _patch_opener(monkeypatch, fake_urlopen)
    client = HttpRunnerClient(base_url="http://runner.local/", api_key="secret")

    root_fence = _claimed_fence()
    root_fence["profile"] = ""  # root session serializes an empty profile
    result = client.start_run(
        StartRunRequest(
            session_id="s1",
            message="hello",
            workspace="/workspace",
            profile=None,
            provider="openai-codex",
            model="gpt-5.5",
            owner_fence=root_fence,
        )
    )

    assert result["run_id"] == "run-1"
    assert captured["body"]["profile"] == "default"
    assert captured["body"]["owner_fence"]["profile"] == "default"


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
