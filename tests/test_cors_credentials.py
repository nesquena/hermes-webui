"""Credentialed cross-origin CORS: opt-in via HERMES_WEBUI_ALLOWED_ORIGINS.

Built on the refactored master where CORS header emission lives in
api/routes.py (apply_cors_preflight_headers / apply_cors_actual_response_headers)
and server.py stays a thin dispatcher.

Covers:
  1. cors_credentialed_origin() — allowlist-only origin echo (never same-origin, never '*').
  2. The Sec-Fetch-Site allowlist ordering fix in _check_same_origin_browser_request
     so cross-origin writes/preflights from allowlisted front-ends aren't 403'd.
  3. apply_cors_preflight_headers() — echoes origin, advertises CSRF token headers +
     Max-Age, gates Allow-Credentials on HTTPS, and Vary: Origin whenever configured.
  4. apply_cors_actual_response_headers() — credentialed CORS on real responses.
  5. set_auth_cookie() — SameSite=None only under allowlist + HTTPS, else Lax.
"""

from types import SimpleNamespace

from api import auth
from api.routes import (
    _check_same_origin_browser_request,
    apply_cors_actual_response_headers,
    apply_cors_preflight_headers,
    cors_credentialed_origin,
)


class _Handler:
    def __init__(self, headers):
        self.headers = headers
        self.request = SimpleNamespace()  # no getpeercert -> not TLS by socket
        self.sent = []

    def send_header(self, key, value):
        self.sent.append((key, value))


def _h(headers):
    return _Handler(headers)


def _sent_dict(handler):
    # Last-wins is fine; these emitters never emit a header twice.
    return dict(handler.sent)


