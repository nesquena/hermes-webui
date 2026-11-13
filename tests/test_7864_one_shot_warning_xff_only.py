"""#7864 round 4: the one-shot warning must be spent on a real X-Forwarded-For.

The round-3 warning is process-wide (it fires at most once), so *which*
request consumes it matters. ``_has_forwarded_header()`` used to count
``X-Real-IP`` alongside ``X-Forwarded-For`` even though the request log only
ever recorded the latter. On the default deployment a stray client, scanner or
misconfigured LB carrying only ``X-Real-IP`` therefore burned the single
warning — with a mislabelled message claiming an X-Forwarded-For was ignored —
while the operator's real proxy request lost its ``forwarded_for`` field in
silence, which is exactly what the warning exists to explain.

These tests pin:

* an X-Real-IP-only request stays silent and leaves the warning unspent,
* a following proxy XFF request is the one that warns (the review's order),
* repeated ``X-Forwarded-For`` headers count (via ``get_all``, matching the
  resolver) while an all-blank multi-header set stays silent,
* both the ``get_all`` path and the plain-mapping fallback path agree.
"""

import logging

import pytest

from tests.test_security_review_fixes import _Handler, _Headers


@pytest.fixture(autouse=True)
def _reset_once_guard():
    """The warning is once-per-process; reset it around every test."""
    from api import routes

    routes._FORWARDED_HEADER_IGNORED_WARNED = False
    yield
    routes._FORWARDED_HEADER_IGNORED_WARNED = False


def _capture(caplog):
    caplog.set_level(logging.WARNING, logger="api.routes")
    return [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ]


def _no_trust_env(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", raising=False)
    clear = getattr(_routes()._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()


def _routes():
    from api import routes

    return routes


class _MultiHeader(_Headers):
    """A headers mapping exposing repeated headers like ``email.message``."""

    def __init__(self, mapping=None, repeated=None):
        super().__init__(mapping or {})
        self._repeated = dict(repeated or {})

    def get_all(self, name, default=None):
        for key, values in self._repeated.items():
            if key.lower() == name.lower():
                return list(values)
        value = self.get(name)
        return [value] if value else default


def _handler_with_repeated(peer, xff_values):
    handler = _Handler(client_ip=peer, headers={})
    handler.headers = _MultiHeader({}, {"X-Forwarded-For": xff_values})
    return handler


def test_real_ip_only_request_does_not_warn(monkeypatch, caplog):
    """The review's step 1: a request carrying only ``X-Real-IP`` must not
    consume the one-shot warning (and must not be labelled X-Forwarded-For)."""
    _no_trust_env(monkeypatch)
    caplog.set_level(logging.WARNING, logger="api.routes")

    handler = _Handler(client_ip="203.0.113.9", headers={"X-Real-IP": "6.6.6.6"})

    assert _routes().trusted_forwarded_client_ip(handler) is None
    assert _capture(caplog) == [], "an X-Real-IP-only request must stay silent"


def test_review_order_real_ip_first_then_proxy_xff(monkeypatch, caplog):
    """The review's exact order: the stray X-Real-IP request leaves the warning
    UNSPENT, so the operator's proxy XFF request is the one that warns."""
    routes = _routes()
    _no_trust_env(monkeypatch)
    caplog.set_level(logging.WARNING, logger="api.routes")

    stray = _Handler(client_ip="203.0.113.9", headers={"X-Real-IP": "6.6.6.6"})
    assert routes.trusted_forwarded_client_ip(stray) is None
    assert _capture(caplog) == [], "step 1 must not spend the one-shot warning"

    proxy = _Handler(
        client_ip="172.17.0.1", headers={"X-Forwarded-For": "198.51.100.23"}
    )
    assert routes.trusted_forwarded_client_ip(proxy) is None
    warnings = _capture(caplog)
    assert len(warnings) == 1, "the proxy XFF request is what must warn"
    rendered = warnings[0].getMessage()
    # Correctly attributed: the peer that actually sent an X-Forwarded-For.
    assert "172.17.0.1" in rendered
    assert "203.0.113.9" not in rendered


def test_repeated_xff_headers_are_seen(monkeypatch, caplog):
    """A chain split across two XFF headers still lost the field, so the
    resolver (``get_all``) and the warning trigger must agree."""
    routes = _routes()
    _no_trust_env(monkeypatch)
    caplog.set_level(logging.WARNING, logger="api.routes")

    # First header is blank; the real value only appears in the repeated one.
    handler = _handler_with_repeated("172.17.0.1", ["", "198.51.100.23"])

    assert routes.trusted_forwarded_client_ip(handler) is None
    assert len(_capture(caplog)) == 1, "a repeated XFF header must still warn"


def test_all_blank_repeated_xff_is_silent(monkeypatch, caplog):
    """Several blank XFF headers are still nothing to explain."""
    routes = _routes()
    _no_trust_env(monkeypatch)
    caplog.set_level(logging.WARNING, logger="api.routes")

    handler = _handler_with_repeated("172.17.0.1", ["", "   "])

    assert routes.trusted_forwarded_client_ip(handler) is None
    assert _capture(caplog) == []


def test_real_ip_plus_xff_warns_once(monkeypatch, caplog):
    """Carrying both headers is a genuine XFF loss — warn exactly once, and
    never echo either header value."""
    routes = _routes()
    _no_trust_env(monkeypatch)
    caplog.set_level(logging.WARNING, logger="api.routes")

    handler = _Handler(
        client_ip="172.17.0.1",
        headers={"X-Real-IP": "6.6.6.6", "X-Forwarded-For": "198.51.100.23"},
    )

    assert routes.trusted_forwarded_client_ip(handler) is None
    warnings = _capture(caplog)
    assert len(warnings) == 1
    rendered = warnings[0].getMessage()
    assert "198.51.100.23" not in rendered
    assert "6.6.6.6" not in rendered
