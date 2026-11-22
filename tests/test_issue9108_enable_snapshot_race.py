"""Enabling YOLO must decide consent on the exact relay snapshot (#9108).

The enable endpoint reads the parked approvals twice: first through
``pending_protected_approval()`` (is a consent prompt already parked?), later
through ``gateway_pending_mirrors()`` (which remote prompts will actually be
relayed?). The gateway stream publishes a mirror through
``_submit_stream_approval_mirror()`` without taking the YOLO handoff, so a
protected prompt (MCP elicitation or ``surface: "publish"``) that arrives
between those two reads is inside the relay snapshot while the earlier decision
never saw it. Relaying it answers a consent prompt with ``once`` as a side
effect of turning YOLO on.

These tests drive the real enable function, the real mirror publisher and the
real queue snapshot; only the Gateway wire (``HttpRunnerClient``) is recorded,
so the assertions are about WebUI's decision, not a copy of the rule. The
arrival is scheduled by wrapping the first read and publishing the mirror right
after it returns, which is exactly the interleaving the review reproduced.
"""

import pytest

import api.gateway_chat as gateway_chat
import api.route_approvals as route_approvals
import api.routes as routes

try:
    from tools.approval import disable_session_yolo, is_session_yolo_enabled

    APPROVAL_AVAILABLE = True
except ImportError:  # pragma: no cover - agent-less installs
    APPROVAL_AVAILABLE = False

try:
    from tools.approval_gateway_wait import _ApprovalEntry
except ImportError:  # pragma: no cover - older Agent layout
    _ApprovalEntry = None


pytestmark = pytest.mark.skipif(
    not APPROVAL_AVAILABLE or _ApprovalEntry is None,
    reason="tools.approval unavailable",
)

ELICITATION = {
    "command": "Publish the drafted release notes to the public blog?",
    "description": "mcp:blog/publish",
    "pattern_key": "mcp_elicitation",
    "pattern_keys": ["mcp_elicitation"],
    "allow_session": False,
    "allow_permanent": False,
}

PUBLISH_SURFACE = {
    "command": "Send the invoice to the customer",
    "description": "mcp:billing/send",
    "surface": "publish",
}

COMMON = {
    "command": "rm -rf /tmp/webui-9108-target",
    "description": "recursive delete",
    "pattern_key": "recursive delete",
    "pattern_keys": ["recursive delete"],
}


@pytest.fixture
def sid(request):
    session_id = f"issue9108race-{request.node.name[:40]}"
    _clear(session_id)
    yield session_id
    disable_session_yolo(session_id)
    _clear(session_id)


def _clear(session_id):
    disable_session_yolo(session_id)
    with route_approvals._lock:
        route_approvals._gateway_queues.pop(session_id, None)
        route_approvals._pending.pop(session_id, None)
        route_approvals._gateway_relay_owners.clear()


def _run_approval(approval, *, run_id="run-9108-race", approval_id=""):
    """A run-backed prompt as the gateway stream publishes it."""
    data = dict(approval)
    data.update(
        {
            "run_id": run_id,
            "approval_id": approval_id or f"{run_id}-prompt",
            "_gateway_agent_identity_v1": True,
            "_gateway_execution_generation": 7,
        }
    )
    return data


def _publish(session_id, approval, **kwargs):
    """Publish a mirror through the real publisher (the stream's own path)."""
    data = _run_approval(approval, **kwargs)
    published, _count = route_approvals.submit_gateway_pending_mirror(session_id, data)
    assert published is not None, "the mirror must be published"
    return data


def _parked(session_id, approval_id):
    return [
        entry
        for entry in (route_approvals._pending.get(session_id) or [])
        if isinstance(entry, dict) and entry.get("approval_id") == approval_id
    ]


class _RecordingWire:
    """Stand-in for the Gateway Runs API: records every answer sent."""

    calls = []

    def __init__(self, base_url="", api_key=""):
        self.base_url = base_url
        self.api_key = api_key

    def respond_approval(self, run_id, prompt_id, choice, **kwargs):
        type(self).calls.append(
            {
                "run_id": run_id,
                "prompt_id": prompt_id,
                "choice": choice,
                "execution_generation": kwargs.get("execution_generation"),
            }
        )


