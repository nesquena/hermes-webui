"""
Ticket #0095: access logging silently dead after an agent turn hijacks stdout.

The WebUI runs agent turns in-process on worker threads (api/streaming.py).
Agent tool dispatch swaps process-wide sys.stdout/sys.stderr to a devnull
around every tool call and restores them in a finally. With concurrent
turns those save/restore pairs race: thread B saves thread A's devnull as
its "real" stdout and restores it after A has closed it, leaving sys.stdout
a *closed* file object forever. Handler._safe_webui_print swallows the
resulting ValueError (by design, so logging can't break responses), so
every [webui] access-log line is silently dropped until the service
restarts — the service keeps serving but is log-blind.

These tests hijack sys.stdout the two ways a racing turn does (swapped for
a capture object mid-window / left permanently closed) and assert the
[webui] access-log line still reaches the real stdout file descriptor,
which is what journald reads.
"""
import io
import os
import sys
import tempfile
import unittest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import Handler

try:
    from api import log_stream
except ImportError:  # red state: fix module absent, access logs go via bare print()
    log_stream = None


def _make_handler():
    """Build a Handler carrying just the attributes log_request reads."""
    handler = Handler.__new__(Handler)
    handler.command = 'GET'
    handler.path = '/api/health/agent'
    handler._req_t0 = 0.0
    handler.client_address = ('127.0.0.1', 54321)
    handler.headers = {}
    return handler


class AccessLogStdoutHijackTest(unittest.TestCase):
    """[webui] access-log lines must survive in-process sys.stdout hijacks."""

    def setUp(self):
        # Redirect the real fd 1 to a temp file so the test can observe what
        # would reach journald, then re-point the durable log stream at it.
        self._tmp = tempfile.TemporaryFile()
        self._saved_fd = os.dup(1)
        os.dup2(self._tmp.fileno(), 1)
        if log_stream is not None:
            log_stream.capture_stdout_fd()
        self._saved_stdout = sys.stdout

    def tearDown(self):
        sys.stdout = self._saved_stdout
        os.dup2(self._saved_fd, 1)
        os.close(self._saved_fd)
        if log_stream is not None:
            log_stream.capture_stdout_fd()
        self._tmp.close()

    def _fd1_contents(self):
        self._tmp.seek(0)
        return self._tmp.read().decode('utf-8', errors='replace')

    def test_access_log_bypasses_hijacked_open_stdout(self):
        """A tool-call window swapped sys.stdout for its own stream object."""
        sys.stdout = io.StringIO()
        _make_handler().log_request(200)
        contents = self._fd1_contents()
        self.assertIn('[webui]', contents)
        self.assertIn('"/api/health/agent"', contents)

    def test_access_log_survives_permanently_closed_stdout(self):
        """The race's end state: sys.stdout is a closed file object forever."""
        closed = open(os.devnull, 'w', encoding='utf-8')
        closed.close()
        sys.stdout = closed
        _make_handler().log_request(200)
        contents = self._fd1_contents()
        self.assertIn('[webui]', contents)
        self.assertIn('"status": 200', contents)


if __name__ == '__main__':
    unittest.main()
