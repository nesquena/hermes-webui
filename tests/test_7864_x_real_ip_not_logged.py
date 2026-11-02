"""#7864: ``X-Real-IP`` alone must never populate the request log's
``forwarded_for``.

nginx relays client-supplied request headers through by default
(``proxy_pass_request_headers on``) rather than overwriting ``X-Real-IP``, so a
header that a client controls cannot be allowed to name the log's client IP:
doing so would re-open the #7863 spoof through a different header, and a
fail2ban jail keyed on ``forwarded_for`` would then ban an arbitrary address.

The pre-#7864 logger never read `X-Real-IP`. Keep it that way at the
request-log boundary: the log resolver consumes only `X-Forwarded-For`, walks
it right-to-left from the right-most untrusted hop, and otherwise resolves to
the raw socket peer.

Scope: the XFF-only rule is a property of the LOG path
(``consult_real_ip=False``). The shared resolver keeps master's ``X-Real-IP``
fallback (``consult_real_ip=True`` default) for the pre-#7863 consumers — the
local-origin gate and trusted-header auth — whose behaviour must not change.
See ``test_7864_shared_resolver_keeps_local_gate_real_ip`` for that contract.
"""

from tests.test_security_review_fixes import _Handler


def _trusted_env(monkeypatch):
    from api import routes

    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "10.9.9.0/24")
    # _trusted_proxy_networks() caches per-process; clear it via the repo idiom.
    clear = getattr(routes._trusted_proxy_networks, "cache_clear", None)
    if clear is not None:
        clear()
    return routes


def test_x_real_ip_alone_never_feeds_forwarded_for(monkeypatch):
    """A loopback/trusted peer sending only `X-Real-IP` resolves to the peer."""
    routes = _trusted_env(monkeypatch)

    handler = _Handler(
        client_ip="10.9.9.7",  # trusted proxy (non-loopback, in the CIDR)
        headers={"X-Real-IP": "203.0.113.99"},
    )

    # The log boundary: XFF-only resolution (consult_real_ip=False) — the raw
    # peer speaks for itself and the client-supplied X-Real-IP is ignored.
    resolved = routes._forwarded_client_ip_from_trusted_proxy(
        handler, consult_real_ip=False
    )

    assert resolved == "10.9.9.7"


def test_x_real_ip_does_not_override_the_xff_chain(monkeypatch):
    """`X-Real-IP` cannot replace a real client hop in an `X-Forwarded-For`."""
    routes = _trusted_env(monkeypatch)

    handler = _Handler(
        client_ip="10.9.9.7",
        headers={
            "X-Forwarded-For": "198.51.100.23",
            "X-Real-IP": "203.0.113.99",
        },
    )

    resolved = routes._forwarded_client_ip_from_trusted_proxy(
        handler, consult_real_ip=False
    )

    assert resolved == "198.51.100.23"


def test_x_real_ip_alone_from_a_trusted_peer_absent_from_log_fields(monkeypatch):
    """End-to-end: the logged field falls back to the peer, never the header."""
    routes = _trusted_env(monkeypatch)

    handler = _Handler(
        client_ip="10.9.9.7",
        headers={"X-Real-IP": "203.0.113.99"},
    )

    # Same gate the request-log boundary uses: trusted-peer check resolves the
    # value that lands in `forwarded_for`.
    assert routes._raw_peer_is_trusted_proxy(handler)
    resolved = routes.trusted_forwarded_client_ip(handler)
    assert resolved is None  # resolver echoes the raw peer → nothing logged


def test_7864_shared_resolver_keeps_local_gate_real_ip(monkeypatch):
    """The CORE regression: deleting the shared X-Real-IP fallback must not
    change the local-origin gate.

    Round 2 removed the fallback from `_forwarded_client_ip_from_trusted_proxy`
    itself — but that helper also feeds `_onboarding_request_is_local`, the gate
    protecting first-run setup and passwordless embedded-terminal access. With
    HERMES_WEBUI_TRUST_FORWARDED_FOR=1, a trusted loopback proxy and only
    `X-Real-IP: 8.8.8.8` (no XFF), the gate then fell back to the raw peer
    (127.0.0.1) and classified an internet client as LOCAL — the opposite of
    what #7863 protects. The default resolver path (consult_real_ip=True) must
    therefore keep master's behavior: the gate refuses the request.
    """
    routes = _trusted_env(monkeypatch)

    handler = _Handler(
        client_ip="127.0.0.1",  # loopback peer: always a trusted proxy
        headers={"X-Real-IP": "8.8.8.8"},
    )

    # Default path = master behavior: X-Real-IP is honoured (the gate sees it).
    assert (
        routes._forwarded_client_ip_from_trusted_proxy(handler) == "8.8.8.8"
    ), "the shared resolver must keep the X-Real-IP fallback for the local-origin gate"
    # The gate therefore classifies the request as remote and refuses it.
    assert routes._onboarding_request_is_local(handler) is False
    # While the log boundary never sees the header.
    assert routes.trusted_forwarded_client_ip(handler) is None


def test_7864_xff_chain_resolution_unchanged_by_the_flag(monkeypatch):
    """The right-to-left XFF walk is identical with or without the fallback.

    A trusted intermediate hop is skipped from the right so the real client is
    resolved; the flag only governs the no-XFF fallback, not the walk.
    """
    routes = _trusted_env(monkeypatch)

    handler = _Handler(
        client_ip="10.9.9.7",  # trusted hop inside the chain
        headers={"X-Forwarded-For": "198.51.100.23, 10.9.9.7"},
    )

    for consult in (True, False):
        resolved = routes._forwarded_client_ip_from_trusted_proxy(
            handler, consult_real_ip=consult
        )
        assert resolved == "198.51.100.23"
