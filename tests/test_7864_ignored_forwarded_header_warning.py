"""#7864 round 3: a one-time operator warning when a forwarded header is ignored.

The #7864 behaviour change (ignore an untrusted peer's ``X-Forwarded-For``) is
correct, but on the DEFAULT deployment a reverse proxy on a Docker bridge or a
LAN address is not an allowlisted trusted proxy — so ``forwarded_for`` silently
leaves the request log and a fail2ban jail keyed on it stops matching with
nothing in the log saying why. These tests pin the one-time warning that closes
that feedback gap:

* it fires for the untreated default environment (the case the review called
  out — no ``HERMES_WEBUI_TRUST_FORWARDED_FOR`` at all),
* it fires at most once per process (rate-limited by construction),
* it stays silent when no forwarded header is present, and when the peer IS a
  trusted proxy (the configured case, no behaviour change to announce),
* it NEVER writes the untrusted header value itself into the log.
"""

import logging

import pytest

from tests.test_security_review_fixes import _Handler


@pytest.fixture(autouse=True)
def _reset_once_guard():
    """The warning is once-per-process; reset it around every test."""
    from api import routes

    routes._FORWARDED_HEADER_IGNORED_WARNED = False
    yield
    routes._FORWARDED_HEADER_IGNORED_WARNED = False


def _capture_warnings(caplog):
    caplog.set_level(logging.WARNING, logger="api.routes")
    return caplog


def test_default_environment_warns_once(monkeypatch, caplog):
    """A Docker-bridge/LAN peer with no trust env at all warns (the review's
    default-deployment case: the field silently disappears otherwise)."""
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", raising=False)
    clear = getattr(routes._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()

    caplog = _capture_warnings(caplog)
    # 172.17.0.1 = the Docker bridge gateway talking to the container: the
    # canonical untrusted proxy in the default deployment.
    handler = _Handler(
        client_ip="172.17.0.1",
        headers={"X-Forwarded-For": "198.51.100.23"},
    )

    assert routes.trusted_forwarded_client_ip(handler) is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "the ignored-header warning must fire exactly once"


def test_warning_is_rate_limited_process_wide(monkeypatch, caplog):
    """Second and later ignored headers do not re-warn (rate-limited by a
    module-level once flag, so it holds across requests)."""
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    clear = getattr(routes._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()

    caplog = _capture_warnings(caplog)
    for _ in range(3):
        handler = _Handler(
            client_ip="192.168.1.10",
            headers={"X-Forwarded-For": "198.51.100.23"},
        )
        assert routes.trusted_forwarded_client_ip(handler) is None

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "three requests must not produce three warnings"


def test_no_forwarded_header_is_silent(monkeypatch, caplog):
    """A direct client with no forwarded header to ignore must not warn —
    direct-LAN access is the common deployment and is entirely normal."""
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    clear = getattr(routes._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()

    caplog = _capture_warnings(caplog)
    handler = _Handler(client_ip="192.168.1.55", headers={})

    assert routes.trusted_forwarded_client_ip(handler) is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []


def test_blank_forwarded_header_is_silent(monkeypatch, caplog):
    """A blank/whitespace header is not an assertion worth warning about."""
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    clear = getattr(routes._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()

    caplog = _capture_warnings(caplog)
    handler = _Handler(client_ip="172.17.0.1", headers={"X-Forwarded-For": "  "})

    assert routes.trusted_forwarded_client_ip(handler) is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_trusted_proxy_does_not_warn(monkeypatch, caplog):
    """A configured trusted proxy resolves normally — no warning, and the
    forwarded_for field is still produced (nothing was ignored)."""
    from api import routes

    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "172.17.0.0/16")
    clear = getattr(routes._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()

    caplog = _capture_warnings(caplog)
    handler = _Handler(
        client_ip="172.17.0.1",
        headers={"X-Forwarded-For": "198.51.100.23"},
    )

    assert routes.trusted_forwarded_client_ip(handler) == "198.51.100.23"
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_warning_never_logs_the_untrusted_header_value(monkeypatch, caplog):
    """The header value is attacker-controlled text: the warning names the peer
    address and the config fix, never the header itself (unbounded log-write
    vector)."""
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    clear = getattr(routes._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()

    caplog = _capture_warnings(caplog)
    hostile = "198.51.100.23, " + "A" * 4000  # a client can send arbitrary text
    handler = _Handler(client_ip="172.17.0.1", headers={"X-Forwarded-For": hostile})

    assert routes.trusted_forwarded_client_ip(handler) is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    rendered = warnings[0].getMessage()
    assert "198.51.100.23" not in rendered
    assert "AAAA" not in rendered
    # The operator still learns the fix from the warning text.
    assert "172.17.0.1" in rendered
    assert "HERMES_WEBUI_TRUST" in rendered
