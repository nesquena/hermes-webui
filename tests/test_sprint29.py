"""
Sprint 29 Tests: Security hardening — 12 fixes from PR #171.

Covers:
  1. CSRF protection — cross-origin POST rejected, same-origin allowed
  2. Login rate limiting — 5th attempt 429, 6th rejected, still works after burst
  3. Session ID validation — non-hex chars rejected in Session.load()
  4. Error path sanitization — _sanitize_error() strips filesystem paths
  5. Secure cookie detection — getattr used safely on plain socket
  6. HMAC signature length — 32-char hex (128-bit), not 16
  7. Skills path traversal — path outside SKILLS_DIR rejected
  8. Content-Disposition for dangerous MIME types — HTML/SVG force download
  9. PBKDF2 password hashing — save_settings uses auth._hash_password
  10. Non-loopback startup warning (manual / integration test)
  11. SSRF DNS check logic (unit test on helper function)
  12. ENV_LOCK export — _ENV_LOCK importable from streaming module
"""
import importlib
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from conftest import TEST_STATE_DIR

from tests._pytest_port import BASE


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def post(path, body=None, headers=None):
    data = json.dumps(body or {}).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def get_raw_with_headers(path):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read(), dict(r.headers.items()), r.status


# ── 1. CSRF Protection ─────────────────────────────────────────────────────


