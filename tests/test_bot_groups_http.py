"""Exercise the real HTTP auth/CSRF boundary before any groups RPC is reached."""
import http.client
import json
import threading

import pytest

import api.auth as auth
from server import Handler, QuietHTTPServer


@pytest.fixture
def bot_http(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_BOT_GROUPS", "1")
    monkeypatch.setenv("HERMES_WEBUI_SKIP_ONBOARDING", "1")
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    with QuietHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=3)


def request(base, method, operation, body=None, headers=None):
    connection = http.client.HTTPConnection(base.removeprefix("http://"))
    connection.request(method, "/api/bot-groups/" + operation,
                       body=json.dumps(body) if body is not None else None,
                       headers=headers or {})
    response = connection.getresponse()
    result = response.status, json.loads(response.read()), dict(response.getheaders())
    connection.close()
    return result


def test_disabled_and_profile_isolated_modes_do_not_call_gateway(bot_http, monkeypatch):
    def unexpected(*_):
        pytest.fail("restricted request reached the gateway")

    monkeypatch.setattr("api.bot_groups.gateway_request", unexpected)
    monkeypatch.delenv("HERMES_WEBUI_BOT_GROUPS")
    assert request(bot_http, "GET", "capabilities")[1] == {"enabled": False, "available": False}
    monkeypatch.setenv("HERMES_WEBUI_BOT_GROUPS", "1")
    monkeypatch.setattr("api.profiles._is_isolated_profile_mode", lambda: True)
    assert request(bot_http, "GET", "list")[0] == 403


def test_auth_csrf_and_bound_profile_gates_precede_rpc(bot_http, monkeypatch):
    calls = []
    monkeypatch.setattr("api.bot_groups.gateway_request", lambda m, p: calls.append((m, p)) or {"accepted": True})
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    assert request(bot_http, "GET", "list")[0] == 401
    owner = auth.create_session()
    bound = auth.create_session(bound_profile="default")
    try:
        headers = {"Cookie": f"{auth.COOKIE_NAME}={owner}", "Origin": bot_http, "Content-Type": "application/json"}
        body = {"room_id": "room-a", "event_id": "event-a", "payload": {"text": "Hello", "thread_id": "main"}}
        assert request(bot_http, "POST", "send", body, headers)[0] == 403
        headers[auth.CSRF_HEADER_NAME] = auth.csrf_token_for_session(owner)
        status, response, response_headers = request(bot_http, "POST", "send", body, headers)
        assert status == 200 and response["accepted"] is True
        assert response_headers["Cache-Control"] == "no-store"
        assert calls == [("groups.send", body)]
        assert request(bot_http, "GET", "list", headers={"Cookie": f"{auth.COOKIE_NAME}={bound}"})[0] == 403
        assert len(calls) == 1
    finally:
        auth.invalidate_session(owner)
        auth.invalidate_session(bound)
