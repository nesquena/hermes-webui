"""#7863 — `log_request` must not record a client-spoofable IP.

The structured `[webui] {...}` request log recorded `X-Forwarded-For[0]`, the
left-most hop of a header any client can write, under a field name that reads
as authenticated. The CHANGELOG says these logs feed "downstream security
tooling like fail2ban", so a jail keyed on that field let anyone get an
arbitrary third-party IP banned — a DoS / self-lockout vector.

These tests pin the fix: the log gains a trustworthy `client_ip` resolved by the
existing trusted-proxy machinery, and the untrusted `forwarded_for` field is
gone. Resolution follows the established trust model in `api/routes.py` — the
chain is consulted ONLY when the un-spoofable raw socket peer is a trusted proxy
AND the operator opted in with HERMES_WEBUI_TRUST_FORWARDED_FOR.
"""

import email.message
import io
import json

import pytest

from server import Handler


@pytest.fixture
def log_output(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr("api.request_logging._STREAM", output)
    return output


def _record(log_output):
    line = log_output.getvalue().strip()
    assert line.startswith("[webui] ")
    return json.loads(line.removeprefix("[webui] "))


def _log(handler, log_output, status="200"):
    Handler.log_request(handler, status)
    return _record(log_output)


def _make_handler(client_ip, forwarded_for=None, real_ip=None, extra_headers=None):
    """A Handler with a real Message header container (supports get_all + repeat)."""
    handler = Handler.__new__(Handler)
    handler.command = "GET"
    handler.path = "/api/agent/sessions"
    handler.client_address = (client_ip, 54321)
    headers = email.message.Message()
    if forwarded_for is not None:
        headers["X-Forwarded-For"] = forwarded_for
    if real_ip is not None:
        headers["X-Real-IP"] = real_ip
    for key, value in (extra_headers or {}).items():
        headers[key] = value
    handler.headers = headers
    return handler


# --- the core vulnerability: a direct client sets the header and wins --------

def test_direct_client_cannot_spoof_client_ip_via_forwarded_for(log_output):
    """A public peer with NO trusted-proxy relationship is logged as itself."""
    handler = _make_handler("203.0.113.9", forwarded_for="203.0.113.7")

    record = _log(handler, log_output)

    assert record["client_ip"] == "203.0.113.9", "attacker-chosen hop must not be recorded"
    assert "forwarded_for" not in record


def test_forwarded_for_field_is_removed_entirely(log_output):
    """No field may carry a raw, unauthenticated header under a trusted name."""
    handler = _make_handler("203.0.113.9", forwarded_for="203.0.113.7")

    record = _log(handler, log_output)

    assert "forwarded_for" not in record
    assert "203.0.113.7" not in json.dumps(record)


def test_spoofed_loopback_header_does_not_change_logged_client_ip(log_output):
    """`X-Forwarded-For: 127.0.0.1` must not be logged as the client."""
    handler = _make_handler("203.0.113.9", forwarded_for="127.0.0.1")

    record = _log(handler, log_output)

    assert record["client_ip"] == "203.0.113.9"


# --- the intended behaviour: trusted proxy resolves the chain from the right --

def test_trusted_proxy_chain_resolves_rightmost_non_trusted_hop(log_output, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    # chain: [client, proxy1] — proxy1 is trusted, so the client is the answer.
    handler = _make_handler("127.0.0.1", forwarded_for="203.0.113.7, 127.0.0.1")

    record = _log(handler, log_output)

    assert record["client_ip"] == "203.0.113.7"


def test_trusted_proxy_chain_skips_leading_spoofed_hops(log_output, monkeypatch):
    """Right-to-left walk: the left-most value is attacker-writable, never used."""
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    handler = _make_handler(
        "127.0.0.1",
        forwarded_for="192.0.2.66, 203.0.113.7, 198.51.100.9, 127.0.0.1",
    )

    record = _log(handler, log_output)

    assert record["client_ip"] == "198.51.100.9"


def test_allowlisted_proxy_cidr_resolves_chain(log_output, monkeypatch):
    """A non-loopback proxy in the allowlist is trusted too (#5764 model)."""
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "10.0.0.5/32")
    handler = _make_handler("10.0.0.5", forwarded_for="203.0.113.7")

    record = _log(handler, log_output)

    assert record["client_ip"] == "203.0.113.7"


def test_ipv4_mapped_trusted_proxy_peer_is_still_trusted(log_output, monkeypatch):
    """A mapped-IPv6 socket peer must match an IPv4 allowlist, not be dropped."""
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "10.0.0.5/32")
    handler = _make_handler("::ffff:10.0.0.5", forwarded_for="203.0.113.7")

    record = _log(handler, log_output)

    assert record["client_ip"] == "203.0.113.7"


