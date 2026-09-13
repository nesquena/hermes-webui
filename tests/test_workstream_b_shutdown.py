"""Focused Workstream B regressions for graceful supervisor shutdown."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler


def test_graceful_shutdown_stops_admission_and_waits_for_active_worker():
    import api.config as config
    from server import Handler, QuietHTTPServer

    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    worker_released = threading.Event()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS["run-under-test"] = {
            "stream_id": "run-under-test",
            "session_id": "session-under-test",
            "phase": "running",
        }

    httpd = QuietHTTPServer(("127.0.0.1", port), Handler)
    try:
        assert httpd.begin_graceful_shutdown(reason="test", timeout_seconds=0.5) is True
        assert httpd.is_draining is True
        assert httpd.shutdown_state == "draining"
        assert httpd.admission_open is False

        def release_worker():
            time.sleep(0.05)
            with config.ACTIVE_RUNS_LOCK:
                config.ACTIVE_RUNS.pop("run-under-test", None)
            worker_released.set()

        threading.Thread(target=release_worker, daemon=True).start()
        result = httpd.wait_for_graceful_drain()

        assert worker_released.is_set()
        assert result["state"] == "drained"
        assert result["active_workers"] == 0
        assert httpd.shutdown_state == "drained"
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.pop("run-under-test", None)
        httpd.server_close()


def test_graceful_shutdown_rejects_new_request_admission():
    from server import QuietHTTPServer

    entered = threading.Event()
    release = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):  # noqa: N802
            entered.set()
            release.wait(timeout=2)
            body = b"released"
            self.send_response(200)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = QuietHTTPServer(("127.0.0.1", 0), _Handler)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    first = http.client.HTTPConnection(*httpd.server_address, timeout=2)
    second = http.client.HTTPConnection(*httpd.server_address, timeout=2)
    server_thread.start()
    try:
        first.request("GET", "/hold")
        assert entered.wait(timeout=2)
        assert httpd.begin_graceful_shutdown(reason="test", timeout_seconds=1) is True

        second.request("GET", "/new")
        response = second.getresponse()
        assert response.status == 503
        assert json.loads(response.read()) == {"status": "draining"}

        release.set()
        first_response = first.getresponse()
        assert first_response.status == 200
        assert first_response.read() == b"released"
        httpd.shutdown()
        server_thread.join(timeout=2)
        assert not server_thread.is_alive()
        assert httpd.wait_for_graceful_drain()["state"] == "drained"
    finally:
        release.set()
        first.close()
        second.close()
        httpd.shutdown()
        httpd.server_close()


def test_graceful_shutdown_has_bounded_deadline_and_force_is_immediate():
    import api.config as config
    from server import Handler, QuietHTTPServer

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS["stuck-run"] = {
            "stream_id": "stuck-run",
            "session_id": "session-under-test",
            "phase": "running",
        }

    httpd = QuietHTTPServer(("127.0.0.1", 0), Handler)
    try:
        assert httpd.begin_graceful_shutdown(reason="test", timeout_seconds=0.01) is True
        started = time.monotonic()
        result = httpd.wait_for_graceful_drain()
        elapsed = time.monotonic() - started

        assert result["state"] == "timed_out"
        assert elapsed < 0.5

        forced = QuietHTTPServer(("127.0.0.1", 0), Handler)
        try:
            assert forced.force_shutdown(reason="test") is True
            assert forced.is_draining is True
            assert forced.shutdown_state == "forced"
            assert forced.admission_open is False
        finally:
            forced.server_close()
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.pop("stuck-run", None)
        httpd.server_close()


def test_ctl_force_stop_is_explicit_and_only_targets_ctl_owned_process(tmp_path):
    from tests.test_ctl_script import (
        _kill_tree,
        assert_process_exits,
        run_ctl,
        wait_for_pid_file,
        write_fake_python,
    )

    fake_python = tmp_path / "fake-python"
    fake_log = tmp_path / "fake-python.log"
    write_fake_python(fake_python)
    env = {
        "HERMES_WEBUI_PYTHON": str(fake_python),
        "FAKE_PYTHON_LOG": str(fake_log),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_PORT": "18992",
        "HERMES_WEBUI_CTL_ALLOW_LAUNCHD_CONFLICT": "1",
    }

    started = run_ctl(tmp_path, "start", env=env)
    assert started.returncode == 0, started.stderr + started.stdout
    pid = wait_for_pid_file(tmp_path / ".hermes" / "webui.pid")
    try:
        stopped = run_ctl(tmp_path, "stop", "--force", env=env)
        assert stopped.returncode == 0, stopped.stderr + stopped.stdout
        assert "Force-stopping" in stopped.stdout
        assert_process_exits(pid)
    finally:
        _kill_tree(pid)
