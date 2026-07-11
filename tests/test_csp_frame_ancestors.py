"""Regression coverage for the configurable CSP frame-ancestors knob.

`HERMES_WEBUI_CSP_FRAME_ANCESTORS` lets an operator allow a trusted app
(e.g. a self-hosted local dashboard) to embed the WebUI in an <iframe>,
opt-in and default-off. With the var unset the policy stays locked down:
frame-ancestors 'none' plus the X-Frame-Options DENY header. It is the
counterpart of `HERMES_WEBUI_CSP_FRAME_EXTRA`, which governs what the
WebUI page itself may embed.
"""

from __future__ import annotations


def test_csp_frame_ancestors_default_is_none(monkeypatch):
    from server import Handler

    monkeypatch.delenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", raising=False)

    policy = Handler.csp_report_only_policy()
    assert "frame-ancestors 'none'; " in policy


def test_csp_frame_ancestors_includes_valid_origins(monkeypatch):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_FRAME_ANCESTORS",
        "http://localhost:3000 https://*.dash.example.com:8443",
    )

    policy = Handler.csp_report_only_policy()
    assert (
        "frame-ancestors "
        "http://localhost:3000 https://*.dash.example.com:8443; "
    ) in policy
    assert "frame-ancestors 'none'" not in policy


def test_csp_frame_ancestors_in_enforced_policy(monkeypatch):
    from api.helpers import _build_csp_enforced_policy

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "http://127.0.0.1:3000")
    enforced = _build_csp_enforced_policy()
    assert "frame-ancestors http://127.0.0.1:3000; " in enforced


def test_csp_frame_ancestors_rejects_directive_injection(monkeypatch, caplog):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_FRAME_ANCESTORS",
        "https://ok.example.com; script-src *",
    )

    policy = Handler.csp_report_only_policy()
    assert "https://ok.example.com" not in policy
    assert "script-src *" not in policy
    assert "frame-ancestors 'none'; " in policy  # falls back to safe default
    assert "Ignoring invalid HERMES_WEBUI_CSP_FRAME_ANCESTORS" in caplog.text


def test_csp_frame_ancestors_rejects_paths(monkeypatch):
    from server import Handler

    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_FRAME_ANCESTORS", "https://app.example.com/embed"
    )
    policy = Handler.csp_report_only_policy()
    assert "https://app.example.com/embed" not in policy
    assert "frame-ancestors 'none'; " in policy


def test_csp_frame_ancestors_rejects_ws_scheme(monkeypatch):
    """An embedding ancestor is always http(s); ws/wss are not valid."""
    from server import Handler

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "wss://socket.example.com")
    policy = Handler.csp_report_only_policy()
    assert "wss://socket.example.com" not in policy
    assert "frame-ancestors 'none'; " in policy


def test_csp_frame_ancestors_does_not_affect_frame_src(monkeypatch):
    """The frame-ancestors knob and the frame-src knob are independent."""
    from server import Handler

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "http://localhost:3000")
    monkeypatch.delenv("HERMES_WEBUI_CSP_FRAME_EXTRA", raising=False)
    policy = Handler.csp_report_only_policy()
    assert "frame-ancestors http://localhost:3000; " in policy
    # ... and NOT leaked into frame-src (which stays same-origin only).
    frame_seg = policy.split("frame-src", 1)[1].split(";", 1)[0]
    assert "localhost:3000" not in frame_seg


def test_x_frame_options_denied_by_default_and_omitted_when_set(monkeypatch):
    from api import helpers

    class _Recorder:
        def __init__(self):
            self.headers = []

        def send_header(self, name, value):
            self.headers.append((name, value))

    monkeypatch.delenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", raising=False)
    rec = _Recorder()
    helpers._security_headers(rec)
    assert ("X-Frame-Options", "DENY") in rec.headers

    monkeypatch.setenv("HERMES_WEBUI_CSP_FRAME_ANCESTORS", "http://localhost:3000")
    rec = _Recorder()
    helpers._security_headers(rec)
    assert all(name != "X-Frame-Options" for name, _ in rec.headers)
