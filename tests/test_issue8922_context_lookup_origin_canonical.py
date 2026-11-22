"""#8922: two spellings of a host share a credential origin only when the Agent's metadata client
puts the same request authority on the wire for both.

#8917 (the #8822 fix) decides whether a key may go to a host by comparing origins
(:func:`_context_lookup_origin`, api/routes.py). v0.52.544 sent the key when the config spelled the
host differently from the session, so a legitimate key was dropped: an IDN spelling against its
punycode. The helper canonicalises that one pair — the pair httpx 0.28 (the Agent's metadata client)
itself folds with ``idna.encode`` (httpx/_urlparse.py) — so both spellings carry one Host header and
one SNI, modulo the root label a trailing dot names.

It deliberately does **not** canonicalise the other pairs it can see, because the wire does not
agree with them and folding them would send a key to a request its source never named:

* an IPv6 address written expanded vs compressed reaches the wire as written — a loopback listener
  records ``Host: [0:0:0:0:0:0:0:1]:<port>`` for one spelling and ``Host: [::1]:<port>`` for the
  other, so they are two request authorities (and two virtual hosts on a shared listener);
* a DNS name keeps its trailing dot in the Host header, and on a resolver with a search list
  ``name`` and ``name.`` need not even resolve alike.

Both stay distinct origins, so such a key is dropped — the pre-#8922 behaviour, fail closed.

The core invariant below is the one the fix is built on: for every spelling the client accepts,
``_context_lookup_origin(url)`` equals the authority the client puts on the wire.
"""
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest

from api import routes

A = "https://a.model-url.test/v1"

# One host, two spellings: the pair #8922 folds into a single origin.
IDN = "https://b\u00fccher.test/v1"               # u-umlaut spelling
PUNYCODE = "https://xn--bcher-kva.test/v1"        # the same host in punycode
IDN_DOTTED = "https://b\u00fccher.test./v1"       # ... plus the root label
PLAIN = "https://proxy.example.test/v1"
EXPLICIT_DEFAULT_PORT = "https://proxy.example.test:443/v1"
OTHER_PORT = "https://proxy.example.test:8443/v1"
USERINFO = "https://someone@proxy.example.test/v1"       # userinfo, host is proxy.example.test
USERINFO_SPOOF = "https://proxy.example.test@evil.test/v1"  # userinfo looks like the host; it is evil.test
OTHER_HOST = "https://other.example.test/v1"

# Two spellings the client sends as two different authorities: no key may cross.
V6_COMPRESSED = "https://[::1]/v1"
V6_EXPANDED = "https://[0:0:0:0:0:0:0:1]/v1"
V6_ANOTHER = "https://[::2]/v1"
DOTTED = "https://proxy.example.test./v1"

# Spellings httpx 0.28 never sends to the same wire host, so no key may cross between them.
SHARP_S = "https://stra\u00dfe.test/v1"           # sharp-s spelling
SS = "https://strasse.test/v1"                    # ss spelling
FINAL_SIGMA = "https://\u03c2.test/v1"            # final-sigma spelling
SIGMA = "https://\u03c3.test/v1"                  # sigma spelling
FULLWIDTH = "https://\uff50roxy.example.test/v1"  # fullwidth-p spelling

SPELLINGS = [
    pytest.param(PUNYCODE, IDN, id="punycode-vs-idn"),
    pytest.param(IDN, PUNYCODE, id="idn-vs-punycode"),
    pytest.param("https://B\u00dcCHER.TEST/v1", PUNYCODE, id="uppercase-idn-vs-punycode"),
    pytest.param("https://example\u3002com/v1", "https://example.com/v1", id="ideographic-full-stop"),
    pytest.param("https://example\uff0ecom/v1", "https://example.com/v1", id="fullwidth-full-stop"),
]

