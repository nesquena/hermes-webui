"""
Test for BrokenPipeError + SSL BAD_LENGTH cascading failure fix.

When a client disconnects mid-response:
1. First write raises BrokenPipeError (or ssl.SSLError on TLS)
2. Exception handler tries to send 500 JSON through same broken socket
3. Without the fix, this produces a second exception and noisy traceback

The fix:
- helpers._safe_write() catches _CLIENT_DISCONNECT_ERRORS (including ssl.SSLError)
- server.py exception handlers wrap their 500-response j() calls in try/except
"""
import ssl
import unittest
from unittest.mock import MagicMock

import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.helpers import j, t, _safe_write, _CLIENT_DISCONNECT_ERRORS


class MockHandler:
    """Minimal mock of BaseHTTPRequestHandler for testing response helpers."""

    def __init__(self, write_raises=None, end_headers_raises=None):
        self._write_raises = write_raises
        self._end_headers_raises = end_headers_raises
        self.headers = {}
        self._wfile = MagicMock()
        self._sent_headers = []
        self._response_status = None

    @property
    def wfile(self):
        return self._wfile

    def send_response(self, status):
        self._response_status = status

    def send_header(self, key, value):
        self._sent_headers.append((key, value))

    def end_headers(self):
        if self._end_headers_raises:
            raise self._end_headers_raises

    def _wfile_write(self, data):
        if self._write_raises:
            raise self._write_raises
        return len(data)


class TestSafeWrite(unittest.TestCase):
    """Test _safe_write swallows client disconnect errors without raising."""

    def _make_handler(self, end_headers_raises=None, write_raises=None):
        handler = MockHandler(
            end_headers_raises=end_headers_raises,
            write_raises=write_raises,
        )
        handler.wfile.write = handler._wfile_write
        return handler

    def test_safe_write_success(self):
        handler = self._make_handler()
        _safe_write(handler, b"hello")
        self.assertEqual(handler._response_status, None)  # send_response not called by _safe_write

    def test_safe_write_broken_pipe(self):
        handler = self._make_handler(write_raises=BrokenPipeError())
        # Should NOT raise
        _safe_write(handler, b"hello")

    def test_safe_write_connection_reset(self):
        handler = self._make_handler(write_raises=ConnectionResetError())
        _safe_write(handler, b"hello")

    def test_safe_write_connection_aborted(self):
        handler = self._make_handler(write_raises=ConnectionAbortedError())
        _safe_write(handler, b"hello")

    def test_safe_write_timeout(self):
        handler = self._make_handler(write_raises=TimeoutError())
        _safe_write(handler, b"hello")

    def test_safe_write_ssl_bad_length(self):
        handler = self._make_handler(
            write_raises=ssl.SSLError("[BAD_LENGTH] write failed")
        )
        _safe_write(handler, b"hello")

    def test_safe_write_end_headers_broken_pipe(self):
        handler = self._make_handler(end_headers_raises=BrokenPipeError())
        _safe_write(handler, b"hello")

    def test_safe_write_end_headers_ssl_error(self):
        handler = self._make_handler(
            end_headers_raises=ssl.SSLError("[BAD_LENGTH]")
        )
        _safe_write(handler, b"hello")

    def test_safe_write_other_error_propagates(self):
        handler = self._make_handler(write_raises=ValueError("unexpected"))
        with self.assertRaises(ValueError):
            _safe_write(handler, b"hello")


class TestJsonHelper(unittest.TestCase):
    """Test j() helper with disconnect errors."""

    def _make_handler(self, end_headers_raises=None, write_raises=None):
        handler = MockHandler(
            end_headers_raises=end_headers_raises,
            write_raises=write_raises,
        )
        handler.wfile.write = handler._wfile_write
        return handler

    def test_j_success(self):
        handler = self._make_handler()
        j(handler, {"ok": True}, status=200)
        self.assertEqual(handler._response_status, 200)
        self.assertTrue(any(h[0] == "Content-Type" for h in handler._sent_headers))

    def test_j_broken_pipe_on_write(self):
        handler = self._make_handler(write_raises=BrokenPipeError())
        # Should NOT raise — headers sent, write fails silently
        j(handler, {"ok": True}, status=200)
        self.assertEqual(handler._response_status, 200)

    def test_j_ssl_error_on_write(self):
        handler = self._make_handler(
            write_raises=ssl.SSLError("[BAD_LENGTH] write failed")
        )
        j(handler, {"ok": True}, status=200)
        self.assertEqual(handler._response_status, 200)

    def test_j_broken_pipe_on_end_headers(self):
        handler = self._make_handler(end_headers_raises=BrokenPipeError())
        j(handler, {"ok": True}, status=200)
        self.assertEqual(handler._response_status, 200)