def test_x_real_ip_used_when_no_forwarded_chain(log_output, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    handler = _make_handler("127.0.0.1", real_ip="203.0.113.7")

    record = _log(handler, log_output)

    assert record["client_ip"] == "203.0.113.7"


# --- fail closed: never fall back to attacker input -------------------------

@pytest.mark.parametrize(
    "forwarded_for",
    [
        "  ,  ,  ",                  # present-but-blank
        "not-an-ip",                 # non-IP token
        "999.999.999.999",           # out-of-range IPv4
        "203.0.113.7, , 127.0.0.1",  # empty hop reached by the right-to-left walk
    ],
)
def test_malformed_chain_from_trusted_proxy_fails_closed_to_raw_peer(
    log_output, monkeypatch, forwarded_for
):
    """A malformed chain must yield the raw socket peer, never a header value."""
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    handler = _make_handler("127.0.0.1", forwarded_for=forwarded_for)

    record = _log(handler, log_output)

    assert record["client_ip"] == "127.0.0.1"
    assert "forwarded_for" not in record


def test_rightmost_proxy_asserted_hop_wins_over_leading_spoof(log_output, monkeypatch):
    """A junk hop before the proxy's own append is still never logged.

    The right-most entry is the one the trusted proxy itself wrote, so it is
    what gets logged — the client-writable prefix is discarded.
    """
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    handler = _make_handler("127.0.0.1", forwarded_for="203.0.113.7, , 10.0.0.1")

    record = _log(handler, log_output)

    assert record["client_ip"] == "10.0.0.1"
    assert "forwarded_for" not in record


# --- the opt-in: no env means the forwarded chain is never consulted --------

def test_chain_ignored_without_trust_forwarded_opt_in(log_output, monkeypatch):
    """Operators must not have XFF suddenly trusted in logs without opting in."""
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    handler = _make_handler("127.0.0.1", forwarded_for="203.0.113.7")

    record = _log(handler, log_output)

    assert record["client_ip"] == "127.0.0.1"


def test_chain_ignored_when_peer_not_a_trusted_proxy(log_output, monkeypatch):
    """Opt-in alone is not enough — the raw peer must be a trusted proxy."""
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    handler = _make_handler("203.0.113.9", forwarded_for="203.0.113.7")

    record = _log(handler, log_output)

    assert record["client_ip"] == "203.0.113.9"


# --- logging must never break a response ------------------------------------

def test_missing_headers_attribute_does_not_raise(log_output):
    """Malformed requests can reach log_request before headers are parsed."""
    handler = Handler.__new__(Handler)
    handler.command = None
    handler.client_address = ("203.0.113.9", 54321)

    record = _log(handler, log_output, status="400")

    assert record["client_ip"] == "203.0.113.9"


def test_missing_client_address_logs_placeholder(log_output):
    handler = Handler.__new__(Handler)
    handler.command = "GET"
    handler.path = "/health"
    handler.headers = email.message.Message()

    record = _log(handler, log_output)

    assert record["remote"] == "-"
    assert record["client_ip"] == "-"
