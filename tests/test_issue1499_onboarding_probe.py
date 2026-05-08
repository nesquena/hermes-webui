"""Regression: onboarding wizard probes <base_url>/models before persisting (#1499).

Pre-#1499, `apply_onboarding_setup` accepted whatever `base_url` the user typed
without ever fetching `<base_url>/models`. The wizard would finish in ~200ms
with no outbound HTTP request, persist an unreachable URL silently, and leave
the user with an empty model dropdown that they had to populate by hand-editing
`config.yaml`.

Reporters: @chwps's log timeline in #1420 was the smoking gun — onboarding
submit completed in 239ms and there was no GET to `<HostIP>:1234/v1/models`
anywhere in the WebUI container's outbound trace.

The fix is the new `probe_provider_endpoint(provider, base_url, api_key)` in
`api/onboarding.py` and the matching `POST /api/onboarding/probe` route. The
frontend wizard runs the probe debounced on baseUrl input and blocking on
Continue for any provider with `requires_base_url=True`.

This file pins the backend probe contract (the function and the endpoint).
The frontend wiring is exercised through manual reproduction during PR review;
testing JS-side debounce behavior in pytest would add an outsized harness for
the value.

Each test mode covers exactly one error code from `PROBE_ERROR_CODES`, plus
the success path with model-list parsing. The probe response is also asserted
to NOT be persisted to config.yaml (the original wizard bug was that probe-
discovered data was indistinguishable from user-entered data after persist).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests._pytest_port import BASE


@pytest.fixture
def mock_models_server():
    """Spin up a tiny HTTP server with several /v1/models response variants."""
    server_box: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server convention
            # /v1/models — happy path with OpenAI shape
            if self.path == "/v1/models":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "data": [
                        {"id": "qwen3-27b", "object": "model", "owned_by": "user"},
                        {"id": "llama-3.3-70b", "object": "model", "owned_by": "user"},
                    ]
                }).encode())
                return

            # /barelist/models — bare list shape some self-hosted servers return
            if self.path == "/barelist/models":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps([
                    {"id": "alpha"}, {"id": "beta"},
                ]).encode())
                return

            # /v1bad/models — 404 (wrong path)
            if self.path == "/v1bad/models":
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "not found"}')
                return

            # /v1/parse/models — 200 with non-JSON body
            if self.path == "/v1/parse/models":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"this is not json")
                return

            # /v1/wrongshape/models — 200 with JSON but not OpenAI shape
            if self.path == "/v1/wrongshape/models":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"unexpected": "shape"}')
                return

            # /v1/auth/models — 200 only with correct bearer token
            if self.path == "/v1/auth/models":
                auth = self.headers.get("Authorization", "")
                if auth == "Bearer correct-token":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"data": [{"id": "auth-only"}]}')
                else:
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b"unauthorized")
                return

            # Default: 500
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "boom"}')

        def log_message(self, *args, **kwargs):  # noqa: N802 — suppress test noise
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    server_box["port"] = httpd.server_address[1]
    server_box["base"] = f"http://127.0.0.1:{server_box['port']}"
    server_box["httpd"] = httpd

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    # Tiny sleep so the listening socket is observable when the test connects.
    time.sleep(0.05)

    yield server_box

    httpd.shutdown()
    httpd.server_close()


class TestIssue1499OnboardingProbe:
    # ── Direct unit tests on probe_provider_endpoint (no HTTP layer) ────────

    def test_invalid_url_empty(self):
        from api.onboarding import probe_provider_endpoint
        r = probe_provider_endpoint("lmstudio", "")
        assert r["ok"] is False
        assert r["error"] == "invalid_url"

    def test_invalid_url_bad_scheme(self):
        from api.onboarding import probe_provider_endpoint
        r = probe_provider_endpoint("lmstudio", "ftp://example.com:1234/v1")
        assert r["ok"] is False
        assert r["error"] == "invalid_url"

    def test_invalid_url_no_host(self):
        from api.onboarding import probe_provider_endpoint
        r = probe_provider_endpoint("lmstudio", "http:///models")
        assert r["ok"] is False
        assert r["error"] == "invalid_url"

    def test_dns_resolution_failure(self):
        """Unresolvable hostname → error='dns'."""
        from api.onboarding import probe_provider_endpoint
        r = probe_provider_endpoint(
            "lmstudio",
            "http://this-host-definitely-does-not-exist-zxq987.invalid:1234/v1",
            timeout=2.0,
        )
        assert r["ok"] is False
        assert r["error"] == "dns", f"Expected dns error, got {r}"

    def test_connect_refused(self):
        """Connecting to a port nobody's listening on → error='connect_refused'."""
        from api.onboarding import probe_provider_endpoint
        # Port 1 is reserved tcpmux and on Linux/macOS dev boxes is universally
        # not listening. If a future CI environment binds something there this
        # will need updating, but no realistic CI binds port 1.
        r = probe_provider_endpoint("lmstudio", "http://127.0.0.1:1/v1", timeout=2.0)
        assert r["ok"] is False
        assert r["error"] == "connect_refused", f"Expected connect_refused, got {r}"

    def test_success_openai_shape(self, mock_models_server):
        from api.onboarding import probe_provider_endpoint
        r = probe_provider_endpoint("lmstudio", f"{mock_models_server['base']}/v1")
        assert r["ok"] is True, f"Expected success, got {r}"
        assert len(r["models"]) == 2
        ids = [m["id"] for m in r["models"]]
        assert "qwen3-27b" in ids
        assert "llama-3.3-70b" in ids
        for m in r["models"]:
            assert m["id"] == m["label"], "label defaults to id when no separate label"

    def test_success_bare_list_shape(self, mock_models_server):
        """Some self-hosted servers return a bare list, not an OpenAI envelope."""
        from api.onboarding import probe_provider_endpoint
        r = probe_provider_endpoint("lmstudio", f"{mock_models_server['base']}/barelist")
        assert r["ok"] is True, f"Expected success, got {r}"
        ids = [m["id"] for m in r["models"]]
        assert ids == ["alpha", "beta"]

    def test_http_4xx(self, mock_models_server):
        from api.onboarding import probe_provider_endpoint
        r = probe_provider_endpoint("lmstudio", f"{mock_models_server['base']}/v1bad")
        assert r["ok"] is False
        assert r["error"] == "http_4xx"
        assert r.get("status") == 404

    def test_http_5xx(self, mock_models_server):
        from api.onboarding import probe_provider_endpoint
        r = probe_provider_endpoint("lmstudio", f"{mock_models_server['base']}/v1explode")
        assert r["ok"] is False
        assert r["error"] == "http_5xx"
        assert r.get("status") == 500

    def test_parse_non_json(self, mock_models_server):
        from api.onboarding import probe_provider_endpoint
        r = probe_provider_endpoint("lmstudio", f"{mock_models_server['base']}/v1/parse")
        assert r["ok"] is False
        assert r["error"] == "parse"
        assert "JSON" in r["detail"] or "json" in r["detail"]

    def test_parse_wrong_shape(self, mock_models_server):
        """JSON body but not OpenAI /models shape → error='parse'."""
        from api.onboarding import probe_provider_endpoint
        r = probe_provider_endpoint("lmstudio", f"{mock_models_server['base']}/v1/wrongshape")
        assert r["ok"] is False
        assert r["error"] == "parse"
        assert "OpenAI" in r["detail"] or "shape" in r["detail"]

    def test_api_key_passes_authorization_header(self, mock_models_server):
        """Probe sends api_key as Bearer when provided."""
        from api.onboarding import probe_provider_endpoint

        # Without key → 401 → http_4xx
        r = probe_provider_endpoint("lmstudio", f"{mock_models_server['base']}/v1/auth")
        assert r["error"] == "http_4xx"

        # With wrong key → 401
        r = probe_provider_endpoint(
            "lmstudio",
            f"{mock_models_server['base']}/v1/auth",
            api_key="wrong-token",
        )
        assert r["error"] == "http_4xx"

        # With correct key → success
        r = probe_provider_endpoint(
            "lmstudio",
            f"{mock_models_server['base']}/v1/auth",
            api_key="correct-token",
        )
        assert r["ok"] is True
        assert [m["id"] for m in r["models"]] == ["auth-only"]

    def test_probe_does_not_persist_to_config(self, mock_models_server, tmp_path, monkeypatch):
        """Probe is read-only — must NOT touch config.yaml or .env.

        Pre-fix the wizard would have happily auto-written the probed model
        list into config.yaml, pinning a stale catalog. The probe path must
        stay pure-read so that ``apply_onboarding_setup`` remains the single
        write surface.
        """
        from api import onboarding as ob

        # Redirect any potential write to tmp_path so we'd notice if the probe
        # wrote anything by accident.
        monkeypatch.setattr(ob, "_get_active_hermes_home", lambda: tmp_path)
        cfg_path = tmp_path / "config.yaml"
        monkeypatch.setattr(ob, "_get_config_path", lambda: cfg_path)

        # Ensure neither file exists before the probe.
        env_path = tmp_path / ".env"
        assert not cfg_path.exists()
        assert not env_path.exists()

        ob.probe_provider_endpoint(
            "lmstudio",
            f"{mock_models_server['base']}/v1",
            api_key="some-key",
        )

        assert not cfg_path.exists(), (
            "probe_provider_endpoint must be read-only — it wrote to config.yaml"
        )
        assert not env_path.exists(), (
            "probe_provider_endpoint must be read-only — it wrote to .env "
            "(api_key would have leaked)"
        )

    def test_probe_does_not_follow_redirects(self):
        """Probe refuses HTTP redirects — surfaces as `unreachable` with a 3xx hint.

        SSRF defense-in-depth: an authenticated user typing a base URL that
        redirects (intentionally or otherwise) should not have the probe
        chase the redirect to internal services.  The auth + local-network
        gate already restricts the practical attack surface, but tightening
        the redirect default is cheap insurance.  Reviewer-flagged on PR #1501.
        """
        import json
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from api.onboarding import probe_provider_endpoint

        class _RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/v1/models":
                    self.send_response(302)
                    self.send_header("Location", "/different-endpoint")
                    self.end_headers()
                    return
                # If we accidentally follow the redirect, the test sees data
                # and assertion fails.
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"id": "should-not-see"}]}).encode())

            def log_message(self, *args, **kwargs):  # noqa: N802
                pass

        httpd = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.05)
        try:
            r = probe_provider_endpoint("lmstudio", f"http://127.0.0.1:{port}/v1")
            assert r["ok"] is False, (
                f"Probe followed a redirect — should have refused. Got {r!r}. "
                f"_NoRedirectHandler is missing or broken."
            )
            assert r["error"] == "unreachable", (
                f"3xx redirect should surface as 'unreachable', got {r['error']!r}"
            )
            assert r.get("status") == 302
            # Detail must mention "redirect" so the user understands what
            # happened — the localized error banner uses this string verbatim.
            assert "redirect" in r["detail"].lower()
            # Crucially, the probe must NOT have surfaced data from the
            # redirect target (which our test handler returned for any other path).
            assert "should-not-see" not in str(r)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_probe_error_codes_set_is_documented(self):
        """The PROBE_ERROR_CODES tuple is the public contract for the frontend.

        Every code returned by probe_provider_endpoint must be in this tuple
        so frontend localization keys can mechanically derive from it.
        """
        from api.onboarding import PROBE_ERROR_CODES
        # If you add a new error code, also add an i18n key
        # `onboarding_probe_error_<code>` to all 9 locale blocks in
        # static/i18n.js (search for `onboarding_probe_error_`).
        expected = {
            "invalid_url", "dns", "connect_refused", "timeout",
            "http_4xx", "http_5xx", "parse", "unreachable",
        }
        assert set(PROBE_ERROR_CODES) == expected, (
            f"PROBE_ERROR_CODES drift: got {set(PROBE_ERROR_CODES)}, "
            f"expected {expected}. Update static/i18n.js if you intentionally "
            f"changed this set."
        )