class TestTextHelper(unittest.TestCase):
    """Test t() helper with disconnect errors."""

    def _make_handler(self, end_headers_raises=None, write_raises=None):
        handler = MockHandler(
            end_headers_raises=end_headers_raises,
            write_raises=write_raises,
        )
        handler.wfile.write = handler._wfile_write
        return handler

    def test_t_success(self):
        handler = self._make_handler()
        t(handler, "hello", status=200)
        self.assertEqual(handler._response_status, 200)

    def test_t_broken_pipe(self):
        handler = self._make_handler(write_raises=BrokenPipeError())
        t(handler, "hello", status=200)
        self.assertEqual(handler._response_status, 200)

    def test_t_ssl_error(self):
        handler = self._make_handler(
            write_raises=ssl.SSLError("[BAD_LENGTH]")
        )
        t(handler, "hello", status=200)
        self.assertEqual(handler._response_status, 200)


class TestClientDisconnectErrorsTuple(unittest.TestCase):
    """Verify _CLIENT_DISCONNECT_ERRORS includes all expected types."""

    def test_includes_broken_pipe(self):
        self.assertIn(BrokenPipeError, _CLIENT_DISCONNECT_ERRORS)

    def test_includes_connection_reset(self):
        self.assertIn(ConnectionResetError, _CLIENT_DISCONNECT_ERRORS)

    def test_includes_connection_aborted(self):
        self.assertIn(ConnectionAbortedError, _CLIENT_DISCONNECT_ERRORS)

    def test_includes_timeout(self):
        self.assertIn(TimeoutError, _CLIENT_DISCONNECT_ERRORS)

    def test_includes_ssl_error(self):
        self.assertIn(ssl.SSLError, _CLIENT_DISCONNECT_ERRORS)

    def test_excludes_broad_oserror(self):
        """OSError is too broad — it masks real errors like file-not-found."""
        self.assertNotIn(OSError, _CLIENT_DISCONNECT_ERRORS)


