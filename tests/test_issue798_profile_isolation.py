"""
Issue #798: Profile isolation — per-client profile selection.

Tests that switching profiles on one HTTP client does NOT affect
a different HTTP client. Uses separate cookie jars to simulate
two independent browser sessions.

Fix: hermes_profile cookie + thread-local context per request.
"""
import http.cookiejar
import json
import pathlib
import urllib.request
import urllib.error
import urllib.parse

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent


# ── Cookie-aware HTTP helpers ───────────────────────────────────────────────

def _make_opener(cookie_jar):
    """Build a urllib opener that sends/receives cookies from *cookie_jar*."""
    handler = urllib.request.HTTPCookieProcessor(cookie_jar)
    return urllib.request.build_opener(handler)


def _get(opener, base, path):
    """GET *path* on *base* using *opener*. Returns (response_dict, headers_dict).
    If *opener* is None, uses plain urllib (no cookies)."""
    req = urllib.request.Request(base + path)
    try:
        if opener is not None:
            resp = opener.open(req, timeout=10)
        else:
            resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read())
        return body, dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        return body, dict(e.headers)


def _post(opener, base, path, body_dict=None):
    """POST *body_dict* to *path* on *base* using *opener*.
    Returns (response_dict, headers_dict).
    If *opener* is None, uses plain urllib (no cookies)."""
    data = json.dumps(body_dict or {}).encode()
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        if opener is not None:
            resp = opener.open(req, timeout=10)
        else:
            resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read())
        return body, dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        return body, dict(e.headers)


def _parse_set_cookies(headers):
    """Extract cookie name=value pairs from Set-Cookie headers."""
    cookies = {}
    for key, val in headers.items():
        if key.lower() == 'set-cookie':
            # May have multiple Set-Cookie headers; urllib collapses them with commas
            # Split on ', ' but be careful: some cookie values contain commas
            # Simple approach: find the cookie name=value before any ';'
            for part in val.split('Set-Cookie: '):
                part = part.strip()
                if not part:
                    continue
                if '=' in part:
                    name, rest = part.split('=', 1)
                    name = name.strip()
                    # Value is everything before the first ';' 
                    value = rest.split(';')[0].strip().strip('"')
                    cookies[name] = value
    return cookies


# ── Profile fixture ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _setup_test_profiles(base_url):
    """Create test profiles p1 and p2 for isolation tests."""
    # Create profile p1
    _post(None, base_url, "/api/profile/create", {
        "name": "p1",
        "clone_from": "default",
        "clone_config": True,
    })
    # Create profile p2
    _post(None, base_url, "/api/profile/create", {
        "name": "p2",
        "clone_from": "default",
        "clone_config": True,
    })
    yield
    # Cleanup: switch back to default and delete test profiles
    try:
        _post(None, base_url, "/api/profile/switch", {"name": "default"})
    except Exception:
        pass
    try:
        _post(None, base_url, "/api/profile/delete", {"name": "p1"})
    except Exception:
        pass
    try:
        _post(None, base_url, "/api/profile/delete", {"name": "p2"})
    except Exception:
        pass


# ── Tests ───────────────────────────────────────────────────────────────────

