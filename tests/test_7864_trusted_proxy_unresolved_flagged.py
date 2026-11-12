"""#7864 round 6: a TRUSTED proxy whose XFF does not resolve must still be flagged.

Round 5 covered the untrusted-peer branch of ``request_log_forwarded_fields()``
only. The trusted branch took a shortcut: once the peer was trusted and the
resolver produced no distinct address it returned a bare ``{}`` — so a request
carrying a non-blank ``X-Forwarded-For`` that resolved to nothing emitted a
record with NEITHER ``forwarded_for`` NOR ``forwarded_for_ignored``. That is
precisely the silence round 5 exists to eliminate, one branch over.

With ``HERMES_WEBUI_TRUSTED_PROXY_CIDRS=172.17.0.0/16`` and peer ``172.17.0.1``:

    X-Forwarded-For                       record fields
    ------------------------------------  --------------------------------
    ``198.51.100.23``                     {'forwarded_for': '198.51.100.23'} ✓
    ``198.51.100.23,``                    {'forwarded_for_ignored': True}
    ``,`` or `` , ``                       {'forwarded_for_ignored': True}
    ``not-an-ip``                         {'forwarded_for_ignored': True}
    ``198.51.100.23, garbage``            {'forwarded_for_ignored': True}

The first row is the review's own control case: master logged a ``forwarded_for``
for every one of these requests (it recorded the raw left-most XFF hop for any
peer), so an operator's fail2ban rule keyed on that field silently stops matching
the other four. A trailing comma is a real-world proxy shape, so this is not a
contrived input.

These tests pin:

* the five review rows above, as emitted ``[webui]`` log records (not just the
  resolver's return value),
* the flag is a BOOLEAN and never echoes the dropped header text,
* the two keys stay mutually exclusive on the trusted branch too,
* the preserved behaviour next door: a trusted peer with NO XFF still gets
  neither key, and a trusted proxy that DOES resolve is not flagged,
* the other consumers of the shared resolver are untouched — the local-origin
  gate keeps the ``consult_real_ip=True`` default and its master verdicts
  across the same five inputs (master-vs-fix comparison).
"""

import json

from server import Handler

from tests.test_security_review_fixes import _Handler, _Headers

# The review's exact environment: the Docker bridge is an allowlisted trusted
# proxy, and the raw peer IS that proxy.
_TRUST_ENV = {
    "HERMES_WEBUI_TRUST_FORWARDED_FOR": "1",
    "HERMES_WEBUI_TRUSTED_PROXY_CIDRS": "172.17.0.0/16",
}
_PEER = "172.17.0.1"
_SOLVED_IP = "198.51.100.23"

# (X-Forwarded-For value, expected record fields) — the review's table.
_UNRESOLVED_XFFS = (
    "198.51.100.23,",      # trailing comma: a real-world proxy shape
    ",",                   # single empty hop
    " , ",                 # single blank hop
    "not-an-ip",           # a non-IP token in the chain
    "198.51.100.23, garbage",  # a valid hop followed by garbage
)