class TestServerDisconnectHandling(unittest.TestCase):
    """Test server.py skips 500 response when client disconnects."""

    def _make_handler(self, route_raises=None):
        """Build a Handler with mocked socket and route."""
        from server import Handler
        handler = Handler.__new__(Handler)
        handler.command = "GET"
        handler.path = "/api/test"
        handler._req_t0 = 0.0
        handler.headers = {}
        handler.wfile = MagicMock()
        handler.wfile.write = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler._route_raises = route_raises
        return handler

    def test_do_get_skips_500_on_broken_pipe(self):
        from server import Handler
        handler = self._make_handler(route_raises=BrokenPipeError())

        def _fake_handle_get(self, parsed):
            raise self._route_raises

        # Patch handle_get to raise BrokenPipeError
        import server as _server_mod
        orig_handle_get = _server_mod.handle_get
        _server_mod.handle_get = _fake_handle_get
        try:
            Handler.do_GET(handler)
        finally:
            _server_mod.handle_get = orig_handle_get

        # send_response should NEVER be called for the 500 — client is gone
        handler.send_response.assert_not_called()

    def test_handle_write_skips_500_on_connection_reset(self):
        from server import Handler
        handler = self._make_handler(route_raises=ConnectionResetError())
        handler.command = "POST"

        def _fake_route(self, parsed):
            raise self._route_raises

        import server as _server_mod
        orig_check_auth = _server_mod.check_auth
        _server_mod.check_auth = lambda h, p: True
        try:
            Handler._handle_write(handler, _fake_route)
        finally:
            _server_mod.check_auth = orig_check_auth

        handler.send_response.assert_not_called()

    def test_do_get_sends_500_on_real_error(self):
        from server import Handler
        handler = self._make_handler(route_raises=ValueError("real bug"))

        def _fake_handle_get(self, parsed):
            raise self._route_raises

        import server as _server_mod
        orig_handle_get = _server_mod.handle_get
        orig_check_auth = _server_mod.check_auth
        _server_mod.handle_get = _fake_handle_get
        _server_mod.check_auth = lambda h, p: True
        try:
            Handler.do_GET(handler)
        finally:
            _server_mod.handle_get = orig_handle_get
            _server_mod.check_auth = orig_check_auth

        # Should send 500 for real errors
        handler.send_response.assert_called_once_with(500)

    def test_do_get_skips_500_on_ssl_disconnect(self):
        """SSL disconnect during route handling should not trigger 500."""
        from server import Handler
        handler = self._make_handler(route_raises=ssl.SSLError("[BAD_LENGTH]"))

        def _fake_handle_get(self, parsed):
            raise self._route_raises

        import server as _server_mod
        orig_handle_get = _server_mod.handle_get
        _server_mod.handle_get = _fake_handle_get
        try:
            Handler.do_GET(handler)
        finally:
            _server_mod.handle_get = orig_handle_get

        handler.send_response.assert_not_called()

    def test_do_get_skips_500_on_timeout_disconnect(self):
        """Timeout disconnect during route handling should not trigger 500."""
        from server import Handler
        handler = self._make_handler(route_raises=TimeoutError())

        def _fake_handle_get(self, parsed):
            raise self._route_raises

        import server as _server_mod
        orig_handle_get = _server_mod.handle_get
        _server_mod.handle_get = _fake_handle_get
        try:
            Handler.do_GET(handler)
        finally:
            _server_mod.handle_get = orig_handle_get

        handler.send_response.assert_not_called()


class TestServer500ResponseSafety(unittest.TestCase):
    """Test that 500-response failures are handled gracefully."""

    def _make_handler(self, route_raises=None, json_write_raises=None):
        """Build a Handler where the 500-response j() call may also fail."""
        from server import Handler
        handler = Handler.__new__(Handler)
        handler.command = "GET"
        handler.path = "/api/test"
        handler._req_t0 = 0.0
        handler.headers = {}
        handler.wfile = MagicMock()
        handler._json_write_raises = json_write_raises

        def _raising_write(data):
            if handler._json_write_raises:
                raise handler._json_write_raises
            return len(data)

        handler.wfile.write = _raising_write
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler._route_raises = route_raises
        return handler

    def test_500_response_survives_client_disconnect(self):
        """If the 500-response itself hits a disconnect, don't crash."""
        from server import Handler
        handler = self._make_handler(
            route_raises=ValueError("real bug"),
            json_write_raises=BrokenPipeError(),
        )

        def _fake_handle_get(self, parsed):
            raise self._route_raises

        import server as _server_mod
        orig_handle_get = _server_mod.handle_get
        orig_check_auth = _server_mod.check_auth
        _server_mod.handle_get = _fake_handle_get
        _server_mod.check_auth = lambda h, p: True
        try:
            Handler.do_GET(handler)
        finally:
            _server_mod.handle_get = orig_handle_get
            _server_mod.check_auth = orig_check_auth

        # send_response WAS called (we tried to send 500), but write failed
        handler.send_response.assert_called_once_with(500)

    def test_500_response_logs_unexpected_failure(self):
        """If the 500-response fails for a NON-disconnect reason, log it."""
        from server import Handler
        handler = self._make_handler(
            route_raises=ValueError("real bug"),
            json_write_raises=RuntimeError("json serializer exploded"),
        )

        def _fake_handle_get(self, parsed):
            raise self._route_raises

        import server as _server_mod
        orig_handle_get = _server_mod.handle_get
        orig_check_auth = _server_mod.check_auth
        _server_mod.handle_get = _fake_handle_get
        _server_mod.check_auth = lambda h, p: True
        try:
            Handler.do_GET(handler)
        finally:
            _server_mod.handle_get = orig_handle_get
            _server_mod.check_auth = orig_check_auth

        # send_response WAS called (we tried to send 500), but write failed
        handler.send_response.assert_called_once_with(500)


if __name__ == "__main__":
    unittest.main()