class TestProfileIsolation:
    """Verify that profile selection is isolated per-client (per-cookie-jar)."""

    def test_default_profile_without_cookie(self, base_url):
        """A fresh client with no cookies sees the process-level default."""
        jar = http.cookiejar.CookieJar()
        opener = _make_opener(jar)
        body, _ = _get(opener, base_url, "/api/profile/active")
        # Process-level default is 'default' in the test server
        assert body.get("name") == "default", (
            f"Expected 'default' but got {body.get('name')!r}"
        )

    def test_profile_switch_sets_cookie(self, base_url):
        """POST /api/profile/switch should set hermes_profile cookie."""
        jar = http.cookiejar.CookieJar()
        opener = _make_opener(jar)
        _, headers = _post(opener, base_url, "/api/profile/switch", {"name": "p1"})
        cookies = _parse_set_cookies(headers)
        assert "hermes_profile" in cookies, (
            f"Expected hermes_profile cookie in Set-Cookie, got: {list(cookies.keys())}"
        )
        assert cookies["hermes_profile"] == "p1"

    def test_profile_cookie_persists_across_requests(self, base_url):
        """After switching profile, subsequent GETs reflect the client's profile."""
        jar = http.cookiejar.CookieJar()
        opener = _make_opener(jar)

        # Switch to p1
        _post(opener, base_url, "/api/profile/switch", {"name": "p1"})

        # Next request should see p1 as active
        body, _ = _get(opener, base_url, "/api/profile/active")
        assert body.get("name") == "p1", (
            f"After switching to p1, expected 'p1' but got {body.get('name')!r}"
        )

    def test_separate_clients_isolated(self, base_url):
        """Two clients with separate cookie jars must have independent profiles.

        This is the core bug #798 reproduction.
        """
        # Client A: fresh cookie jar
        jar_a = http.cookiejar.CookieJar()
        opener_a = _make_opener(jar_a)

        # Client B: fresh cookie jar
        jar_b = http.cookiejar.CookieJar()
        opener_b = _make_opener(jar_b)

        # Both start on default
        body_a, _ = _get(opener_a, base_url, "/api/profile/active")
        body_b, _ = _get(opener_b, base_url, "/api/profile/active")
        assert body_a.get("name") == "default"
        assert body_b.get("name") == "default"

        # Client A switches to p1
        _post(opener_a, base_url, "/api/profile/switch", {"name": "p1"})

        # Client A should see p1
        body_a, _ = _get(opener_a, base_url, "/api/profile/active")
        assert body_a.get("name") == "p1"

        # Client B should STILL see default (BUG #798: before fix, B sees "p1")
        body_b, _ = _get(opener_b, base_url, "/api/profile/active")
        assert body_b.get("name") == "default", (
            f"Client B should see 'default' but got {body_b.get('name')!r} — "
            f"profile isolation broken!"
        )

        # Client B switches to p2
        _post(opener_b, base_url, "/api/profile/switch", {"name": "p2"})

        # Client A should STILL see p1
        body_a, _ = _get(opener_a, base_url, "/api/profile/active")
        assert body_a.get("name") == "p1", (
            f"Client A should still see 'p1' but got {body_a.get('name')!r} — "
            f"profile isolation broken!"
        )

        # Client B should see p2
        body_b, _ = _get(opener_b, base_url, "/api/profile/active")
        assert body_b.get("name") == "p2"

    def test_profile_switch_to_default_clears_cookie(self, base_url):
        """Switching to 'default' should clear or reset the profile cookie."""
        jar = http.cookiejar.CookieJar()
        opener = _make_opener(jar)

        # Switch to p1
        _post(opener, base_url, "/api/profile/switch", {"name": "p1"})

        # Switch back to default
        _, headers = _post(opener, base_url, "/api/profile/switch", {"name": "default"})
        cookies = _parse_set_cookies(headers)

        # The cookie should be set to empty string or removed
        if "hermes_profile" in cookies:
            assert cookies["hermes_profile"] == "", (
                f"Expected empty hermes_profile cookie on default switch, "
                f"got {cookies['hermes_profile']!r}"
            )

        # Subsequent request should show default
        body, _ = _get(opener, base_url, "/api/profile/active")
        assert body.get("name") == "default"

    def test_profiles_list_reflects_client_profile(self, base_url):
        """/api/profiles should show is_active based on the client's cookie."""
        jar = http.cookiejar.CookieJar()
        opener = _make_opener(jar)

        # Switch to p1
        _post(opener, base_url, "/api/profile/switch", {"name": "p1"})

        body, _ = _get(opener, base_url, "/api/profiles")
        profiles = body.get("profiles", [])
        active_names = [p["name"] for p in profiles if p.get("is_active")]
        assert active_names == ["p1"], (
            f"Expected only p1 active, got: {active_names}"
        )