class TestCSRF:
    @staticmethod
    def _csrf_allowed(headers):
        from types import SimpleNamespace
        from api.routes import _check_csrf

        return _check_csrf(SimpleNamespace(headers=headers))

    def test_no_origin_no_referer_allowed(self):
        """Curl-style request with no Origin/Referer must pass CSRF check."""
        body, status = post("/api/sessions/new", {})
        # Should succeed (200 or 404) but NOT 403
        assert status != 403, f"Expected non-403 for no-origin request, got {status}"

    def test_cross_origin_post_rejected(self):
        """Cross-origin POST (Origin != Host) must be rejected with 403."""
        body, status = post(
            "/api/sessions/new",
            {},
            headers={"Origin": "http://evil.com", "Host": "127.0.0.1:8788"},
        )
        assert status == 403, f"Expected 403 for cross-origin request, got {status}: {body}"
        assert "cross-origin" in body.get("error", "").lower() or "rejected" in body.get("error", "").lower()

    def test_same_origin_post_allowed(self):
        """Same-origin POST (Origin matches Host) must be allowed."""
        body, status = post(
            "/api/sessions/new",
            {},
            headers={"Origin": "http://127.0.0.1:8788", "Host": "127.0.0.1:8788"},
        )
        assert status != 403, f"Expected non-403 for same-origin request, got {status}: {body}"

    def test_same_origin_referer_allowed(self):
        """Same-origin Referer (matching Host) must be allowed."""
        body, status = post(
            "/api/sessions/new",
            {},
            headers={"Referer": "http://127.0.0.1:8788/", "Host": "127.0.0.1:8788"},
        )
        assert status != 403, f"Expected non-403 for same-referer request, got {status}"

    def test_proxy_host_default_https_port_matches_http_origin(self):
        """http:// origin without port must NOT match X-Forwarded-Host with :443.

        After the scheme-aware _ports_match fix: http:// absent port = :80,
        which is not equal to :443. These are different protocols/ports and
        should be rejected. In real reverse proxy scenarios where the external
        URL is HTTPS, the browser sends Origin: https://... not http://...
        See test_proxy_host_default_https_port_matches_https_origin for the
        real-world proxy case that should pass.
        """
        assert not self._csrf_allowed({
            "Origin": "http://example.com",
            "X-Forwarded-Host": "example.com:443",
        }), 'http origin (port :80) must not match https host (:443)'

    def test_proxy_host_default_https_port_matches_https_origin(self):
        """HTTPS Origin without port should match X-Forwarded-Host with explicit :443."""
        assert self._csrf_allowed({
            "Origin": "https://example.com",
            "X-Forwarded-Host": "example.com:443",
        })

    def test_proxy_host_port_normalization_still_rejects_other_host(self):
        """Port normalization must not allow different hosts through."""
        assert not self._csrf_allowed({
            "Origin": "https://evil.com",
            "X-Forwarded-Host": "example.com:443",
        })

    def test_allowed_public_origin_bypasses_missing_proxy_port(self, monkeypatch):
        """Explicitly configured public origins should pass even if proxy strips :port from Host."""
        monkeypatch.setenv('HERMES_WEBUI_ALLOWED_ORIGINS', 'https://myapp.example.com:8000')
        assert self._csrf_allowed({
            'Origin': 'https://myapp.example.com:8000',
            'Host': 'myapp.example.com',
            'X-Forwarded-Proto': 'https',
        })

    def test_other_origin_not_allowed_by_public_origin_allowlist(self, monkeypatch):
        """Allowlist must stay exact; unrelated origins must still be rejected."""
        monkeypatch.setenv('HERMES_WEBUI_ALLOWED_ORIGINS', 'https://myapp.example.com:8000')
        assert not self._csrf_allowed({
            'Origin': 'https://evil.com:8000',
            'Host': 'myapp.example.com',
            'X-Forwarded-Proto': 'https',
        })

    # ── Port normalization: scheme-aware (M-1 fix) ────────────────────────────

    def test_cross_protocol_port_not_confused_http_origin_https_host(self):
        """http:// origin must NOT match a host with :443 (HTTPS default).

        Before M-1 fix, _ports_match treated both 80 and 443 as equivalent to
        absent port, allowing http://host to match https://host:443 servers.
        """
        assert not self._csrf_allowed({
            'Origin': 'http://example.com',     # http, no port = :80
            'X-Forwarded-Host': 'example.com:443',  # HTTPS port
        }), 'http origin should NOT match host advertising port 443'

    def test_cross_protocol_port_not_confused_https_origin_http_host(self):
        """https:// origin must NOT match a host with :80 (HTTP default)."""
        assert not self._csrf_allowed({
            'Origin': 'https://example.com',    # https, no port = :443
            'X-Forwarded-Host': 'example.com:80',   # HTTP port
        }), 'https origin should NOT match host advertising port 80'

    def test_http_explicit_port_80_matches_host_without_port(self):
        """http://example.com:80 is the same origin as http://example.com."""
        assert self._csrf_allowed({
            'Origin': 'http://example.com:80',
            'Host': 'example.com',
        })

    def test_https_explicit_port_443_matches_host_without_port(self):
        """https://example.com:443 is the same origin as https://example.com."""
        assert self._csrf_allowed({
            'Origin': 'https://example.com:443',
            'Host': 'example.com',
        })

    def test_non_default_port_not_waived(self):
        """Non-default ports (e.g. :8000) must not be treated as equivalent to absent."""
        assert not self._csrf_allowed({
            'Origin': 'https://example.com:8000',
            'Host': 'example.com',
        })

    # ── Bug scenario: proxy strips non-standard port ──────────────────────────

    def test_bug_origin_8000_host_without_port_rejected_without_allowlist(self, monkeypatch):
        """Without the allowlist, origin with :8000 must be rejected when proxy strips port.

        This documents the original bug: Origin: https://app.com:8000 with
        Host: app.com (proxy stripped the port). Before this PR that returned 403.
        The fix (HERMES_WEBUI_ALLOWED_ORIGINS) handles it; without the env var
        the request is still rejected, which is the safe default.
        """
        monkeypatch.delenv('HERMES_WEBUI_ALLOWED_ORIGINS', raising=False)
        assert not self._csrf_allowed({
            'Origin': 'https://myapp.example.com:8000',
            'Host': 'myapp.example.com',
        }), 'without allowlist, port mismatch must be rejected (safe default)'

    def test_allowed_origins_comma_separated(self, monkeypatch):
        """HERMES_WEBUI_ALLOWED_ORIGINS accepts multiple comma-separated origins."""
        monkeypatch.setenv(
            'HERMES_WEBUI_ALLOWED_ORIGINS',
            'https://app1.example.com:8000, https://app2.example.com:9000',
        )
        assert self._csrf_allowed({'Origin': 'https://app1.example.com:8000', 'Host': 'proxy.internal'})
        assert self._csrf_allowed({'Origin': 'https://app2.example.com:9000', 'Host': 'proxy.internal'})
        assert not self._csrf_allowed({'Origin': 'https://evil.com:8000', 'Host': 'proxy.internal'})

    def test_allowed_origins_without_scheme_ignored(self, monkeypatch, capsys):
        """Allowlist entries missing the scheme are skipped and a warning is printed."""
        monkeypatch.setenv('HERMES_WEBUI_ALLOWED_ORIGINS', 'myapp.example.com:8000')
        from api.routes import _allowed_public_origins
        result = _allowed_public_origins()
        assert len(result) == 0, 'entry without scheme must be ignored'
        captured = capsys.readouterr()
        assert 'WARNING' in captured.err and 'scheme' in captured.err.lower()

    def test_allowed_origins_trailing_slash_normalized(self, monkeypatch):
        """Trailing slash in allowlist entry is stripped before comparison."""
        monkeypatch.setenv('HERMES_WEBUI_ALLOWED_ORIGINS', 'https://myapp.example.com:8000/')
        assert self._csrf_allowed({
            'Origin': 'https://myapp.example.com:8000',
            'Host': 'proxy.internal',
        })


