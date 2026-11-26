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

# A plugin's own approval rule: the Agent stamps the plugin's rule key, so
# this is the "ask before an outbound send / a public post / a money change"
# gate issue #9108 names. The rule key is the plugin's, so the pattern key is
# namespaced.
PLUGIN_RULE = {
    "command": "Publish the drafted release notes to the public blog?",
    "description": "Plugin requires approval for publish",
    "pattern_key": "plugin_rule:blog.publish",
    "pattern_keys": ["plugin_rule:blog.publish"],
}

# The guard on writes to a protected instruction file: a consent prompt the
# Agent raises with its own pattern key, not an elicitation.
PROTECTED_FILE = {
    "command": "Write the agent's standing instructions to AGENTS.md",
    "description": "protected instruction file",
    "pattern_key": "protected_instruction_file",
    "pattern_keys": ["protected_instruction_file"],
}

# A computer-use action: the Agent asks a person before it acts on the
# screen, so YOLO must not answer it either.
COMPUTER_USE = {
    "command": "Click the Pay button on the checkout page",
    "description": "computer use action",
    "pattern_key": "cua:click:execute",
    "pattern_keys": ["cua:click:execute"],
}

# A plain command approval: YOLO is expected to keep answering this one.
COMMON = {
    "command": "rm -rf /tmp/webui-9108-target",
    "description": "recursive delete",
    "pattern_key": "recursive delete",
    "pattern_keys": ["recursive delete"],
}

# #9251: the Gateway's ``pending_controls`` projection carries no pattern key
# and no surface — the payload states nothing about which kind of gate it is.
UNCLASSIFIED = {
    "command": "Approve the pending request",
    "description": "pending_controls projection without a marker",
}

PROTECTED = [
    ELICITATION,
    PUBLISH_SURFACE,
    PLUGIN_RULE,
    PROTECTED_FILE,
    COMPUTER_USE,
]
PROTECTED_IDS = ["elicitation", "publish", "plugin-rule", "protected-file", "computer-use"]


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
        for key in [
            key
            for key in route_approvals._answered_approval_markers
            if key[0] == session_id
        ]:
            del route_approvals._answered_approval_markers[key]


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


def test_enabling_yolo_still_relays_when_only_a_late_ordinary_command_arrives(
    sid, yolo_state, monkeypatch, runner_spy
):
    """The race guard must not turn into a blanket refusal.

    The maintainer's race matrix pairs the late-protected cases with a late
    ORDINARY command control: a command approval that lands in the same
    window must still be relayed and must still enable YOLO. This is the
    control that proves the protected check narrows the refusal to protected
    approvals instead of refusing every enable that races anything.
    """
    early = _run_approval(COMMON, run_id="run-9108-early")
    route_approvals.submit_gateway_pending_mirror(sid, dict(early))
    original_snapshot = routes.gateway_pending_mirrors
    state = {"published": False}

    def snapshot_publishing_late_command(session_id):
        mirrors = original_snapshot(session_id)
        if not state["published"]:
            state["published"] = True
            late = _run_approval(COMMON, run_id="run-9108-late-ordinary")
            route_approvals.submit_gateway_pending_mirror(session_id, dict(late))
        return mirrors

    monkeypatch.setattr(routes, "gateway_pending_mirrors", snapshot_publishing_late_command)

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 200, payload
    assert payload.get("ok") is not False
    assert sid in yolo_state["enabled"], "an ordinary command must still enable YOLO"
    assert runner_spy, "the ordinary command approval must still be relayed"
    assert all(call["choice"] == "once" for call in runner_spy), runner_spy


