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

    def get_all(self, key):
        # Production HTTPMessage.get_all returns a list of every header line for
        # the key. A plain single-valued fake holds at most one line, so mirror
        # it as a one-element list (or empty when absent). Implementing this —
        # not just get() — is load-bearing: if the resolver calls get_all() on a
        # double whose get_all is missing, the AttributeError is swallowed by
        # log_request's broad `except` and the field is absent by exception, not
        # by the peer/validation check we mean to assert. (test_issue2775 and
        # the real multi-value-header test below pin real get_all() too.)
        value = dict.get(self, key)
        return [value] if value is not None else []


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

    def test_x_real_ip_alone_from_trusted_peer_is_not_recorded(self):
        """Round 2 (blocker): X-Real-IP must not feed forwarded_for.

        nginx passes client-supplied request headers through by default
        (proxy_pass_request_headers on), so a proxy that does not explicitly
        overwrite X-Real-IP relays whatever the client sent. With the fallback
        that this PR previously had, a loopback peer could put any address into
        X-Real-IP and get it logged as forwarded_for — the #7863 attack
        through a different header. The field must be absent.
        """
        handler, cap = _make_handler(
            remote_ip="127.0.0.1", xff=None, real_ip="198.51.100.7"
        )
        Handler.log_request(handler, "200", "-")
        rec = _log_record(cap)
        assert rec["remote"] == "127.0.0.1"
        assert "forwarded_for" not in rec, (
            "X-Real-IP alone must never be recorded as forwarded_for"
        )

    def test_x_real_ip_ignored_even_with_valid_xff_present(self):
        """X-Real-IP must not override or merge with the XFF resolution.

        A trusted peer sends both headers: X-Real-IP carries the attacker's
        chosen marker while XFF carries the real chain. Only the XFF walk
        feeds the log.
        """
        handler, cap = _make_handler(
            remote_ip="127.0.0.1",
            xff="198.51.100.7",
            real_ip="203.0.113.99",
        )
        Handler.log_request(handler, "200", "-")
        rec = _log_record(cap)
        assert rec["forwarded_for"] == "198.51.100.7", (
            "resolution must come from XFF, never from X-Real-IP"
        )

    def test_malformed_x_real_ip_from_trusted_peer_has_no_field(self):
        """X-Real-IP carrying a non-IP token yields no field at all.

        The old fallback validated the value but still honored the header; the
        fix removes the header from the resolver entirely, so malformed input
        is as harmless as valid input.
        """
        handler, cap = _make_handler(
            remote_ip="127.0.0.1", xff=None, real_ip="not-an-ip"
        )
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