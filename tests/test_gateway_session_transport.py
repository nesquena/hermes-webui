"""The WebUI canonical adapter must preserve the Gateway's identity and ownership."""
import asyncio
from contextlib import asynccontextmanager
import sys
import threading
from types import ModuleType

import pytest

from api import gateway_sessions as sessions


@pytest.mark.parametrize("source", ["desktop", "local", "cli", "tui", "telegram", "discord"])
def test_external_sessions_can_select_existing_session_transport(source):
    shape = {"session_source": "other", "source_tag": source, "is_cli_session": False}
    assert sessions.configured(shape, {"webui_gateway_session_transport": "local"})
    assert not sessions.configured(shape, {})


@pytest.mark.parametrize("shape", [
    {"source_tag": "subagent"}, {"source_tag": "cron"}, {"source_tag": "claude_code"},
    {"source_tag": "unknown"}, {"source_tag": "api_server"},
    {"source_tag": "desktop", "session_source": "fork"},
])
def test_config_does_not_grant_unsupported_sources_write_access(shape):
    assert not sessions.configured(shape, {"webui_gateway_session_transport": "local"})


@pytest.mark.parametrize("stored", [None, "", "unknown"])
def test_messaging_without_model_accepts_implicit_browser_default(stored):
    from types import SimpleNamespace
    session = SimpleNamespace(model=stored)
    assert not sessions.model_change_requested(session, {"model": "profile-default"})
    assert sessions.model_change_requested(session, {"model": "picked", "explicit_model_pick": True})


def test_known_native_model_cannot_be_overridden():
    from types import SimpleNamespace
    session = SimpleNamespace(model="gateway-model")
    assert not sessions.model_change_requested(session, {"model": "gateway-model"})
    assert sessions.model_change_requested(session, {"model": "different-model"})


def test_unchanged_cross_provider_selection_is_not_a_model_change():
    from types import SimpleNamespace
    session = SimpleNamespace(model="gateway-model", model_provider="native-provider")
    body = {"model": "gateway-model", "model_provider": "native-provider", "explicit_model_pick": True}
    assert not sessions.model_change_requested(session, body)
    assert sessions.model_change_requested(session, {**body, "model_provider": "another-provider"})


def test_default_profile_uses_its_config(monkeypatch):
    from api import config, profiles
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: name)
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda home, **_kw:
        {"webui_gateway_session_transport": "local"} if home == "default" else {})
    assert sessions.configured({"profile": "default", "source_tag": "desktop"})
    assert not sessions.configured({"profile": "other", "source_tag": "desktop"})


@pytest.fixture
def protocol(monkeypatch):
    module = ModuleType("hermes_cli.gateway_client")
    class Refused(Exception):
        pass
    module.GatewayRPCError = Refused
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_client", module)
    return Refused


class Client:
    def __init__(self, *, lose_ack=False, refusal=None):
        self.calls, self.events = [], asyncio.Queue()
        self.lose_ack, self.refusal = lose_ack, refusal
        self.status = "terminal"
        self.result = {"final_response": "original session reply"}
        self.generation = 3

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == "session.resume":
            if self.refusal:
                raise self.refusal
            return {"session_id": "same-session", "prompts": []}
        if method == "prompt.submit" and self.lose_ack:
            raise ConnectionError("lost acknowledgment")
        if method in {"prompt.submit", "prompt.receipt"}:
            return {"admission_id": "one-admission", "status": self.status,
                    "execution_generation": self.generation, "outcome": "completed", "result": self.result}
        if method == "session.events.since":
            return {"events": []}
        return {"status": "resolved"}


def relay():
    events, records = [], []
    request = {"session_id": "same-session", "input_id": "webui:stable", "text": "follow-up"}
    instance = sessions.SessionRelay("same-session", "stable", "profile-home", request,
                                     lambda *args: events.append(args), threading.Event(),
                                     lambda *args: records.append(args))
    return instance, events, records


def test_lost_ack_retries_exact_request_and_durable_result(protocol, monkeypatch):
    first, second = Client(lose_ack=True), Client()
    peers = iter([first, second])
    @asynccontextmanager
    async def connect(home):
        assert home == "profile-home"
        yield next(peers)
    monkeypatch.setattr(sessions, "connect_local", connect)
    instance, events, records = relay()
    assert asyncio.run(instance.run()) == ("original session reply", {})
    assert first.calls[0][0] == second.calls[0][0] == "session.resume"
    assert first.calls[1] == second.calls[1] == ("prompt.submit", instance.request)
    assert records[0] == ("", instance.request)  # persisted before either send
    assert records[-1] == ("session:stable", instance.request)
    assert "session:stable" not in sessions._CONNECTIONS
    assert all("source" not in params for _, params in first.calls + second.calls)


def test_authority_refusal_is_terminal_without_submit_or_http_fallback(protocol, monkeypatch):
    client = Client(refusal=protocol("permission_denied"))
    @asynccontextmanager
    async def connect(home):
        yield client
    monkeypatch.setattr(sessions, "connect_local", connect)
    instance, _, _ = relay()
    with pytest.raises(sessions.SessionTransportError, match="permission_denied"):
        asyncio.run(instance.run())
    assert [method for method, _ in client.calls] == ["session.resume"]
    assert "session:stable" not in sessions._CONNECTIONS


