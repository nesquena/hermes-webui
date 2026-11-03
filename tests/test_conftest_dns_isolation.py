"""The shared test network isolation covers DNS, not only connects.

conftest.py used to wrap socket.create_connection and socket.socket.connect only. A code path that
resolves a hostname before connecting (the custom-provider SSRF pre-check does) still waited on the
runner's resolver with no timeout; on a slow CI resolver that pushed the model-catalog rebuild past its
4 s budget and tests saw the budget fallback (test_credential_pool_providers, CI shard 4).
"""

import socket
import time

import _socket


def test_public_name_lookup_fails_at_once_without_reaching_the_resolver(monkeypatch):
    reached = []

    def resolver(host, *args, **kwargs):
        reached.append(host)
        time.sleep(5)
        raise socket.gaierror(socket.EAI_AGAIN, "simulated slow resolver")

    monkeypatch.setattr(_socket, "getaddrinfo", resolver)
    started = time.monotonic()
    try:
        socket.getaddrinfo("bothub.chat", 443)
    except socket.gaierror as exc:
        assert "network isolation" in str(exc)
    else:  # pragma: no cover - the guard must refuse
        raise AssertionError("a public hostname resolved inside the test network isolation")
    assert time.monotonic() - started < 1.0
    assert reached == []


def test_ip_literals_and_localhost_still_resolve():
    assert socket.getaddrinfo("127.0.0.1", 80)
    assert socket.getaddrinfo("::1", 80)
    assert socket.getaddrinfo("localhost", 80)
    assert socket.getaddrinfo(None, 80)


def test_zero_padded_ipv4_literals_still_resolve():
    # The resolver answers these without DNS and the connect guard allows them, so the DNS guard must too.
    for host in ("127.000.000.001", "010.000.000.001", "169.254.000.001", "192.000.002.001"):
        assert socket.getaddrinfo(host, 80), host


def test_allow_outbound_network_restores_the_real_resolver(allow_outbound_network):
    assert (socket.getaddrinfo.__module__, socket.getaddrinfo.__name__) == ("socket", "getaddrinfo")
