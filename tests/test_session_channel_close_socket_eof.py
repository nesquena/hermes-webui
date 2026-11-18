"""The close sentinel must END the HTTP response, not merely exit the handler.

Re-gate regression for PR #7302 (review 2026-09-23, item 1).
``SessionChannel.close()`` signals every subscriber with a ``None`` sentinel and
``_handle_session_sse_stream`` breaks its ``q.get()`` loop on it. Breaking alone is not
enough: under HTTP/1.1 keep-alive, with no ``Content-Length`` and no chunked framing,
returning from the handler leaves the socket open. The browser never sees EOF,
``EventSource`` never reconnects, and the tab stays attached to a channel the reaper
already removed -- silently missing every later event, which is the "tab stops receiving
updates" symptom this change exists to fix.

This drives the REAL ``_handle_session_sse_stream`` over a REAL TCP socket (the shape the
review reproduced) and asserts the socket-level consequence, which no assertion on
``handler.close_connection`` can show. The second test is the bug itself, using a minimal
handler of the same shape, so the first is known to be testing something: the very same
sequence without the flag leaves the connection open.

The #3103 decision is unchanged and deliberately not re-litigated here: the response must
not advertise ``Connection: close`` up front (that causes reconnect storms). Closing is
only for a deliberate end-of-channel.
"""
import queue
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import api.background_process as bp
import api.routes as routes

_LIVE_SID = "sse-eof-live"
_LEGACY_SID = "sse-eof-legacy"


class _ProductionSseHandler(BaseHTTPRequestHandler):
    """Delegate GET to the real production SSE entry point."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the test output quiet

    def do_GET(self):
        routes._handle_session_sse_stream(self, urllib.parse.urlparse(self.path))


class _NoCloseSseHandler(BaseHTTPRequestHandler):
    """Same shape, but the sentinel only breaks the loop (pre-fix behavior)."""

    protocol_version = "HTTP/1.1"
    session_id = _LEGACY_SID

    def log_message(self, *args):
        pass

    def do_GET(self):
        ch, q = bp.subscribe_to_session_channel(self.session_id, maxsize=8)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(b"event: initial\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if payload is None:
                    break  # <-- the pre-fix break: no handler.close_connection
                self.wfile.write(f"event: {payload[0]}\ndata: {{}}\n\n".encode())
                self.wfile.flush()
        except OSError:
            pass  # client went away
        finally:
            ch.unsubscribe(q)


def _serve(handler_cls, session_id: str):
    """Start the handler on a real ephemeral port and send one SSE request."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sock = socket.create_connection(server.server_address, timeout=5)
    sock.sendall(
        (
            f"GET /api/session/stream?session_id={session_id} HTTP/1.1\r\n"
            "Host: localhost\r\n\r\n"
        ).encode()
    )
    return server, sock


def _read_until(sock, needle: bytes, deadline: float = 5.0) -> bytes:
    """Read until ``needle`` appears, the peer closes, or the deadline passes."""
    buf = b""
    end = time.time() + deadline
    sock.settimeout(0.25)
    while time.time() < end:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            return buf
        if not chunk:
            return buf
        buf += chunk
        if needle in buf:
            return buf
    return buf


def _eof_within(sock, deadline: float = 5.0) -> bool:
    """True once the server closes the connection (``recv`` returns ``b''``)."""
    end = time.time() + deadline
    sock.settimeout(0.25)
    while time.time() < end:
        try:
            if sock.recv(4096) == b"":
                return True
        except socket.timeout:
            continue
        except OSError:
            return True
    return False


def _drive_reaper_until(predicate, timeout: float = 3.0, interval: float = 0.02):
    """Run the REAL reaper loop until ``predicate()`` holds (tight tick, real body)."""
    original = bp._REAPER_INTERVAL_SECS
    bp._REAPER_INTERVAL_SECS = interval
    bp.start_session_channel_reaper()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()
    finally:
        bp.stop_session_channel_reaper()
        bp._REAPER_INTERVAL_SECS = original


def test_channel_close_ends_the_response_so_the_client_reconnects():
    """close() -> sentinel -> handler exits -> the socket is closed, so the tab reconnects."""
    bp.SESSION_CHANNELS.pop(_LIVE_SID, None)
    server, sock = _serve(_ProductionSseHandler, _LIVE_SID)
    try:
        assert b"event: initial" in _read_until(sock, b"event: initial"), (
            "the SSE stream never opened"
        )

        ch = bp.get_session_channel(_LIVE_SID)
        assert ch is not None and ch.subscriber_count() == 1
        assert ch.close("test") == 1

        assert _eof_within(sock), (
            "the server kept the socket open after the channel closed: the browser's "
            "EventSource sees no EOF (only keepalives) and never reconnects, so the tab "
            "silently misses every later event"
        )

        # The reaper collects the explicitly-closed channel on its next tick, and
        # that is what frees the registry slot: the reconnection must land on a NEW
        # channel, never on the dead one.
        assert _drive_reaper_until(lambda: bp.get_session_channel(_LIVE_SID) is None), (
            "the closed channel was never collected by the reaper"
        )

        replacement, _q = bp.subscribe_to_session_channel(_LIVE_SID, maxsize=8)
        assert replacement is not ch
        assert replacement.closed is False
    finally:
        sock.close()
        server.shutdown()
        server.server_close()
        bp.SESSION_CHANNELS.pop(_LIVE_SID, None)


def test_without_closing_the_socket_the_client_never_sees_eof():
    """The bug itself, so the test above is known to be testing something.

    The same sequence -- subscribe, ``close()``, sentinel delivered -- leaves the
    connection open when the handler only breaks out of its loop.
    """
    bp.SESSION_CHANNELS.pop(_LEGACY_SID, None)
    server, sock = _serve(_NoCloseSseHandler, _LEGACY_SID)
    try:
        assert b"initial" in _read_until(sock, b"initial"), "the SSE stream never opened"

        ch = bp.get_session_channel(_LEGACY_SID)
        assert ch is not None
        assert ch.close("test") == 1

        assert not _eof_within(sock, deadline=1.5), (
            "this arm is supposed to leave the connection open (pre-fix shape)"
        )
    finally:
        sock.close()
        server.shutdown()
        server.server_close()
        bp.SESSION_CHANNELS.pop(_LEGACY_SID, None)
