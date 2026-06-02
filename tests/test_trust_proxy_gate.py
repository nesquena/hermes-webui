from types import SimpleNamespace

from api.auth import _is_secure_context
from api.helpers import _trust_proxy


class _Handler:
    def __init__(self, headers):
        self.headers = headers
        self.request = object()


def test_forwarded_proto_is_ignored_without_trust_proxy(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_TRUST_PROXY", raising=False)
    assert _trust_proxy() is False
    assert _is_secure_context(_Handler({"X-Forwarded-Proto": "https"})) is False


def test_forwarded_proto_is_honored_with_trust_proxy(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TRUST_PROXY", "1")
    assert _trust_proxy() is True
    assert _is_secure_context(_Handler({"X-Forwarded-Proto": "https"})) is True