# ── The Gateway event chain: a consent prompt must survive YOLO ────────────
#
# The Gateway describes one prompt twice. The ``approval.request`` event
# carries the Agent's ``pattern_key``; the ``pending_controls`` authority
# projection carries the same prompt WITHOUT one (the projection keeps only
# kind / prompt_id / command / description / choices). Both descriptions reach
# WebUI through the same translation, so a consent prompt that only the
# projection describes must still be protected -- otherwise session YOLO
# answers a publish/spend consent the person never saw.
#
# These tests drive the real Gateway event handling chain
# (``gateway_chat._relay_gateway_run_approval``) over a synthetic
# ``approval.request`` event and a synthetic ``pending_controls`` projection,
# with the remote Runs API replaced by a spy. No real Gateway is contacted and
# no command or publish action is executed.


def _gateway_event_payload(approval, *, prompt_id="prompt-9108", generation=7):
    """A synthetic Gateway ``approval.request`` event, as the Runs API emits it."""
    data = dict(approval)
    data.update({
        "event": "approval.request",
        "run_id": "run-9108",
        "prompt_id": prompt_id,
        "execution_generation": generation,
        "choices": ["once", "deny"],
    })
    return data


def _gateway_projection_payload(approval, *, prompt_id="prompt-9108", generation=7):
    """The ``pending_controls`` projection of the same prompt (no pattern key)."""
    return {
        "kind": "approval",
        "prompt_id": prompt_id,
        "execution_generation": generation,
        "command": str(approval.get("command") or ""),
        "description": str(approval.get("description") or ""),
        "choices": ["once", "deny"],
    }


class _EventSink:
    """Fake transport: records every approval event WebUI would publish."""

    def __init__(self):
        self.events = []

    def __call__(self, name, payload):
        self.events.append((name, payload))

    @property
    def approvals(self):
        return [payload for name, payload in self.events if name == "approval"]


def _relay_gateway_event(sid, payload, *, run_id="run-9108"):
    """Run one Gateway approval through the real event handling chain."""
    sink = _EventSink()
    relayed = gateway_chat._relay_gateway_run_approval(
        sid,
        run_id,
        payload,
        "http://gateway.test",
        "key",
        put_gateway_event=sink,
    )
    return relayed, sink


@pytest.mark.parametrize("approval", PROTECTED, ids=PROTECTED_IDS)
def test_gateway_approval_request_event_is_not_auto_answered_by_yolo(
    sid, yolo_state, runner_spy, approval
):
    """The Gateway event path: a consent prompt stays a card for the person."""
    yolo_state["enabled"].add(sid)

    relayed, sink = _relay_gateway_event(sid, _gateway_event_payload(approval))

    assert relayed is True
    assert runner_spy == [], "YOLO must not answer a consent prompt on the Gateway"
    assert sink.approvals, "the consent prompt must still surface as a card"
    assert _parked_protected_rows(sid), "the consent card must stay published"


def test_a_first_seen_unclassified_event_is_parked_for_a_manual_answer(
    sid, yolo_state, runner_spy
):
    """The canonical producer can omit every marker from the event itself.

    ``PendingControls.register()`` rebuilds a pending prompt from kind, prompt
    id, generation, command, description and choices — no ``pattern_key`` and
    no ``surface``. On a first observation there is no earlier mirror to
    recover from, so nothing in the payload proves this is an ordinary command.
    Missing classification must not buy an automatic answer.
    """
    yolo_state["enabled"].add(sid)

    payload = _gateway_event_payload(COMMON)
    payload.pop("pattern_key", None)
    payload.pop("pattern_keys", None)

    relayed, sink = _relay_gateway_event(sid, payload)

    assert relayed is True
    assert runner_spy == [], (
        "an unclassified prompt must not be auto-answered; "
        f"remote answers: {runner_spy!r}"
    )
    assert sink.approvals, "the unclassified prompt must still surface as a card"


def test_a_first_seen_unclassified_projection_is_parked_for_a_manual_answer(
    sid, yolo_state, runner_spy
):
    """Projection-first/reattach with an empty local cache.

    The authority projection carries no marker, and this session has mirrored
    nothing for the prompt, so there is nothing to recover. The prompt must
    stay a card rather than becoming an automatic answer.
    """
    yolo_state["enabled"].add(sid)

    relayed, sink = _relay_gateway_event(sid, _gateway_projection_payload(COMMON))

    assert relayed is True
    assert runner_spy == [], (
        "an unclassified projection must not be auto-answered; "
        f"remote answers: {runner_spy!r}"
    )
    assert sink.approvals, "the unclassified prompt must still surface as a card"