@pytest.fixture
def wire(monkeypatch):
    """Only the outbound Runs API is replaced; queues and decisions stay real."""
    _RecordingWire.calls = []
    import api.runner_client as runner_client

    monkeypatch.setattr(runner_client, "HttpRunnerClient", _RecordingWire)
    monkeypatch.setattr(
        gateway_chat,
        "gateway_run_endpoint",
        lambda run_id: ("http://gateway.invalid", "test-key"),
    )
    return _RecordingWire


def _arrive_during_first_read(monkeypatch, approval, **kwargs):
    """Schedule a mirror publication between the enable's two reads.

    The enable asks ``pending_protected_approval()`` first and snapshots the
    relay set later. Publishing from inside that first read reproduces a prompt
    that the gateway stream parked in between, with the real publisher.
    """
    real_read = route_approvals.pending_protected_approval
    published = {}

    def racing_read(session_key):
        result = real_read(session_key)
        published.update(_publish(session_key, approval, **kwargs))
        return result

    monkeypatch.setattr(routes, "pending_protected_approval", racing_read)
    return published


# ── The interleaving the review asks for a regression on ────────────────────


@pytest.mark.parametrize(
    "approval", [ELICITATION, PUBLISH_SURFACE], ids=["elicitation", "publish"]
)
def test_protected_mirror_arriving_mid_relay_refuses_the_whole_enable(
    sid, wire, monkeypatch, approval
):
    arrival = _arrive_during_first_read(monkeypatch, approval)

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 409, "a consent prompt inside the relay snapshot must refuse the enable"
    assert payload["ok"] is False
    assert payload["code"] == "gateway_protected_approval_pending"
    # Nothing was relayed: the consent prompt was not answered by enabling YOLO.
    assert wire.calls == []
    assert is_session_yolo_enabled(sid) is False
    # And it is still parked, so its card stays visible for a manual answer.
    assert _parked(sid, arrival["approval_id"]), "the protected card must stay parked"


def test_protected_arrival_does_not_let_an_ordinary_mirror_be_relayed_first(
    sid, wire, monkeypatch
):
    """Whole-enable: a parked ordinary prompt is not approved when the enable refuses."""
    _publish(sid, COMMON, run_id="run-9108-ordinary", approval_id="ordinary-prompt")
    _arrive_during_first_read(
        monkeypatch, ELICITATION, run_id="run-9108-late", approval_id="late-prompt"
    )

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 409
    assert payload["code"] == "gateway_protected_approval_pending"
    assert wire.calls == [], "no partial effect: the ordinary prompt was not relayed"
    assert is_session_yolo_enabled(sid) is False
    assert _parked(sid, "ordinary-prompt"), "the ordinary card survives for a later answer"


# ── Controls: the refusal stays narrow ─────────────────────────────────────


def test_ordinary_mirror_arriving_mid_relay_is_still_relayed(sid, wire, monkeypatch):
    _arrive_during_first_read(
        monkeypatch, COMMON, run_id="run-9108-ok", approval_id="late-ordinary"
    )

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 200
    assert payload["yolo_enabled"] is True
    assert [call["prompt_id"] for call in wire.calls] == ["late-ordinary"]
    assert wire.calls[0]["choice"] == "once"
    assert is_session_yolo_enabled(sid) is True


def test_protected_already_parked_still_refuses_before_the_snapshot(sid, wire):
    _publish(sid, ELICITATION, run_id="run-9108-parked", approval_id="parked-prompt")

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 409
    assert payload["code"] == "gateway_protected_approval_pending"
    assert wire.calls == []
    assert is_session_yolo_enabled(sid) is False


def test_enable_with_nothing_pending_still_works(sid, wire):
    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 200
    assert payload["yolo_enabled"] is True
    assert wire.calls == []
    assert is_session_yolo_enabled(sid) is True