def test_disconnect_keeps_recovering_past_old_retry_limit(protocol, monkeypatch):
    """A disconnect AFTER a submission keeps recovering past the old retry limit.

    #8905 changed when the relay gives up: a failure BEFORE the first
    submission is terminal (nothing was sent, so there is nothing to
    recover). This test keeps its original subject — the recovery path has
    no attempt ceiling, so a turn that WAS admitted still comes back after
    more reconnects than the old limit allowed — but it now disconnects
    after the submission was attempted, which is the state the recovery
    contract is about.

    The old shape (five connect failures BEFORE any submission) asserted the
    opposite contract, so it is rewritten here rather than deleted: the test
    body, the leak-safe error text and the journal assertions all stay.
    """
    attempts = []
    first, second = Client(lose_ack=True), Client()
    peers = iter([first, None, None, None, None, second])

    @asynccontextmanager
    async def connect(home):
        attempts.append(home)
        peer = next(peers)
        if peer is None:
            raise ConnectionError("do not leak ws://***@host")
        yield peer

    monkeypatch.setattr(sessions, "connect_local", connect)

    async def no_delay(_):
        pass

    monkeypatch.setattr(sessions.asyncio, "sleep", no_delay)
    instance, _, records = relay()
    assert asyncio.run(instance.run()) == ("original session reply", {})
    assert len(attempts) == 6
    assert records[0] == ("", instance.request)
    assert "session:stable" not in sessions._CONNECTIONS


@pytest.mark.parametrize("failure", ["discovery", "refusal"])
def test_lost_ack_then_connection_failure_keeps_original_admission(protocol, monkeypatch, failure):
    first, final = Client(lose_ack=True), Client()
    peers = iter([first, None, final])
    @asynccontextmanager
    async def connect(home):
        peer = next(peers)
        if peer is None:
            if failure == "discovery":
                raise sessions.SessionTransportError("Gateway temporarily unavailable")
            yield Client(refusal=protocol("permission_denied"))
        else:
            yield peer
    monkeypatch.setattr(sessions, "connect_local", connect)
    async def no_delay(_):
        pass
    monkeypatch.setattr(sessions.asyncio, "sleep", no_delay)
    instance, _, _ = relay()
    assert asyncio.run(instance.run()) == ("original session reply", {})
    assert first.calls[1] == final.calls[1] == ("prompt.submit", instance.request)


@pytest.mark.parametrize("is_error", [True, False])
def test_canonical_tool_verdict_reaches_event_and_reconnect_snapshot(monkeypatch, is_error):
    from api import gateway_chat
    snapshots = []
    monkeypatch.setattr(gateway_chat, "_note_live_gateway_event", lambda *args: snapshots.append(args))
    instance, events, _ = relay()
    instance.receipt = {"admission_id": "mine"}
    instance.event({"session_id": instance.session_id, "admission_id": "mine", "type": "tool.complete",
                    "payload": {"tool_call_id": "tool-1", "name": "terminal", "is_error": is_error}})
    assert events[-1][1]["is_error"] is is_error
    assert snapshots[-1][2]["is_error"] is is_error


@pytest.mark.parametrize("batch", [True, False])
def test_clarification_control_matches_canonical_parameter_shape(monkeypatch, batch):
    from api import clarify
    monkeypatch.setattr(clarify, "submit_pending", lambda *args: None)
    instance, _, _ = relay()
    client = Client()
    client.status = "started"
    instance.client, instance.receipt = client, {"admission_id": "one-admission"}
    if batch:
        instance.clarify({"questions": [{"qid": "q1", "question": "Continue?"}]}, request_id="request-1")
        expected = ("clarify.lock", {"request_id": "request-1", "question_id": "q1", "answer": "yes"})
    else:
        instance.clarify({"prompt_id": "q1", "question": "Continue?", "execution_generation": 3})
        expected = ("clarify.respond", {"session_id": instance.session_id, "prompt_id": "q1",
                                       "execution_generation": 3, "answer": "yes"})
    asyncio.run(instance.control("clarify", clarify_id=next(iter(instance.clarifications)), answer="yes"))
    assert client.calls[-1] == expected


def test_only_own_admission_is_relayed_and_replayed_deltas_are_deduplicated(monkeypatch):
    from api import gateway_chat
    monkeypatch.setattr(gateway_chat, "_append_gateway_stream_text", lambda _, old, delta: old + delta)
    instance, events, _ = relay()
    instance.receipt = {"admission_id": "mine"}
    def frame(seq, owner, text):
        return {"session_id": "same-session", "admission_id": owner, "type": "message.delta",
                "seq": seq, "replay_epoch": "epoch", "payload": {"text": text}}
    instance.event(frame(1, "another-client", "foreign"))
    instance.event(frame(2, "mine", "answer"))
    instance.event(frame(2, "mine", "answer"))
    assert events == [("token", {"text": "answer"})]
    assert instance.text == "answer"


@pytest.mark.parametrize("status,verb", [("queued", "prompt.cancel"), ("started", "session.interrupt")])
def test_stop_targets_only_owned_admission_or_generation(status, verb):
    instance, _, _ = relay()
    client = Client()
    client.status = status
    instance.client, instance.receipt = client, {"admission_id": "one-admission"}
    assert asyncio.run(instance.control("stop"))
    method, params = client.calls[-1]
    assert method == verb
    assert params["session_id"] == "same-session"
    assert params.get("admission_id") == ("one-admission" if status == "queued" else None)
    assert params.get("execution_generation") == (3 if status == "started" else None)


def test_stale_approval_cannot_approve_new_generation():
    instance, _, _ = relay()
    client = Client()
    client.status = "started"
    instance.client, instance.receipt = client, {"admission_id": "one-admission"}
    instance.approvals["old-prompt"] = 2
    with pytest.raises(sessions.SessionTransportError, match="no longer pending"):
        asyncio.run(instance.control("approval", approval_id="old-prompt", choice="once"))
    assert all(method != "approval.respond" for method, _ in client.calls)


