from __future__ import annotations

import http.client
import json
import threading

import pytest

import server
from api import auth


@pytest.fixture
def bridge_server():
    access_logs: list[str] = []

    class _CapturingHandler(server.Handler):
        def _safe_webui_print(self, message: str) -> None:
            access_logs.append(message)

    httpd = server.QuietHTTPServer(("127.0.0.1", 0), _CapturingHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], access_logs
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "auth_state,expected_status",
    [
        ("unauthenticated", 401),
        ("expired", 401),
        ("profile_forbidden", 403),
    ],
)
def test_stream_auth_failures_hide_query_and_send_bridge_headers(
    monkeypatch,
    bridge_server,
    auth_state,
    expected_status,
):
    port, access_logs = bridge_server
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    if auth_state == "unauthenticated":
        monkeypatch.setattr(auth, "parse_cookie", lambda _handler: None)
        monkeypatch.setattr(auth, "verify_session", lambda _value: False)
        monkeypatch.setattr(auth, "ensure_trusted_auth_session", lambda _handler: None)
    elif auth_state == "expired":
        monkeypatch.setattr(auth, "parse_cookie", lambda _handler: "expired")
        monkeypatch.setattr(auth, "verify_session", lambda _value: False)
        monkeypatch.setattr(auth, "ensure_trusted_auth_session", lambda _handler: None)
    else:
        monkeypatch.setattr(auth, "parse_cookie", lambda _handler: "valid")
        monkeypatch.setattr(auth, "verify_session", lambda _value: True)
        monkeypatch.setattr(
            auth,
            "ensure_trusted_auth_session",
            lambda _handler: {"bound_profile": "ornith"},
        )
        monkeypatch.setattr(
            auth,
            "trusted_session_allows_active_profile",
            lambda _info: False,
        )

    handle = "handle-secret-for-log-test"
    generation = "generation-secret-for-log-test"
    path = (
        "/api/claude-code/terminal/output"
        f"?handle={handle}&generation={generation}"
    )
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request("GET", path)
    response = connection.getresponse()
    response.read()
    connection.close()

    assert response.status == expected_status
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("Referrer-Policy") == "no-referrer"
    records = [
        json.loads(line.removeprefix("[webui] "))
        for line in access_logs
        if line.startswith("[webui] ")
    ]
    assert records[-1]["path"] == "/api/claude-code/terminal/output"
    assert handle not in "\n".join(access_logs)
    assert generation not in "\n".join(access_logs)


def test_generic_auth_failure_keeps_existing_log_and_header_behaviour(
    monkeypatch,
    bridge_server,
):
    port, access_logs = bridge_server
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: None)
    monkeypatch.setattr(auth, "verify_session", lambda _value: False)
    monkeypatch.setattr(auth, "ensure_trusted_auth_session", lambda _handler: None)

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request("GET", "/api/sessions?existing=query")
    response = connection.getresponse()
    response.read()
    connection.close()

    assert response.status == 401
    assert response.getheader("Referrer-Policy") is None
    assert '"path": "/api/sessions?existing=query"' in "\n".join(access_logs)
