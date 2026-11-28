"""Tests for #6289 r5: the Settings latency budget and the probed-endpoint binding.

Review 2026-11-23T03:44:33Z left two items open on head ``c549b9084``:

1. ``_detect_agent_version_from_gateway_health`` checks the total deadline around
   each ``resp.read(CHUNK)``, but one read of that size performs several socket
   receives and every receive is allowed the full per-operation timeout, so a
   gateway that trickles a byte every 200 ms blocks a real ``/api/settings``
   read for ~3.0 s against the 1.5 s budget.
2. ``_cached_agent_version_from_gateway`` resolves the cache key with
   ``_gateway_health_base_url()`` and then lets the detector resolve the URL it
   probes AGAIN, so an environment mirrored between the two reads publishes
   gateway B's version under gateway A's key -- and A keeps serving it after
   the environment is restored.

The first two cases are the defects; the last two are controls that must pass
on both blobs.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

import api.routes as routes
import api.updates as upd


class _SlowTrickleHandler(http.server.BaseHTTPRequestHandler):
    """One body byte every 200 ms -- the cadence the review measured at ~3.0 s."""

    body_bytes = 200
    delay = 0.20

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(self.body_bytes))
        self.end_headers()
        for _ in range(self.body_bytes):
            try:
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(self.delay)
            except Exception:
                return

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def slow_trickling_gateway():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowTrickleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


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


def test_slow_trickle_gateway_returns_within_the_deadline(slow_trickling_gateway, monkeypatch):
    """One slow-trickle read must abandon the body when the total budget expires."""
    _pin_gateway(monkeypatch, slow_trickling_gateway)
    saved = _save_and_clear_cache()
    try:
        started = time.monotonic()
        result = upd._cached_agent_version_from_gateway()
        elapsed = time.monotonic() - started
    finally:
        _restore_cache(saved)

    budget = upd._GATEWAY_AGENT_VERSION_DEADLINE
    assert result is None, "a trickling gateway must not yield a version"
    assert elapsed < budget + 0.5, (
        f"probe took {elapsed:.2f}s for a {budget:.1f}s budget: one body read still "
        "performs several receives, each allowed the full per-operation timeout"
    )


def _settings_read_raw() -> float:
    """Drive one real /api/settings read with ``load_settings``/``j`` already patched."""
    handler = MagicMock()
    parsed = urlparse("/api/settings")
    started = time.monotonic()
    routes.handle_get(handler, parsed)
    return time.monotonic() - started


def test_two_real_settings_readers_keep_the_settings_latency_budget(
    slow_trickling_gateway, monkeypatch
):
    """Two real /api/settings reads: the probe part stays inside the deadline.

    The handler also does pre-existing work that has nothing to do with the probe
    (measured ~0.5 s on this host), so the assertion is differential: the penalty
    the trickling probe adds over a control read that never probes must stay
    inside the deadline budget.  Both patches are entered ONCE around the whole
    exercise: two threads entering the same ``patch`` contexts would restore each
    other's mocks mid-flight and swallow the payload.
    """
    _pin_gateway(monkeypatch, slow_trickling_gateway)
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
                t.join(timeout=20.0)
    finally:
        _restore_cache(saved)

    budget = upd._GATEWAY_AGENT_VERSION_DEADLINE
    assert not any(t.is_alive() for t in threads), "settings readers never returned"
    assert len(durations) == 2, f"expected two settings readers, saw {len(durations)}"
    assert len(captured) == 3, f"expected three settings payloads, saw {len(captured)}"
    penalty = max(durations) - control_elapsed
    assert penalty < budget + 0.4, (
        f"the trickling probe added {penalty:.2f}s to a real settings read against a "
        f"{budget:.1f}s budget (control {control_elapsed:.2f}s, trickle "
        f"{[round(d, 2) for d in durations]})"
    )
    for payload in captured[1:]:
        assert payload.get("agent_version") == upd.AGENT_VERSION, (
            "a failed live probe must keep the import-time agent_version"
        )


class _FakeResponse:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._offset:]
            self._offset = len(self._data)
            return chunk
        chunk = self._data[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


def _fake_urlopen_factory(probed: list[str], versions: dict[str, str]):
    def _fake(url: str, timeout: float = 0.75):
        probed.append(url)
        for host, version in versions.items():
            if host in url:
                return _FakeResponse({"version": version})
        raise urllib.error.URLError(f"unknown gateway in {url}")

    return _fake


def test_published_entry_is_bound_to_the_endpoint_that_was_probed(monkeypatch):
    """An environment mirrored between key resolution and probe must not cross-publish."""
    probed: list[str] = []
    monkeypatch.setattr(
        urllib.request, "urlopen",
        _fake_urlopen_factory(probed, {"gateway-a": "vA", "gateway-b": "vB"}),
    )
    monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", "http://gateway-a:8642")

    saved = _save_and_clear_cache()
    real_detector = upd._detect_agent_version_from_gateway_health
    mirrored = {"done": False}

    def mirroring_detector(*args, **kwargs):
        # A profile worker mirrors another profile's environment into os.environ
        # between the cache key resolution and the probe.
        if not mirrored["done"]:
            mirrored["done"] = True
            monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", "http://gateway-b:8642")
        return real_detector(*args, **kwargs)

    monkeypatch.setattr(upd, "_detect_agent_version_from_gateway_health", mirroring_detector)
    try:
        first = upd._cached_agent_version_from_gateway()
        monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", "http://gateway-a:8642")
        probed_before_restore = len(probed)
        second = upd._cached_agent_version_from_gateway()
    finally:
        _restore_cache(saved)
        monkeypatch.setattr(upd, "_detect_agent_version_from_gateway_health", real_detector)

    assert first == "vA", (
        f"gateway A was the resolved endpoint but the call returned {first!r} "
        f"(probed={probed})"
    )
    assert all("gateway-a" in url for url in probed), (
        f"every probe must hit the endpoint resolved once for the key, saw {probed}"
    )
    assert second == "vA", (
        f"after the environment was restored, gateway A reported {second!r} instead of vA"
    )
    assert len(probed) == probed_before_restore, (
        "the restored gateway A answer must come from its own cache entry"
    )


def test_cache_is_still_keyed_by_gateway(monkeypatch):
    """Control: A -> vA, B -> vB, back to A -> cached vA with no extra probe."""
    probed: list[str] = []
    monkeypatch.setattr(
        urllib.request, "urlopen",
        _fake_urlopen_factory(probed, {"gateway-a": "vA", "gateway-b": "vB"}),
    )
    saved = _save_and_clear_cache()
    try:
        monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", "http://gateway-a:8642")
        assert upd._cached_agent_version_from_gateway() == "vA"
        monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", "http://gateway-b:8642")
        assert upd._cached_agent_version_from_gateway() == "vB"
        monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", "http://gateway-a:8642")
        probed_before = len(probed)
        assert upd._cached_agent_version_from_gateway() == "vA"
        assert len(probed) == probed_before, "gateway A must be served from its own entry"
    finally:
        _restore_cache(saved)


def test_healthy_gateway_still_returns_its_version(monkeypatch):
    """Control: a fast, healthy gateway still yields its version."""
    probed: list[str] = []
    monkeypatch.setattr(
        urllib.request, "urlopen",
        _fake_urlopen_factory(probed, {"gateway-a": "v1.2.3"}),
    )
    monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", "http://gateway-a:8642")
    saved = _save_and_clear_cache()
    try:
        started = time.monotonic()
        assert upd._cached_agent_version_from_gateway() == "v1.2.3"
        assert time.monotonic() - started < 1.0
    finally:
        _restore_cache(saved)