class TestIssue1499ProbeRouteEndToEnd:
    """End-to-end smoke test for `POST /api/onboarding/probe`.

    The route is a thin wrapper around `probe_provider_endpoint`; the unit
    tests above cover the function logic exhaustively.  This class verifies
    the wiring: route exists, parses JSON body, returns probe result as JSON.
    """

    def _post(self, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            BASE + "/api/onboarding/probe",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read()), r.status
        except urllib.error.HTTPError as e:
            return json.loads(e.read()), e.code

    def test_route_returns_invalid_url_for_empty_base(self):
        body, status = self._post({"provider": "lmstudio", "base_url": ""})
        assert status == 200
        assert body["ok"] is False
        assert body["error"] == "invalid_url"

    def test_route_returns_success_against_mock(self, mock_models_server):
        body, status = self._post({
            "provider": "lmstudio",
            "base_url": f"{mock_models_server['base']}/v1",
        })
        assert status == 200, f"unexpected status {status}: {body}"
        assert body["ok"] is True
        assert isinstance(body["models"], list)
        assert any(m["id"] == "qwen3-27b" for m in body["models"])

    def test_route_returns_dns_error_for_bad_host(self):
        body, status = self._post({
            "provider": "lmstudio",
            "base_url": "http://this-host-definitely-does-not-exist-zxq987.invalid:1234/v1",
        })
        assert status == 200
        assert body["ok"] is False
        assert body["error"] == "dns"
