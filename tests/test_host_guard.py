"""Unit tests for api/host_guard.py — DNS-rebinding Host allowlist."""

import io

import pytest

import api.host_guard as host_guard
from api.host_guard import (
    _strip_port,
    enforce_host_guard,
    get_allowed_hosts_from_env,
    is_host_allowed,
    parse_allowed_hosts,
)


# ── _strip_port ────────────────────────────────────────────────────────────


class TestStripPort:
    def test_bare_hostname(self):
        assert _strip_port("localhost") == "localhost"

    def test_hostname_with_port(self):
        assert _strip_port("localhost:8787") == "localhost"

    def test_ipv4_with_port(self):
        assert _strip_port("127.0.0.1:8787") == "127.0.0.1"

    def test_ipv4_bare(self):
        assert _strip_port("192.168.1.5") == "192.168.1.5"

    def test_bracketed_ipv6_with_port(self):
        assert _strip_port("[::1]:8787") == "::1"

    def test_bracketed_ipv6_no_port(self):
        assert _strip_port("[::1]") == "::1"

    def test_unbracketed_ipv6(self):
        # Tolerated even though RFC 7230 requires brackets.
        assert _strip_port("::1") == "::1"

    def test_lowercases(self):
        assert _strip_port("LocalHost:8787") == "localhost"
        assert _strip_port("Example.COM") == "example.com"

    def test_strips_whitespace(self):
        assert _strip_port("  localhost  ") == "localhost"

    def test_empty(self):
        assert _strip_port("") is None
        assert _strip_port("   ") is None

    def test_non_string(self):
        assert _strip_port(None) is None  # type: ignore[arg-type]
        assert _strip_port(123) is None  # type: ignore[arg-type]

    def test_malformed_brackets(self):
        assert _strip_port("[::1") is None
        assert _strip_port("[::1]junk") is None

    def test_invalid_port(self):
        assert _strip_port("localhost:abc") is None

    def test_unbracketed_garbage(self):
        assert _strip_port("not::a::valid::ip") is None


# ── parse_allowed_hosts ────────────────────────────────────────────────────


class TestParseAllowedHosts:
    def test_empty(self):
        assert parse_allowed_hosts("") == set()
        assert parse_allowed_hosts(None) == set()

    def test_single(self):
        assert parse_allowed_hosts("webui.example.com") == {"webui.example.com"}

    def test_comma_separated(self):
        assert parse_allowed_hosts("a.com,b.com") == {"a.com", "b.com"}

    def test_whitespace_separated(self):
        assert parse_allowed_hosts("a.com b.com") == {"a.com", "b.com"}

    def test_mixed_separators_and_padding(self):
        assert parse_allowed_hosts("  a.com , b.com\tc.com  ") == {"a.com", "b.com", "c.com"}

    def test_strips_scheme(self):
        assert parse_allowed_hosts("https://x.com,http://y.com") == {"x.com", "y.com"}

    def test_strips_trailing_slash(self):
        assert parse_allowed_hosts("https://x.com/") == {"x.com"}

    def test_strips_path_component(self):
        # A pasted URL with a path must reduce to the bare host, not an
        # unmatchable ``host/path`` token that silently never matches.
        assert parse_allowed_hosts("https://webui.example.com/path") == {"webui.example.com"}
        assert parse_allowed_hosts("x.com/a/b,y.com:8000/c") == {"x.com", "y.com"}

    def test_strips_port(self):
        assert parse_allowed_hosts("x.com:8000,y.com:9000") == {"x.com", "y.com"}

    def test_lowercases(self):
        assert parse_allowed_hosts("WebUI.Example.com") == {"webui.example.com"}

    def test_skips_blank_entries(self):
        assert parse_allowed_hosts(",,, ,,") == set()

    def test_dedupes(self):
        assert parse_allowed_hosts("x.com,x.com,X.COM") == {"x.com"}


# ── is_host_allowed: always-allowed (no explicit list) ─────────────────────


class TestIsHostAllowedBuiltin:
    @pytest.mark.parametrize("host", [
        "127.0.0.1",
        "127.0.0.1:8787",
        "127.0.0.42",
        "127.255.255.254:80",
        "localhost",
        "localhost:8787",
        "ip6-localhost",
        "ip6-loopback:8787",
        "anything.localhost",
        "anything.localhost:8787",
        "[::1]",
        "[::1]:8787",
        "::1",
    ])
    def test_loopback_allowed(self, host):
        assert is_host_allowed(host) is True, host

    @pytest.mark.parametrize("host", [
        "10.0.0.1",
        "10.255.255.254:8787",
        "172.16.0.1",
        "172.31.255.254:8787",
        "192.168.1.5",
        "192.168.0.0:8787",
        "169.254.1.1",       # IPv4 link-local
        "[fe80::1]:8787",    # IPv6 link-local
        "[fc00::1]",         # IPv6 ULA
        "[fd00::abcd]",
    ])
    def test_lan_allowed(self, host):
        assert is_host_allowed(host) is True, host

    @pytest.mark.parametrize("host", [
        "100.64.0.1",
        "100.64.0.1:8787",
        "100.127.255.254",
    ])
    def test_tailscale_cgn_allowed(self, host):
        assert is_host_allowed(host) is True, host

    @pytest.mark.parametrize("host", [
        "evil.example.com",
        "evil.example.com:8787",
        "victim.attacker.com:8787",
        "8.8.8.8",
        "1.2.3.4:8787",
        "[2001:4860:4860::8888]",
        # Edge of CGN range — adjacent public IPs MUST be rejected.
        "100.63.255.254",
        "100.128.0.1",
    ])
    def test_public_rejected(self, host):
        assert is_host_allowed(host) is False, host

    def test_empty_rejected(self):
        assert is_host_allowed("") is False
        assert is_host_allowed("   ") is False