def test_recovered_cancel_reconciles_saved_admission_before_interrupt(protocol, monkeypatch):
    class Running(Client):
        async def rpc(self, method, **params):
            result = await super().rpc(method, **params)
            if method == "session.interrupt":
                self.status = "terminal"
            if method == "prompt.receipt" and self.status == "terminal":
                result["outcome"] = "interrupted"
            return result
    client = Running()
    client.status = "started"
    @asynccontextmanager
    async def connect(home):
        yield client
    monkeypatch.setattr(sessions, "connect_local", connect)
    instance, _, _ = relay()
    instance.attempted = True  # restored durable request may already be admitted
    instance.cancel.set()
    assert asyncio.run(instance.run()) == (None, {})
    assert any(method == "prompt.submit" and params == instance.request for method, params in client.calls)
    assert ("session.interrupt", {"session_id": instance.session_id, "execution_generation": 3}) in client.calls


def test_display_projection_keeps_native_identity_without_inventing_merge_id():
    rows = sessions.display_history({"display_messages": [
        {"role": "user", "text": "question", "row_id": 7, "message_uid": "native", "timestamp": 1},
        {"role": "assistant", "text": "answer", "row_id": 8},
        {"role": "tool", "content": "private tool result"},
    ]})
    assert [row["content"] for row in rows] == ["question", "answer"]
    assert rows[0]["row_id"] == 7 and rows[0]["timestamp"] == 1
    assert "id" not in rows[0]  # state.db cold reads do not synthesize this either


def test_distinct_clarification_ids_do_not_collapse_equal_questions():
    from api import clarify
    sid = "canonical-clarify-identity-test"
    try:
        first = clarify.submit_pending(sid, {"clarify_id": "first", "question": "Which one?"})
        second = clarify.submit_pending(sid, {"clarify_id": "second", "question": "Which one?"})
        assert first is not second
        assert clarify.submit_pending(sid, {"clarify_id": "second", "question": "Which one?"}) is second
        assert clarify.resolve_clarify_by_id(sid, "first", "A")
        assert not second.event.is_set()
    finally:
        clarify.clear_pending(sid)


def test_clarify_event_carries_generation_and_rejects_stale_response(monkeypatch):
    from api import clarify
    monkeypatch.setattr(clarify, "submit_pending", lambda *args: None)
    instance, _, _ = relay()
    client = Client()
    client.status = "started"
    instance.client, instance.receipt = client, {"admission_id": "one-admission"}
    instance.event({"session_id": instance.session_id, "admission_id": "one-admission",
                    "type": "clarify.request", "execution_generation": 2,
                    "payload": {"prompt_id": "question", "question": "Continue?"}})
    local_id = next(iter(instance.clarifications))
    with pytest.raises(sessions.SessionTransportError, match="no longer pending"):
        asyncio.run(instance.control("clarify", clarify_id=local_id, answer="yes"))
    assert all(method != "clarify.respond" for method, _ in client.calls)


def test_restart_refuses_changed_profile_home(monkeypatch):
    from api import profiles
    from types import SimpleNamespace
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _: "new-profile")
    with pytest.raises(sessions.SessionTransportError, match="profile changed"):
        sessions.run_session(SimpleNamespace(profile="default"), "stream", "text",
            emit=None, cancel=None, record=None, saved={"profile_home": "old-profile"})


def test_canonical_auto_approval_uses_exact_socket_owner(monkeypatch):
    from api import gateway_chat
    calls = []
    monkeypatch.setattr(sessions, "control", lambda *args, **kwargs: calls.append((args, kwargs)))
    gateway_chat._auto_approve_gateway_run("", "", "session:stable", "prompt-1")
    assert calls == [(("session:stable", "approval"), {"approval_id": "prompt-1", "choice": "once"})]


def test_approval_reuses_existing_yolo_handoff_and_mirror(monkeypatch):
    from api import gateway_chat
    calls = []
    def settle(sid, data, base, key, **kwargs):
        calls.append((sid, data, kwargs))
        return True, None, 0
    monkeypatch.setattr(gateway_chat, "_settle_gateway_run_approval", settle)
    instance, events, _ = relay()
    instance.approval({"prompt_id": "prompt-1", "command": "example"}, 3)
    assert not events  # auto-settled prompt must not show an actionable card
    assert calls[0][1]["run_id"] == instance.run_id
    assert calls[0][1]["approval_id"] == "prompt-1"
    assert calls[0][2]["cancel_event"] is instance.cancel  # Stop-aware publication


