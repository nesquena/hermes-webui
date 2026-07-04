"""CORS preflight must not advertise wider access than the CSRF gate permits.

server.py's do_OPTIONS previously answered every preflight with
``Access-Control-Allow-Origin: *``. It now echoes the request Origin only
when it is same-origin or explicitly allowlisted via
HERMES_WEBUI_ALLOWED_ORIGINS — reusing _check_same_origin_browser_request,
the same policy enforced for real requests.
"""

from pathlib import Path
from types import SimpleNamespace

from api.routes import preflight_allow_origin

ROOT = Path(__file__).resolve().parent.parent


def _preflight(headers):
    return preflight_allow_origin(SimpleNamespace(headers=headers))


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
        assert '"Access-Control-Allow-Origin", "*"' not in src
        assert "preflight_allow_origin" in src