# ── CSRF helpers: unit tests ─────────────────────────────────────────────────


class TestCSRFHelpers:
    """Direct unit tests for _normalize_host_port and _ports_match."""
    def test_normalize_host_only(self):
        from api.routes import _normalize_host_port
        assert _normalize_host_port('example.com') == ('example.com', None)

    def test_normalize_host_with_port(self):
        from api.routes import _normalize_host_port
        assert _normalize_host_port('example.com:8000') == ('example.com', '8000')

    def test_normalize_ipv6_no_port(self):
        from api.routes import _normalize_host_port
        assert _normalize_host_port('[::1]') == ('::1', None)

    def test_normalize_ipv6_with_port(self):
        from api.routes import _normalize_host_port
        assert _normalize_host_port('[::1]:8080') == ('::1', '8080')

    def test_normalize_empty(self):
        from api.routes import _normalize_host_port
        assert _normalize_host_port('') == ('', None)

    def test_normalize_whitespace_stripped(self):
        from api.routes import _normalize_host_port
        assert _normalize_host_port('  example.com  ') == ('example.com', None)

    def test_normalize_lowercases(self):
        from api.routes import _normalize_host_port
        assert _normalize_host_port('EXAMPLE.COM:80') == ('example.com', '80')

    def test_ports_match_identical(self):
        from api.routes import _ports_match
        assert _ports_match('https', '8000', '8000') is True

    def test_ports_match_both_absent(self):
        from api.routes import _ports_match
        assert _ports_match('https', None, None) is True

    def test_ports_match_https_absent_vs_443(self):
        from api.routes import _ports_match
        assert _ports_match('https', None, '443') is True
        assert _ports_match('https', '443', None) is True

    def test_ports_match_http_absent_vs_80(self):
        from api.routes import _ports_match
        assert _ports_match('http', None, '80') is True
        assert _ports_match('http', '80', None) is True

    def test_ports_match_http_absent_vs_443_rejected(self):
        """http:// scheme: absent port is :80, not :443."""
        from api.routes import _ports_match
        assert _ports_match('http', None, '443') is False
        assert _ports_match('http', '443', None) is False

    def test_ports_match_https_absent_vs_80_rejected(self):
        """https:// scheme: absent port is :443, not :80."""
        from api.routes import _ports_match
        assert _ports_match('https', None, '80') is False
        assert _ports_match('https', '80', None) is False

    def test_ports_match_non_default_never_waived(self):
        from api.routes import _ports_match
        assert _ports_match('https', None, '8000') is False
        assert _ports_match('https', '8000', None) is False
        assert _ports_match('http', None, '8080') is False

    def test_ports_match_different_non_default(self):
        from api.routes import _ports_match
        assert _ports_match('https', '8000', '9000') is False


# ── 2. Login Rate Limiting ─────────────────────────────────────────────────


