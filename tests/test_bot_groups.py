"""The bridge speaks the shipped Hermes groups protocol, not a local executor."""
import json
import threading
from contextlib import contextmanager

import pytest

from api.bot_groups import BotGroupsError, call_group_method, gateway_request


@contextmanager
def gateway(responder):
    from websockets.sync.server import serve

    requests = []

    def handle(socket):
        data = json.loads(socket.recv())
        requests.append(data)
        socket.send(json.dumps({"method": "event", "params": {"type": "ready"}}))
        socket.send(json.dumps({"jsonrpc": "2.0", "id": data["id"], **responder(data)}))

    with serve(handle, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"ws://127.0.0.1:{server.socket.getsockname()[1]}/api/ws", requests
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_real_websocket_rpc_skips_notifications_and_closes(monkeypatch):
    with gateway(lambda _: {"result": {"rooms": [], "next_offset": None}}) as (url, calls):
        monkeypatch.setenv("HERMES_WEBUI_BOT_GATEWAY_URL", url)
        monkeypatch.setenv("HERMES_WEBUI_BOT_GATEWAY_TOKEN", "test-only-dashboard-token")
        result = gateway_request("groups.list", {"offset": 0, "limit": 100})
        assert result == {"rooms": [], "next_offset": None}
        assert calls[0]["method"] == "groups.list"
        assert calls[0]["params"] == {"offset": 0, "limit": 100}
        assert "token" not in json.dumps(calls)


def test_gateway_errors_never_return_credentials(monkeypatch):
    with gateway(lambda _: {"error": {"code": 4111, "message": "test-secret"}}) as (url, _):
        monkeypatch.setenv("HERMES_WEBUI_BOT_GATEWAY_URL", url)
        monkeypatch.setenv("HERMES_WEBUI_BOT_GATEWAY_TOKEN", "test-secret")
        with pytest.raises(BotGroupsError) as error:
            gateway_request("groups.send", {})
        assert error.value.status == 409
        assert "4111" in str(error.value)
        assert "test-secret" not in str(error.value)


def test_send_preserves_idempotency_and_thread(monkeypatch):
    calls = []
    monkeypatch.setattr("api.bot_groups.gateway_request", lambda m, p: calls.append((m, p)) or {"accepted": True})
    params = {"room_id": "room-a", "event_id": "send-a", "payload": {"text": "@review check this", "thread_id": "main"}}
    assert call_group_method("send", params) == {"accepted": True}
    assert call_group_method("send", params) == {"accepted": True}
    assert calls == [("groups.send", params), ("groups.send", params)]


@pytest.mark.parametrize("operation,params", [
    ("replicate", {"room_id": "a"}),
    ("send", {"room_id": "a", "event_id": "b", "payload": {"text": "hello", "thread_id": "main", "actor": "admin"}}),
    ("approve", {"room_id": "a", "member_id": "m", "task_id": "t", "execution_generation": 1, "request_id": "r", "choice": "always"}),
    ("log", {"room_id": "a", "since_seq": -1}),
    ("list", {"limit": 10000}),
    ("create", {"room_id": "a", "name": "Team", "members": [{"profile": "one"}]}),
    ("state", {"room_id": "a", "url": "https://example.com"}),
    ("state", {"room_id": "invalid/path"}),
    ("send", {"room_id": "a", "event_id": "b", "payload": {"text": "研" * 22000, "thread_id": "main"}}),
])
def test_invalid_or_privileged_commands_never_reach_gateway(monkeypatch, operation, params):
    def unexpected(*args):
        pytest.fail("invalid request reached gateway")

    monkeypatch.setattr("api.bot_groups.gateway_request", unexpected)
    with pytest.raises(BotGroupsError):
        call_group_method(operation, params)


def test_roster_does_not_expose_paths_or_hidden_chat_previews(monkeypatch):
    monkeypatch.setattr("api.bot_groups.gateway_request", lambda *_: {"profiles": [
        {"name": "research", "display_name": "Research", "path": "/private", "canonical_session": {"preview": "private"}},
    ]})
    assert call_group_method("profiles", {}) == {"profiles": [{"name": "research", "display_name": "Research"}]}
