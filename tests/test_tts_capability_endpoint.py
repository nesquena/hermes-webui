"""Tests for GET /api/tts/capability endpoint.

Verifies the capability probe returns provider info and availability.
"""
import io
import json
import sys
from types import SimpleNamespace

import pytest

import api.routes as routes


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    from tests._tts_helpers import install_fake_hermes_cli
    install_fake_hermes_cli(monkeypatch)
    yield


class _FakeHandler:
    def __init__(self, command="GET", headers=None, client="127.0.0.1"):
        self.command = command
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.headers = headers or {}
        self.client_address = (client, 12345)
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        try:
            return json.loads(self.wfile.getvalue().decode("utf-8"))
        except Exception:
            return None


def test_capability_returns_provider_and_available(monkeypatch):
    """Capability endpoint returns provider from config and availability."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"tts": {"provider": "mistral"}},
        raising=False,
    )
    # Create a fake tts_tool module with check_tts_requirements
    fake = SimpleNamespace(check_tts_requirements=lambda: True)
    monkeypatch.setitem(sys.modules, "tools.tts_tool", fake)

    h = _FakeHandler()
    routes._handle_tts_capability(h)
    p = h.payload()
    assert p is not None
    assert p["ok"] is True
    assert p["provider"] == "mistral"
    assert p["available"] is True


def test_capability_defaults_to_edge_on_config_error(monkeypatch):
    """When config load fails, provider defaults to 'edge'."""
    def _bad_config():
        raise RuntimeError("no config")
    monkeypatch.setattr("hermes_cli.config.load_config", _bad_config, raising=False)
    # Remove tts_tool from sys.modules so the import fails
    monkeypatch.delitem(sys.modules, "tools.tts_tool", raising=False)

    h = _FakeHandler()
    routes._handle_tts_capability(h)
    p = h.payload()
    assert p is not None
    assert p["provider"] == "edge"


def test_capability_available_false_when_agent_missing(monkeypatch):
    """When check_tts_requirements is unavailable, available is False."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"tts": {"provider": "edge"}},
        raising=False,
    )
    # Mock a module where check_tts_requirements raises
    fake = SimpleNamespace(check_tts_requirements=lambda: (_ for _ in ()).throw(RuntimeError("no agent")))
    monkeypatch.setitem(sys.modules, "tools.tts_tool", fake)

    h = _FakeHandler()
    routes._handle_tts_capability(h)
    p = h.payload()
    assert p is not None
    assert p["available"] is False