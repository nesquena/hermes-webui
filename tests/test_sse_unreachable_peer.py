"""
Regression test: a peer that vanished at the network layer must be treated as
a client disconnect on long-lived SSE streams, not as a server error.

Observed on Asus (hermes-webui, /api/sessions/events and /api/session/stream):
a LAN client dropped off the network, the next SSE keepalive write raised
``OSError: [Errno 113] No route to host`` — a bare OSError, NOT a
BrokenPipe/ConnectionReset — so it escaped every handler's
``except _CLIENT_DISCONNECT_ERRORS:`` and the request was logged as a 500 with
a full traceback (22 of them in a week) for what was just a normal disconnect.

The fix keeps ``_CLIENT_DISCONNECT_ERRORS`` unchanged (OSError is deliberately
excluded there — see TestClientDisconnectErrorsTuple: it is too broad and would
mask real errors) and narrows by errno at the SSE write boundary instead:
``helpers._is_client_disconnect_error`` classifies, ``streaming._sse_write``
converts the routing errnos into ConnectionResetError, and every existing
``except _CLIENT_DISCONNECT_ERRORS:`` in api/routes.py then handles them.
"""
import errno
import os
import ssl
import sys
import unittest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.helpers import (  # noqa: E402
    _CLIENT_DISCONNECT_ERRORS,
    _CLIENT_DISCONNECT_ERRNOS,
    _is_client_disconnect_error,
)
from api.streaming import _sse, _sse_keepalive, _sse_write  # noqa: E402


class MockWriteStream:
    """File-like object whose write() optionally raises a canned exception."""

    def __init__(self, raises=None):
        self._raises = raises
        self.written = []
        self.flushes = 0

    def write(self, payload):
        if self._raises is not None:
            raise self._raises
        self.written.append(payload)

    def flush(self):
        self.flushes += 1


class MockSSEHandler:
    def __init__(self, raises=None):
        self.wfile = MockWriteStream(raises)
        self.path = "/api/sessions/events"


class TestDisconnectClassification(unittest.TestCase):
    """errno narrowing, without widening the shared tuple."""

    def test_routing_errnos_are_client_disconnects(self):
        for err in (
            errno.EHOSTUNREACH,   # the observed one: "No route to host"
            errno.ENETUNREACH,
            errno.ENETDOWN,
            errno.EHOSTDOWN,
            errno.ENOTCONN,
            errno.ECONNREFUSED,
            errno.ETIMEDOUT,
            errno.ECONNRESET,
            errno.ECONNABORTED,
            errno.EPIPE,
        ):
            with self.subTest(errno=err):
                self.assertTrue(
                    _is_client_disconnect_error(OSError(err, os.strerror(err)))
                )

    def test_real_errors_are_not_client_disconnects(self):
        """A broken server must still surface: these are not "client gone"."""
        for err in (errno.ENOSPC, errno.EACCES, errno.ENOENT, errno.EIO):
            with self.subTest(errno=err):
                self.assertFalse(
                    _is_client_disconnect_error(OSError(err, os.strerror(err)))
                )

    def test_non_oserror_is_not_a_client_disconnect(self):
        self.assertFalse(_is_client_disconnect_error(ValueError("nope")))

    def test_explicit_tuple_members_still_classify(self):
        for exc in (BrokenPipeError(), ConnectionResetError(),
                    ConnectionAbortedError(), TimeoutError(), ssl.SSLError()):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(_is_client_disconnect_error(exc))

    def test_shared_tuple_untouched(self):
        """The broad-OSError decision is intentional and must not regress."""
        self.assertNotIn(OSError, _CLIENT_DISCONNECT_ERRORS)
        self.assertNotIn(OSError, _CLIENT_DISCONNECT_ERRNOS)


