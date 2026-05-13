"""Regression tests for #1909 CSP report-only security header."""

from http.server import BaseHTTPRequestHandler

from server import Handler


def test_handler_adds_content_security_policy_report_only(monkeypatch):
    sent_headers = []
    handler = Handler.__new__(Handler)
    handler.send_header = lambda key, value: sent_headers.append((key, value))
    monkeypatch.setattr(BaseHTTPRequestHandler, "end_headers", lambda self: None)

    Handler.end_headers(handler)

    headers = dict(sent_headers)
    assert "Content-Security-Policy-Report-Only" in headers
    assert "Content-Security-Policy" not in headers
    policy = headers["Content-Security-Policy-Report-Only"]
    assert "default-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "frame-ancestors 'self'" in policy
    assert "base-uri 'self'" in policy


def test_csp_report_only_keeps_legacy_inline_allowances_for_current_ui():
    policy = Handler.csp_report_only_policy()

    assert "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in policy
    assert "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in policy
    # unsafe-eval was dropped after Opus stage-339 verification — no production
    # JS uses eval(), new Function(), or string-form setTimeout/setInterval.
    assert "'unsafe-eval'" not in policy
    assert "img-src 'self' data: blob:" in policy
    assert "connect-src 'self'" in policy
