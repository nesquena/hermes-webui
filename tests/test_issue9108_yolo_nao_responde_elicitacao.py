"""Session YOLO must never answer an MCP elicitation / publish approval (#9108).

YOLO is a statement about *command* approvals. An elicitation (`pattern_key:
"mcp_elicitation"`, surface `publish`) is a consent prompt for an outbound send,
a public post, a money change or a destructive action, raised by plugins. Turning
YOLO on must not pre-answer those: neither settle path may resolve one, and
enabling YOLO must not release one that is already parked.

These tests run the real settle/enable functions over real
``tools.approval`` queue state; only the remote Runs API call is spied on, so the
assertions are about WebUI's actual decisions, not a re-implementation of the
rule in the test.
"""

import pytest

import api.gateway_chat as gateway_chat
import api.route_approvals as route_approvals
import api.routes as routes

try:
    from tools.approval import (
        disable_session_yolo,
        enable_session_yolo,
        is_session_yolo_enabled,
    )

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


# Elicitation shape the Agent's `request_elicitation_consent` enqueues.
ELICITATION = {
    "command": "Publish the drafted release notes to the public blog?",
    "description": "mcp:blog/publish",
    "pattern_key": "mcp_elicitation",
    "pattern_keys": ["mcp_elicitation"],
    "allow_session": False,
    "allow_permanent": False,
}

# Same consent, marked only by its surface instead of its pattern key.
PUBLISH_SURFACE = {
    "command": "Send the invoice to the customer",
    "description": "mcp:billing/send",
    "surface": "publish",
}

# A plain command approval: YOLO is expected to keep answering this one.
COMMON = {
    "command": "rm -rf /tmp/webui-9108-target",
    "description": "recursive delete",
    "pattern_key": "recursive delete",
    "pattern_keys": ["recursive delete"],
}


@pytest.fixture
def sid(request):
    """Unique session per test, with the Agent-side and WebUI-side state cleared."""
    session_id = f"issue9108-{request.node.name[:40]}"
    _clear(session_id)
    yield session_id
    disable_session_yolo(session_id)
    _clear(session_id)


def _clear(session_id):
    disable_session_yolo(session_id)
    with route_approvals._lock:
        route_approvals._gateway_queues.pop(session_id, None)
        route_approvals._pending.pop(session_id, None)


def _park(session_id, data):
    """Park one real producer entry the way the Agent's gate does before notifying."""
    entry = _ApprovalEntry(dict(data))
    with route_approvals._lock:
        route_approvals._gateway_queues.setdefault(session_id, []).append(entry)
    return entry


# ── Local (no-run) settle path ───────────────────────────────────────────────


@pytest.mark.parametrize("approval", [ELICITATION, PUBLISH_SURFACE], ids=["elicitation", "publish"])
def test_local_settle_publishes_protected_approval_as_a_card(sid, approval):
    enable_session_yolo(sid)
    assert is_session_yolo_enabled(sid) is True
    entry = _park(sid, approval)

    auto_resolved, head, total = route_approvals.settle_gateway_pending_local_notification(
        sid, dict(entry.data)
    )

    # Not auto-answered: the card is published instead.
    assert auto_resolved is False
    assert head is not None
    assert total >= 1
    assert not entry.event.is_set()
    assert entry.result is None


def test_local_settle_still_auto_answers_a_common_approval(sid):
    enable_session_yolo(sid)
    entry = _park(sid, COMMON)

    auto_resolved, _head, _total = route_approvals.settle_gateway_pending_local_notification(
        sid, dict(entry.data)
    )

    assert auto_resolved is True
    assert entry.event.is_set()
    assert entry.result == "once"


def test_local_settle_of_a_common_approval_leaves_a_parked_elicitation_unanswered(sid):
    """The drain behind the auto-answer must not sweep up a parked elicitation."""
    enable_session_yolo(sid)
    elicitation = _park(sid, ELICITATION)
    route_approvals.submit_gateway_pending_mirror(sid, dict(elicitation.data))
    common = _park(sid, COMMON)

    route_approvals.settle_gateway_pending_local_notification(sid, dict(common.data))

    assert common.event.is_set()
    assert common.result == "once"
    assert not elicitation.event.is_set()
    assert elicitation.result is None
    assert any(
        isinstance(entry, dict) and entry.get("pattern_key") == "mcp_elicitation"
        for entry in (route_approvals._pending.get(sid) or [])
    ), "the elicitation card must stay published"


# ── Run settle path ──────────────────────────────────────────────────────────


def _run_approval(approval, *, run_id="run-9108"):
    data = dict(approval)
    data.update({"run_id": run_id, "approval_id": f"{run_id}-approval"})
    data["_gateway_agent_identity_v1"] = True
    return data


@pytest.mark.parametrize("approval", [ELICITATION, PUBLISH_SURFACE], ids=["elicitation", "publish"])
def test_run_settle_does_not_auto_approve_a_protected_approval(sid, approval, monkeypatch):
    auto_approved = []
    monkeypatch.setattr(
        gateway_chat,
        "_auto_approve_gateway_run",
        lambda *args, **kwargs: auto_approved.append((args, kwargs)),
    )
    enable_session_yolo(sid)
    data = _run_approval(approval)

    auto_resolved, head, total = gateway_chat._settle_gateway_run_approval(
        sid, dict(data), "http://gateway", ""
    )

    assert auto_approved == []
    assert auto_resolved is False
    assert head is not None
    assert total >= 1


def test_run_settle_still_auto_approves_a_common_approval(sid, monkeypatch):
    auto_approved = []
    monkeypatch.setattr(
        gateway_chat,
        "_auto_approve_gateway_run",
        lambda *args, **kwargs: auto_approved.append((args, kwargs)),
    )
    enable_session_yolo(sid)
    data = _run_approval(COMMON)

    auto_resolved, head, total = gateway_chat._settle_gateway_run_approval(
        sid, dict(data), "http://gateway", ""
    )

    assert len(auto_approved) == 1
    assert auto_resolved is True
    assert (head, total) == (None, 0)


# ── Enabling YOLO must not release a protected approval ──────────────────────


@pytest.mark.parametrize("approval", [ELICITATION, PUBLISH_SURFACE], ids=["elicitation", "publish"])
def test_enabling_yolo_refuses_while_a_protected_approval_is_pending(sid, approval):
    entry = _park(sid, approval)
    route_approvals.submit_gateway_pending_mirror(sid, dict(entry.data))

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 409
    assert payload["ok"] is False
    assert payload["error"]
    assert payload["code"] == "gateway_protected_approval_pending"
    # The elicitation is untouched and YOLO did not commit.
    assert not entry.event.is_set()
    assert entry.result is None
    assert is_session_yolo_enabled(sid) is False


def test_enabling_yolo_still_releases_a_common_local_approval(sid):
    entry = _park(sid, COMMON)
    route_approvals.submit_gateway_pending_mirror(sid, dict(entry.data))

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 200
    assert payload["ok"] is True
    assert payload["yolo_enabled"] is True
    assert entry.event.is_set()
    assert entry.result == "once"


# ── The run mapper must not drop a surface marker on the way in ──────────────


def test_runs_approval_mapper_keeps_the_surface_marker():
    event = gateway_chat._gateway_runs_approval_event(
        {
            "request_id": "req-9108",
            "command": "Send the invoice to the customer",
            "description": "mcp:billing/send",
            "surface": "publish",
        }
    )

    assert event is not None
    assert event["surface"] == "publish"