@pytest.mark.parametrize("stopped_when,yolo", [
    ("before_event", False), ("before_event", True), ("during_publication", False),
])
def test_native_approval_after_stop_publishes_no_card_and_sends_nothing(monkeypatch, stopped_when, yolo):
    """A native approval that arrives after Stop must stay dead (#8634 on the session transport).

    The relay keeps reading its socket until the Gateway confirms the interrupt,
    so an approval can still arrive. Before the fix it recreated the card and a
    click relayed ``approval.respond``; now no card exists, the click is refused
    and nothing reaches the Gateway, with or without session YOLO.
    """
    from unittest.mock import MagicMock

    from api import gateway_chat, route_approvals, routes

    instance, events, _ = relay()
    sid = instance.session_id
    client = Client()
    client.status = "started"
    instance.client, instance.receipt = client, {"admission_id": "one-admission"}
    monkeypatch.setattr(gateway_chat, "_gateway_session_yolo_enabled", lambda _: yolo)
    auto = MagicMock(side_effect=AssertionError("a stopped run must not be auto-approved"))
    monkeypatch.setattr(gateway_chat, "_auto_approve_gateway_run", auto)
    if stopped_when == "before_event":
        instance.cancel.set()
    else:
        # Stop lands while the card is being published: the registry-lock recheck
        # (or the post-publication retirement) must leave no card behind.
        real_submit = route_approvals.submit_gateway_pending_mirror

        def submit(session_key, approval, *, cancel_event=None):
            instance.cancel.set()
            return real_submit(session_key, approval, cancel_event=cancel_event)

        monkeypatch.setattr(route_approvals, "submit_gateway_pending_mirror", submit)
    replies = []
    monkeypatch.setattr(routes, "j", lambda h, data, status=200, **kw: replies.append((status, data)))
    try:
        instance.approval({"prompt_id": "prompt-1", "command": "example", "choices": ["once", "deny"]}, 3)
        assert route_approvals.gateway_pending_mirror(sid, run_id=instance.run_id) is None
        assert not [e for e in events if e and e[0] == "approval"]
        auto.assert_not_called()
        routes._handle_approval_respond(MagicMock(), {
            "session_id": sid, "run_id": instance.run_id, "approval_id": "prompt-1",
            "mirror_token": "stale-token", "choice": "once",
        })
        assert replies and replies[0][0] == 409 and replies[0][1].get("ok") is not True
        assert all(method != "approval.respond" for method, _ in client.calls)
    finally:
        with route_approvals._lock:
            route_approvals._pending.pop(sid, None)
            route_approvals._gateway_queues.pop(sid, None)


@pytest.mark.parametrize("profile", ["default", "josue", "tomo", "tristan"])
@pytest.mark.parametrize("outcome", ["accepted", "stale", "disconnected", "not_pending"])
@pytest.mark.parametrize("choice", ["once", "deny"])
def test_native_approval_http_response_keeps_transport_owner(
    monkeypatch, tmp_path, profile, outcome, choice,
):
    """REST generation support must not redirect native controls to Runs API."""
    from unittest.mock import MagicMock

    from api import gateway_chat, route_approvals, routes, runner_client

    sid = f"native-approval-{profile}"
    instance = sessions.SessionRelay(
        sid, f"request-{profile}", str(tmp_path / profile),
        {"session_id": sid}, lambda *args: None, threading.Event(), lambda *args: None,
    )
    client = Client()
    client.status = "started"
    instance.client = client
    instance.receipt = {"admission_id": f"admission-{profile}"}
    monkeypatch.setattr(gateway_chat, "_gateway_session_yolo_enabled", lambda _: False)
    rest = MagicMock(side_effect=AssertionError("native control must not use REST"))
    monkeypatch.setattr(runner_client, "HttpRunnerClient", rest)
    refresh = MagicMock(side_effect=AssertionError("native control must not refresh a REST run"))
    monkeypatch.setattr(routes, "_gateway_run_prompt", refresh)
    control_calls = []

    def control(run_id, action, **params):
        control_calls.append((run_id, action, params))
        assert run_id == instance.run_id
        assert instance.home == str(tmp_path / profile)
        if outcome == "not_pending":
            raise runner_client.RunnerClientError("No longer pending", code="approval_not_pending")
        return asyncio.run(instance.control(action, **params))

    monkeypatch.setattr(sessions, "control", control)
    replies = []
    monkeypatch.setattr(routes, "j", lambda h, data, status=200, **kw: replies.append((status, data)))
    try:
        instance.approval({"prompt_id": "prompt-1", "command": "example", "choices": ["once", "deny"]}, 3)
        mirror = route_approvals.gateway_pending_mirror(sid, run_id=instance.run_id)
        assert mirror is not None
        if outcome == "stale":
            client.generation = 4
        elif outcome == "disconnected":
            instance.client = None
        routes._handle_approval_respond(MagicMock(), {
            "session_id": sid, "run_id": instance.run_id, "approval_id": "prompt-1",
            "mirror_token": mirror[route_approvals._GATEWAY_MIRROR_TOKEN], "choice": choice,
        })
        assert control_calls == [(instance.run_id, "approval", {"approval_id": "prompt-1", "choice": choice})]
        rest.assert_not_called()
        refresh.assert_not_called()
        answers = [params for method, params in client.calls if method == "approval.respond"]
        pending = route_approvals.gateway_pending_mirror(sid, run_id=instance.run_id)
        if outcome == "accepted":
            assert replies[0][0] == 200 and replies[0][1]["ok"] is True
            assert answers == [{"session_id": sid, "execution_generation": 3,
                                "prompt_id": "prompt-1", "choice": choice}]
            assert pending is None
        else:
            assert replies[0][0] == 502 and replies[0][1]["ok"] is False
            assert not answers
            assert pending is not None
    finally:
        with route_approvals._lock:
            route_approvals._pending.pop(sid, None)
            route_approvals._gateway_queues.pop(sid, None)


# ── #8905: a missing transport socket must not reconnect forever ─────────────
#
# The native Gateway session transport connects through `connector()`. When the
# `unix` socket path does not exist, the connector raises BEFORE any
# `prompt.submit`, so the relay never learns whether the turn was admitted.
# The generic `except Exception:` in `SessionRelay.run` used to swallow that
# error and fall through to the reconnect loop, so the turn retried forever
# (every <=5s) and Stop could not end it: `control()` raised "Gateway session is
# reconnecting." because no client was attached.
#
# The rule: a failure BEFORE the first submission is terminal (nothing was
# sent, so there is nothing to recover). A failure AFTER a submission keeps the
# existing indefinite recovery, because the admission is then uncertain.


