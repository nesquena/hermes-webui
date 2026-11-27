"""Tests for #6289 r6: the total deadline has to cover the response HEADERS.

Re-review 2026-11-27T16:45:19Z on head ``4c7a68c90`` credited the endpoint
binding, the byte ceiling and the body-read capping, and left one part of the
original total-deadline request open: ``urllib.request.urlopen(..., timeout=)``
limits individual socket operations only, so a gateway that trickles the
header block keeps the probe alive far past
``_GATEWAY_AGENT_VERSION_DEADLINE``.  The first deadline check and the first
socket re-arm happen only after the headers are complete.

The fixture sends a normal status line and then 20 header-value bytes at
200 ms intervals (the schedule the maintainer measured at ~4.0 s against a
1.5 s budget), finishing with a well-formed JSON body so a probe that survives
the header phase would publish a version.

The first two cases are the defects; the last one is the healthy control that
must pass on both blobs.
"""

from __future__ import annotations

import http.server
import threading
import time
from urllib.parse import urlparse
from unittest.mock import MagicMock, patch

import pytest

import api.routes as routes
import api.updates as upd


class _SlowHeaderHandler(http.server.BaseHTTPRequestHandler):
    """Status line now, then header-value bytes at 200 ms each (#6289 r6)."""

    slow_bytes = 20
    delay = 0.20
    requests = 0

    def do_GET(self) -> None:  # noqa: N802
        type(self).requests += 1
        try:
            # Written by hand: BaseHTTPRequestHandler buffers the status line
            # until end_headers(), which would hide the trickle entirely.
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            self.wfile.write(b"X-Slow: ")
            self.wfile.flush()
            for _ in range(self.slow_bytes):
                self.wfile.write(b"a")
                self.wfile.flush()
                time.sleep(self.delay)
            body = b'{"version": "v-slow-header"}'
            self.wfile.write(b"\r\nContent-Type: application/json\r\n")
            self.wfile.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n")
            self.wfile.write(body)
            self.wfile.flush()
        except Exception:
            return
        self.close_connection = True

    def log_message(self, *args: object) -> None:
        pass


class _HealthyHandler(http.server.BaseHTTPRequestHandler):
    """A gateway that answers immediately, as the control arm."""

    def do_GET(self) -> None:  # noqa: N802
        body = b'{"version": "v-healthy"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _serve(handler) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def slow_header_gateway():
    _SlowHeaderHandler.requests = 0
    server = _serve(_SlowHeaderHandler)
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def healthy_gateway():
    server = _serve(_HealthyHandler)
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _pin_gateway(monkeypatch, url: str) -> None:
    monkeypatch.delenv("GATEWAY_HEALTH_URL", raising=False)
    monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", url)


def _save_and_clear_cache():
    saved = upd._GATEWAY_AGENT_VERSION_CACHE
    with upd._gateway_version_lock:
        upd._GATEWAY_AGENT_VERSION_CACHE = {}
    return saved


def _restore_cache(saved) -> None:
    with upd._gateway_version_lock:
        upd._GATEWAY_AGENT_VERSION_CACHE = saved


def _settings_read_raw() -> float:
    """Drive one real /api/settings read with ``load_settings``/``j`` patched."""
    handler = MagicMock()
    parsed = urlparse("/api/settings")
    started = time.monotonic()
    routes.handle_get(handler, parsed)
    return time.monotonic() - started


def test_slow_header_gateway_is_abandoned_within_the_budget(
    slow_header_gateway, monkeypatch
):
    """The header phase must be inside the budget, not the full socket timeout."""
    _pin_gateway(monkeypatch, slow_header_gateway)
    saved = _save_and_clear_cache()
    try:
        started = time.monotonic()
        result = upd._cached_agent_version_from_gateway()
        elapsed = time.monotonic() - started
    finally:
        _restore_cache(saved)

    budget = upd._GATEWAY_AGENT_VERSION_DEADLINE
    assert elapsed < budget + 0.75, (
        f"the probe took {elapsed:.2f}s against a {budget:.1f}s budget: urlopen keeps "
        "every header receive inside the per-operation timeout, so the deadline only "
        "starts being enforced after the header block is complete"
    )
    assert result is None, (
        f"a gateway that never finishes the headers inside the budget yielded {result!r}"
    )
    assert _SlowHeaderHandler.requests == 1, (
        f"the abandoned probe must not walk on to /health/detailed, saw "
        f"{_SlowHeaderHandler.requests} requests"
    )