# Pairs that look like one host to a human but are two authorities to the client.
DISTINCT_AUTHORITIES = [
    pytest.param(V6_COMPRESSED, V6_EXPANDED, id="ipv6-compressed-vs-expanded"),
    pytest.param(V6_EXPANDED, V6_COMPRESSED, id="ipv6-expanded-vs-compressed"),
    pytest.param(V6_COMPRESSED, V6_ANOTHER, id="ipv6-different-address"),
    pytest.param(PLAIN, DOTTED, id="trailing-dot"),
    pytest.param(DOTTED, PLAIN, id="trailing-dot-reverse"),
    pytest.param(IDN_DOTTED, IDN, id="idn-with-vs-without-root-label"),
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


def _wire_authority(url):
    """The ``host:port`` httpx 0.28 puts on the wire for ``url``, or None when it refuses the URL."""
    try:
        parsed = httpx.Request("GET", url).url
    except (httpx.InvalidURL, httpx.UnsupportedProtocol, TypeError):
        return None
    raw = parsed.raw_host
    host = raw.decode("ascii") if isinstance(raw, bytes) else raw
    port = parsed.port
    if port is None:
        port = 80 if parsed.scheme == "http" else 443
    return f"{host}:{port}"


def _wire_host_header(url):
    """The Host header httpx 0.28 sends for ``url`` (the request authority a vhost routes on)."""
    try:
        return httpx.Request("GET", url).headers["host"]
    except (httpx.InvalidURL, httpx.UnsupportedProtocol, TypeError):
        return None


def _todays_origin(url):
    """The pre-#8922 ``_context_lookup_origin``, verbatim, for the differential control below."""
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


# ── The invariant: the compare's answer is the authority the client puts on the wire ──


@pytest.mark.parametrize("url", [
    "https://api.anthropic.com/v1",
    "https://API.ANTHROPIC.COM/v1",
    "https://proxy.example.test/v1",
    "https://proxy.example.test:8443/v1",
    "http://plain.test",
    "https://plain.test",
    IDN,
    PUNYCODE,
    IDN_DOTTED,
    "https://example\u3002com/v1",
    SHARP_S,
    FINAL_SIGMA,
    V6_COMPRESSED,
    V6_EXPANDED,
    "https://[2001:db8::1]/v1",
    "https://[2001:0db8:0000:0000:0000:0000:0000:0001]/v1",
    "https://[fe80::1%eth0]/v1",
    "https://[fe80::1%25eth0]/v1",
    "http://127.0.0.1:8080/v1",
    "https://127.0.0.1./v1",
    "https://2130706433/v1",
    DOTTED,
    "https://proxy.example.test../v1",
    USERINFO,
    USERINFO_SPOOF,
], ids=[
    "builtin-host", "uppercase", "plain", "explicit-port", "http-scheme", "https-scheme",
    "idn", "punycode", "idn-dotted", "ideographic-full-stop", "sharp-s",
    "final-sigma", "ipv6-compressed", "ipv6-expanded", "ipv6-2001", "ipv6-2001-expanded",
    "ipv6-zone", "ipv6-zone-encoded", "ipv4-port", "ipv4-trailing-dot", "ipv4-integer",
    "trailing-dot", "double-trailing-dot", "userinfo", "userinfo-spoof",
])
def test_the_origin_is_the_authority_the_client_puts_on_the_wire(url):
    """``_context_lookup_origin`` may not answer an origin the request never addresses.

    This is the safety property the gate reproduced: a compare broader than the wire lets one host's
    key ride a request to a different authority. Pinning equality makes the compare exactly as wide
    as httpx's own request, so a future canonicalisation cannot silently widen it again.
    """
    assert routes._context_lookup_origin(url) == _wire_authority(url)


# ── The canonicalisation, at the origin itself ─────────────────────────────────


def test_an_idn_spelling_and_its_punycode_are_one_origin():
    assert routes._context_lookup_origin(IDN) == routes._context_lookup_origin(PUNYCODE) == "xn--bcher-kva.test:443"


def test_an_idn_spelling_and_its_punycode_are_one_wire_host():
    """The compare's answer is the Agent's metadata client's own: httpx sends both spellings to one host."""
    assert _wire_host_header(IDN) == _wire_host_header(PUNYCODE) == "xn--bcher-kva.test"


def test_an_idn_spelling_keeps_the_root_label_the_wire_keeps():
    """``idna.encode`` drops a trailing dot; the client does not. Re-appending it keeps the compare
    from folding ``b\u00fccher.test.`` onto the dotless host the request never addresses."""
    assert routes._context_lookup_origin(IDN_DOTTED) == "xn--bcher-kva.test.:443"
    assert _wire_host_header(IDN_DOTTED) == "xn--bcher-kva.test."
    assert routes._context_lookup_origin(IDN_DOTTED) != routes._context_lookup_origin(IDN)


def test_an_ipv6_address_written_two_ways_is_two_origins():
    """The client sends the spelling it was given: two Host headers, two request authorities."""
    assert routes._context_lookup_origin(V6_COMPRESSED) == "::1:443"
    assert routes._context_lookup_origin(V6_EXPANDED) == "0:0:0:0:0:0:0:1:443"
    assert routes._context_lookup_origin(V6_COMPRESSED) != routes._context_lookup_origin(V6_EXPANDED)


def test_a_trailing_dot_is_a_distinct_origin():
    """The root label stays in the Host header, so the dotless host is a different authority."""
    assert routes._context_lookup_origin(DOTTED) == "proxy.example.test.:443"
    assert routes._context_lookup_origin(PLAIN) == "proxy.example.test:443"
    assert routes._context_lookup_origin(DOTTED) != routes._context_lookup_origin(PLAIN)
    # Two root labels are a malformed name, not another spelling of this host, and keep both dots.
    assert routes._context_lookup_origin("https://proxy.example.test../v1") == "proxy.example.test..:443"


def test_an_ipv4_literal_is_unchanged():
    assert routes._context_lookup_origin("https://192.0.2.10:8443/v1") == "192.0.2.10:8443"
    assert routes._context_lookup_origin("https://127.0.0.1./v1") == "127.0.0.1.:443"


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


# ── Key kept: the one spelling pair the wire folds, at the lookup and at the session entry ──


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
    """The Agent's own key after a fallback owns the URL it came with; the IDN pair is that URL."""
    got = _lookup({"model": {"provider": "custom:my-proxy"}}, base_url=PUNYCODE,
                  fallback_api_key="K-FB", fallback_base_url=IDN)
    assert (got.base_url, got.api_key) == (PUNYCODE, "K-FB")


# ── No key: a spelling the client sends to a different authority ────────────────


@pytest.mark.parametrize("session_url, dropped_url", DISTINCT_AUTHORITIES + DROPPED)
def test_a_different_request_authority_gets_no_key(session_url, dropped_url):
    """Each pair names two distinct wire authorities to httpx 0.28, so the key stays home.

    The IPv6 and trailing-dot rows are the gate's reproduction: the pre-fix compare folded them and
    handed the owner's key to the other authority. ``_wire_host_header`` is the routing key a shared
    listener sees, and it differs for every row.
    """
    assert _wire_host_header(session_url) != _wire_host_header(dropped_url)
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
    assert routes._context_lookup_origin(IDN) == "b\u00fccher.test:443"
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
    "https://***@proxy.example.test/v1",
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


# ── Real outbound: the recipient a listener sees, and the credential it gets ────


class _Recorder(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append({"host": self.headers.get("Host"),
                                 "authorization": self.headers.get("Authorization")})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


@pytest.fixture
def loopback6():
    """A real IPv6 loopback listener that records the Host header and credential of every request."""
    try:
        srv = _V6Server(("::1", 0), _Recorder)
    except OSError:
        pytest.skip("no IPv6 loopback on this host")
    srv.seen = []
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def loopback4():
    """A real IPv4 loopback listener that records the Host header and credential of every request."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    srv.seen = []
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _send(client, url, credential):
    """Send the lookup's credential the way the metadata client would: one GET carrying it."""
    headers = {"Authorization": f"Bearer {credential}"} if credential else {}
    return client.get(url, headers=headers)


def test_the_real_client_tells_two_ipv6_spellings_apart_and_keeps_each_key_home(loopback6):
    """The gate's blocker, end to end: one unchanged session URL, two real recipients.

    ``[::1]`` and ``[0:0:0:0:0:0:0:1]`` are the same address but two request authorities, because the
    Host header is the spelling. The record's key belongs to the compressed spelling only; the session
    asks the expanded one. Before the fix the compare folded them, the lookup answered the record's key,
    and that key went out on the expanded request — to a virtual host that does not own it.
    """
    port = loopback6.server_address[1]
    owner = f"http://[::1]:{port}"
    other = f"http://[0:0:0:0:0:0:0:1]:{port}"
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": other},
                   "providers": {"custom:my-proxy": {"base_url": owner, "api_key": "K-OWNER"}}})
    assert (got.base_url, got.api_key) == (other, "")

    with httpx.Client(trust_env=False, timeout=5) as client:
        _send(client, other, got.api_key)              # the session's own (unowned) request
        _send(client, owner, "K-OWNER")                # the owner's request

    assert [r["host"] for r in loopback6.seen] == [f"[0:0:0:0:0:0:0:1]:{port}", f"[::1]:{port}"]
    assert loopback6.seen[0]["authorization"] is None      # the non-owner received no credential
    assert loopback6.seen[1]["authorization"] == "Bearer K-OWNER"