class _RetryLimitExceeded(BaseException):
    """Raised when the relay dials the transport more often than allowed.

    Derives from BaseException on purpose: `SessionRelay.run` swallows bare
    `Exception`, so an `AssertionError` guard would be absorbed by the very
    reconnect loop this test is meant to catch, and the test would hang
    instead of failing.
    """


class _FailingConnector:
    """A transport connector that raises before any client is attached.

    The attempt counter is bounded so a regression that reconnects forever
    fails the test instead of hanging the suite.
    """

    def __init__(self, attempts, error, limit=5):
        self.attempts = attempts
        self.error = error
        self.limit = limit

    def __call__(self):
        return self

    async def __aenter__(self):
        self.attempts.append(self)
        if len(self.attempts) > self.limit:
            raise _RetryLimitExceeded(
                f"the transport was dialled {len(self.attempts)} times; "
                "a first-connect failure must not be retried"
            )
        raise self.error

    async def __aexit__(self, *exc_info):
        return False


def failing_connector(attempts, error, limit=5):
    return _FailingConnector(attempts, error, limit)


def test_missing_transport_socket_fails_terminally_instead_of_reconnecting(protocol, monkeypatch):
    """A connector that cannot reach the transport ends the turn; it does not retry."""
    attempts = []
    instance, _, _ = relay()
    instance.connector = failing_connector(attempts, FileNotFoundError(2, "No such file or directory"))
    with pytest.raises(sessions.SessionTransportError) as excinfo:
        asyncio.run(instance.run())
    message = str(excinfo.value)
    assert "transport is unavailable" in message, (
        f"the failure must name the transport as unavailable, got: {message!r}"
    )
    assert "not submitted" in message, (
        f"the failure must say the turn was never submitted, got: {message!r}"
    )
    assert len(attempts) == 1, (
        f"the relay retried {len(attempts)} times after a terminal connect failure; "
        "it must stop at the first failure"
    )
    assert "session:stable" not in sessions._CONNECTIONS


def test_first_connect_failure_never_submits_or_records_an_admission(protocol, monkeypatch):
    """Nothing was submitted, so the durable request must not be journaled as sent."""
    attempts = []
    instance, _, records = relay()
    instance.connector = failing_connector(attempts, FileNotFoundError(2, "No such file or directory"))
    with pytest.raises(sessions.SessionTransportError):
        asyncio.run(instance.run())
    assert records == [], f"no submission happened, so nothing may be journaled: {records!r}"


def test_failure_after_a_submission_keeps_recovering(protocol, monkeypatch):
    """Control: once a submission was attempted, the indefinite recovery stays.

    The shape mirrors the existing lost-ack test: the FIRST connection
    succeeds and `prompt.submit` is attempted; the LOST acknowledgment is what
    makes the admission uncertain, so the relay must keep reconnecting instead
    of ending the turn.
    """
    attempts = []
    first, second = Client(lose_ack=True), Client()
    peers = iter([first, second])

    @asynccontextmanager
    async def connect(home):
        attempts.append(home)
        if len(attempts) > 8:
            raise _RetryLimitExceeded(
                f"the transport was dialled {len(attempts)} times; "
                "the recovery loop must stop once the turn completes"
            )
        peer = next(peers)
        yield peer

    monkeypatch.setattr(sessions, "connect_local", connect)

    async def no_delay(_):
        pass

    monkeypatch.setattr(sessions.asyncio, "sleep", no_delay)

    instance, _, _ = relay()
    assert asyncio.run(instance.run()) == ("original session reply", {})
    assert len(attempts) == 2, (
        f"expected the recovery loop to keep retrying after a submission, got {len(attempts)} attempts"
    )
    assert any(method == "prompt.submit" for method, _ in first.calls), (
        "the control must really have attempted a submission"
    )


def test_stop_before_any_submission_ends_the_turn_locally(protocol, monkeypatch):
    """Stop while nothing was submitted must not raise 'Gateway session is reconnecting.'."""
    attempts = []
    instance, _, _ = relay()
    instance.connector = failing_connector(attempts, FileNotFoundError(2, "No such file or directory"))
    instance.cancel.set()
    assert asyncio.run(instance.run()) == (None, {}), (
        "a cancelled turn that never reached the Gateway must end locally"
    )
    assert len(attempts) == 1, (
        f"the relay retried {len(attempts)} times for a cancelled turn; "
        "it must stop at the first failure"
    )


def test_stop_ends_an_unsubmitted_turn_locally_instead_of_raising(protocol, monkeypatch):
    """The Stop path must not raise 'Gateway session is reconnecting.' for a turn that was never sent."""
    instance, _, _ = relay()
    assert instance.client is None and instance.receipt is None and instance.attempted is False
    assert asyncio.run(instance.control("stop")) is True
    assert instance.cancel.is_set(), "Stop must cancel the turn locally"


def test_stop_still_refuses_after_a_submission(protocol, monkeypatch):
    """Control: an uncertain admission keeps the previous contract — the caller retries after reconnect."""
    instance, _, _ = relay()
    instance.attempted = True
    with pytest.raises(sessions.SessionTransportError, match="reconnecting"):
        asyncio.run(instance.control("stop"))
    assert not instance.cancel.is_set()


