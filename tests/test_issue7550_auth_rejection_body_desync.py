"""Issue #7550 — an answered-but-unread body must not poison its connection.

``check_auth`` writes a complete response and returns before ``rfile`` is read.
Because that response is framed with ``Content-Length`` and the connection is
HTTP/1.1 keep-alive, ``BaseHTTPRequestHandler`` then parses the leftover body
bytes as the *next* request line: a client — or a reverse proxy pooling upstream
connections — that reuses the connection is answered with ``501 Unsupported
method`` whose message contains the previous request's JSON body.

Same failure class, same guard, other early answers: an unknown route (404) and
an internal error (500) also answer before the body is consumed.

These tests drive the real ``server.Handler`` on the real ``QuietHTTPServer``
over a loopback socket, so the behaviour is observed on the wire instead of
being asserted against source text.
"""
from __future__ import annotations

import contextlib
import socket
import threading

import pytest

import api.auth as auth_api
import server as server_module


BODY = '{"session_id":"x","text":"y"}'


@contextlib.contextmanager
def _running_server(monkeypatch, *, auth_enabled: bool):
    """Boot the production Handler/HTTPServer pair on an ephemeral loopback port."""
    monkeypatch.setattr(auth_api, 'is_auth_enabled', lambda: bool(auth_enabled))
    if auth_enabled:
        # No session cookie and no trusted header: every non-public path is
        # rejected before any route runs — the exact path from the report.
        monkeypatch.setattr(
            auth_api, 'ensure_trusted_auth_session', lambda handler: None
        )
    httpd = server_module.QuietHTTPServer(('127.0.0.1', 0), server_module.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _open(port: int) -> socket.socket:
    sock = socket.create_connection(('127.0.0.1', port), timeout=5)
    sock.settimeout(5)
    return sock


def _send(sock, method: str, path: str, port: int, body: str | None = None) -> None:
    head = (
        f'{method} {path} HTTP/1.1\r\n'
        f'Host: 127.0.0.1:{port}\r\n'
        'Connection: keep-alive\r\n'
    )
    if body is None:
        sock.sendall((head + '\r\n').encode('latin-1'))
        return
    payload = body.encode('utf-8')
    sock.sendall(
        (
            head
            + 'Content-Type: application/json\r\n'
            + f'Content-Length: {len(payload)}\r\n\r\n'
        ).encode('latin-1')
        + payload
    )


def _read_response(sock) -> tuple[str, dict, bytes]:
    """Read one framed response; ('', {}, b'') when the peer closed instead."""
    data = b''
    while b'\r\n\r\n' not in data:
        chunk = sock.recv(65536)
        if not chunk:
            return '', {}, b''
        data += chunk
    head, _, body = data.partition(b'\r\n\r\n')
    lines = head.split(b'\r\n')
    status = lines[0].decode('latin-1') if lines else ''
    headers: dict = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b':')
        if sep:
            headers[name.decode('latin-1').strip().lower()] = value.decode('latin-1').strip()
    length = int(headers.get('content-length') or 0)
    while len(body) < length:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return status, headers, body


def _next_request_status(sock, port: int, method='GET', path='/health') -> str:
    """Answer for a follow-up request on a possibly-poisoned connection."""
    try:
        _send(sock, method, path, port)
    except OSError:
        return ''  # peer already closed — the connection cannot be desynced
    try:
        status, _headers, _body = _read_response(sock)
    except OSError:
        return ''
    return status


def _assert_connection_not_desynced(follow_up: str) -> None:
    """The follow-up must be answered for *itself*, or the socket must be closed.

    A desynced connection answers with a parse error produced by the previous
    request's body: ``501 Unsupported method ('{"session_id":"x",...}GET')`` in
    the report, or ``400 Bad request syntax`` when the leftover bytes are
    parsed immediately as a request line.
    """
    assert follow_up in ('', 'HTTP/1.1 200 OK'), follow_up


