"""#7864 round 5: a dropped X-Forwarded-For must be visible PER REQUEST.

The round-3 one-shot operator warning is process-wide, so it fires at most
once. Round 4 fixed *which header* spends it (XFF only, never X-Real-IP), but
the review identified what is left as structural: one process-wide line can
only ever explain ONE peer. On an exposed instance any internet client can
send its own X-Forwarded-For and spend the warning on the first random
request, so the operator's unallowlisted proxy — the request whose
``forwarded_for`` field actually disappeared — is never named:

    direct client (peer 203.0.113.9, own XFF) -> consumes the warning
    operator proxy (peer 172.17.0.1, XFF 198.51.100.23) -> silent forever

``request_log_forwarded_fields()`` closes that by returning
``{"forwarded_for_ignored": True}`` on EVERY record that dropped an XFF, so a
fail2ban jail or log query can see exactly which requests lost the field.

These tests pin:

* the review's exact order (direct XFF first, proxy second): the one warning
  still names the proxy, AND the direct request's record is still flagged,
* the flag is boolean and header-value-free (never the dropped text),
* a direct client with no forwarded header gets NEITHER key,
* a trusted proxy that resolves gets ``forwarded_for`` and NOT the flag,
* the two keys are mutually exclusive, and
* ``log_request`` actually writes the flag into the structured line.
"""

import io
import json
import logging

import pytest

from server import Handler

from tests.test_security_review_fixes import _Handler, _Headers


@pytest.fixture
def log_output(monkeypatch):
    """Capture the real emitted request-log stream, as the neighbouring
    test_issue2775_log_request.py does."""
    output = io.StringIO()
    monkeypatch.setattr("api.request_logging._STREAM", output)
    return output


@pytest.fixture(autouse=True)
def _reset_once_guard():
    """The warning is once-per-process; reset it around every test."""
    from api import routes

    routes._FORWARDED_HEADER_IGNORED_WARNED = False
    yield
    routes._FORWARDED_HEADER_IGNORED_WARNED = False


def _routes():
    from api import routes

    return routes


