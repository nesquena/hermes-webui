"""Tests for Mission #6289 r4: Gateway Agent-version cache.

Covers:
1. Total deadline (bounded chunks, socket timeout capped by budget, under 2.0s).
2. Two concurrent threads on trickling gateway: waiter released under 2.5s.
3. Cache keyed by resolved gateway base URL (A -> vA, B -> vB, A -> vA without extra probe).
4. Negative entry per key: unreachable gateway A does not block gateway B.
5. Payload contract preserving source AGENT_VERSION and exposing agent_gateway_version.
6. Node harness validating _agentVersionBadgeText helper extracted from static/panels.js.
"""

from __future__ import annotations

import http.client
import http.server
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

import api.routes as routes
import api.updates as upd
from tests.js_source_extract import extract_function


class _TrickleHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that sends headers then trickles bytes slowly (1 byte / 50ms)."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "200")
        self.end_headers()
        for _ in range(200):
            try:
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.05)
            except Exception:
                break

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def trickling_gateway():
    """Start an ephemeral ThreadingHTTPServer serving trickled responses."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TrickleHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.server_port
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=1.0)


def _reset_cache() -> None:
    """Clear the cache in either revision of the contract.

    A keyed cache (post-#6289) is an empty mapping; the pre-fix revision uses a
    single global slot, cleared by ``None``.  This keeps the behavioural
    assertions below meaningful when they run against a reverting blob.
    """
    upd._GATEWAY_AGENT_VERSION_CACHE = {} if isinstance(
        upd._GATEWAY_AGENT_VERSION_CACHE, dict
    ) else None


def _pin_gateway(monkeypatch, url: str) -> None:
    """Point every gateway-URL env var at ``url`` (the resolver prefers the first)."""
    monkeypatch.delenv("GATEWAY_HEALTH_URL", raising=False)
    monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", url)


def test_trickling_gateway_deadline_under_two_seconds(trickling_gateway, monkeypatch):
    """A trickling gateway abandons the read when the deadline expires and returns None in < 2.0s."""
    _reset_cache()
    _pin_gateway(monkeypatch, trickling_gateway)

    t0 = time.monotonic()
    result = upd._cached_agent_version_from_gateway()
    elapsed = time.monotonic() - t0

    assert result is None, "Trickling gateway probe must return None on deadline exhaustion"
    assert elapsed < 2.0, f"Expected probe to finish in under 2.0s, took {elapsed:.2f}s"


def test_two_threads_trickling_gateway_waiter_released(trickling_gateway, monkeypatch):
    """Two concurrent threads on an empty cache: the waiter is released under 2.5s."""
    _reset_cache()
    _pin_gateway(monkeypatch, trickling_gateway)

    results: list[str | None] = [None, None]
    durations: list[float] = [0.0, 0.0]

    def _worker(idx: int) -> None:
        t0 = time.monotonic()
        results[idx] = upd._cached_agent_version_from_gateway()
        durations[idx] = time.monotonic() - t0

    t1 = threading.Thread(target=_worker, args=(0,))
    t2 = threading.Thread(target=_worker, args=(1,))

    t_start = time.monotonic()
    t1.start()
    time.sleep(0.1)  # Ensure t1 acquires the probe lock first
    t2.start()

    t1.join(timeout=4.0)
    t2.join(timeout=4.0)
    total_elapsed = time.monotonic() - t_start

    assert not t1.is_alive(), "Thread 1 did not complete within timeout"
    assert not t2.is_alive(), "Thread 2 did not complete within timeout"
    assert results[0] is None
    assert results[1] is None
    assert total_elapsed < 2.5, (
        f"Both threads must complete under 2.5s, total took {total_elapsed:.2f}s "
        f"(t1: {durations[0]:.2f}s, t2: {durations[1]:.2f}s)"
    )


def test_cache_keyed_by_gateway(monkeypatch):
    """Cache is keyed by resolved gateway base URL: A -> vA, B -> vB, A -> vA without extra probe."""
    _reset_cache()

    call_counts: dict[str, int] = {"gateway-a": 0, "gateway-b": 0}

    class _MockResponse:
        def __init__(self, data: bytes):
            self._data = data
            self._offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, n: int = -1) -> bytes:
            if n < 0:
                chunk = self._data[self._offset:]
                self._offset = len(self._data)
                return chunk
            chunk = self._data[self._offset : self._offset + n]
            self._offset += len(chunk)
            return chunk

    def _fake_urlopen(url: str, timeout: float = 0.75):
        if "gateway-a" in url:
            call_counts["gateway-a"] += 1
            return _MockResponse(json.dumps({"version": "vA"}).encode("utf-8"))
        if "gateway-b" in url:
            call_counts["gateway-b"] += 1
            return _MockResponse(json.dumps({"version": "vB"}).encode("utf-8"))
        raise urllib.error.URLError("unknown gateway")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    # 1. Gateway A -> probes and caches vA
    monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", "http://gateway-a:8642")
    assert upd._cached_agent_version_from_gateway() == "vA"
    assert call_counts["gateway-a"] == 1

    # 2. Switch to Gateway B -> probes and caches vB
    monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", "http://gateway-b:8642")
    assert upd._cached_agent_version_from_gateway() == "vB"
    assert call_counts["gateway-b"] == 1

    # 3. Switch back to Gateway A -> serves vA from cache, zero extra probes
    monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", "http://gateway-a:8642")
    assert upd._cached_agent_version_from_gateway() == "vA"
    assert call_counts["gateway-a"] == 1, "Gateway A must be served from cache with no extra probe"


def test_negative_entry_per_key_does_not_block_other_gateways(monkeypatch):
    """A negative cache entry (unreachable gateway A) does not stop gateway B from probing."""
    _reset_cache()

    class _MockResponse:
        def __init__(self, data: bytes):
            self._data = data
            self._offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, n: int = -1) -> bytes:
            if n < 0:
                chunk = self._data[self._offset:]
                self._offset = len(self._data)
                return chunk
            chunk = self._data[self._offset : self._offset + n]
            self._offset += len(chunk)
            return chunk

    def _fake_urlopen(url: str, timeout: float = 0.75):
        if "gateway-a" in url:
            raise urllib.error.URLError("connection refused")
        if "gateway-b" in url:
            return _MockResponse(json.dumps({"version": "vB"}).encode("utf-8"))
        raise urllib.error.URLError("unknown gateway")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    # Gateway A fails -> caches negative entry None
    monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", "http://gateway-a:8642")
    assert upd._cached_agent_version_from_gateway() is None

    # Gateway B is probed immediately and returns vB
    monkeypatch.setenv("HERMES_GATEWAY_HEALTH_URL", "http://gateway-b:8642")
    assert upd._cached_agent_version_from_gateway() == "vB"


def test_settings_payload_contract_preserves_source_revision():
    """Settings route preserves AGENT_VERSION and exposes agent_gateway_version when available."""
    handler = MagicMock()
    parsed = urlparse("/api/settings")

    captured: dict[str, dict] = {}

    def _fake_j(h, data, status=200):
        captured["data"] = data

    # Subcase A: probe returns live version 'v0.80.0'
    with (
        patch("api.routes.load_settings", return_value={}),
        patch("api.routes.j", side_effect=_fake_j),
        patch.object(upd, "_cached_agent_version_from_gateway", return_value="v0.80.0"),
    ):
        routes.handle_get(handler, parsed)

    assert captured["data"]["agent_version"] == upd.AGENT_VERSION, (
        "agent_version must retain the source AGENT_VERSION"
    )
    assert captured["data"]["agent_gateway_version"] == "v0.80.0", (
        "agent_gateway_version must hold the live version"
    )

    # Subcase B: probe raises
    captured.clear()
    with (
        patch("api.routes.load_settings", return_value={}),
        patch("api.routes.j", side_effect=_fake_j),
        patch.object(
            upd,
            "_cached_agent_version_from_gateway",
            side_effect=RuntimeError("network error"),
        ),
    ):
        routes.handle_get(handler, parsed)

    assert captured["data"]["agent_version"] == upd.AGENT_VERSION
    assert "agent_gateway_version" not in captured["data"], (
        "agent_gateway_version must be absent when probe raises"
    )

    # Subcase C: probe returns None
    captured.clear()
    with (
        patch("api.routes.load_settings", return_value={}),
        patch("api.routes.j", side_effect=_fake_j),
        patch.object(upd, "_cached_agent_version_from_gateway", return_value=None),
    ):
        routes.handle_get(handler, parsed)

    assert captured["data"]["agent_version"] == upd.AGENT_VERSION
    assert "agent_gateway_version" not in captured["data"], (
        "agent_gateway_version must be absent when probe returns None"
    )


def test_node_harness_badge_text_helper():
    """Extract _agentVersionBadgeText from static/panels.js and assert the 3 badge strings via node stdin."""
    js_path = Path(__file__).resolve().parent.parent / "static" / "panels.js"
    js_content = js_path.read_text(encoding="utf-8")
    fn_code = extract_function(js_content, "_agentVersionBadgeText")
    assert fn_code, "_agentVersionBadgeText must be extracted from static/panels.js"

    node_script = f"""
{fn_code}

const tests = [
  _agentVersionBadgeText({{ agent_version: "v0.23.3" }}),
  _agentVersionBadgeText({{ agent_version: "v0.23.3", agent_gateway_version: "v0.23.3" }}),
  _agentVersionBadgeText({{ agent_version: "v0.23.3", agent_gateway_version: "v0.80.0" }})
];

console.log(JSON.stringify(tests));
"""

    res = subprocess.run(
        ["node"],
        input=node_script,
        text=True,
        capture_output=True,
        check=True,
    )
    outputs = json.loads(res.stdout.strip())
    assert len(outputs) == 3

    # Case 1: no agent_gateway_version -> Agent: <source>
    assert outputs[0] == "Agent: v0.23.3"

    # Case 2: gateway value equal to source revision -> Agent: <source> (no duplicate suffix)
    assert outputs[1] == "Agent: v0.23.3"

    # Case 3: different value -> Agent: <source> · gateway <value>
    assert outputs[2] == "Agent: v0.23.3 · gateway v0.80.0"