def test_the_real_client_sends_the_owner_its_own_credential_for_the_idn_pair(loopback6):
    """Same-owner control: the pair the wire does fold resolves to one authority and keeps its key."""
    port = loopback6.server_address[1]
    # Two spellings of the same loopback authority (an address literal, so the control needs no DNS).
    url = f"http://[::1]:{port}"
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": url},
                   "providers": {"custom:my-proxy": {"base_url": url, "api_key": "K-OWNER"}}})
    assert got.api_key == "K-OWNER"
    with httpx.Client(trust_env=False, timeout=5) as client:
        _send(client, url, got.api_key)
    assert loopback6.seen[0]["authorization"] == "Bearer K-OWNER"


def test_the_real_client_sends_a_different_address_its_own_credential(loopback4):
    """Different-address control: another port is another authority, and it gets its own key."""
    port = loopback4.server_address[1]
    owner = f"http://127.0.0.1:{port}"
    other = f"http://127.0.0.1:{port + 1}"
    got = _lookup({"model": {"provider": "custom:my-proxy", "base_url": other},
                   "providers": {"custom:my-proxy": {"base_url": owner, "api_key": "K-OWNER"}}})
    assert (got.base_url, got.api_key) == (other, "")
    with httpx.Client(trust_env=False, timeout=5) as client:
        _send(client, owner, "K-OWNER")
    assert loopback4.seen[0]["host"] == f"127.0.0.1:{port}"
    assert loopback4.seen[0]["authorization"] == "Bearer K-OWNER"