def _no_trust_env(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", raising=False)
    clear = getattr(_routes()._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()


def _warnings(caplog):
    caplog.set_level(logging.WARNING, logger="api.routes")
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_review_order_direct_xff_first_then_proxy(monkeypatch, caplog):
    """The review's order: a direct client spends the one warning, and the
    operator's proxy that follows is STILL flagged per record — which is the
    whole point of the per-request field."""
    routes = _routes()
    _no_trust_env(monkeypatch)

    # Step 1: a direct client sending its own XFF.
    direct = _Handler(
        client_ip="203.0.113.9", headers={"X-Forwarded-For": "198.51.100.77"}
    )
    first = routes.request_log_forwarded_fields(direct)
    warnings = _warnings(caplog)
    assert first == {"forwarded_for_ignored": True}
    assert len(warnings) == 1
    assert "203.0.113.9" in warnings[0].getMessage()

    # Step 2: the operator's unallowlisted Docker-bridge proxy. No second
    # warning is possible — the record itself must carry the signal.
    proxy = _Handler(
        client_ip="172.17.0.1", headers={"X-Forwarded-For": "198.51.100.23"}
    )
    second = routes.request_log_forwarded_fields(proxy)
    assert second == {"forwarded_for_ignored": True}
    assert len(_warnings(caplog)) == 1, "still exactly one warning, process-wide"
    # The peer the operator cares about is recoverable from the record alone.
    assert "forwarded_for" not in second


def test_flag_is_boolean_and_never_echoes_the_header(monkeypatch, caplog):
    """The dropped value is attacker-controlled text; the record must carry a
    boolean, never the header — otherwise this is a log-write vector."""
    routes = _routes()
    _no_trust_env(monkeypatch)

    hostile = _Handler(
        client_ip="203.0.113.9",
        headers={"X-Forwarded-For": "1.2.3.4\nFORGED injected line"},
    )
    fields = routes.request_log_forwarded_fields(hostile)

    assert fields == {"forwarded_for_ignored": True}
    assert fields["forwarded_for_ignored"] is True
    rendered = json.dumps(fields)
    assert "1.2.3.4" not in rendered
    assert "FORGED" not in rendered


def test_direct_client_without_forwarded_header_gets_no_keys(monkeypatch, caplog):
    """The common direct-LAN case must stay byte-identical to master."""
    routes = _routes()
    _no_trust_env(monkeypatch)

    handler = _Handler(client_ip="192.168.1.50", headers={})
    assert routes.request_log_forwarded_fields(handler) == {}
    assert _warnings(caplog) == []


def test_real_ip_only_request_gets_no_keys(monkeypatch, caplog):
    """X-Real-IP was never recorded by the log, so losing it needs no flag."""
    routes = _routes()
    _no_trust_env(monkeypatch)

    handler = _Handler(client_ip="203.0.113.9", headers={"X-Real-IP": "6.6.6.6"})
    assert routes.request_log_forwarded_fields(handler) == {}
    assert _warnings(caplog) == []


def test_trusted_proxy_resolves_and_is_not_flagged(monkeypatch, caplog):
    """A configured trusted proxy still resolves, and the record is not
    simultaneously flagged as ignored — the keys are mutually exclusive."""
    routes = _routes()
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    clear = getattr(routes._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()

    handler = _Handler(
        client_ip="10.1.2.3", headers={"X-Forwarded-For": "198.51.100.23"}
    )
    fields = routes.request_log_forwarded_fields(handler)

    assert fields == {"forwarded_for": "198.51.100.23"}
    assert "forwarded_for_ignored" not in fields


def test_loopback_tunnel_resolves_and_is_not_flagged(monkeypatch, caplog):
    """Loopback is trusted by default: the right-most non-trusted hop is
    recorded, and the record carries no ignore flag."""
    routes = _routes()
    _no_trust_env(monkeypatch)

    handler = _Handler(
        client_ip="127.0.0.1",
        headers={"X-Forwarded-For": "203.0.113.7, 198.51.100.9"},
    )
    fields = routes.request_log_forwarded_fields(handler)

    assert fields == {"forwarded_for": "198.51.100.9"}
    assert "forwarded_for_ignored" not in fields


def test_thin_wrapper_agrees_with_dict(monkeypatch, caplog):
    """``trusted_forwarded_client_ip`` must stay a faithful address-only view of
    the dict, so the 15+ existing call sites keep their exact contract."""
    routes = _routes()
    _no_trust_env(monkeypatch)

    untrusted = _Handler(
        client_ip="203.0.113.9", headers={"X-Forwarded-For": "198.51.100.77"}
    )
    assert routes.trusted_forwarded_client_ip(untrusted) is None
    assert routes.request_log_forwarded_fields(untrusted) == {
        "forwarded_for_ignored": True
    }

    loopback = _Handler(
        client_ip="127.0.0.1",
        headers={"X-Forwarded-For": "203.0.113.7, 198.51.100.9"},
    )
    assert (
        routes.trusted_forwarded_client_ip(loopback) == "198.51.100.9"
    )
    assert routes.request_log_forwarded_fields(loopback) == {
        "forwarded_for": "198.51.100.9"
    }


def test_log_request_writes_the_flag_into_the_structured_line(
    monkeypatch, caplog, log_output
):
    """End-to-end: the flag must reach the emitted ``[webui] {...}`` line, not
    just the resolver — that is what a log query actually greps."""
    _no_trust_env(monkeypatch)

    handler = Handler.__new__(Handler)
    handler.command = "GET"
    handler.path = "/api/status"
    handler.client_address = ("203.0.113.9", 54321)
    handler.headers = _Headers({"X-Forwarded-For": "198.51.100.77"})

    Handler.log_request(handler, "200")

    line = log_output.getvalue().strip()
    assert line.startswith("[webui] ")
    record = json.loads(line.removeprefix("[webui] "))
    assert record["forwarded_for_ignored"] is True
    assert record["remote"] == "203.0.113.9"
    assert "forwarded_for" not in record
    # The dropped value is never written to the log.
    assert "198.51.100.77" not in line


def test_log_request_omits_flag_for_plain_direct_client(
    monkeypatch, caplog, log_output
):
    """No forwarded header -> no key, so existing log consumers see the same
    line shape they always did."""
    _no_trust_env(monkeypatch)

    handler = Handler.__new__(Handler)
    handler.command = "GET"
    handler.path = "/api/status"
    handler.client_address = ("192.168.1.50", 54321)
    handler.headers = _Headers({})

    Handler.log_request(handler, "200")

    line = log_output.getvalue().strip()
    record = json.loads(line.removeprefix("[webui] "))
    assert "forwarded_for_ignored" not in record
    assert "forwarded_for" not in record
    assert record["remote"] == "192.168.1.50"