def test_two_real_settings_readers_stay_within_the_budget_on_slow_headers(
    slow_header_gateway, monkeypatch
):
    """Two real /api/settings reads share one request and both return in budget.

    The handler also does pre-existing work that has nothing to do with the
    probe (measured ~0.5 s on this host), so the assertion is differential: the
    penalty the slow-header probe adds over a control read that never probes
    must stay inside the budget.  Both patches are entered ONCE around the
    whole exercise: two threads entering the same ``patch`` contexts would
    restore each other's mocks mid-flight and swallow the payload.
    """
    _pin_gateway(monkeypatch, slow_header_gateway)
    saved = _save_and_clear_cache()
    captured: list[dict] = []
    capture_lock = threading.Lock()
    durations: list[float] = []
    threads: list[threading.Thread] = []

    def fake_j(_h, data, status=200):
        with capture_lock:
            captured.append(dict(data))

    try:
        with patch("api.routes.load_settings", return_value={}), \
                patch("api.routes.j", side_effect=fake_j):
            # Control: a fresh positive entry means the handler never probes.
            with upd._gateway_version_lock:
                upd._GATEWAY_AGENT_VERSION_CACHE = {
                    upd._gateway_health_base_url(): (upd.AGENT_VERSION, time.monotonic())
                }
            control_elapsed = _settings_read_raw()
            assert captured[-1].get("agent_version") == upd.AGENT_VERSION, (
                "the control read must publish the import-time agent_version"
            )

            # Defect arm: two concurrent readers on a cold cache.
            with upd._gateway_version_lock:
                upd._GATEWAY_AGENT_VERSION_CACHE = {}
            _SlowHeaderHandler.requests = 0

            result_lock = threading.Lock()
            barrier = threading.Barrier(2)

            def reader() -> None:
                barrier.wait()
                elapsed = _settings_read_raw()
                with result_lock:
                    durations.append(elapsed)

            threads = [threading.Thread(target=reader) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30.0)
    finally:
        _restore_cache(saved)

    budget = upd._GATEWAY_AGENT_VERSION_DEADLINE
    assert not any(t.is_alive() for t in threads), "settings readers never returned"
    assert len(durations) == 2, f"expected two settings readers, saw {len(durations)}"
    assert len(captured) == 3, f"expected three settings payloads, saw {len(captured)}"
    penalty = max(durations) - control_elapsed
    assert penalty < budget + 0.75, (
        f"the slow-header probe added {penalty:.2f}s to a real settings read against a "
        f"{budget:.1f}s budget (control {control_elapsed:.2f}s, readers "
        f"{[round(d, 2) for d in durations]})"
    )
    assert _SlowHeaderHandler.requests == 1, (
        "concurrent readers of one gateway must share a single probe, saw "
        f"{_SlowHeaderHandler.requests} requests"
    )
    for payload in captured[1:]:
        assert payload.get("agent_version") == upd.AGENT_VERSION, (
            "an abandoned live probe must keep the import-time agent_version"
        )


def test_healthy_gateway_still_reports_its_version(healthy_gateway, monkeypatch):
    """Control: a fast, well-formed gateway still yields its live version."""
    _pin_gateway(monkeypatch, healthy_gateway)
    saved = _save_and_clear_cache()
    try:
        started = time.monotonic()
        version = upd._cached_agent_version_from_gateway()
        elapsed = time.monotonic() - started
    finally:
        _restore_cache(saved)

    assert version == "v-healthy", f"healthy gateway reported {version!r}"
    assert elapsed < 1.0, f"the healthy control took {elapsed:.2f}s"
