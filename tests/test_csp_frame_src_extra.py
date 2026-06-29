"""Regression coverage for the configurable CSP frame-src allowlist knob.

`HERMES_WEBUI_CSP_FRAME_EXTRA` lets an operator widen what the WebUI page may
embed in an <iframe> (e.g. a self-hosted dashboard pinned as an extension tab),
opt-in and default-off. The base policy is same-origin only, and the knob never
touches `frame-ancestors` (who may embed the WebUI), which stays 'none'.
"""

from __future__ import annotations


def test_csp_frame_src_default_is_self_only(monkeypatch):
    from server import Handler

    monkeypatch.delenv("HERMES_WEBUI_CSP_FRAME_EXTRA", raising=False)

    policy = Handler.csp_report_only_policy()
    assert "frame-src 'self'; " in policy
    # frame-ancestors must remain locked down regardless.
    assert "frame-ancestors 'none'" in policy


def test_csp_frame_src_includes_valid_extra_origins(monkeypatch):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_FRAME_EXTRA",
        "https://grafana.example.com https://*.dash.example.com:8443",
    )

    policy = Handler.csp_report_only_policy()
    assert (
        "frame-src 'self' "
        "https://grafana.example.com https://*.dash.example.com:8443; "
    ) in policy


def test_csp_frame_src_extra_in_enforced_policy(monkeypatch):
    from api.helpers import _build_csp_enforced_policy

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_EXTRA", "http://127.0.0.1:3000")
    enforced = _build_csp_enforced_policy()
    assert "frame-src 'self' http://127.0.0.1:3000;" in enforced


def test_csp_frame_src_rejects_directive_injection(monkeypatch, caplog):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_FRAME_EXTRA",
        "https://ok.example.com; script-src *",
    )

    policy = Handler.csp_report_only_policy()
    assert "https://ok.example.com" not in policy
    assert "script-src *" not in policy
    assert "frame-src 'self'; " in policy  # falls back to safe default
    assert "Ignoring invalid HERMES_WEBUI_CSP_FRAME_EXTRA" in caplog.text


def test_csp_frame_src_rejects_paths(monkeypatch):
    from server import Handler

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_EXTRA", "https://app.example.com/embed")
    policy = Handler.csp_report_only_policy()
    assert "https://app.example.com/embed" not in policy


def test_csp_frame_src_rejects_ws_scheme(monkeypatch):
    """An iframe src is always http(s); ws/wss are not valid frame sources."""
    from server import Handler

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_EXTRA", "wss://socket.example.com")
    policy = Handler.csp_report_only_policy()
    assert "wss://socket.example.com" not in policy
    assert "frame-src 'self'; " in policy


def test_csp_frame_src_rejects_invalid_ports(monkeypatch):
    from server import Handler

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_EXTRA", "https://app.example.com:99999")
    policy = Handler.csp_report_only_policy()
    assert "https://app.example.com:99999" not in policy


def test_csp_frame_src_does_not_affect_connect_src(monkeypatch):
    """The frame knob and the connect knob are independent."""
    from server import Handler

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_EXTRA", "https://dash.example.com")
    monkeypatch.delenv("HERMES_WEBUI_CSP_CONNECT_EXTRA", raising=False)
    policy = Handler.csp_report_only_policy()
    # frame-extra present in frame-src ...
    assert "frame-src 'self' https://dash.example.com;" in policy
    # ... and NOT leaked into connect-src (which ends at cdn.jsdelivr.net).
    connect_seg = policy.split("connect-src", 1)[1].split(";", 1)[0]
    assert "dash.example.com" not in connect_seg