@pytest.mark.parametrize("approval", PROTECTED, ids=PROTECTED_IDS)
def test_gateway_pending_controls_projection_does_not_answer_a_consent_prompt(
    sid, yolo_state, runner_spy, approval
):
    """The projection path: the same consent prompt, described without a pattern key.

    The Gateway's ``pending_controls`` projection keeps no ``pattern_key``, so
    the translation cannot read the consent marker off this payload. The
    markers WebUI already recorded for the prompt (the ``approval.request``
    event mirrored it) must carry over, or this projection hands a consent
    prompt straight to YOLO.
    """
    yolo_state["enabled"].add(sid)

    # The event arrives first and mirrors the consent prompt.
    relayed, _sink = _relay_gateway_event(sid, _gateway_event_payload(approval))
    assert relayed is True
    assert runner_spy == [], "the event path must not answer the consent prompt"

    # The projection re-describes the same prompt without a pattern key.
    relayed, _sink = _relay_gateway_event(
        sid, _gateway_projection_payload(approval)
    )

    assert relayed is True
    assert runner_spy == [], (
        "YOLO must not answer a consent prompt the Gateway describes without a "
        f"pattern key; remote answers: {runner_spy!r}"
    )
    assert _parked_protected_rows(sid), "the consent card must stay published"


@pytest.mark.parametrize("approval", PROTECTED, ids=PROTECTED_IDS)
def test_a_parked_consent_prompt_is_still_visible_and_manually_answerable(
    sid, yolo_state, runner_spy, approval
):
    """A protected prompt must stay visible AND be answerable by the person.

    Not auto-answering is only half the contract: the card has to remain in
    the queue and a manual answer has to reach the Gateway.
    """
    yolo_state["enabled"].add(sid)

    relayed, sink = _relay_gateway_event(sid, _gateway_event_payload(approval))

    assert relayed is True
    assert runner_spy == [], "YOLO must not answer the consent prompt"

    rows = _pending_rows(sid)
    assert rows, "the consent card must stay visible in the approval queue"
    card = rows[0]
    assert card.get("approval_id"), "the visible card must carry its approval id"

    # The person's own answer reaches the Gateway.
    mirror = route_approvals.gateway_pending_mirror(
        sid, approval_id=card["approval_id"], run_id="run-9108"
    )
    assert mirror is not None, "the visible card must be a relayable mirror"
    relay_payload, relay_status = routes._relay_gateway_run_approval(
        sid, mirror, "once", enable_yolo=False
    )

    assert relay_status == 200, relay_payload
    assert relay_payload.get("ok") is not False, relay_payload
    assert len(runner_spy) == 1, "the manual answer must reach the Gateway"
    assert runner_spy[0]["choice"] == "once"
    assert runner_spy[0]["run_id"] == "run-9108"


@pytest.mark.parametrize(
    "surface",
    ["publish", "mcp-elicitation", "mcp-trust/blog", "vault-payment"],
)
def test_gateway_translation_marks_a_consent_surface_as_protected(
    sid, yolo_state, runner_spy, surface
):
    """Every consent surface the Agent raises must read as protected.

    The surface travels with the Gateway approval payload; dropping it in the
    shared translation would let YOLO answer the consent prompt.
    """
    yolo_state["enabled"].add(sid)
    approval = {
        "command": "Publish the drafted release notes",
        "description": "mcp:blog/publish",
        "surface": surface,
    }

    relayed, sink = _relay_gateway_event(sid, _gateway_event_payload(approval))

    assert relayed is True
    assert runner_spy == [], f"YOLO answered a '{surface}' consent prompt"
    assert sink.approvals, "the consent prompt must still surface as a card"