def test_rejected_post_body_does_not_desync_the_next_request(monkeypatch):
    """The reported repro: unauthenticated POST, then reuse the connection."""
    with _running_server(monkeypatch, auth_enabled=True) as port:
        sock = _open(port)
        try:
            _send(sock, 'POST', '/api/chat/steer', port, body=BODY)
            status, headers, body = _read_response(sock)
            assert status.startswith('HTTP/1.1 401'), (status, body)

            # The reported failure: the leftover body must never be parsed as a
            # request line on this connection.
            follow_up = _next_request_status(sock, port)
            assert 'session_id' not in follow_up, follow_up
            _assert_connection_not_desynced(follow_up)

            # ... and the rejection must tell the client/proxy not to reuse it.
            assert headers.get('connection', '').lower() == 'close', headers
        finally:
            sock.close()


def test_redirect_rejection_with_body_does_not_desync(monkeypatch):
    """Non-API POSTs are answered with a 302 — same unread-body path."""
    with _running_server(monkeypatch, auth_enabled=True) as port:
        sock = _open(port)
        try:
            _send(sock, 'POST', '/session/anything', port, body=BODY)
            status, headers, _body = _read_response(sock)
            assert status.startswith('HTTP/1.1 302'), status
            assert headers.get('location', '').startswith('login'), headers
            assert headers.get('connection', '').lower() == 'close', headers

            follow_up = _next_request_status(sock, port)
            _assert_connection_not_desynced(follow_up)
        finally:
            sock.close()


def test_rejection_without_a_body_keeps_keepalive(monkeypatch):
    """No body to leave behind → keep-alive is preserved as before."""
    with _running_server(monkeypatch, auth_enabled=True) as port:
        sock = _open(port)
        try:
            _send(sock, 'GET', '/api/sessions', port)
            status, headers, _body = _read_response(sock)
            assert status.startswith('HTTP/1.1 401'), status
            assert headers.get('connection', '').lower() != 'close', headers

            _send(sock, 'GET', '/api/sessions', port)
            status2, _headers2, _body2 = _read_response(sock)
            assert status2.startswith('HTTP/1.1 401'), status2
        finally:
            sock.close()


def test_unknown_route_with_body_does_not_desync(monkeypatch):
    """The 404 sibling: routing never ran, so the body was never read."""
    with _running_server(monkeypatch, auth_enabled=False) as port:
        sock = _open(port)
        try:
            _send(sock, 'POST', '/api/definitely-not-a-route', port, body=BODY)
            status, headers, _body = _read_response(sock)
            assert status.startswith('HTTP/1.1 404'), status
            assert headers.get('connection', '').lower() == 'close', headers

            follow_up = _next_request_status(sock, port)
            _assert_connection_not_desynced(follow_up)
        finally:
            sock.close()


def test_route_that_reads_the_body_keeps_keepalive(monkeypatch):
    """Positive control: a handled body must not close the connection."""
    with _running_server(monkeypatch, auth_enabled=False) as port:
        sock = _open(port)
        try:
            _send(sock, 'POST', '/api/csp-report', port, body='{"csp-report":{}}')
            status, headers, _body = _read_response(sock)
            assert status.startswith('HTTP/1.1 204'), status
            assert headers.get('connection', '').lower() != 'close', headers

            follow_up = _next_request_status(sock, port)
            assert follow_up.startswith('HTTP/1.1 200'), follow_up
        finally:
            sock.close()


@pytest.mark.parametrize('content_length', ['7', 'not-a-number'])
def test_unparsable_or_positive_length_is_treated_as_body_bearing(monkeypatch, content_length):
    """Defensive unit check for the predicate used by every early answer."""
    from api.helpers import close_if_body_unread, request_has_body

    class _Headers(dict):
        pass

    class _Handler:
        def __init__(self):
            self.headers = _Headers({'Content-Length': content_length})
            self.close_connection = False

    handler = _Handler()
    assert request_has_body(handler) is True
    assert close_if_body_unread(handler) == {'Connection': 'close'}
    assert handler.close_connection is True
