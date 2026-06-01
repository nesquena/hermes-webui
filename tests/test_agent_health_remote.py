"""Tests for HERMES_API_URL remote gateway probe (#3281)."""
from __future__ import annotations

from unittest import mock

import pytest

from api import agent_health


@pytest.fixture(autouse=True)
def _clear_cache():
    agent_health._reset_remote_probe_cache_for_tests()
    yield
    agent_health._reset_remote_probe_cache_for_tests()


class _FakeResp:
    def __init__(self, status: int = 200):
        self.status = status

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return b""

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
