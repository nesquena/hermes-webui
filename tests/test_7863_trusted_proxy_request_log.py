"""#7863: log_request must not record a spoofable X-Forwarded-For[0].

A direct attacker can set the left-most XFF hop; the structured request log
currently writes that value verbatim, feeding fail2ban-style jails with an
attacker-chosen address (arbitrary third-party IP ban / self-lockout vector).

Fix under test: only a TRUSTED raw socket peer (loopback or allowlisted
HERMES_WEBUI_TRUSTED_PROXY_CIDRS) may assert a forwarded client IP, and the
chain is then resolved right-to-left through the existing
``_forwarded_client_ip_from_trusted_proxy`` helper. Direct clients get no
``forwarded_for`` field at all (fail closed).
"""

import json
import types

from server import Handler


class FakeHeaders(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def _make_handler(remote_ip, xff=None, real_ip=None):
    headers = FakeHeaders()
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    if real_ip is not None:
        headers["X-Real-IP"] = real_ip
    handler = types.SimpleNamespace(
        client_address=(remote_ip, 0),
        headers=headers,
        command="GET",
        path="/health",
        _req_t0=0.0,
    )
    captured = {}

    def _print(msg):
        captured["line"] = msg

    handler._safe_webui_print = _print
    return handler, captured


def _log_record(captured):
    line = captured["line"]
    assert line.startswith("[webui] "), line
    return json.loads(line[len("[webui] "):])


class TestDirectClientCannotSpoofForwardedFor:
    def test_public_peer_with_forged_xff_is_fail_closed(self):
        handler, cap = _make_handler(remote_ip="203.0.113.9", xff="198.51.100.7")
        Handler.log_request(handler, "200", "-")
        rec = _log_record(cap)
        assert rec["remote"] == "203.0.113.9"
        assert "forwarded_for" not in rec, (
            "direct client's XFF must never be recorded"
        )

    def test_public_peer_with_x_real_ip_is_fail_closed(self):
        handler, cap = _make_handler(
            remote_ip="203.0.113.9", xff="198.51.100.7", real_ip="198.51.100.8"
        )
        Handler.log_request(handler, "200", "-")
        rec = _log_record(cap)
        assert "forwarded_for" not in rec


class TestTrustedProxyResolution:
    def test_loopback_proxy_resolves_rightmost_real_client(self):
        # same-host tunnel: socket peer is loopback; the tunnel's own hop is
        # loopback too, so the right-most *non-trusted* hop past it is the
        # real client (XFF[0] "198.51.100.1" is attacker-controlled).
        handler, cap = _make_handler(
            remote_ip="127.0.0.1", xff="198.51.100.1, 203.0.113.77, 127.0.0.1"
        )
        Handler.log_request(handler, "200", "-")
        rec = _log_record(cap)
        assert rec["forwarded_for"] == "203.0.113.77"

    def test_loopback_proxy_without_forwarded_header_has_no_field(self):
        handler, cap = _make_handler(remote_ip="127.0.0.1", xff=None)
        Handler.log_request(handler, "200", "-")
        rec = _log_record(cap)
        assert "forwarded_for" not in rec

    def test_allowlisted_remote_proxy_is_trusted(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
        import api.routes as routes_mod

        _clear = getattr(routes_mod._trusted_proxy_networks, "cache_clear", None)
        if _clear is not None:
            _clear()
        try:
            handler, cap = _make_handler(remote_ip="10.0.0.5", xff="203.0.113.77")
            Handler.log_request(handler, "200", "-")
            rec = _log_record(cap)
            assert rec["forwarded_for"] == "203.0.113.77"
        finally:
            _clear2 = getattr(routes_mod._trusted_proxy_networks, "cache_clear", None)
            if _clear2 is not None:
                _clear2()


def test_security_helpers_exist():
    import api.routes as r
    assert hasattr(r, "_raw_peer_is_trusted_proxy")
    assert hasattr(r, "_forwarded_client_ip_from_trusted_proxy")