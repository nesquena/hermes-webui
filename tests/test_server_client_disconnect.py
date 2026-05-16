"""Regression tests for client disconnect handling in the WebUI server."""

import server


class _FakeHandler:
    path = "/api/sessions/gateway/stream?probe=1"
    command = "GET"


def test_get_client_disconnect_does_not_emit_500(monkeypatch, capsys):
    """A probe/client abort can raise BrokenPipe while writing JSON.

    That is a normal client disconnect, not an application 500. The top-level
    GET handler must swallow it without logging an ERROR or trying to write an
    additional 500 response to the already-closed socket.
    """

    calls = {"error_response": 0}

    monkeypatch.setattr(server, "get_profile_cookie", lambda handler: None)
    monkeypatch.setattr(server, "check_auth", lambda handler, parsed: True)
    monkeypatch.setattr(server, "handle_get", lambda handler, parsed: (_ for _ in ()).throw(BrokenPipeError("client closed")))

    def fail_json_response(*args, **kwargs):
        calls["error_response"] += 1
        raise AssertionError("client disconnect must not trigger JSON 500 response")

    monkeypatch.setattr(server, "j", fail_json_response)

    server.Handler.do_GET(_FakeHandler())  # type: ignore[arg-type]

    captured = capsys.readouterr()
    assert "ERROR GET" not in captured.out
    assert calls["error_response"] == 0