# ── #9227: the public Stop path must reach an unsubmitted session turn ───────
#
# `SessionRelay.run` used to register itself in `_CONNECTIONS` and publish its
# WebUI run id only AFTER `prompt.submit` returned. The browser's Stop goes
# through `/api/chat/cancel`, which waits for that published run id; before the
# first submission there is nothing to wait for, so the route timed out and
# returned 502 without setting the cancel flag. The turn then submitted
# normally once the transport reconnected, even though the user had pressed
# Stop. The relay now registers under its deterministic run id before the
# first dial, and the cancel route resolves that id first, so the SAME public
# Stop path reaches it. A submission that was already attempted stays
# uncertain on purpose: Stop must not treat it as never submitted, must not
# clear its recovery state, and must not submit twice.

class _HeldConnector:
    """A transport connector whose first dial parks until the test releases it."""

    def __init__(self, client, entered, release):
        self.client, self.entered, self.release = client, entered, release

    def __call__(self):
        return self

    async def __aenter__(self):
        await asyncio.to_thread(self.entered.set)
        await asyncio.to_thread(self.release.wait, 5)
        return self.client

    async def __aexit__(self, *exc_info):
        return False


def test_cancel_route_cancels_an_unsubmitted_session_turn(protocol, monkeypatch):
    """A Stop during the first connect must cancel the turn, not submit it.

    Drives the REAL `/api/chat/cancel` route against a REAL relay whose fake
    transport is parked before `prompt.submit`. This is the browser's path: it
    never calls `SessionRelay.control("stop")` directly.
    """
    import urllib.parse

    from api import gateway_chat, routes

    stream_id = "stable"
    instance, events, records = relay()
    client = Client()
    entered, release = threading.Event(), threading.Event()
    instance.connector = _HeldConnector(client, entered, release)
    outcome = {}

    def run_relay():
        try:
            outcome["value"] = asyncio.run(instance.run())
        except BaseException as exc:  # recorded so a failure cannot hang the test
            outcome["error"] = exc

    worker = threading.Thread(target=run_relay, daemon=True)
    worker.start()
    assert entered.wait(timeout=5), "the relay never dialled the transport"

    # The Gateway worker marks its run `pending` before the session transport
    # publishes a run id. This is the exact state a pre-submission Stop lands in.
    gateway_chat._mark_gateway_run_starting(stream_id)
    replies = []
    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "j", lambda _handler, data, status=200, **_kw: replies.append((status, data)))
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: True)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda *_args, **_kw: False)
    # Baseline path (no deterministic resolution) waits this long for a run id
    # that cannot exist before the submission; keep the RED run short.
    monkeypatch.setattr(gateway_chat, "GATEWAY_RUN_ID_WAIT_TIMEOUT", 0.05)
    try:
        routes.handle_get(object(), urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}"))

        assert replies, "the cancel route produced no response"
        status, payload = replies[0]
        assert status == 200, (
            f"Stop before the first submission must succeed, got {status}: {payload}"
        )
        assert payload.get("ok") is True and payload.get("cancelled") is True, payload
        assert instance.cancel.is_set(), (
            "the public Stop path must set the turn's cancel flag before it is submitted"
        )
    finally:
        release.set()
        worker.join(timeout=5)
        gateway_chat._STREAM_RUN_LIFECYCLE.pop(stream_id, None)
        gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)

    assert not worker.is_alive(), "the relay did not finish after the transport was released"
    assert "error" not in outcome, outcome.get("error")
    assert outcome.get("value") == (None, {}), (
        f"a cancelled, never-submitted turn must end locally, got {outcome.get('value')!r}"
    )
    assert [method for method, _ in client.calls if method == "prompt.submit"] == [], (
        "a stopped turn must never be submitted"
    )
    assert records == [], f"an unsubmitted turn must leave no admission record: {records!r}"
    assert "session:stable" not in sessions._CONNECTIONS


class _SubmitParkedClient(Client):
    """A client whose `prompt.submit` was attempted but whose receipt is unknown."""

    def __init__(self):
        super().__init__()
        self.submit_entered = threading.Event()
        self.submit_release = threading.Event()

    async def rpc(self, method, **params):
        if method == "prompt.submit":
            self.calls.append((method, params))
            await asyncio.to_thread(self.submit_entered.set)
            await asyncio.to_thread(self.submit_release.wait, 5)
            return {"admission_id": "one-admission", "status": "terminal",
                    "execution_generation": self.generation, "outcome": "completed",
                    "result": self.result}
        return await super().rpc(method, **params)