def test_gateway_translation_keeps_an_ordinary_command_unprotected(
    sid, yolo_state, runner_spy
):
    """Differential control: an ordinary command approval stays answerable."""
    yolo_state["enabled"].add(sid)

    relayed, _sink = _relay_gateway_event(sid, _gateway_event_payload(COMMON))

    assert relayed is True
    assert len(runner_spy) == 1, runner_spy
    assert runner_spy[0]["choice"] == "once"


def test_gateway_projection_of_an_ordinary_command_is_still_auto_answered(
    sid, yolo_state, runner_spy
):
    """Differential control on the projection path: an ordinary command still goes."""
    yolo_state["enabled"].add(sid)

    relayed, _sink = _relay_gateway_event(sid, _gateway_event_payload(COMMON))
    assert relayed is True
    assert len(runner_spy) == 1, runner_spy

    relayed, _sink = _relay_gateway_event(
        sid, _gateway_projection_payload(COMMON)
    )
    assert relayed is True
    assert len(runner_spy) == 2, runner_spy
    assert runner_spy[1]["choice"] == "once"


# ── A late ordinary command: which approval was relayed, and when ──────────
#
# The enable path relays the approvals its snapshot contains. An approval that
# lands AFTER the snapshot is not part of that round: it stays parked and is
# answered by the next round, when session YOLO is already on. The test must
# say which approval was relayed, how many times, and what the queue looked
# like afterwards -- "the spy was called" cannot tell an early relay from a
# late one.


def _relayed_run_ids(calls):
    return [call["run_id"] for call in calls]


def test_late_ordinary_command_is_not_relayed_in_the_enabling_round(
    sid, yolo_state, monkeypatch, runner_spy
):
    """The enabling round relays the EARLY approval only.

    The late approval lands after the enable took its snapshot, so this round
    must not relay it; it stays parked for the next round. The assertion names
    the run id of every relayed approval, so an early relay cannot satisfy it.
    """
    early = _run_approval(COMMON, run_id="run-9108-early")
    route_approvals.submit_gateway_pending_mirror(sid, dict(early))
    original_snapshot = routes.gateway_pending_mirrors
    state = {"published": False}

    def snapshot_publishing_late_command(session_id):
        mirrors = original_snapshot(session_id)
        if not state["published"]:
            state["published"] = True
            late = _run_approval(COMMON, run_id="run-9108-late-ordinary")
            route_approvals.submit_gateway_pending_mirror(session_id, dict(late))
        return mirrors

    monkeypatch.setattr(routes, "gateway_pending_mirrors", snapshot_publishing_late_command)

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 200, payload
    assert sid in yolo_state["enabled"], "an ordinary command must still enable YOLO"

    # Exactly one approval was relayed, and it is the EARLY one.
    assert len(runner_spy) == 1, f"expected one relayed approval, got {runner_spy!r}"
    assert _relayed_run_ids(runner_spy) == ["run-9108-early"], runner_spy
    assert runner_spy[0]["choice"] == "once"

    # The late approval is still parked: this round did not answer it.
    parked_run_ids = [
        row.get("run_id")
        for row in _pending_rows(sid)
        if row.get("run_id")
    ]
    assert "run-9108-late-ordinary" in parked_run_ids, (
        "the late approval must stay parked for the next round; "
        f"parked: {parked_run_ids!r}"
    )
    assert "run-9108-early" not in parked_run_ids, (
        "the relayed approval must not stay parked; "
        f"parked: {parked_run_ids!r}"
    )