class TestSSEWriteClassification(unittest.TestCase):
    """The write boundary converts narrowly and never masks a real failure."""

    def test_keepalive_with_unreachable_peer_is_swallowed(self):
        handler = MockSSEHandler(OSError(errno.EHOSTUNREACH, "No route to host"))
        try:
            _sse_keepalive(handler)
        except _CLIENT_DISCONNECT_ERRORS:
            return
        self.fail(
            "EHOSTUNREACH escaped: the SSE loops would log a 500 + traceback"
        )

    def test_event_write_with_unreachable_peer_is_swallowed(self):
        handler = MockSSEHandler(OSError(errno.ENETUNREACH, "Network is unreachable"))
        try:
            _sse(handler, "sessions_changed", {"sessions": []})
        except _CLIENT_DISCONNECT_ERRORS:
            return
        self.fail("ENETUNREACH escaped the SSE event write")

    def test_real_oserror_still_propagates(self):
        handler = MockSSEHandler(OSError(errno.ENOSPC, "No space left on device"))
        with self.assertRaises(OSError) as ctx:
            _sse_keepalive(handler)
        self.assertNotIsInstance(ctx.exception, _CLIENT_DISCONNECT_ERRORS)
        self.assertEqual(ctx.exception.errno, errno.ENOSPC)

    def test_healthy_writes_are_unchanged(self):
        handler = MockSSEHandler()
        _sse_keepalive(handler)
        _sse_write(handler, b"raw")
        self.assertEqual(
            handler.wfile.written, [b": keepalive\n\n", b"raw"]
        )
        self.assertEqual(handler.wfile.flushes, 2)


class TestSSELoopSwallowsUnreachablePeer(unittest.TestCase):
    """End-to-end shape of the gateway SSE loop against a dead peer."""

    def test_gateway_loop_exits_quietly_on_no_route_to_host(self):
        import queue as _queue

        handler = MockSSEHandler(OSError(errno.EHOSTUNREACH, "No route to host"))
        q = _queue.Queue()
        unsubscribed = []

        def loop():
            try:
                while True:
                    try:
                        event_data = q.get(timeout=0)
                    except _queue.Empty:
                        _sse_keepalive(handler)   # <-- the line that used to 500
                        continue
                    if event_data is None:
                        break
                    _sse(handler, event_data.get("type", "sessions_changed"), event_data)
            except _CLIENT_DISCONNECT_ERRORS:
                pass
            finally:
                unsubscribed.append(True)
            return True

        self.assertTrue(loop())                # no exception escaped
        self.assertEqual(unsubscribed, [True])  # subscriber still released


class TestSSEWithIdClassification(unittest.TestCase):
    """The ``id:`` prefix is a write too, and must convert like the body.

    An SSE frame that carries a journal id writes the id line *before* the
    event body. Writing it directly left that first write outside the boundary,
    so a vanished peer still produced the 500 this change exists to prevent.
    """

    def test_id_prefix_write_with_unreachable_peer_is_swallowed(self):
        from api.routes import _sse_with_id

        handler = MockSSEHandler(OSError(errno.EHOSTUNREACH, "No route to host"))
        try:
            _sse_with_id(handler, "token", {"text": "x"}, "42")
        except _CLIENT_DISCONNECT_ERRORS:
            return
        self.fail(
            "EHOSTUNREACH escaped the id-prefix write: an id-carrying stream "
            "would still log a 500 + traceback"
        )

    def test_id_prefix_real_oserror_still_propagates(self):
        from api.routes import _sse_with_id

        handler = MockSSEHandler(OSError(errno.ENOSPC, "No space left on device"))
        with self.assertRaises(OSError) as ctx:
            _sse_with_id(handler, "token", {"text": "x"}, "42")
        self.assertNotIsInstance(ctx.exception, _CLIENT_DISCONNECT_ERRORS)
        self.assertEqual(ctx.exception.errno, errno.ENOSPC)

    def test_healthy_id_prefixed_write_is_unchanged(self):
        from api.routes import _sse_with_id

        handler = MockSSEHandler()
        _sse_with_id(handler, "token", {"text": "x"}, "42")
        self.assertEqual(handler.wfile.written[0], b"id: 42\n")
        self.assertTrue(handler.wfile.written[1].startswith(b"event: token\n"))
        self.assertIn(b'"text": "x"', handler.wfile.written[1])
        self.assertEqual(handler.wfile.flushes, 2)

    def test_without_event_id_no_prefix_is_written(self):
        from api.routes import _sse_with_id

        handler = MockSSEHandler()
        _sse_with_id(handler, "token", {"text": "x"})
        self.assertEqual(len(handler.wfile.written), 1)
        self.assertTrue(handler.wfile.written[0].startswith(b"event: token\n"))


if __name__ == "__main__":
    unittest.main()
