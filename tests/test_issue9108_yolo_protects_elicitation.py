"""#9108: session YOLO must never answer an elicitation / publish approval.

YOLO is a statement about *command* approvals. An elicitation
(``pattern_key: "mcp_elicitation"``, surface ``publish``) is a consent prompt
for an outbound send, a public post, a money change or a destructive action,
raised by plugins. Turning YOLO on must not pre-answer those: neither settle
path may resolve one, and enabling YOLO must not release one that is already
parked.

These tests run the real settle/enable functions over the real WebUI approval
queue (``_gateway_queues`` / ``_pending``). The only substitutes are:

* the Agent's session-YOLO state — ``api.route_approvals`` binds the Agent's
  ``tools.approval`` names once at import time through ``_bind_agent_name``,
  and without an installed Agent those bindings are inert stubs
  (``is_session_yolo_enabled`` always False, ``enable_session_yolo`` a no-op);
* the producer entry object — a shape-compatible stand-in for the Agent's
  ``_ApprovalEntry`` (``data``/``result``/``reason``/``event``);
* the remote Runs API call, spied on.

Nothing under test is mocked: the settle functions, mirrors, drains and the
enable/release path are the production code.
"""

import threading
import uuid

import pytest

import api.config as config
import api.gateway_chat as gateway_chat
import api.route_approvals as route_approvals
import api.routes as routes
from api.runner_client import HttpRunnerClient

# Elicitation shape the Agent's ``request_elicitation_consent`` enqueues.
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

PROTECTED = [ELICITATION, PUBLISH_SURFACE]
PROTECTED_IDS = ["elicitation", "publish"]


# ── Scripted Agent state (no installed Agent required) ─────────────────────


@pytest.fixture
def yolo_state(monkeypatch):
    """Scripted session-YOLO state installed at every namespace a consumer reads.

    ``api.route_approvals`` binds the Agent's ``tools.approval`` names once at
    import time through ``_bind_agent_name``; without an installed Agent those
    bindings are inert stubs. Consumers read module-level names, so the scripted
    state is installed here:

    - ``api.route_approvals.is_session_yolo_enabled`` — settle + state helpers
    - ``api.route_approvals.enable_session_yolo`` / ``disable_session_yolo`` —
      the YOLO transition commit/rollback
    - ``api.routes.is_session_yolo_enabled`` / ``enable_session_yolo`` /
      ``disable_session_yolo`` — the enable/release path's own bindings
    - ``api.gateway_chat._gateway_session_yolo_enabled`` — the run settle path
      (a module-level def whose body lazy-imports ``tools.approval``)
    """
    state = {"enabled": set()}

    def is_enabled(session_key):
        return str(session_key or "") in state["enabled"]

    def enable(session_key):
        state["enabled"].add(str(session_key or ""))

    def disable(session_key):
        state["enabled"].discard(str(session_key or ""))

    for module in (route_approvals, routes):
        monkeypatch.setattr(module, "is_session_yolo_enabled", is_enabled)
        monkeypatch.setattr(module, "enable_session_yolo", enable)
        monkeypatch.setattr(module, "disable_session_yolo", disable)
    monkeypatch.setattr(gateway_chat, "_gateway_session_yolo_enabled", is_enabled)
    return state


class _ScriptedEntry:
    """Shape-compatible stand-in for the Agent's ``_ApprovalEntry`` producer.

    ``_settle_gateway_entry`` touches only ``result``/``reason``/``event``;
    mirror binding reads ``data`` and the per-entry ``request_id`` the real
    class stamps on construction.
    """

    def __init__(self, data):
        self.data = dict(data)
        self.data.setdefault("request_id", uuid.uuid4().hex[:12])
        self.result = None
        self.reason = None
        self.event = threading.Event()


@pytest.fixture
def sid(request, yolo_state):
    """Unique session per test, with the WebUI-side state cleared."""
    session_id = f"issue9108-{request.node.name[:40]}"
    _clear(session_id)
    yield session_id
    yolo_state["enabled"].discard(session_id)
    _clear(session_id)


@pytest.fixture
def runner_spy(monkeypatch):
    """Spy on the remote Runs API answer; record every relayed approval."""
    calls = []

    def fake_respond(_self, run_id, approval_id, choice, **kwargs):
        calls.append(
            {"run_id": run_id, "approval_id": approval_id, "choice": choice, **kwargs}
        )
        return {"resolved": 1}

    monkeypatch.setattr(HttpRunnerClient, "respond_approval", fake_respond)
    monkeypatch.setattr(
        gateway_chat, "gateway_run_endpoint", lambda _run_id: ("http://gateway.test", "key")
    )
    monkeypatch.setattr(
        config, "gateway_supports_approval_identity_v1", lambda *_a, **_k: True
    )
    return calls


def _clear(session_id):
    with route_approvals._lock:
        route_approvals._gateway_queues.pop(session_id, None)
        route_approvals._pending.pop(session_id, None)


