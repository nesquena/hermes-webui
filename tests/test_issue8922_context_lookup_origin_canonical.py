"""#8922: one host spelled two ways is one origin, so the key its source owns survives the compare.

#8917 (the #8822 fix) decides whether a key may go to a host by comparing origins
(:func:`_context_lookup_origin`, api/routes.py). The helper lowercased the host and filled the
default port but canonicalised nothing, so one host spelled two ways in two config sources
counted as two hosts and a legitimate key was dropped: an IDN spelling against its punycode, a
trailing dot, an IPv6 address written expanded against compressed. Metadata that needs the key
then falls back to the default context window instead of the host's own (in an auth-gated mock,
8,192 tokens with the key and the 256,000 default without). v0.52.544 sent the key in these
shapes.

The canonicalisation lives inside ``_context_lookup_origin``, so every ownership check (the
session gate and the lookup's owned and contacted origins) agrees:

* IP literals: ``ipaddress.ip_address(host).compressed`` (a host that parses as no address
  keeps today's spelling rather than raising).
* one trailing dot stripped from a DNS name.
* IDN: the ``idna`` package's IDNA 2008 encode — the same call httpx 0.28 (the Agent's metadata
  client) makes (``idna.encode(host.lower())``, httpx/_urlparse.py) — imported optionally;
  without it today's compare stands and IDN spellings drop the key (fail closed). Never the
  built-in ``str.encode("idna")``: it is IDNA 2003, maps sharp-s to ss and final-sigma to sigma,
  and would send a key to a host its source never named.
* distinct effective ports stay distinct, and userinfo never enters the host.

It stays fail closed throughout: a host no spelling names still gets no key. The no-key cases
below are checked against httpx's wire host, not assumed.
"""
import sys
from urllib.parse import urlsplit

import pytest

import httpx

from api import routes

A = "https://a.model-url.test/v1"

# One host, two spellings: the shapes #8922 folds into a single origin.
IDN = "https://bücher.test/v1"               # sharp-u umlaut spelling
PUNYCODE = "https://xn--bcher-kva.test/v1"   # the same host in punycode
DOTTED = "https://proxy.example.test./v1"    # trailing-dot spelling
PLAIN = "https://proxy.example.test/v1"      # the same host without it
V6_COMPRESSED = "https://[2001:db8::1]/v1"
V6_EXPANDED = "https://[2001:0db8:0000:0000:0000:0000:0000:0001]/v1"
EXPLICIT_DEFAULT_PORT = "https://proxy.example.test:443/v1"

# Spellings httpx 0.28 never sends to the same wire host, so no key may cross between them.
SHARP_S = "https://straße.test/v1"           # sharp-s spelling
SS = "https://strasse.test/v1"               # ss spelling
FINAL_SIGMA = "https://ς.test/v1"       # final-sigma spelling
SIGMA = "https://σ.test/v1"             # sigma spelling
FULLWIDTH = "https://ｐroxy.example.test/v1"  # fullwidth-p spelling
OTHER_HOST = "https://other.example.test/v1"
USERINFO = "https://user@proxy.example.test/v1"           # userinfo, host is proxy.example.test
USERINFO_SPOOF = "https://proxy.example.test@evil.test/v1"  # userinfo looks like the host; it is evil.test
OTHER_PORT = "https://proxy.example.test:8443/v1"

SPELLINGS = [
    pytest.param(PUNYCODE, IDN, id="punycode-vs-idn"),
    pytest.param(IDN, PUNYCODE, id="idn-vs-punycode"),
    pytest.param(DOTTED, PLAIN, id="trailing-dot"),
    pytest.param(V6_EXPANDED, V6_COMPRESSED, id="ipv6-expanded-vs-compressed"),
    pytest.param(V6_COMPRESSED, V6_EXPANDED, id="ipv6-compressed-vs-expanded"),
]

DROPPED = [
    pytest.param(SS, SHARP_S, id="sharp-s-vs-ss"),
    pytest.param(SIGMA, FINAL_SIGMA, id="final-sigma-vs-sigma"),
    pytest.param(PLAIN, FULLWIDTH, id="fullwidth"),
    pytest.param(PLAIN, OTHER_HOST, id="different-host"),
    pytest.param(PLAIN, USERINFO_SPOOF, id="userinfo-spoof"),
    pytest.param(PLAIN, OTHER_PORT, id="different-port"),
]


def _lookup(cfg, **kw):
    cfg.setdefault("providers", {})
    cfg.setdefault("custom_providers", [])
    return routes._context_length_lookup_inputs_for_model("m-x", "custom:my-proxy", cfg=cfg, **kw)