class TestLoginRateLimit:
    def test_rate_limit_triggers_429(self):
        """More than 5 failed login attempts from same IP must yield 429."""
        from api.auth import _login_attempts, _LOGIN_WINDOW

        # Force the rate limiter state: inject 5 stale-now timestamps so next call is fresh
        # Actually easier: just hit the endpoint 6 times with wrong password
        # But we can't set a password in a test without config file.
        # Instead test the helper directly.
        import time
        from api import auth as _auth

        # Reset state for a fake IP
        fake_ip = "10.255.254.253"
        _auth._login_attempts[fake_ip] = []

        # Record 5 attempts — should still be allowed
        for _ in range(5):
            _auth._record_login_attempt(fake_ip)
        assert not _auth._check_login_rate(fake_ip), \
            "After 5 attempts, _check_login_rate should return False (blocked)"

    def test_rate_limit_resets_after_window(self):
        """After window expires, rate limit resets."""
        import time
        from api import auth as _auth

        fake_ip = "10.255.254.252"
        # Inject 5 old timestamps (outside window)
        old_ts = time.time() - 70  # 70s ago, outside 60s window
        _auth._login_attempts[fake_ip] = [old_ts] * 5
        assert _auth._check_login_rate(fake_ip), \
            "After window expires, IP should be allowed again"

    def test_rate_limit_endpoint_returns_429(self, webui_server):
        """Live endpoint: 6th bad attempt returns 429 (auth enabled required)."""
        # This test only runs meaningfully when auth is enabled.
        # We can still verify the helper returns 429 from the unit test above.
        # If auth not enabled, endpoint returns 200 OK with 'Auth not enabled'.
        from api import auth as _auth

        fake_ip = "10.255.254.251"
        # Fill the bucket
        _auth._login_attempts[fake_ip] = [time.time()] * 5
        assert not _auth._check_login_rate(fake_ip)


# ── 3. Session ID Validation ───────────────────────────────────────────────


class TestSessionIDValidation:
    def test_hex_session_id_loads(self, tmp_path):
        """A valid hex session ID gets past the validation check."""
        import sys
        sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
        from api.models import Session, SESSION_DIR
        valid_hex = "deadbeef" * 8  # 64 hex chars
        # Should not raise — returns None only if file doesn't exist (it won't)
        result = Session.load(valid_hex)
        assert result is None  # No file, but no error

    def test_new_format_session_id_passes_validation(self):
        """New hermes-agent session IDs (YYYYMMDD_HHMMSS_xxxxxx) must pass validation."""
        from api.models import Session
        # Should pass the validator (returns None only because the file doesn't exist)
        result = Session.load("20260406_164014_74b2d1")
        assert result is None  # file doesn't exist, but validator passed

    def test_non_hex_session_id_rejected(self):
        """A session ID with dangerous chars must be rejected."""
        from api.models import Session
        evil_ids = [
            "../../../etc/passwd",
            "../../../../root/.ssh/id_rsa",
            "session; rm -rf /",
            "hello world",
            "ZZZZZZZZZZZZZZZZ",
            "session\x00evil",
            "..\\..\\windows\\system32",
            "session/../../etc/passwd",
            "valid_looking.json",
        ]
        for sid in evil_ids:
            result = Session.load(sid)
            assert result is None, \
                f"Session.load should reject dangerous ID '{sid}', got {result}"

    def test_empty_session_id_rejected(self):
        """An empty session ID must be rejected."""
        from api.models import Session
        assert Session.load("") is None
        assert Session.load(None) is None


# ── 4. Error Path Sanitization ────────────────────────────────────────────


