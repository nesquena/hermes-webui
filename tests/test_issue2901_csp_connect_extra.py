"""Regression coverage for configurable CSP connect-src extras (#2901)."""

from __future__ import annotations


def test_csp_connect_src_default_header_unchanged(monkeypatch):
    from server import Handler

    monkeypatch.delenv("HERMES_WEBUI_CSP_CONNECT_EXTRA", raising=False)

    policy = Handler.csp_report_only_policy()
    expected = (
        "connect-src 'self' http://127.0.0.1:* http://localhost:* "
        "http://ipc.localhost ws://127.0.0.1:* ws://localhost:* "
        "https://cdn.jsdelivr.net; "
    )

    assert expected in policy


def test_csp_connect_src_includes_valid_extra_origins(monkeypatch):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_CONNECT_EXTRA",
        "https://metrics.example.com wss://events.example.com:443",
    )

    policy = Handler.csp_report_only_policy()

    assert (
        "connect-src 'self' http://127.0.0.1:* http://localhost:* "
        "http://ipc.localhost ws://127.0.0.1:* ws://localhost:* "
        "https://cdn.jsdelivr.net "
        "https://metrics.example.com wss://events.example.com:443; "
    ) in policy


def test_csp_connect_src_includes_explicit_trusted_sidecar_origin(monkeypatch):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_CONNECT_EXTRA",
        "http://127.0.0.1:17787 ws://127.0.0.1:17787",
    )

    report_only = Handler.csp_report_only_policy()

    assert "http://127.0.0.1:17787" in report_only
    assert "ws://127.0.0.1:17787" in report_only

    from api.helpers import _build_csp_enforced_policy

    enforced = _build_csp_enforced_policy()
    assert "http://127.0.0.1:17787" in enforced
    assert "ws://127.0.0.1:17787" in enforced


def test_csp_connect_src_rejects_directive_injection(monkeypatch, caplog):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_CONNECT_EXTRA",
        "https://metrics.example.com; script-src *",
    )

    policy = Handler.csp_report_only_policy()

    assert "https://metrics.example.com" not in policy
    assert "script-src *" not in policy
    assert "Ignoring invalid HERMES_WEBUI_CSP_CONNECT_EXTRA" in caplog.text


def test_csp_connect_src_rejects_paths(monkeypatch):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_CONNECT_EXTRA",
        "https://metrics.example.com/api",
    )

    policy = Handler.csp_report_only_policy()

    assert "https://metrics.example.com/api" not in policy


def test_csp_connect_src_rejects_invalid_ports(monkeypatch):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_CONNECT_EXTRA",
        "https://metrics.example.com:99999",
    )

    policy = Handler.csp_report_only_policy()

    assert "https://metrics.example.com:99999" not in policy
