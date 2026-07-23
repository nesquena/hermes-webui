"""CORS preflight must not advertise wider access than the CSRF gate permits.

server.py's do_OPTIONS previously answered every preflight with
``Access-Control-Allow-Origin: *``. It now echoes the request Origin only
when it is same-origin or explicitly allowlisted via
HERMES_WEBUI_ALLOWED_ORIGINS — reusing _check_same_origin_browser_request,
the same policy enforced for real requests.
"""

from pathlib import Path

from api.routes import apply_cors_preflight_headers

ROOT = Path(__file__).resolve().parent.parent


class _FakeHandler:
    """Captures send_header() calls so tests can assert emitted CORS headers."""

    def __init__(self, headers):
        self.headers = headers
        self.sent = {}

    def send_header(self, key, value):
        self.sent[key] = value


def _preflight(headers):
    """Return the Access-Control-Allow-Origin value the preflight would emit,
    or '' when no CORS headers are emitted (disallowed / no-origin)."""
    h = _FakeHandler(headers)
    apply_cors_preflight_headers(h)
    return h.sent.get("Access-Control-Allow-Origin", "")


def _preflight_headers(headers):
    h = _FakeHandler(headers)
    apply_cors_preflight_headers(h)
    return h.sent


class TestPreflightAllowOrigin:
    def test_no_origin_header_omits_cors(self):
        """Non-browser OPTIONS (no Origin) gets no Allow-Origin header."""
        assert _preflight({}) == ""

    def test_same_origin_echoed(self):
        assert _preflight({
            "Origin": "http://127.0.0.1:8787",
            "Host": "127.0.0.1:8787",
        }) == "http://127.0.0.1:8787"

    def test_cross_origin_rejected(self):
        assert _preflight({
            "Origin": "http://evil.com",
            "Host": "127.0.0.1:8787",
        }) == ""

    def test_wildcard_never_returned(self):
        """Even a same-origin match echoes the Origin, never '*'."""
        result = _preflight({
            "Origin": "http://localhost:8787",
            "Host": "localhost:8787",
        })
        assert result != "*"

    def test_sec_fetch_site_cross_site_rejected(self):
        """Sec-Fetch-Site: cross-site is refused even if hosts happened to match."""
        assert _preflight({
            "Origin": "http://127.0.0.1:8787",
            "Host": "127.0.0.1:8787",
            "Sec-Fetch-Site": "cross-site",
        }) == ""

    def test_allowlisted_public_origin_echoed(self, monkeypatch):
        monkeypatch.setenv(
            "HERMES_WEBUI_ALLOWED_ORIGINS", "https://myapp.example.com:8000"
        )
        assert _preflight({
            "Origin": "https://myapp.example.com:8000",
            "Host": "127.0.0.1:8787",
        }) == "https://myapp.example.com:8000"

    def test_non_allowlisted_public_origin_rejected(self, monkeypatch):
        monkeypatch.setenv(
            "HERMES_WEBUI_ALLOWED_ORIGINS", "https://myapp.example.com:8000"
        )
        assert _preflight({
            "Origin": "https://evil.example.com:8000",
            "Host": "127.0.0.1:8787",
        }) == ""

    def test_forwarded_host_untrusted_by_default(self, monkeypatch):
        """Without HERMES_WEBUI_TRUST_FORWARDED_HOST, X-Forwarded-Host is ignored."""
        monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_HOST", raising=False)
        assert _preflight({
            "Origin": "https://webui.example.com",
            "Host": "127.0.0.1:8787",
            "X-Forwarded-Host": "webui.example.com:443",
        }) == ""

    def test_forwarded_host_trusted_when_opted_in(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_HOST", "1")
        assert _preflight({
            "Origin": "https://webui.example.com",
            "Host": "127.0.0.1:8787",
            "X-Forwarded-Host": "webui.example.com:443",
        }) == "https://webui.example.com"


class TestServerNoWildcard:
    def test_server_source_has_no_wildcard_allow_origin(self):
        """Regression guard: the literal `*` Allow-Origin must not come back."""
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        routes_src = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        assert '"Access-Control-Allow-Origin", "*"' not in src
        assert '"Access-Control-Allow-Origin", "*"' not in routes_src
        # server.py stays a thin dispatcher: the preflight logic lives in api/routes.
        assert "apply_cors_preflight_headers" in src
        assert "def apply_cors_preflight_headers" in routes_src

    def test_allowed_preflight_sets_vary_origin(self):
        """An allowed preflight must set Vary: Origin (correct for caching an
        origin-conditional response)."""
        sent = _preflight_headers({
            "Origin": "http://127.0.0.1:8787",
            "Host": "127.0.0.1:8787",
        })
        assert sent.get("Vary") == "Origin"
        assert sent.get("Access-Control-Allow-Origin") == "http://127.0.0.1:8787"

    def test_denied_preflight_emits_no_cors_headers(self):
        """A disallowed origin gets zero CORS headers (browser preflight denial)."""
        sent = _preflight_headers({
            "Origin": "http://evil.com",
            "Host": "127.0.0.1:8787",
        })
        assert sent == {}