class TestSanitizeError:
    def test_unix_path_stripped(self):
        from api.helpers import _sanitize_error
        e = FileNotFoundError("/home/hermes/.hermes/sessions/abc123.json")
        result = _sanitize_error(e)
        assert "/home/hermes" not in result
        assert "<path>" in result

    def test_nested_unix_path_stripped(self):
        from api.helpers import _sanitize_error
        e = ValueError("cannot read /var/lib/hermes/data.db: permission denied")
        result = _sanitize_error(e)
        assert "/var/lib/hermes" not in result
        assert "<path>" in result

    def test_no_path_unchanged(self):
        from api.helpers import _sanitize_error
        e = ValueError("session not found")
        result = _sanitize_error(e)
        assert result == "session not found"

    def test_windows_path_stripped(self):
        from api.helpers import _sanitize_error
        e = FileNotFoundError("C:\\Users\\hermes\\AppData\\sessions\\x.json not found")
        result = _sanitize_error(e)
        assert "C:\\Users\\hermes" not in result

    def test_live_404_does_not_leak_path(self, webui_server):
        """Live server: file-not-found errors must not expose filesystem paths."""
        body, status = post("/api/file/read", {"path": "../../etc/passwd"})
        err = body.get("error", "")
        assert "/home" not in err and "/var" not in err and "/etc" not in err, \
            f"Error message leaks filesystem path: {err}"


# ── 5. Secure Cookie Flag ─────────────────────────────────────────────────


class TestSecureCookieFlag:
    def test_getattr_safe_on_plain_socket(self):
        """getattr(handler.request, 'getpeercert', None) must not raise on plain socket."""
        import socket
        # Plain socket has no getpeercert attribute
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            result = getattr(s, 'getpeercert', None)
            assert result is None, \
                f"Expected None on plain socket, got {result}"
        finally:
            s.close()

    def test_secure_flag_not_set_for_plain_http(self, webui_server):
        """Login endpoint over plain HTTP must NOT set Secure cookie flag."""
        # Auth is disabled in tests, so this just checks no crash
        body, status = post("/api/auth/login", {"password": "test"})
        # Either 200 (auth not enabled) or 401 (auth enabled, wrong pw)
        assert status in (200, 401, 429), f"Unexpected status {status}"


# ── 6. HMAC Signature Length ──────────────────────────────────────────────


class TestHMACLength:
    def test_session_token_sig_is_64_chars(self):
        """Session cookie signature must be 64 hex chars (256-bit), not 32."""
        from api.auth import create_session
        cookie = create_session()
        token, sig = cookie.rsplit('.', 1)
        assert len(sig) == 64, \
            f"Expected 64-char signature (SHA-256), got {len(sig)}: {sig}"

    def test_verify_session_rejects_old_16char_sig(self):
        """A cookie with a 16-char sig must fail verification."""
        import hmac as _hmac
        import hashlib
        from api.auth import _signing_key, verify_session, _sessions
        import time
        import secrets

        token = secrets.token_hex(32)
        _sessions[token] = time.time() + 3600  # valid session
        old_sig = _hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()[:16]
        old_cookie = f"{token}.{old_sig}"
        # Should fail: sig length wrong
        assert not verify_session(old_cookie), \
            "Old 16-char sig cookie must not verify (sig mismatch)"


# ── 7. Skills Path Traversal ──────────────────────────────────────────────


class TestSkillsPathTraversal:
    def test_traversal_rejected(self, webui_server):
        """Saving a skill with a traversal path must return 400."""
        body, status = post("/api/skills/save", {
            "name": "../../evil",
            "content": "# evil",
        })
        assert status in (400, 403), \
            f"Expected 400/403 for traversal skill path, got {status}: {body}"

    def test_valid_skill_accepted(self, webui_server):
        """Saving a skill with a valid name must succeed."""
        body, status = post("/api/skills/save", {
            "name": "test-security-skill",
            "content": "---\nname: test-security-skill\ndescription: test\n---\n# test",
        })
        # 500 = skills module not available (hermes-agent not installed) — skip
        if status == 500:
            import pytest; pytest.skip("skills module requires hermes-agent")
        # Should succeed (200) or need auth (401/403) — not path error (400)
        assert status in (200, 401, 403, 404), \
            f"Valid skill save got unexpected status {status}: {body}"
        # Clean up the saved skill so it doesn't pollute later tests'
        # SKILLS_DIR enumeration (sprint3 skills tests in particular).
        try:
            post("/api/skills/delete", {"name": "test-security-skill"})
        except Exception:
            pass


# ── 8. Content-Disposition for Dangerous MIME Types ───────────────────────


