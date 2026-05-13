"""Adversarial test for the network-isolation fixture in conftest.py.

The autouse module-level monkey-patch in tests/conftest.py wraps
socket.create_connection so that any non-loopback / non-RFC1918 / non-link-local
destination raises OSError. This file proves:

  1. The block actually fires for outbound to a real public IP.
  2. Loopback / RFC1918 / link-local / reserved-TLD destinations pass through.
  3. The `allow_outbound_network` fixture re-enables real network for tests
     that legitimately need it.

Without this enforcement, a test that accidentally calls real outbound
(forgotten mock, leaked credential triggering an SDK initialisation, new
code path bypassing an existing mock) can leak production credentials,
slow the test suite into 10-minute waits on TLS handshakes, and produce
flaky failures depending on whether the destination is reachable.
"""
from __future__ import annotations

import socket
import pytest


def test_outbound_to_public_ipv4_is_blocked():
    """Attempting to connect to a public IP must raise OSError."""
    with pytest.raises(OSError, match="hermes test network isolation"):
        # 8.8.8.8 (Google DNS) is a stable real public IPv4.
        # If we accidentally connect, the test goes to 53/tcp which is
        # genuinely listening — so the block is what stops us, not lack of
        # destination.
        socket.create_connection(("8.8.8.8", 53), timeout=1)


def test_outbound_to_anthropic_ipv6_is_blocked():
    """The exact destination we observed leaking from earlier pytest runs."""
    with pytest.raises(OSError, match="hermes test network isolation"):
        socket.create_connection(("2607:6bc0::10", 443), timeout=1)


def test_outbound_to_amazon_is_blocked():
    """AWS endpoints (botocore / bedrock) must not reach the real service."""
    with pytest.raises(OSError, match="hermes test network isolation"):
        socket.create_connection(("3.173.21.63", 443), timeout=1)


def test_loopback_v4_is_allowed():
    """127.0.0.1 must continue to work — test_server fixture depends on it."""
    # Listen on a temporary port + connect via the wrapped create_connection.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    try:
        client = socket.create_connection(("127.0.0.1", port), timeout=1)
        client.close()
    finally:
        listener.close()


def test_rfc1918_private_ipv4_is_allowed():
    """RFC1918 (10/8, 172.16/12, 192.168/16) must pass — devs run LM Studio
    on their LAN. The block only refuses non-RFC1918 + non-loopback."""
    import tests.conftest as _conftest
    # Direct unit test on the predicate so we don't have to start a real listener
    # in a private-IP subnet just to prove this.
    assert _conftest._hermes_addr_is_local("10.0.0.5") is True
    assert _conftest._hermes_addr_is_local("172.16.5.1") is True
    assert _conftest._hermes_addr_is_local("172.31.255.254") is True
    assert _conftest._hermes_addr_is_local("192.168.1.22") is True


def test_link_local_is_allowed():
    """169.254.0.0/16 (link-local / IMDS) — AWS_EC2_METADATA_DISABLED already
    short-circuits the actual probe but the socket layer allows it."""
    import tests.conftest as _conftest
    assert _conftest._hermes_addr_is_local("169.254.169.254") is True


def test_reserved_tlds_are_allowed():
    """RFC 2606/6761 reserved TLDs — used as documentation hostnames in tests
    (e.g. example.com, test-host.invalid)."""
    import tests.conftest as _conftest
    assert _conftest._hermes_addr_is_local("example.com") is True
    assert _conftest._hermes_addr_is_local("my-mac.tailnet.example") is True
    assert _conftest._hermes_addr_is_local("anything.invalid") is True
    assert _conftest._hermes_addr_is_local("test-host.test") is True
    assert _conftest._hermes_addr_is_local("printer.local") is True
    assert _conftest._hermes_addr_is_local("localhost") is True


def test_public_ipv4_is_blocked():
    """Public IPs must NOT be treated as local."""
    import tests.conftest as _conftest
    assert _conftest._hermes_addr_is_local("8.8.8.8") is False
    assert _conftest._hermes_addr_is_local("1.1.1.1") is False
    assert _conftest._hermes_addr_is_local("203.0.113.0") is True  # TEST-NET-3
    assert _conftest._hermes_addr_is_local("204.0.113.0") is False  # outside


def test_allow_outbound_network_fixture_unswaps_the_wrappers(allow_outbound_network):
    """When a test opts in to the fixture, socket.create_connection and
    socket.socket.connect are restored to their real (unwrapped) implementations
    for this test only.

    Check by qname so this is robust against pytest re-importing conftest
    under multiple roots (which produces two distinct function objects with
    the same __qualname__ but different `is` identity).
    """
    # Inside the fixture, the symbol should NOT be the blocked wrapper.
    assert "_hermes_blocked_create_connection" not in getattr(
        socket.create_connection, "__qualname__", ""
    ), "allow_outbound_network fixture did not restore the real create_connection"
    assert "_hermes_blocked_socket_connect" not in getattr(
        socket.socket.connect, "__qualname__", ""
    ), "allow_outbound_network fixture did not restore the real socket.connect"


def test_block_is_active_outside_the_fixture():
    """Sanity: a test that does NOT request the fixture has the wrapped
    socket.create_connection installed.

    Check by qname so this is robust against pytest re-importing conftest
    under multiple roots (which produces two distinct function objects with
    the same __qualname__ but different `is` identity)."""
    assert "_hermes_blocked_create_connection" in getattr(
        socket.create_connection, "__qualname__", ""
    ), "default state should have the blocked wrapper installed on socket.create_connection"
    assert "_hermes_blocked_socket_connect" in getattr(
        socket.socket.connect, "__qualname__", ""
    ), "default state should have the blocked wrapper installed on socket.socket.connect"