def _park(session_id, data):
    """Park one producer entry the way the Agent's gate does before notifying."""
    entry = _ScriptedEntry(dict(data))
    with route_approvals._lock:
        route_approvals._gateway_queues.setdefault(session_id, []).append(entry)
    return entry


def _run_approval(approval, *, run_id="run-9108"):
    data = dict(approval)
    data.update({"run_id": run_id, "approval_id": f"{run_id}-approval"})
    data["_gateway_agent_identity_v1"] = True
    return data


def _pending_rows(session_id):
    with route_approvals._lock:
        return [
            dict(entry)
            for entry in (route_approvals._pending.get(session_id) or [])
            if isinstance(entry, dict)
        ]


def _parked_protected_rows(session_id):
    return [
        row
        for row in _pending_rows(session_id)
        if route_approvals.is_protected_approval(row)
    ]


# ── The protection predicate ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("approval", "expected"),
    [
        (ELICITATION, True),
        (PUBLISH_SURFACE, True),
        (COMMON, False),
        ({}, False),
        (None, False),
        ("not-a-dict", False),
        ({"pattern_key": "other"}, False),
        ({"pattern_keys": ["other", "mcp_elicitation"]}, True),
        ({"surface": "read"}, False),
    ],
    ids=[
        "elicitation",
        "publish",
        "common",
        "empty",
        "none",
        "not-a-dict",
        "other-pattern",
        "pattern-keys-list",
        "read-surface",
    ],
)
def test_is_protected_approval_matches_the_documented_shapes(approval, expected):
    assert route_approvals.is_protected_approval(approval) is expected


# ── Local (no-run) settle path ──────────────────────────────────────────────


@pytest.mark.parametrize("approval", PROTECTED, ids=PROTECTED_IDS)
def test_local_settle_publishes_protected_approval_as_a_card(sid, yolo_state, approval):
    """Protected prompt arriving with YOLO already enabled: card, no auto-answer."""
    yolo_state["enabled"].add(sid)
    entry = _park(sid, approval)

    auto_resolved, head, total = route_approvals.settle_gateway_pending_local_notification(
        sid, dict(entry.data)
    )

    assert auto_resolved is False
    assert head is not None
    assert total >= 1
    assert not entry.event.is_set()
    assert entry.result is None
    assert _parked_protected_rows(sid), "the protected card must stay published"


def test_local_settle_still_auto_answers_a_common_approval(sid, yolo_state):
    """Differential control: an ordinary command is still auto-answered."""
    yolo_state["enabled"].add(sid)
    entry = _park(sid, COMMON)

    auto_resolved, _head, _total = route_approvals.settle_gateway_pending_local_notification(
        sid, dict(entry.data)
    )

    assert auto_resolved is True
    assert entry.event.is_set()
    assert entry.result == "once"


@pytest.mark.parametrize("approval", PROTECTED, ids=PROTECTED_IDS)
def test_common_command_notification_does_not_drain_a_parked_protected_waiter(
    sid, yolo_state, approval
):
    """Mixed pending queues: a later ordinary command must not release the older
    protected waiter, because the YOLO branch drains every local waiter."""
    protected = _park(sid, approval)
    route_approvals.submit_gateway_pending_mirror(sid, dict(protected.data))
    common = _park(sid, COMMON)
    yolo_state["enabled"].add(sid)

    auto_resolved, _head, _total = route_approvals.settle_gateway_pending_local_notification(
        sid, dict(common.data)
    )

    assert auto_resolved is True
    assert common.event.is_set()
    assert common.result == "once"
    assert not protected.event.is_set()
    assert protected.result is None
    assert _parked_protected_rows(sid), "the protected card must stay published"


def test_resolve_gateway_pending_local_all_retains_protected_waiters(sid):
    """The drain behind every YOLO answer must itself skip protected waiters."""
    protected = _park(sid, ELICITATION)
    common = _park(sid, COMMON)
    with route_approvals._lock:
        route_approvals._gateway_queues[sid] = [protected, common]
        route_approvals._pending[sid] = [dict(protected.data), dict(common.data)]

    resolved, _head, _total = route_approvals.resolve_gateway_pending_local_all(sid, "once")

    assert resolved == 1
    assert common.event.is_set()
    assert common.result == "once"
    assert not protected.event.is_set()
    assert protected.result is None
    with route_approvals._lock:
        retained = route_approvals._gateway_queues.get(sid) or []
        assert protected in retained
    assert _parked_protected_rows(sid), "the protected card must stay published"


# ── Run-backed settle path ──────────────────────────────────────────────────


@pytest.mark.parametrize("approval", PROTECTED, ids=PROTECTED_IDS)
def test_run_settle_does_not_auto_approve_a_protected_approval(
    sid, yolo_state, runner_spy, approval
):
    """Protected prompt through the run-backed settle path with YOLO enabled."""
    yolo_state["enabled"].add(sid)
    data = _run_approval(approval)

    auto_resolved, head, total = gateway_chat._settle_gateway_run_approval(
        sid, dict(data), "http://gateway.test", "key"
    )

    assert runner_spy == []
    assert auto_resolved is False
    assert head is not None
    assert total >= 1
    assert _parked_protected_rows(sid), "the protected card must stay published"