def _session_state(cfg, monkeypatch):
    """Bind ``cfg`` for the real resolvers and return (base_url, api_key) as GET /api/session hands them on."""
    import api.config as config

    cfg.setdefault("providers", {})
    cfg.setdefault("custom_providers", [])
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    _model, _provider, base_url, api_key = routes._session_context_length_lookup_state(
        "m-x", "custom:my-proxy")
    return base_url, api_key


def _split_spelling(session_url, record_url, key="KB"):
    """A session at ``session_url`` whose config record holds ``key`` at ``record_url``: one host, two spellings."""
    return _lookup({"model": {"provider": "custom:my-proxy", "base_url": session_url},
                    "providers": {"custom:my-proxy": {"base_url": record_url, "api_key": key}}})


def _wire_host(url):
    """The host httpx 0.28 puts on the wire for ``url``, or None when it refuses the URL (nothing is sent)."""
    try:
        request = httpx.Request("GET", url)
    except httpx.InvalidURL:
        return None
    return request.headers["host"]


def _todays_origin(url):
    """The pre-#8922 ``_context_lookup_origin``, verbatim, for the differential control below."""
    from urllib.parse import urlsplit

    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text if "://" in text else f"https://{text}")
        host = parts.hostname or ""
        if ":" not in host:
            host = host.lower()
        port = parts.port or (80 if parts.scheme == "http" else 443)
    except ValueError:
        return ""
    return f"{host}:{port}" if host else ""


# ── The canonicalisation, at the origin itself ─────────────────────────────────


def test_an_idn_spelling_and_its_punycode_are_one_origin():
    assert routes._context_lookup_origin(IDN) == routes._context_lookup_origin(PUNYCODE) == "xn--bcher-kva.test:443"


def test_an_idn_spelling_and_its_punycode_are_one_wire_host():
    """The compare's answer is the Agent's metadata client's own: httpx sends both spellings to one host."""
    assert _wire_host(IDN) == _wire_host(PUNYCODE) == "xn--bcher-kva.test"


def test_one_trailing_dot_is_stripped_from_a_dns_name():
    assert routes._context_lookup_origin(DOTTED) == "proxy.example.test:443"
    # Exactly one root label: a second dot is a malformed name, not another spelling of this host.
    assert routes._context_lookup_origin("https://proxy.example.test../v1") == "proxy.example.test.:443"


def test_ipv6_expanded_and_compressed_are_one_origin():
    assert routes._context_lookup_origin(V6_EXPANDED) == routes._context_lookup_origin(V6_COMPRESSED) == "2001:db8::1:443"


def test_an_ipv4_literal_is_unchanged():
    assert routes._context_lookup_origin("https://192.0.2.10:8443/v1") == "192.0.2.10:8443"


def test_an_unparseable_host_keeps_todays_spelling():
    """A host that parses as no IP address keeps today's spelling instead of raising (and a bracket pair
    urlsplit itself refuses still answers "", as before)."""
    assert routes._context_lookup_origin("https://1.2.3.4.5/v1") == "1.2.3.4.5:443"
    assert routes._context_lookup_origin("https://[not-an-ip]/v1") == ""


def test_distinct_effective_ports_stay_distinct():
    assert routes._context_lookup_origin(PLAIN) == routes._context_lookup_origin(EXPLICIT_DEFAULT_PORT)
    assert routes._context_lookup_origin(OTHER_PORT) != routes._context_lookup_origin(PLAIN)


def test_userinfo_never_enters_the_host():
    assert routes._context_lookup_origin(USERINFO) == routes._context_lookup_origin(PLAIN)
    assert routes._context_lookup_origin(USERINFO_SPOOF) == "evil.test:443"


# ── Key kept: the same host spelled two ways, at the lookup and at the session entry ──


@pytest.mark.parametrize("session_url, record_url", SPELLINGS)
def test_one_host_spelled_two_ways_keeps_the_key_at_the_lookup(session_url, record_url):
    got = _split_spelling(session_url, record_url)
    assert (got.base_url, got.api_key) == (session_url, "KB")


@pytest.mark.parametrize("session_url, record_url", SPELLINGS)
def test_one_host_spelled_two_ways_keeps_the_key_at_the_session(session_url, record_url, monkeypatch):
    """GET /api/session takes the record's own pair; the canonical origin keeps them one host's."""
    base_url, api_key = _session_state({"model": {"provider": "custom:my-proxy", "base_url": session_url},
                                        "providers": {"custom:my-proxy": {"base_url": record_url, "api_key": "KB"}}},
                                       monkeypatch)
    assert (routes._context_lookup_origin(base_url), api_key) == (routes._context_lookup_origin(record_url), "KB")