def _trusted_env(monkeypatch):
    """Configure the review's trusted-proxy env and clear the net cache."""
    from api import routes

    for key, value in _TRUST_ENV.items():
        monkeypatch.setenv(key, value)
    # _trusted_proxy_networks() caches per-process; clear it via the repo idiom.
    clear = getattr(routes._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()
    return routes


def _log_handler(client_ip, headers):
    """Build a real ``Handler`` instance for the log path, mirroring the
    neighbouring test_7864_forwarded_ignored_per_record.py: ``Handler.__new__``
    gives the real ``log_request``/``_safe_webui_print`` without a socket."""
    handler = Handler.__new__(Handler)
    handler.command = "GET"
    handler.path = "/api/status"
    handler.client_address = (client_ip, 54321)
    handler.headers = _Headers(headers)
    return handler


def _emitted_record(handler):
    """Run the real log_request and return the parsed emitted record."""
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
    return json.loads(line[len("[webui] "):])


def test_control_a_resolving_xff_still_logs_forwarded_for(monkeypatch):
    """The review's control row: with a solvable XFF nothing is lost, so no flag."""
    _trusted_env(monkeypatch)

    record = _emitted_record(
        _log_handler(_PEER, {"X-Forwarded-For": _SOLVED_IP})
    )

    assert record["remote"] == _PEER
    assert record["forwarded_for"] == _SOLVED_IP
    assert "forwarded_for_ignored" not in record


def test_the_five_review_rows_are_flagged_in_the_emitted_log(monkeypatch):
    """Each review row: a non-blank XFF that resolved to nothing still flags."""
    _trusted_env(monkeypatch)

    for xff in _UNRESOLVED_XFFS:
        record = _emitted_record(_log_handler(_PEER, {"X-Forwarded-For": xff}))
        assert record["remote"] == _PEER, xff
        assert record["forwarded_for_ignored"] is True, xff
        assert "forwarded_for" not in record, xff


def test_flag_is_boolean_and_never_echoes_the_dropped_value(monkeypatch):
    """A trusted peer relays the client's malformed chain verbatim, so the value
    is just as attacker-controlled as on the untrusted branch: the record must
    carry the BOOLEAN, never the header text."""
    _trusted_env(monkeypatch)

    hostile = "1.2.3.4\nFORGED injected line"
    record = _emitted_record(
        _log_handler(_PEER, {"X-Forwarded-For": hostile})
    )

    assert record["forwarded_for_ignored"] is True
    rendered = json.dumps(record)
    assert "1.2.3.4" not in rendered
    assert "FORGED" not in rendered


def test_trusted_peer_without_xff_still_gets_no_keys(monkeypatch):
    """PRESERVED master behaviour: a direct-to-proxy request with no forwarded
    header drops nothing, so the line stays byte-identical to master's."""
    _trusted_env(monkeypatch)

    record = _emitted_record(_log_handler(_PEER, {}))

    assert "forwarded_for_ignored" not in record
    assert "forwarded_for" not in record
    assert record["remote"] == _PEER


def test_trusted_proxy_that_resolves_is_not_also_flagged(monkeypatch):
    """The keys stay mutually exclusive on the trusted branch."""
    routes = _trusted_env(monkeypatch)

    fields = routes.request_log_forwarded_fields(
        _Handler(client_ip=_PEER, headers={"X-Forwarded-For": _SOLVED_IP})
    )
    assert fields == {"forwarded_for": _SOLVED_IP}
    assert "forwarded_for_ignored" not in fields


def test_thin_wrapper_never_sees_the_new_flag(monkeypatch):
    """``trusted_forwarded_client_ip`` stays an address-only view, so the
    trusted chain still yields None when nothing resolves."""
    routes = _trusted_env(monkeypatch)

    for xff in _UNRESOLVED_XFFS:
        handler = _Handler(client_ip=_PEER, headers={"X-Forwarded-For": xff})
        assert routes.trusted_forwarded_client_ip(handler) is None, xff


def test_local_origin_gate_verdicts_are_unchanged(monkeypatch):
    """The OTHER consumer of the shared resolver must not flip.

    ``_onboarding_request_is_local`` resolves with the default
    ``consult_real_ip=True`` (master behaviour kept for the non-log consumers).
    For every one of the review's five inputs the gate must keep rejecting —
    a malformed forwarded chain from a trusted proxy fails closed both before and
    after this change, so no gate flipped local here.
    """
    routes = _trusted_env(monkeypatch)

    for xff in _UNRESOLVED_XFFS:
        handler = _Handler(client_ip=_PEER, headers={"X-Forwarded-For": xff})
        assert routes._forwarded_client_ip_from_trusted_proxy(handler) is None, xff
        assert routes._onboarding_request_is_local(handler) is False, xff


def test_gate_still_admits_a_loopback_client_unchanged(monkeypatch):
    """PRESERVED: the common direct-LAN gateway path is untouched by the flag.

    A loopback raw peer with a solvable chain resolves to the real client and the
    gate keeps its master verdict.
    """
    routes = _trusted_env(monkeypatch)

    handler = _Handler(
        client_ip="127.0.0.1",
        headers={"X-Forwarded-For": "127.0.0.1, 192.168.1.50"},
    )

    assert (
        routes._forwarded_client_ip_from_trusted_proxy(handler) == "192.168.1.50"
    )
    assert routes._onboarding_request_is_local(handler) is True
    # ...and the log path records the client, with no ignore flag.
    assert routes.request_log_forwarded_fields(handler) == {
        "forwarded_for": "192.168.1.50"
    }
