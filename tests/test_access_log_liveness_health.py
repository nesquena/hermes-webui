"""
Ticket #0574: a log-blind WebUI must stop being invisible.

The #0095 durable-fd writer stops the known `sys.stdout` hijack from killing
access logs, but the shape that made it cost ~54h of blind service was the
*silence*: `_safe_webui_print` swallowed the write failure, `/health` kept
answering 200, and nothing anywhere reported that the journal had gone quiet
while requests were still being served.

So a lost log line must now (a) announce itself once on a stderr duplicate the
same hijack can't touch, and (b) be reportable — `GET /api/health/logging`
answers 503 for a serving-but-log-blind process, which is what a plain URL
health-watch can actually detect.
"""
import json
import os
import tempfile
from urllib.parse import urlparse

import pytest

from api import log_stream, routes


@pytest.fixture
def reset_log_stream():
    """Give each test a pristine module state and restore the real one after."""
    saved = (
        log_stream._log_fd,
        log_stream._alarm_fd,
        log_stream._emitted,
        log_stream._dropped,
        log_stream._last_emit_at,
        log_stream._last_error,
        log_stream._alarm_raised,
    )
    log_stream._emitted = 0
    log_stream._dropped = 0
    log_stream._last_emit_at = None
    log_stream._last_error = None
    log_stream._alarm_raised = False
    yield
    (
        log_stream._log_fd,
        log_stream._alarm_fd,
        log_stream._emitted,
        log_stream._dropped,
        log_stream._last_emit_at,
        log_stream._last_error,
        log_stream._alarm_raised,
    ) = saved


class _FakeHandler:
    """Minimal handler capturing what j() writes back."""

    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _get(path):
    handler = _FakeHandler()
    # handle_get signals "no such route" with a literal False (server.py:386);
    # a handler that answered via j() returns None, like /health does.
    assert routes.handle_get(handler, urlparse(f"http://example.test{path}")) is not False
    return handler


def test_healthy_writer_reports_ok_and_counts_lines(reset_log_stream):
    log_stream.durable_print("[webui] hello")

    status = log_stream.access_log_status()
    assert status["status"] == "ok"
    assert status["healthy"] is True
    assert status["durable_fd"] is True
    assert status["emitted"] == 1
    assert status["dropped"] == 0
    assert status["last_error"] is None
    assert status["last_line_age_seconds"] is not None


def test_failed_write_is_counted_and_announced_once_on_stderr(reset_log_stream):
    """The old bare `except: pass` is what made a dead log stream look idle."""
    alarm = tempfile.TemporaryFile()
    log_stream._alarm_fd = alarm.fileno()
    # Closed fd: os.write raises EBADF, the same shape as the wedge (a write
    # that fails for the life of the process while the service keeps serving).
    dead_fd = os.dup(1)
    os.close(dead_fd)
    log_stream._log_fd = dead_fd

    log_stream.durable_print("[webui] first lost line")
    log_stream.durable_print("[webui] second lost line")

    status = log_stream.access_log_status()
    assert status["status"] == "failing"
    assert status["healthy"] is False
    assert status["dropped"] == 2
    assert status["emitted"] == 0
    assert "OSError" in status["last_error"]

    alarm.seek(0)
    announced = alarm.read().decode("utf-8", errors="replace")
    # Loud on the first loss, and exactly once — a per-request alarm would be
    # its own log flood.
    assert announced.count("access logging is dropping lines") == 1
    assert "/api/health/logging" in announced
    alarm.close()
    log_stream._log_fd = None


def test_missing_durable_fd_reports_degraded(reset_log_stream):
    """Without the private dup, logging rides the sys.stdout #0095 can hijack."""
    log_stream._log_fd = None

    status = log_stream.access_log_status()
    assert status["status"] == "degraded"
    assert status["healthy"] is False
    assert status["durable_fd"] is False


def test_logging_health_endpoint_is_200_when_logs_flow(reset_log_stream):
    log_stream.durable_print("[webui] alive")

    handler = _get("/api/health/logging")

    assert handler.status == 200
    payload = handler.json_body()
    assert payload["status"] == "ok"
    assert payload["emitted"] >= 1


def test_logging_health_endpoint_is_503_when_serving_but_log_blind(reset_log_stream):
    """The detector: 200 elsewhere, 503 here, so a URL watch can see the wedge."""
    log_stream._dropped = 3
    log_stream._last_error = "ValueError: I/O operation on closed file"

    handler = _get("/api/health/logging")

    assert handler.status == 503
    payload = handler.json_body()
    assert payload["status"] == "failing"
    assert payload["healthy"] is False
    assert payload["dropped"] == 3


def test_logging_health_endpoint_needs_no_auth():
    """An unauthenticated monitor must be able to poll it, like /health."""
    from api.auth import PUBLIC_PATHS

    assert "/api/health/logging" in PUBLIC_PATHS