def test_cancel_route_keeps_an_uncertain_submission_recoverable(protocol, monkeypatch):
    """A Stop after a submission was attempted must not be treated as never sent.

    `prompt.submit` is parked: the turn was attempted, its acknowledgement is
    unknown. The cancel route must keep the existing contract (502, retry after
    reconnect), must NOT set the local cancel flag, must keep the durable
    request, and must not submit a second time.
    """
    import urllib.parse

    from api import gateway_chat, routes

    stream_id = "stable"
    instance, events, records = relay()
    client = _SubmitParkedClient()

    @asynccontextmanager
    async def connect():
        yield client

    instance.connector = connect
    outcome = {}

    def run_relay():
        try:
            outcome["value"] = asyncio.run(instance.run())
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run_relay, daemon=True)
    worker.start()
    assert client.submit_entered.wait(timeout=5), "the relay never attempted the submission"
    assert instance.attempted is True and instance.receipt is None

    gateway_chat._mark_gateway_run_starting(stream_id)
    replies = []
    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "j", lambda _handler, data, status=200, **_kw: replies.append((status, data)))
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: True)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda *_args, **_kw: False)
    monkeypatch.setattr(gateway_chat, "GATEWAY_RUN_ID_WAIT_TIMEOUT", 0.05)
    try:
        routes.handle_get(object(), urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}"))

        assert replies, "the cancel route produced no response"
        status, payload = replies[0]
        assert status == 502, (
            f"an uncertain submission must keep the retry contract, got {status}: {payload}"
        )
        assert payload.get("ok") is False and payload.get("cancelled") is False, payload
        assert not instance.cancel.is_set(), (
            "an attempted submission must not be cancelled locally as if it never happened"
        )
        assert records and records[0] == ("", instance.request), (
            f"the durable request must survive for recovery: {records!r}"
        )
        assert len([m for m, _ in client.calls if m == "prompt.submit"]) == 1
    finally:
        client.submit_release.set()
        worker.join(timeout=5)
        gateway_chat._STREAM_RUN_LIFECYCLE.pop(stream_id, None)
        gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)

    assert not worker.is_alive(), "the relay did not finish after the submission was released"
    assert "error" not in outcome, outcome.get("error")
    assert outcome.get("value") == ("original session reply", {}), outcome.get("value")
    assert len([m for m, _ in client.calls if m == "prompt.submit"]) == 1, (
        "a stopped-but-uncertain turn must not be submitted twice"
    )


class _ParkedAckClient:
    """A real RPC peer whose submission acknowledgement is held until released.

    `prompt.submit` parks on `submit_release`, then admits the turn as `started`
    so a Stop that waited for the acknowledgement interrupts it. The interrupt
    settles the receipt as terminal/interrupted, which lets the relay finish.
    """

    def __init__(self, release_delay=0.0):
        self.calls, self.events = [], asyncio.Queue()
        self.release_delay = release_delay
        self.status = "started"
        self.generation = 3
        self.result = {"final_response": "original session reply"}
        self.submit_entered = threading.Event()
        self.submit_release = threading.Event()

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == "session.resume":
            return {"session_id": "same-session", "prompts": []}
        if method == "prompt.submit":
            await asyncio.to_thread(self.submit_entered.set)
            await asyncio.to_thread(self.submit_release.wait, 5)
            if self.release_delay:
                await asyncio.to_thread(threading.Event().wait, self.release_delay)
            return {"admission_id": "one-admission", "status": "started",
                    "execution_generation": self.generation}
        if method == "prompt.receipt":
            return {"admission_id": "one-admission", "status": self.status,
                    "execution_generation": self.generation,
                    "outcome": "interrupted" if self.status == "terminal" else None,
                    "result": self.result}
        if method == "session.interrupt":
            self.status = "terminal"
            return {"status": "ok"}
        return {"status": "resolved"}


def _drive_cancel_route(instance, client, stream_id, monkeypatch, *, wait_timeout):
    """Run a REAL relay against `client` and drive the REAL cancel route in a thread.

    Returns `(replies, worker, outcome, waiting, route, route_done)`. `waiting` is
    set as soon as the route enters the bounded published-run-ID wait, so a test
    releases the acknowledgement only once the route can observe it — never on a
    timing assumption about how fast the route reaches the wait.
    """
    import urllib.parse

    from api import gateway_chat, routes

    @asynccontextmanager
    async def connect():
        yield client

    instance.connector = connect
    outcome = {}

    def run_relay():
        try:
            outcome["value"] = asyncio.run(instance.run())
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run_relay, daemon=True)
    worker.start()
    assert client.submit_entered.wait(timeout=5), "the relay never attempted the submission"
    assert instance.attempted is True and instance.receipt is None

    gateway_chat._mark_gateway_run_starting(stream_id)
    replies = []
    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "j", lambda _handler, data, status=200, **_kw: replies.append((status, data)))
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: True)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda *_args, **_kw: False)
    monkeypatch.setattr(gateway_chat, "GATEWAY_RUN_ID_WAIT_TIMEOUT", wait_timeout)

    waiting, route_done = threading.Event(), threading.Event()
    real_wait = gateway_chat.wait_for_gateway_run_id

    def spying_wait(stream, timeout):
        waiting.set()
        return real_wait(stream, timeout)

    monkeypatch.setattr(gateway_chat, "wait_for_gateway_run_id", spying_wait)

    def run_route():
        try:
            routes.handle_get(object(), urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}"))
        finally:
            route_done.set()

    route = threading.Thread(target=run_route, daemon=True)
    route.start()
    return replies, worker, outcome, waiting, route, route_done


def _settle_relay(client, worker, route, stream_id):
    """Join both threads and drop the stream lifecycle rows."""
    from api import gateway_chat

    route.join(timeout=10)
    if isinstance(client, _ParkedAckClient):
        client.status = "terminal"
    worker.join(timeout=10)
    gateway_chat._STREAM_RUN_LIFECYCLE.pop(stream_id, None)
    gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)