def test_late_ordinary_command_is_answered_by_the_next_round(
    sid, yolo_state, monkeypatch, runner_spy
):
    """The next round answers the late approval, and answers it once.

    This is the contract the enabling round leaves behind: the late approval
    is not relayed by the enable, it is answered when it is next delivered
    with session YOLO already enabled.
    """
    early = _run_approval(COMMON, run_id="run-9108-early")
    route_approvals.submit_gateway_pending_mirror(sid, dict(early))
    original_snapshot = routes.gateway_pending_mirrors
    state = {"published": False}
    late_payload = {}

    def snapshot_publishing_late_command(session_id):
        mirrors = original_snapshot(session_id)
        if not state["published"]:
            state["published"] = True
            late = _run_approval(COMMON, run_id="run-9108-late-ordinary")
            route_approvals.submit_gateway_pending_mirror(session_id, dict(late))
            late_payload["event"] = _gateway_event_payload(
                COMMON, prompt_id="prompt-9108-late"
            )
            late_payload["event"]["run_id"] = "run-9108-late-ordinary"
        return mirrors

    monkeypatch.setattr(routes, "gateway_pending_mirrors", snapshot_publishing_late_command)

    _payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")
    assert status == 200
    assert _relayed_run_ids(runner_spy) == ["run-9108-early"], runner_spy

    # The next round: the late approval is delivered with YOLO already on.
    relayed, _sink = _relay_gateway_event(
        sid, late_payload["event"], run_id="run-9108-late-ordinary"
    )

    assert relayed is True
    assert _relayed_run_ids(runner_spy) == [
        "run-9108-early",
        "run-9108-late-ordinary",
    ], runner_spy
    assert all(call["choice"] == "once" for call in runner_spy), runner_spy
    assert len(runner_spy) == 2, "the late approval must be answered exactly once"

    # Queue终态: the late approval was answered exactly once and is no longer
    # an outstanding question for the enable. (The auto-approve path does not
    # retire the local mirror -- that is the shipped behaviour of the
    # YOLO auto-answer, unchanged here -- so the queue is checked through the
    # remote answers, which are the answer of record.)
    assert len(runner_spy) == 2, "the late approval must be answered exactly once"
    assert _relayed_run_ids(runner_spy).count("run-9108-late-ordinary") == 1, (
        f"the late approval must be answered exactly once, got {runner_spy!r}"
    )
    assert runner_spy[1]["approval_id"] == "prompt-9108-late", runner_spy


# ── One prompt id, two generations: the markers must not travel ─────────────


def test_a_newer_generation_does_not_inherit_the_old_consent_markers(
    sid, yolo_state, runner_spy
):
    """A prompt id may describe a different command at a newer generation.

    The Gateway re-publishes one prompt id at a newer execution generation, and
    the newer generation can describe an ORDINARY command. The consent
    markers of the older generation must not be copied onto it: that would
    park an ordinary command for a manual answer under session YOLO.
    """
    yolo_state["enabled"].add(sid)

    # Generation 7: a consent prompt under this prompt id.
    relayed, _sink = _relay_gateway_event(
        sid,
        _gateway_event_payload(ELICITATION, prompt_id="prompt-9108", generation=7),
    )
    assert relayed is True
    assert runner_spy == [], "the consent prompt must not be answered"

    # Generation 9 re-uses the prompt id for an ordinary command. Its marker
    # is explicit: an unclassified description is parked for a manual answer
    # by design, so the projection has to carry the ordinary classification
    # for this test to isolate INHERITANCE rather than classification.
    newer = {
        **_gateway_projection_payload(COMMON, prompt_id="prompt-9108", generation=9),
        "pattern_key": "recursive delete",
        "pattern_keys": ["recursive delete"],
    }
    relayed, _sink = _relay_gateway_event(
        sid, newer, run_id="run-9108"
    )

    assert relayed is True
    assert len(runner_spy) == 1, (
        "the newer generation is an ordinary command and must stay answerable; "
        f"remote answers: {runner_spy!r}"
    )
    assert runner_spy[0]["choice"] == "once"