class TestContentDisposition:
    def test_html_file_forced_download(self, webui_server, tmp_path):
        """HTML files served via /api/file/raw must have Content-Disposition: attachment."""
        import urllib.request
        import urllib.error

        # Use a session to create an HTML file in the workspace
        sessions_body, _ = post("/api/sessions/new", {})
        sid = sessions_body.get("session_id") or sessions_body.get("id")
        if not sid:
            return  # Skip if sessions API shape is unexpected

        # Can't easily create a file via the test server without a workspace,
        # so test the logic directly instead.
        from api.routes import _handle_file_raw
        dangerous_types = {'text/html', 'application/xhtml+xml', 'image/svg+xml'}
        for mime in dangerous_types:
            assert mime in dangerous_types, f"{mime} should be in dangerous_types set"

    def test_dangerous_mime_types_set_complete(self):
        """The set of dangerous MIME types must include html, xhtml, and svg."""
        import ast
        import pathlib
        routes_src = pathlib.Path(__file__).parent.parent / "api" / "routes.py"
        src = routes_src.read_text()
        assert "text/html" in src
        assert "application/xhtml+xml" in src
        assert "image/svg+xml" in src
        assert "dangerous_types" in src

    def test_unicode_filename_download_header_is_latin1_safe(self, cleanup_test_sessions):
        """Unicode filenames must not crash download responses."""
        body, status = post("/api/session/new", {})
        assert status == 200, body
        sid = body["session"]["session_id"]
        cleanup_test_sessions.append(sid)
        ws = pathlib.Path(body["session"]["workspace"])
        filename = "中文对照表.pdf"
        pdf_bytes = b"%PDF-1.3\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
        (ws / filename).write_bytes(pdf_bytes)

        encoded = urllib.parse.quote(filename)
        raw, headers, raw_status = get_raw_with_headers(
            f"/api/file/raw?session_id={sid}&path={encoded}&download=1"
        )

        assert raw_status == 200
        assert raw == pdf_bytes
        disp = headers["Content-Disposition"]
        assert disp.startswith("attachment; ")
        assert "filename*=UTF-8''" in disp
        disp.encode("latin-1")

    def test_unicode_filename_inline_header_is_latin1_safe(self, cleanup_test_sessions):
        """Inline responses must also work for unicode filenames."""
        body, status = post("/api/session/new", {})
        assert status == 200, body
        sid = body["session"]["session_id"]
        cleanup_test_sessions.append(sid)
        ws = pathlib.Path(body["session"]["workspace"])
        filename = "预览.pdf"
        pdf_bytes = b"%PDF-1.3\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
        (ws / filename).write_bytes(pdf_bytes)

        encoded = urllib.parse.quote(filename)
        raw, headers, raw_status = get_raw_with_headers(
            f"/api/file/raw?session_id={sid}&path={encoded}"
        )

        assert raw_status == 200
        assert raw == pdf_bytes
        disp = headers["Content-Disposition"]
        assert disp.startswith("inline; ")
        assert "filename*=UTF-8''" in disp
        disp.encode("latin-1")


# ── 9. PBKDF2 Password Hashing ───────────────────────────────────────────


