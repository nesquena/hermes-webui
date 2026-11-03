"""#7864 release fold: scoped IPv6 hops and the one-shot warning under concurrency.

Gate W17 (2026-10-22) on head bae842e712b0 found two loose ends in the request
log's client attribution:

* ``ipaddress.ip_address("fe80::1%<zone>")`` accepts free-form zone text and
  ``str()`` echoes it, so a chain ending in a scoped IPv6 address put header
  text into ``forwarded_for`` from any trusted peer (including a loopback
  client with zero configuration). A zone id means nothing off-host, so the
  log now treats a scoped hop as unresolved and flags the record instead.
* The process-wide "forwarded header ignored" warning checked and set its
  once-flag without a lock, so forced interleaving could emit it twice.

The shared resolver ``_forwarded_client_ip_from_trusted_proxy`` is unchanged:
only the logging boundary rejects scoped hops, so the local-origin gate keeps
master's behaviour.
"""

import json
import logging
import threading
import time

import pytest

from server import Handler

from tests.test_security_review_fixes import _Handler, _Headers

_PEER = "172.17.0.1"
_TRUST_ENV = {
    "HERMES_WEBUI_TRUST_FORWARDED_FOR": "1",
    "HERMES_WEBUI_TRUSTED_PROXY_CIDRS": "172.17.0.0/16",
}
_SCOPED_HOPS = (
    "fe80::1%eth0",
    "2001:4860::8888%FORGED_TEXT",
    "fe80::1%" + "A" * 2048,
)


@pytest.fixture(autouse=True)
def _reset_once_guard():
    from api import routes

    routes._FORWARDED_HEADER_IGNORED_WARNED = False
    yield
    routes._FORWARDED_HEADER_IGNORED_WARNED = False


def _clear_net_cache(routes):
    clear = getattr(routes._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()


def _trusted_env(monkeypatch):
    from api import routes

    for key, value in _TRUST_ENV.items():
        monkeypatch.setenv(key, value)
    _clear_net_cache(routes)
    return routes


def _log_handler(client_ip, headers):
    handler = Handler.__new__(Handler)
    handler.command = "GET"
    handler.path = "/api/status"
    handler.client_address = (client_ip, 54321)
    handler.headers = _Headers(headers)
    return handler


def _emitted_line_and_record(handler):
    import io

    import api.request_logging

    output = io.StringIO()
    previous = api.request_logging._STREAM
    api.request_logging._STREAM = output
    try:
        Handler.log_request(handler, "200", "-")
    finally:
        api.request_logging._STREAM = previous
    line = output.getvalue().strip()
    assert line.startswith("[webui] "), line
    return line, json.loads(line[len("[webui] "):])


def test_scoped_ipv6_hop_from_a_trusted_proxy_is_flagged_not_logged(monkeypatch):
    _trusted_env(monkeypatch)

    for hop in _SCOPED_HOPS:
        line, record = _emitted_line_and_record(
            _log_handler(_PEER, {"X-Forwarded-For": f"198.51.100.7, {hop}"})
        )
        assert "forwarded_for" not in record, hop[:40]
        assert record["forwarded_for_ignored"] is True, hop[:40]
        zone = hop.split("%", 1)[1]
        assert zone not in line, hop[:40]


def test_scoped_ipv6_hop_from_an_unconfigured_loopback_peer_is_flagged(monkeypatch):
    """Loopback is trusted with zero configuration (SSH-tunnel setups)."""
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", raising=False)
    _clear_net_cache(routes)

    line, record = _emitted_line_and_record(
        _log_handler("127.0.0.1", {"X-Forwarded-For": "2001:4860::8888%FORGED_TEXT"})
    )
    assert "forwarded_for" not in record
    assert record["forwarded_for_ignored"] is True
    assert "FORGED_TEXT" not in line


def test_unscoped_ipv6_client_is_still_attributed(monkeypatch):
    _trusted_env(monkeypatch)

    _, record = _emitted_line_and_record(
        _log_handler(_PEER, {"X-Forwarded-For": "2001:db8::1"})
    )
    assert record["forwarded_for"] == "2001:db8::1"
    assert "forwarded_for_ignored" not in record


def test_shared_resolver_still_returns_scoped_hops_for_other_callers(monkeypatch):
    """Only the logging boundary rejects scoped hops; the local-origin gate's
    resolver keeps master's behaviour."""
    routes = _trusted_env(monkeypatch)

    handler = _Handler(client_ip=_PEER, headers={"X-Forwarded-For": "fe80::1%eth0"})
    assert routes._forwarded_client_ip_from_trusted_proxy(handler) == "fe80::1%eth0"


def test_ignored_header_warning_is_emitted_once_under_concurrency(monkeypatch, caplog):
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", raising=False)
    _clear_net_cache(routes)
    caplog.set_level(logging.WARNING, logger="api.routes")

    handler = _Handler(client_ip=_PEER, headers={"X-Forwarded-For": "198.51.100.23"})
    threads = [
        threading.Thread(target=routes._warn_forwarded_header_ignored, args=(handler,))
        for _ in range(16)
    ]
    # Hold the once-guard's lock so every thread queues on it, then release
    # them together: exactly one may pass the check.
    with routes._FORWARDED_HEADER_IGNORED_LOCK:
        for thread in threads:
            thread.start()
        time.sleep(0.2)
    for thread in threads:
        thread.join(timeout=10)

    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "X-Forwarded-For header from peer" in r.getMessage()
    ]
    assert len(warnings) == 1
