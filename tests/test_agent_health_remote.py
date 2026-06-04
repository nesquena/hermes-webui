"""Tests for HERMES_API_URL remote gateway probe (#3281, #3355)."""
from __future__ import annotations

import json
from unittest import mock

import pytest

from api import agent_health


@pytest.fixture(autouse=True)
def _clear_cache():
    agent_health._reset_remote_probe_cache_for_tests()
    yield
    agent_health._reset_remote_probe_cache_for_tests()


class _FakeResp:
    def __init__(self, status: int = 200, body: bytes = b""):
        self.status = status
        self._body = body

    def getcode(self) -> int:
        return self.status

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            return self._body
        return self._body[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_remote_gateway_healthy_when_200(monkeypatch):
    monkeypatch.setenv("HERMES_API_URL", "http://gateway:8080")
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _FakeResp(200)

    with mock.patch.object(agent_health.urllib_request, "urlopen", fake_urlopen):
        payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is True
    assert payload["details"]["reason"] == "remote_gateway"
    assert payload["details"]["status_code"] == 200
    assert calls and calls[0].startswith("http://gateway:8080/")


def test_remote_gateway_unreachable_when_network_error(monkeypatch):
    monkeypatch.setenv("HERMES_API_URL", "http://gateway:8080/")

    def fake_urlopen(req, timeout=None):
        raise OSError("connection refused")

    with mock.patch.object(agent_health.urllib_request, "urlopen", fake_urlopen):
        payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is False
    assert payload["details"]["reason"] == "remote_gateway_unreachable"
    assert payload["details"]["endpoint"] == "http://gateway:8080"
    assert "error" in payload["details"]


def test_falls_back_to_local_when_no_env(monkeypatch):
    monkeypatch.delenv("HERMES_API_URL", raising=False)

    # Force the local importlib path to fail so we hit the well-known
    # "gateway_not_configured" terminal state — proving the remote probe was
    # NOT invoked and the legacy local path ran.
    def boom(name):
        raise ModuleNotFoundError(name)

    with mock.patch.object(agent_health.importlib, "import_module", boom):
        payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is None
    assert payload["details"]["reason"] == "gateway_status_unavailable"


def test_remote_probe_result_cached_for_5s(monkeypatch):
    monkeypatch.setenv("HERMES_API_URL", "http://gateway:8080")
    call_count = {"n": 0}

    def fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        return _FakeResp(200)

    with mock.patch.object(agent_health.urllib_request, "urlopen", fake_urlopen):
        first = agent_health.build_agent_health_payload()
        second = agent_health.build_agent_health_payload()

    assert first["alive"] is True
    assert second["alive"] is True
    assert second["details"]["reason"] == "remote_gateway"
    # Second call must NOT have hit the network.
    assert call_count["n"] == 1
    # checked_at is refreshed even on cache hit so the UI shows a current time.
    assert "checked_at" in second


# ── #3355: gateway_state extraction and probe-order tests ─────────────────


def test_gateway_state_populated_from_health_detailed(monkeypatch):
    """Remote probe should extract gateway_state from /health/detailed JSON body."""
    monkeypatch.setenv("HERMES_API_URL", "http://fake-gateway:8642")
    body = json.dumps({"gateway_state": "running", "uptime": 3600}).encode()

    def fake_urlopen(req, timeout=None):
        return _FakeResp(200, body=body)

    with mock.patch.object(agent_health.urllib_request, "urlopen", fake_urlopen):
        payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is True
    assert payload["details"]["gateway_state"] == "running"
    assert payload["details"]["reason"] == "remote_gateway"


def test_probe_order_prefers_health_detailed(monkeypatch):
    """First probe path should be /health/detailed so gateway_state is available."""
    monkeypatch.setenv("HERMES_API_URL", "http://fake-gateway:8642")
    probed_urls: list[str] = []

    def fake_urlopen(req, timeout=None):
        probed_urls.append(req.full_url)
        # Return 200 on first hit so the loop stops immediately
        return _FakeResp(200, body=b'{"gateway_state": "running"}')

    with mock.patch.object(agent_health.urllib_request, "urlopen", fake_urlopen):
        agent_health.build_agent_health_payload()

    assert len(probed_urls) == 1
    assert probed_urls[0] == "http://fake-gateway:8642/health/detailed"


def test_gateway_health_url_env_used(monkeypatch):
    """GATEWAY_HEALTH_URL should be respected by the remote probe."""
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_HEALTH_URL", raising=False)
    monkeypatch.setenv("GATEWAY_HEALTH_URL", "http://custom:9999")
    probed_urls: list[str] = []

    def fake_urlopen(req, timeout=None):
        probed_urls.append(req.full_url)
        return _FakeResp(200, body=b'{}')

    with mock.patch.object(agent_health.urllib_request, "urlopen", fake_urlopen):
        payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is True
    assert probed_urls[0].startswith("http://custom:9999/")


def test_default_url_when_no_env(monkeypatch):
    """Without any gateway env vars, should fall back to local detection (not remote)."""
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("GATEWAY_HEALTH_URL", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_HEALTH_URL", raising=False)

    # Force the local importlib path to fail so we hit "gateway_status_unavailable",
    # proving the remote probe was NOT invoked.
    def boom(name):
        raise ModuleNotFoundError(name)

    with mock.patch.object(agent_health.importlib, "import_module", boom):
        payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is None
    assert payload["details"]["reason"] == "gateway_status_unavailable"


def test_gateway_health_url_with_health_suffix_is_normalized(monkeypatch):
    """A gateway env var that already points AT a health path must not produce
    a doubled path like /health/health/detailed (#3355 Codex follow-up)."""
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_HEALTH_URL", raising=False)
    monkeypatch.setenv("GATEWAY_HEALTH_URL", "http://gw:8642/health")
    probed: list[str] = []

    def fake_urlopen(req, timeout=None):
        probed.append(req.full_url)
        if req.full_url == "http://gw:8642/health/detailed":
            return _FakeResp(200, body=json.dumps({"gateway_state": "running"}).encode())
        return _FakeResp(404)

    with mock.patch.object(agent_health.urllib_request, "urlopen", fake_urlopen):
        payload = agent_health.build_agent_health_payload()

    assert all("/health/health" not in u for u in probed), probed
    assert "http://gw:8642/health/detailed" in probed
    assert payload["alive"] is True
    assert payload["details"].get("gateway_state") == "running"


def test_gateway_webui_base_url_env_is_used_for_remote_probe(monkeypatch):
    """`HERMES_WEBUI_GATEWAY_BASE_URL` should be treated like a gateway health base URL."""
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_HEALTH_URL", raising=False)
    monkeypatch.delenv("GATEWAY_HEALTH_URL", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://container-gw:8642")
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _FakeResp(200, body=b'{"gateway_state":"running"}')

    with mock.patch.object(agent_health.urllib_request, "urlopen", fake_urlopen):
        payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is True
    assert payload["details"]["reason"] == "remote_gateway"
    assert seen[0].startswith("http://container-gw:8642/")


def test_oversized_remote_body_does_not_hang_and_skips_parse(monkeypatch):
    """A 2xx response with a huge body must be capped (not read unbounded) and
    still report the gateway alive, just without a parsed gateway_state."""
    monkeypatch.setenv("HERMES_API_URL", "http://gateway:8080")
    huge = b"x" * (agent_health._REMOTE_PROBE_BODY_LIMIT_BYTES * 4)
    captured_amt: list[int | None] = []

    class _HugeResp(_FakeResp):
        def read(self, amt: int | None = None):
            captured_amt.append(amt)
            return huge[:amt] if amt is not None else huge

    def fake_urlopen(req, timeout=None):
        return _HugeResp(200, body=huge)

    with mock.patch.object(agent_health.urllib_request, "urlopen", fake_urlopen):
        payload = agent_health.build_agent_health_payload()

    assert captured_amt and all(a is not None for a in captured_amt)
    assert payload["alive"] is True
    assert "gateway_state" not in payload["details"]