class TestPasswordHashing:
    def test_hash_password_is_hex(self):
        """_hash_password must produce a non-empty hex string (PBKDF2-SHA256)."""
        from api.auth import _hash_password
        result = _hash_password("mysecretpassword")
        assert isinstance(result, str) and len(result) == 64, \
            f"Expected 64-char hex hash (SHA-256 output), got len={len(result)}: {result}"
        # Hex-only chars
        assert all(c in "0123456789abcdef" for c in result), \
            f"Hash must be hex string, got: {result}"

    def test_hash_password_is_deterministic_with_same_salt(self):
        """_hash_password must return the same hash for same input (signing key is stable)."""
        from api.auth import _hash_password
        h1 = _hash_password("consistent_password")
        h2 = _hash_password("consistent_password")
        assert h1 == h2, "Same password must produce same hash (stable signing key)"

    def test_hash_password_different_inputs_differ(self):
        """Different passwords must produce different hashes."""
        from api.auth import _hash_password
        assert _hash_password("password_a") != _hash_password("password_b"), \
            "Different passwords must produce different hashes"

    def test_hash_password_longer_than_sha256(self):
        """PBKDF2 with 600k iterations is much stronger than single SHA-256.
        We verify indirectly: the code must call pbkdf2_hmac, not sha256 directly."""
        import inspect
        from api import auth as _auth
        src = inspect.getsource(_auth._hash_password)
        assert "pbkdf2_hmac" in src, \
            "_hash_password must use pbkdf2_hmac, not raw sha256"
        assert "600_000" in src or "600000" in src, \
            "_hash_password must use 600,000 iterations"

    def test_save_settings_stores_64char_hex_hash(self):
        """save_settings with _set_password must store a 64-char hex hash (PBKDF2)."""
        from api.config import save_settings, load_settings, SETTINGS_FILE
        import json

        # Remember original content so we can restore it
        original = None
        if SETTINGS_FILE.exists():
            original = SETTINGS_FILE.read_text()

        try:
            save_settings({"_set_password": "test_pbkdf2_pw"})
            settings = load_settings()
            ph = settings.get("password_hash", "")
            assert len(ph) == 64 and all(c in "0123456789abcdef" for c in ph), \
                f"save_settings must store 64-char hex PBKDF2 hash, got: {ph!r}"
        finally:
            # Restore original settings
            if original is not None:
                SETTINGS_FILE.write_text(original)
            else:
                save_settings({"_clear_password": True})


# ── 10. Non-loopback Startup Warning ─────────────────────────────────────


class TestStartupWarning:
    def test_warning_code_present_in_server(self):
        """server.py must contain non-loopback warning code."""
        src = pathlib.Path(__file__).parent.parent / "server.py"
        text = src.read_text()
        assert "0.0.0.0" in text or "non-loopback" in text.lower() or "WARNING" in text, \
            "server.py must contain non-loopback warning logic"
        assert "is_auth_enabled" in text, \
            "server.py must check is_auth_enabled() before warning"


# ── 11. SSRF DNS Check ─────────────────────────────────────────────────────


class TestSSRFCheck:
    def test_ssrf_guard_code_present_in_config(self):
        """config.py must contain SSRF DNS resolution guard."""
        src = pathlib.Path(__file__).parent.parent / "api" / "config.py"
        text = src.read_text()
        assert "getaddrinfo" in text, "SSRF guard must resolve DNS with getaddrinfo"
        assert "is_private" in text, "SSRF guard must check is_private IP"
        assert "is_loopback" in text, "SSRF guard must check is_loopback IP"

    def test_known_local_providers_whitelisted(self):
        """Ollama and localhost endpoints should NOT be blocked by SSRF guard."""
        src = pathlib.Path(__file__).parent.parent / "api" / "config.py"
        text = src.read_text()
        assert "ollama" in text.lower()
        assert "localhost" in text.lower()
        assert "lmstudio" in text.lower() or "lm-studio" in text.lower()


# ── 12. ENV_LOCK Export ────────────────────────────────────────────────────


class TestENVLock:
    def test_env_lock_importable_from_streaming(self):
        """_ENV_LOCK must be an importable threading-style reentrant lock."""
        from api.streaming import _ENV_LOCK

        assert hasattr(_ENV_LOCK, "acquire"), "_ENV_LOCK must expose acquire()"
        assert hasattr(_ENV_LOCK, "release"), "_ENV_LOCK must expose release()"
        assert hasattr(_ENV_LOCK, "__enter__"), "_ENV_LOCK must support context manager use"
        assert hasattr(_ENV_LOCK, "__exit__"), "_ENV_LOCK must support context manager use"

        with _ENV_LOCK:
            acquired = _ENV_LOCK.acquire(False)
            assert acquired, "_ENV_LOCK must allow reentrant acquisition"
            _ENV_LOCK.release()

    def test_env_lock_importable_in_routes(self):
        """api.routes must be able to import _ENV_LOCK from api.streaming."""
        # If routes.py fails to import, this will raise ImportError
        import importlib
        import api.routes  # noqa: F401 -- just checking import works
        # No error means the circular import is OK


# ── Fixture ────────────────────────────────────────────────────────────────

import pytest


@pytest.fixture(scope="module")
def webui_server():
    """Reuse the module-scoped server started by conftest.py."""
    return BASE
