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
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda home:
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
    attempts = []
    client = Client()
    @asynccontextmanager
    async def connect(home):
        attempts.append(home)
        if len(attempts) <= 5:
            raise ConnectionError("do not leak ws://credential@host")
        yield client
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