def test_the_same_generation_keeps_the_consent_markers(
    sid, yolo_state, runner_spy
):
    """The control: the SAME generation is the same prompt and stays protected."""
    yolo_state["enabled"].add(sid)

    relayed, _sink = _relay_gateway_event(
        sid,
        _gateway_event_payload(ELICITATION, prompt_id="prompt-9108", generation=7),
    )
    assert relayed is True
    assert runner_spy == []

    # The projection of the SAME prompt: no pattern key, same generation.
    relayed, _sink = _relay_gateway_event(
        sid,
        _gateway_projection_payload(ELICITATION, prompt_id="prompt-9108", generation=7),
    )

    assert relayed is True
    assert runner_spy == [], (
        "the same prompt must stay protected on the projection path; "
        f"remote answers: {runner_spy!r}"
    )


# ── #9251: an UNCLASSIFIED prompt must refuse the enable too ───────────────


def test_enabling_yolo_refuses_while_an_unclassified_approval_is_pending(sid, yolo_state):
    """A prompt with no pattern key and no surface cannot be classified, so YOLO
    must not pre-answer it — the whole enable is refused (#9251)."""
    entry = _park(sid, UNCLASSIFIED)
    route_approvals.submit_gateway_pending_mirror(sid, dict(entry.data))

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 409
    assert payload["ok"] is False
    assert payload["code"] == "gateway_unclassified_approval_pending"
    assert payload["error"]
    assert not entry.event.is_set()
    assert entry.result is None
    assert sid not in yolo_state["enabled"], "the enable must not commit"


@pytest.mark.parametrize(
    ("data", "expected"),
    [(COMMON, None), (ELICITATION, "protected"), (UNCLASSIFIED, "unclassified")],
    ids=["common", "protected", "unclassified"],
)
def test_pending_yolo_answer_block_names_the_reason(sid, data, expected):
    """The enable guard answers the same question the settle path does: may YOLO
    auto-answer this, and if not, why (#9251)."""
    _park(sid, data)
    assert routes._pending_yolo_answer_block(sid) is expected


def test_enabling_yolo_refuses_when_an_unclassified_run_mirror_arrives_mid_relay(
    sid, yolo_state, monkeypatch, runner_spy
):
    """An unclassified run mirror published between the enable snapshot and the
    first relay must refuse the whole enable, exactly like a protected one."""
    ordinary_run = _run_approval(COMMON, run_id="run-9251-ordinary")
    route_approvals.submit_gateway_pending_mirror(sid, dict(ordinary_run))
    original_snapshot = routes.gateway_pending_mirrors
    late = _run_approval(UNCLASSIFIED, run_id="run-9251-late")
    state = {"published": False}

    def snapshot_publishing_late_unclassified(session_id):
        mirrors = original_snapshot(session_id)
        if not state["published"]:
            state["published"] = True
            route_approvals.submit_gateway_pending_mirror(session_id, dict(late))
        return mirrors

    monkeypatch.setattr(routes, "gateway_pending_mirrors", snapshot_publishing_late_unclassified)

    payload, status = routes._enable_session_yolo_and_release_pending(sid, choice="once")

    assert status == 409
    assert payload["ok"] is False
    assert payload["code"] == "gateway_unclassified_approval_pending"
    assert runner_spy == [], "no approval may be relayed once an unclassified one is seen"
    assert sid not in yolo_state["enabled"]
    remaining = route_approvals.gateway_pending_mirrors(sid)
    assert any(not route_approvals.is_classified_approval(row) for row in remaining), (
        "the late unclassified mirror must stay parked"
    )


def test_local_settle_publishes_unclassified_approval_as_a_card(sid, yolo_state):
    """The settle path already refuses to answer an unclassified prompt; the
    enable guard must not be the only place that knows (#9251)."""
    yolo_state["enabled"].add(sid)
    entry = _park(sid, UNCLASSIFIED)

    auto_resolved, head, total = route_approvals.settle_gateway_pending_local_notification(
        sid, dict(entry.data)
    )

    assert auto_resolved is False
    assert head is not None
    assert total >= 1
    assert not entry.event.is_set()
    assert entry.result is None