@pytest.mark.parametrize("release_delay", [0.0, 0.05])
def test_cancel_route_interrupts_a_submission_acknowledged_inside_the_wait(
    protocol, monkeypatch, release_delay,
):
    """A Stop during a pending acknowledgement must still interrupt, exactly once.

    The acknowledgement is released only after the route has entered its bounded
    wait, so the inside-budget path is exercised deterministically: 200, exactly
    one `session.interrupt`, exactly one `prompt.submit`.
    """
    stream_id = "stable"
    instance, _events, _records = relay()
    client = _ParkedAckClient(release_delay)

    replies, worker, outcome, waiting, route, route_done = _drive_cancel_route(
        instance, client, stream_id, monkeypatch, wait_timeout=1.0,
    )
    try:
        if not waiting.wait(timeout=5):
            # A head that never enters the bounded wait still has to answer the
            # Stop; let the status assertion below report which answer it gave.
            assert route_done.wait(timeout=5), "the cancel route neither waited nor finished"
        client.submit_release.set()
        assert route_done.wait(timeout=10), "the cancel route did not finish"
    finally:
        _settle_relay(client, worker, route, stream_id)

    assert replies, "the cancel route produced no response"
    status, payload = replies[0]
    assert status == 200, f"a Stop inside the wait budget must interrupt, got {status}: {payload}"
    assert payload.get("ok") is True and payload.get("cancelled") is True, payload
    interrupts = [method for method, _ in client.calls if method == "session.interrupt"]
    assert interrupts == ["session.interrupt"], f"expected exactly one interrupt, got {interrupts!r}"
    assert len([method for method, _ in client.calls if method == "prompt.submit"]) == 1
    assert not worker.is_alive(), "the relay did not finish after the interrupt"
    assert "error" not in outcome, outcome.get("error")
    assert outcome.get("value") == (None, {}), outcome.get("value")


def test_cancel_route_keeps_recovery_when_acknowledgement_misses_the_wait(
    protocol, monkeypatch,
):
    """An acknowledgement that never arrives inside the budget keeps recovery.

    The acknowledgement is parked for the whole route call, so the outcome does
    not depend on scheduler speed: the turn was attempted, Stop must not mark it
    locally cancelled or drop its durable request, and the route answers 502 so
    the client retries once the relay reconnects.
    """
    import urllib.parse

    from api import gateway_chat, routes

    stream_id = "stable"
    instance, _events, records = relay()
    client = _ParkedAckClient()

    @asynccontextmanager
    async def connect():
        yield client

    instance.connector = connect
    outcome = {}

    def run_relay():
        try:
            outcome["value"] = asyncio.run(instance.run())
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run_relay, daemon=True)
    worker.start()
    assert client.submit_entered.wait(timeout=5), "the relay never attempted the submission"
    assert instance.attempted is True and instance.receipt is None

    gateway_chat._mark_gateway_run_starting(stream_id)
    replies = []
    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "j", lambda _handler, data, status=200, **_kw: replies.append((status, data)))
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: True)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda *_args, **_kw: False)
    monkeypatch.setattr(gateway_chat, "GATEWAY_RUN_ID_WAIT_TIMEOUT", 0.05)
    try:
        routes.handle_get(object(), urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}"))

        assert replies, "the cancel route produced no response"
        status, payload = replies[0]
        assert status == 502, f"a Stop past the wait budget must keep recovery, got {status}: {payload}"
        assert payload.get("ok") is False and payload.get("cancelled") is False, payload
        assert not instance.cancel.is_set(), (
            "an attempted submission must not be cancelled locally as if it never happened"
        )
        assert records and records[0] == ("", instance.request), (
            f"the durable request must survive for recovery: {records!r}"
        )
        assert len([method for method, _ in client.calls if method == "prompt.submit"]) == 1
    finally:
        client.submit_release.set()
        client.status = "terminal"
        worker.join(timeout=10)
        gateway_chat._STREAM_RUN_LIFECYCLE.pop(stream_id, None)
        gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)

    assert not worker.is_alive(), "the relay did not finish after the acknowledgement arrived"
    assert "error" not in outcome, outcome.get("error")


def test_cancel_route_keeps_recovery_when_no_run_id_is_published(protocol, monkeypatch):
    """An uncertain submission with no published run id must stay recoverable.

    The early registration reaches the relay, but there is no bounded wait to
    fall back on and no acknowledgement to interrupt. The handler-level 502 is
    the retry contract here — never a local cancellation.
    """
    stream_id = "stable"
    instance, _events, records = relay()
    client = _SubmitParkedClient()

    @asynccontextmanager
    async def connect():
        yield client

    instance.connector = connect
    outcome = {}

    def run_relay():
        try:
            outcome["value"] = asyncio.run(instance.run())
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run_relay, daemon=True)
    worker.start()
    assert client.submit_entered.wait(timeout=5), "the relay never attempted the submission"
    assert instance.attempted is True and instance.receipt is None

    # No `_mark_gateway_run_starting`: nothing published a run id, so the
    # bounded wait reports `(False, None)` instead of blocking.
    import urllib.parse

    from api import gateway_chat, routes

    replies = []
    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "j", lambda _handler, data, status=200, **_kw: replies.append((status, data)))
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: True)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda *_args, **_kw: False)
    monkeypatch.setattr(gateway_chat, "GATEWAY_RUN_ID_WAIT_TIMEOUT", 0.05)
    try:
        routes.handle_get(object(), urllib.parse.urlparse(f"/api/chat/cancel?stream_id={stream_id}"))

        assert replies, "the cancel route produced no response"
        status, payload = replies[0]
        assert status == 502, f"an unresolved submission must keep recovery, got {status}: {payload}"
        assert payload.get("ok") is False and payload.get("cancelled") is False, payload
        assert not instance.cancel.is_set()
        assert records and records[0] == ("", instance.request)
        assert len([method for method, _ in client.calls if method == "prompt.submit"]) == 1
    finally:
        client.submit_release.set()
        worker.join(timeout=10)
        gateway_chat._STREAM_RUN_LIFECYCLE.pop(stream_id, None)
        gateway_chat._STREAM_RUN_IDS.pop(stream_id, None)

    assert not worker.is_alive(), "the relay did not finish after the submission was released"
    assert "error" not in outcome, outcome.get("error")