# ── is_host_allowed: explicit allowlist ────────────────────────────────────


class TestIsHostAllowedExplicit:
    def test_explicit_match(self):
        assert is_host_allowed("webui.example.com:8787", explicit={"webui.example.com"}) is True

    def test_explicit_no_match(self):
        assert is_host_allowed("evil.com:8787", explicit={"webui.example.com"}) is False

    def test_case_insensitive(self):
        assert is_host_allowed("Webui.Example.Com:8787", explicit={"webui.example.com"}) is True
        assert is_host_allowed("webui.example.com:8787", explicit={"WEBUI.EXAMPLE.COM"}) is True

    def test_no_wildcard_matching(self):
        # Exact match only — sub.example.com is NOT covered by example.com.
        # If we ever add wildcard support this test must change deliberately.
        assert is_host_allowed("sub.example.com:8787", explicit={"example.com"}) is False

    def test_loopback_always_allowed_even_without_explicit(self):
        assert is_host_allowed("127.0.0.1:8787", explicit=set()) is True


# ── get_allowed_hosts_from_env ─────────────────────────────────────────────


class TestEnvReader:
    def test_unset(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_ALLOWED_HOSTS", raising=False)
        assert get_allowed_hosts_from_env() == set()

    def test_blank(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_HOSTS", "")
        assert get_allowed_hosts_from_env() == set()

    def test_set(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_HOSTS", "webui.example.com,api.example.com")
        assert get_allowed_hosts_from_env() == {"webui.example.com", "api.example.com"}


# ── enforce_host_guard: handler-level integration ──────────────────────────


class _FakeHandler:
    """Minimal handler stub mimicking BaseHTTPRequestHandler."""

    def __init__(self, host_header: str = ""):
        self.headers = {"Host": host_header}
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}
        self.ended = False

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        self.ended = True


class TestEnforceHostGuard:
    def test_loopback_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_ALLOWED_HOSTS", raising=False)
        h = _FakeHandler("127.0.0.1:8787")
        assert enforce_host_guard(h) is True
        assert h.status is None  # no response sent on accept

    def test_rebound_attacker_host_rejected(self, monkeypatch):
        """The DNS-rebinding bypass case: attacker hostname resolves to
        127.0.0.1 but the Host header carries the attacker hostname."""
        monkeypatch.delenv("HERMES_WEBUI_ALLOWED_HOSTS", raising=False)
        h = _FakeHandler("victim.attacker.com:8787")
        assert enforce_host_guard(h) is False
        assert h.status == 400
        assert b"Host header" in h.wfile.getvalue()

    def test_missing_host_header_rejected(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_ALLOWED_HOSTS", raising=False)
        h = _FakeHandler("")
        assert enforce_host_guard(h) is False
        assert h.status == 400

    def test_env_allowlist_admits_public_host(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_HOSTS", "webui.example.com")
        h = _FakeHandler("webui.example.com:8787")
        assert enforce_host_guard(h) is True
        assert h.status is None

    def test_env_allowlist_still_rejects_other_hosts(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_HOSTS", "webui.example.com")
        h = _FakeHandler("evil.com:8787")
        assert enforce_host_guard(h) is False
        assert h.status == 400

    def test_tailscale_host_allowed(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_ALLOWED_HOSTS", raising=False)
        h = _FakeHandler("100.64.0.10:8787")
        assert enforce_host_guard(h) is True

    def test_handler_without_headers_is_safe(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_ALLOWED_HOSTS", raising=False)

        class _NoHeaders:
            wfile = io.BytesIO()
            status = None
            sent_headers: dict = {}
            ended = False
            # No `headers` attribute.

            def send_response(self, s):
                self.status = s

            def send_header(self, k, v):
                self.sent_headers[k] = v

            def end_headers(self):
                self.ended = True

        h = _NoHeaders()
        assert enforce_host_guard(h) is False
        assert h.status == 400

    def test_rejected_response_is_well_formed_json(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_ALLOWED_HOSTS", raising=False)
        h = _FakeHandler("evil.com")
        enforce_host_guard(h)
        import json as _json
        body = h.wfile.getvalue()
        parsed = _json.loads(body)
        assert "error" in parsed
        # Content-Length must match body length so HTTP/1.1 framing stays sane.
        assert h.sent_headers.get("Content-Length") == str(len(body))

    def test_send_failure_does_not_propagate(self, monkeypatch):
        """A client that disconnects mid-reject must not crash the handler."""
        monkeypatch.delenv("HERMES_WEBUI_ALLOWED_HOSTS", raising=False)

        class _BrokenHandler(_FakeHandler):
            def send_response(self, status):
                raise BrokenPipeError("client gone")

        h = _BrokenHandler("evil.com")
        # Must return False without raising.
        assert enforce_host_guard(h) is False