def test_run_settle_still_auto_approves_a_common_approval(sid, yolo_state, runner_spy):
    """Differential control: an ordinary run approval is still auto-approved."""
    yolo_state["enabled"].add(sid)
    data = _run_approval(COMMON)

    auto_resolved, head, total = gateway_chat._settle_gateway_run_approval(
        sid, dict(data), "http://gateway.test", "key"
    )

    assert len(runner_spy) == 1
    assert runner_spy[0]["choice"] == "once"
    assert auto_resolved is True
    assert (head, total) == (None, 0)


# ── Enabling YOLO must not release a protected approval ─────────────────────


@pytest.mark.parametrize("approval", PROTECTED, ids=PROTECTED_IDS)
def test_enabling_yolo_refuses_while_a_protected_approval_is_pending(
    sid, yolo_state, approval
):
    """A protected approval already parked: whole-enable refusal, no side effect."""
    entry = _park(sid, approval)
    route_approvals.submit_gateway_pending_mirror(sid, dict(entry.data))

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 409
    assert payload["ok"] is False
    assert payload["error"]
    assert payload["code"] == "gateway_protected_approval_pending"
    assert not entry.event.is_set()
    assert entry.result is None
    assert sid not in yolo_state["enabled"]
    assert _parked_protected_rows(sid), "the protected card must stay published"


def test_enabling_yolo_still_releases_a_common_local_approval(sid, yolo_state):
    """Differential control: an ordinary parked local approval is still released."""
    entry = _park(sid, COMMON)
    route_approvals.submit_gateway_pending_mirror(sid, dict(entry.data))

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 200
    assert payload["ok"] is True
    assert payload["yolo_enabled"] is True
    assert entry.event.is_set()
    assert entry.result == "once"
    assert sid in yolo_state["enabled"]


@pytest.mark.parametrize("approval", PROTECTED, ids=PROTECTED_IDS)
def test_enabling_yolo_refuses_when_a_protected_run_mirror_arrives_mid_relay(
    sid, yolo_state, monkeypatch, runner_spy, approval
):
    """The protected decision must cover the actual relay snapshot: a protected
    run mirror published after the enable snapshot but before the first relay
    must refuse the whole enable instead of being relayed or left to a later
    YOLO answer."""
    ordinary_run = _run_approval(COMMON, run_id="run-9108-ordinary")
    route_approvals.submit_gateway_pending_mirror(sid, dict(ordinary_run))
    original_snapshot = routes.gateway_pending_mirrors
    late = _run_approval(approval, run_id="run-9108-late")
    state = {"published": False}

    def snapshot_publishing_late_mirror(session_id):
        mirrors = original_snapshot(session_id)
        if not state["published"]:
            state["published"] = True
            # The real publisher (gateway_chat._submit_stream_approval_mirror)
            # does not take the YOLO handoff, so it can land between the
            # enable's snapshot and its first relay.
            route_approvals.submit_gateway_pending_mirror(session_id, dict(late))
        return mirrors

    monkeypatch.setattr(routes, "gateway_pending_mirrors", snapshot_publishing_late_mirror)

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 409
    assert payload["ok"] is False
    assert payload["code"] == "gateway_protected_approval_pending"
    assert runner_spy == [], "no approval may be relayed once a protected one is seen"
    assert sid not in yolo_state["enabled"]
    remaining = route_approvals.gateway_pending_mirrors(sid)
    assert any(route_approvals.is_protected_approval(mirror) for mirror in remaining), (
        "the late protected mirror must stay parked"
    )


@pytest.mark.parametrize("approval", PROTECTED, ids=PROTECTED_IDS)
def test_enabling_yolo_refuses_when_a_protected_local_waiter_arrives_mid_relay(
    sid, yolo_state, monkeypatch, runner_spy, approval
):
    """Same interleaving on the local side: a protected no-run waiter parked after
    the enable snapshot but before the local drain must refuse the whole enable."""
    ordinary_run = _run_approval(COMMON, run_id="run-9108-ordinary")
    route_approvals.submit_gateway_pending_mirror(sid, dict(ordinary_run))
    original_snapshot = routes.gateway_pending_mirrors
    state = {"published": False}

    def snapshot_publishing_late_waiter(session_id):
        mirrors = original_snapshot(session_id)
        if not state["published"]:
            state["published"] = True
            entry = _park(session_id, approval)
            route_approvals.submit_gateway_pending_mirror(session_id, dict(entry.data))
        return mirrors

    monkeypatch.setattr(routes, "gateway_pending_mirrors", snapshot_publishing_late_waiter)

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 409
    assert payload["ok"] is False
    assert payload["code"] == "gateway_protected_approval_pending"
    assert runner_spy == [], "no approval may be relayed once a protected one is seen"
    assert sid not in yolo_state["enabled"]
    assert _parked_protected_rows(sid), "the protected card must stay published"