# ── cors_credentialed_origin ────────────────────────────────────────────────
class TestCorsCredentialedOrigin:
    def test_no_allowlist(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_ALLOWED_ORIGINS", raising=False)
        assert cors_credentialed_origin(_h({"Origin": "https://app.example.com"})) == ""

    def test_allowlisted(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://app.example.com")
        assert cors_credentialed_origin(
            _h({"Origin": "https://app.example.com"})
        ) == "https://app.example.com"

    def test_non_allowlisted(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://app.example.com")
        assert cors_credentialed_origin(_h({"Origin": "https://evil.example.com"})) == ""

    def test_never_wildcard(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://app.example.com")
        assert cors_credentialed_origin(_h({"Origin": "https://app.example.com"})) != "*"


# ── Sec-Fetch-Site allowlist ordering ───────────────────────────────────────
class TestSecFetchSiteAllowlistOrdering:
    def test_allowlisted_cross_site_accepted(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://app.example.com")
        assert _check_same_origin_browser_request(_h({
            "Origin": "https://app.example.com",
            "Host": "127.0.0.1:8787",
            "Sec-Fetch-Site": "cross-site",
        })) is True

    def test_non_allowlisted_cross_site_rejected(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://app.example.com")
        assert _check_same_origin_browser_request(_h({
            "Origin": "https://evil.example.com",
            "Host": "127.0.0.1:8787",
            "Sec-Fetch-Site": "cross-site",
        })) is False

    def test_no_allowlist_cross_site_unchanged(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_ALLOWED_ORIGINS", raising=False)
        assert _check_same_origin_browser_request(_h({
            "Origin": "http://127.0.0.1:8787",
            "Host": "127.0.0.1:8787",
            "Sec-Fetch-Site": "cross-site",
        })) is False


# ── apply_cors_preflight_headers ────────────────────────────────────────────
class TestPreflightHeaders:
    def test_allowlisted_https_full_credentialed_set(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://app.example.com")
        monkeypatch.setenv("HERMES_WEBUI_SECURE", "1")
        h = _h({"Origin": "https://app.example.com", "Host": "127.0.0.1:8787",
                "Sec-Fetch-Site": "cross-site"})
        apply_cors_preflight_headers(h)
        d = _sent_dict(h)
        assert d["Access-Control-Allow-Origin"] == "https://app.example.com"
        assert d["Vary"] == "Origin"
        assert "X-Hermes-CSRF-Token" in d["Access-Control-Allow-Headers"]
        assert "X-CSRF-Token" in d["Access-Control-Allow-Headers"]
        assert d["Access-Control-Max-Age"] == "600"
        assert d["Access-Control-Allow-Credentials"] == "true"

    def test_allowlisted_http_no_credentials(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://app.example.com")
        monkeypatch.setenv("HERMES_WEBUI_SECURE", "0")
        h = _h({"Origin": "https://app.example.com", "Host": "127.0.0.1:8787",
                "Sec-Fetch-Site": "cross-site"})
        apply_cors_preflight_headers(h)
        d = _sent_dict(h)
        assert d["Access-Control-Allow-Origin"] == "https://app.example.com"
        assert "Access-Control-Allow-Credentials" not in d

    def test_non_allowlisted_only_vary(self, monkeypatch):
        """Configured allowlist + rejected origin: Vary (cache safety), no ACAO."""
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://app.example.com")
        h = _h({"Origin": "https://evil.example.com", "Host": "127.0.0.1:8787",
                "Sec-Fetch-Site": "cross-site"})
        apply_cors_preflight_headers(h)
        d = _sent_dict(h)
        assert d.get("Vary") == "Origin"
        assert "Access-Control-Allow-Origin" not in d
        assert "Access-Control-Allow-Credentials" not in d

    def test_no_wildcard_ever(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://app.example.com")
        h = _h({"Origin": "https://app.example.com", "Host": "127.0.0.1:8787"})
        apply_cors_preflight_headers(h)
        assert _sent_dict(h).get("Access-Control-Allow-Origin") != "*"


# ── apply_cors_actual_response_headers ──────────────────────────────────────
class TestActualResponseHeaders:
    def test_allowlisted_https(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://app.example.com")
        monkeypatch.setenv("HERMES_WEBUI_SECURE", "1")
        h = _h({"Origin": "https://app.example.com", "Host": "127.0.0.1:8787"})
        apply_cors_actual_response_headers(h)
        d = _sent_dict(h)
        assert d["Access-Control-Allow-Origin"] == "https://app.example.com"
        assert d["Access-Control-Allow-Credentials"] == "true"
        assert d["Vary"] == "Origin"

    def test_allowlisted_http_no_credentials(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://app.example.com")
        monkeypatch.setenv("HERMES_WEBUI_SECURE", "0")
        h = _h({"Origin": "https://app.example.com", "Host": "127.0.0.1:8787"})
        apply_cors_actual_response_headers(h)
        d = _sent_dict(h)
        assert d["Access-Control-Allow-Origin"] == "https://app.example.com"
        assert "Access-Control-Allow-Credentials" not in d
        assert d["Vary"] == "Origin"

    def test_non_allowlisted_only_vary(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://app.example.com")
        h = _h({"Origin": "https://evil.example.com", "Host": "127.0.0.1:8787"})
        apply_cors_actual_response_headers(h)
        d = _sent_dict(h)
        assert d.get("Vary") == "Origin"
        assert "Access-Control-Allow-Origin" not in d

    def test_no_allowlist_emits_nothing(self, monkeypatch):
        """Default deployment: no CORS headers at all on actual responses."""
        monkeypatch.delenv("HERMES_WEBUI_ALLOWED_ORIGINS", raising=False)
        h = _h({"Origin": "https://app.example.com", "Host": "127.0.0.1:8787"})
        apply_cors_actual_response_headers(h)
        assert h.sent == []


# ── set_auth_cookie SameSite gating ─────────────────────────────────────────
class _CookieHandler:
    def __init__(self):
        self.headers = {}
        self.request = SimpleNamespace()
        self.sent = []

    def send_header(self, key, value):
        self.sent.append((key, value))


def _set_cookie_string(monkeypatch, allowlist, secure_env):
    if allowlist:
        monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", allowlist)
    else:
        monkeypatch.delenv("HERMES_WEBUI_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_SECURE", secure_env)
    h = _CookieHandler()
    auth.set_auth_cookie(h, "tok123")
    return next(v for (k, v) in h.sent if k == "Set-Cookie")


class TestAuthCookieSameSite:
    def test_allowlist_and_secure_none(self, monkeypatch):
        sc = _set_cookie_string(monkeypatch, "https://app.example.com", "1")
        assert "SameSite=None" in sc and "Secure" in sc

    def test_allowlist_without_secure_lax(self, monkeypatch):
        sc = _set_cookie_string(monkeypatch, "https://app.example.com", "0")
        assert "SameSite=Lax" in sc and "SameSite=None" not in sc

    def test_no_allowlist_secure_lax(self, monkeypatch):
        sc = _set_cookie_string(monkeypatch, "", "1")
        assert "SameSite=Lax" in sc and "SameSite=None" not in sc

    def test_default_unchanged(self, monkeypatch):
        sc = _set_cookie_string(monkeypatch, "", "0")
        assert "SameSite=Lax" in sc and "HttpOnly" in sc

    def test_none_implies_secure_invariant(self, monkeypatch):
        for allowlist in ("", "https://app.example.com"):
            for secure in ("0", "1"):
                sc = _set_cookie_string(monkeypatch, allowlist, secure)
                if "SameSite=None" in sc:
                    assert "Secure" in sc
