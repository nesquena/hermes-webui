"""Regression tests for enforced CSP alignment with report-only policy (#1909)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from api.helpers import _security_headers
from server import Handler


class _HeaderCapture:
    def __init__(self):
        self.sent_headers = []

    def send_header(self, key, value):
        self.sent_headers.append((key, value))


class _SecureRequest:
    def getpeercert(self):
        return {}


def _headers_from_handler(monkeypatch, *, request=None, headers=None):
    sent_headers = []
    handler = Handler.__new__(Handler)
    handler.__dict__.update(
        {
            "request": request if request is not None else object(),
            "headers": headers if headers is not None else {},
            "send_header": lambda key, value: sent_headers.append((key, value)),
        }
    )
    monkeypatch.setattr(BaseHTTPRequestHandler, "end_headers", lambda self: None)

    Handler.end_headers(handler)
    return dict(sent_headers)


def _headers_from_security_helper():
    handler = _HeaderCapture()
    _security_headers(handler)
    return dict(handler.sent_headers)


def _directives(policy: str):
    directives = {}
    for entry in policy.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        key, _, value = entry.partition(" ")
        directives[key] = value.strip()
    return directives


def test_security_helper_sends_enforcing_csp_with_hardening_directives(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_CSP_CONNECT_EXTRA", raising=False)

    headers = _headers_from_security_helper()

    policy = headers["Content-Security-Policy"]
    assert "default-src 'self' https://*.cloudflareaccess.com" in policy
    assert "base-uri 'self'" in policy
    assert "form-action 'self'" in policy
    assert "manifest-src 'self' https://*.cloudflareaccess.com" in policy
    assert "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://static.cloudflareinsights.com blob:" in policy
    assert "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com" in policy
    assert "worker-src blob: 'self' https://cdn.jsdelivr.net" in policy
    assert "font-src 'self' data: https://fonts.gstatic.com" in policy
    assert "object-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "media-src 'self' data: blob:" in policy
    assert "connect-src 'self' http://127.0.0.1:* http://localhost:* http://ipc.localhost https://127.0.0.1:* https://localhost:* ws://127.0.0.1:* ws://localhost:* https://cdn.jsdelivr.net" in policy


def test_enforcing_csp_honors_valid_extra_connect_origins(monkeypatch):
    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_CONNECT_EXTRA",
        "https://metrics.example.com wss://events.example.com:443",
    )

    headers = _headers_from_security_helper()

    policy = headers["Content-Security-Policy"]
    assert (
        "connect-src 'self' http://127.0.0.1:* http://localhost:* "
        "http://ipc.localhost https://127.0.0.1:* https://localhost:* "
        "ws://127.0.0.1:* ws://localhost:* https://cdn.jsdelivr.net "
        "https://metrics.example.com wss://events.example.com:443; "
    ) in policy


def test_enforcing_csp_allows_trusted_loopback_sidecars_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_CSP_CONNECT_EXTRA", raising=False)

    policy = _headers_from_security_helper()["Content-Security-Policy"]

    assert "connect-src" in policy
    assert "http://127.0.0.1:*" in policy
    assert "http://localhost:*" in policy
    assert "http://ipc.localhost" in policy
    assert "ws://127.0.0.1:*" in policy
    assert "ws://localhost:*" in policy
    assert "http://127.0.0.1:17787" not in policy


def test_enforcing_and_report_only_csp_share_validated_connect_extra(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_CSP_CONNECT_EXTRA", "https://metrics.example.com")

    enforced = _headers_from_security_helper()["Content-Security-Policy"]
    report_only = Handler.csp_report_only_policy()

    assert "https://metrics.example.com" in enforced
    assert "https://metrics.example.com" in report_only


def test_report_only_policy_tracks_enforced_directives(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_CSP_CONNECT_EXTRA", raising=False)

    enforced = _directives(_headers_from_security_helper()["Content-Security-Policy"])
    report_only = _directives(Handler.csp_report_only_policy())

    assert report_only.pop("report-uri") == "/api/csp-report"
    assert report_only.pop("report-to") == "csp-endpoint"
    assert report_only == enforced


def test_report_only_csp_headers_still_point_to_collector(monkeypatch):
    sent_headers = []
    handler = Handler.__new__(Handler)
    handler.send_header = lambda key, value: sent_headers.append((key, value))
    monkeypatch.setattr(BaseHTTPRequestHandler, "end_headers", lambda self: None)

    Handler.end_headers(handler)

    headers = dict(sent_headers)
    assert "Content-Security-Policy-Report-Only" in headers
    assert headers["Report-To"] == (
        '{"group":"csp-endpoint","max_age":10886400,'
        '"endpoints":[{"url":"/api/csp-report"}]}'
    )
    assert "report-uri /api/csp-report" in headers["Content-Security-Policy-Report-Only"]
    assert "report-to csp-endpoint" in headers["Content-Security-Policy-Report-Only"]


def test_hsts_is_opt_in(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_HSTS", raising=False)

    headers = _headers_from_handler(monkeypatch, request=_SecureRequest())

    assert "Strict-Transport-Security" not in headers


def test_hsts_requires_secure_transport_even_when_opted_in(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_HSTS", "1")
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_PROTO", raising=False)

    headers = _headers_from_handler(monkeypatch)

    assert "Strict-Transport-Security" not in headers


def test_hsts_emits_for_direct_tls_when_opted_in(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_HSTS", "1")

    headers = _headers_from_handler(monkeypatch, request=_SecureRequest())

    assert headers["Strict-Transport-Security"] == "max-age=86400"


def test_hsts_emits_for_trusted_forwarded_https_when_opted_in(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_HSTS", "true")
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_PROTO", "1")

    headers = _headers_from_handler(monkeypatch, headers={"X-Forwarded-Proto": "https"})

    assert headers["Strict-Transport-Security"] == "max-age=86400"


def test_hsts_does_not_trust_forwarded_https_without_proxy_opt_in(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_HSTS", "1")
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_PROTO", raising=False)

    headers = _headers_from_handler(monkeypatch, headers={"X-Forwarded-Proto": "https"})

    assert "Strict-Transport-Security" not in headers


def test_hsts_does_not_follow_cookie_secure_override_on_plain_http(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_HSTS", "1")
    monkeypatch.setenv("HERMES_WEBUI_SECURE", "1")
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_PROTO", raising=False)

    headers = _headers_from_handler(monkeypatch)

    assert "Strict-Transport-Security" not in headers



def test_end_headers_reuses_cached_extra_connect_validation(monkeypatch, caplog):
    monkeypatch.setenv(
        "HERMES_WEBUI_CSP_CONNECT_EXTRA",
        "https://metrics.example.com; script-src *",
    )

    sent_headers = []
    handler = Handler.__new__(Handler)
    handler.send_header = lambda key, value: sent_headers.append((key, value))
    monkeypatch.setattr(BaseHTTPRequestHandler, "end_headers", lambda self: None)

    _security_headers(handler)
    Handler.end_headers(handler)

    assert caplog.text.count("Ignoring invalid HERMES_WEBUI_CSP_CONNECT_EXTRA value") == 1