@pytest.mark.parametrize("session_url, record_url", [
    pytest.param(EXPLICIT_DEFAULT_PORT, PLAIN, id="explicit-default-port"),
    pytest.param(USERINFO, PLAIN, id="userinfo-stripped"),
])
def test_ordinary_pairings_keep_the_key_at_the_lookup(session_url, record_url):
    """Spellings that were already one origin before #8922 stay one origin (the fix adds no match)."""
    got = _split_spelling(session_url, record_url)
    assert (got.base_url, got.api_key) == (session_url, "KB")


def test_a_fallback_pair_spelled_two_ways_keeps_the_key_at_the_lookup():
    """The Agent's own key after a fallback owns the URL it came with; one host spelled two ways is that URL."""
    got = _lookup({"model": {"provider": "custom:my-proxy"}}, base_url=PUNYCODE,
                  fallback_api_key="K-FB", fallback_base_url=IDN)
    assert (got.base_url, got.api_key) == (PUNYCODE, "K-FB")


# ── No key: spellings httpx never sends to the same wire host ───────────────────


@pytest.mark.parametrize("session_url, dropped_url", DROPPED)
def test_a_spelling_httpx_sends_nowhere_near_gets_no_key(session_url, dropped_url):
    """Each pair names two distinct wire targets for httpx 0.28 (or none at all), so the key stays home."""
    assert _wire_host(session_url) != _wire_host(dropped_url)
    got = _split_spelling(session_url, dropped_url)
    assert (got.base_url, got.api_key) == (session_url, "")


@pytest.mark.parametrize("spelling, pair, folded", [
    pytest.param(SHARP_S, SS, "strasse.test", id="sharp-s"),
    pytest.param(FINAL_SIGMA, SIGMA, "xn--4xa.test", id="final-sigma"),
    pytest.param(FULLWIDTH, PLAIN, "proxy.example.test", id="fullwidth"),
])
def test_the_built_in_idna_codec_folds_these_onto_their_pair(spelling, pair, folded):
    """The control that keeps the no-key cases from passing vacuously: under the built-in IDNA 2003 codec
    each of these spellings folds onto its pair's host, so only the httpx-wire IDNA 2008 encode separates
    them — and a swap to the built-in codec (or a UTS46 fold of the fullwidth form) shows up as a kept key."""
    host = urlsplit(spelling).hostname
    assert host.encode("idna").decode("ascii") == folded
    assert routes._context_lookup_origin(pair).rsplit(":", 1)[0] == folded


# ── No idna installed: the compare falls back to today's behaviour ──────────────


def test_without_the_idna_package_the_compare_falls_back_to_todays_behaviour(monkeypatch):
    """``idna`` is optional (WebUI does not depend on it): stubbed out, an IDN spelling drops the key again
    and nothing new matches, exactly as before #8922."""
    monkeypatch.setitem(sys.modules, "idna", None)   # importing idna now raises ImportError
    assert routes._context_lookup_origin(IDN) == "bücher.test:443"
    assert routes._context_lookup_origin(IDN) != routes._context_lookup_origin(PUNYCODE)
    got = _split_spelling(PUNYCODE, IDN)
    assert (got.base_url, got.api_key) == (PUNYCODE, "")
    # Nothing new matches either: the sharp-s, final-sigma and fullwidth spellings stay where they were.
    assert routes._context_lookup_origin(SHARP_S) != routes._context_lookup_origin(SS)
    assert routes._context_lookup_origin(FINAL_SIGMA) != routes._context_lookup_origin(SIGMA)
    assert routes._context_lookup_origin(FULLWIDTH) != routes._context_lookup_origin(PLAIN)


# ── Differential control: an ordinary host's origin is byte-for-byte today's ────


@pytest.mark.parametrize("url", [
    "https://api.anthropic.com/v1",
    "https://API.ANTHROPIC.COM/v1",
    "https://api.novita.ai/v1",
    "https://chatgpt.com/v1",
    "https://proxy.example.test:8443/v1",
    "http://plain.test",
    "https://plain.test",
    "plain.test",
    "https://xn--bcher-kva.test/v1",
    "https://XN--BCHER-KVA.TEST/v1",
    "https://user@proxy.example.test/v1",
    A,
    "",
    None,
], ids=[
    "builtin-host", "uppercase", "fixed-host", "codex-host", "explicit-port", "http-scheme",
    "https-scheme", "no-scheme", "punycode", "uppercase-punycode", "userinfo", "plain", "empty", "none",
])
def test_an_ordinary_hosts_origin_is_byte_for_byte_todays(url):
    """No IDN, no trailing dot, no IP literal: the canonicalisation must not move an ordinary host's origin."""
    assert routes._context_lookup_origin(url) == _todays_origin(url)
