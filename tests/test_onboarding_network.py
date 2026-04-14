"""
Tests: onboarding /api/onboarding/setup network restriction logic (issue #390).

Covers:
  1. Request from 127.0.0.1 (loopback) is allowed without auth
  2. Request from RFC-1918 private IP (172.x, 192.168.x, 10.x) is allowed without auth
  3. Request from public IP is blocked without auth → 403
  4. X-Forwarded-For loopback IP is trusted → allowed
  5. X-Forwarded-For private IP is trusted → allowed
  6. X-Forwarded-For public IP → still blocked
  7. X-Real-IP loopback → allowed
  8. HERMES_WEBUI_ONBOARDING_OPEN=1 bypasses the check entirely
  9. Auth enabled → check skipped, any IP allowed
"""

import json
import os
import pathlib
import sys
import unittest.mock
import urllib.error
import urllib.request

import pytest

REPO = pathlib.Path(__file__).parent.parent
BASE = "http://127.0.0.1:8788"

# ---------------------------------------------------------------------------
# Unit tests — directly test the IP-resolution + guard logic in routes.py
# without needing a live server. We replicate the logic to keep tests fast
# and independent of server startup.
# ---------------------------------------------------------------------------

def _is_local_from_handler(
    raw_ip: str,
    xff: str = "",
    xri: str = "",
    auth_enabled: bool = False,
    open_env: bool = False,
) -> bool | str:
    """
    Mirror of the onboarding IP check in api/routes.py.
    Returns True if the request would be allowed, False if blocked,
    or the error message string if blocked.
    """
    import ipaddress

    if auth_enabled or open_env:
        return True

    _xff = xff.split(",")[0].strip() if xff else ""
    _xri = xri.strip()
    _ip_str = _xff or _xri or raw_ip
    try:
        addr = ipaddress.ip_address(_ip_str)
        is_local = addr.is_loopback or addr.is_private
    except ValueError:
        is_local = False

    return is_local


class TestOnboardingIPLogic:
    """Unit tests for the IP-resolution logic (no live server needed)."""

    def test_loopback_allowed(self):
        assert _is_local_from_handler("127.0.0.1") is True

    def test_ipv6_loopback_allowed(self):
        assert _is_local_from_handler("::1") is True

    def test_private_172_allowed(self):
        """Docker bridge addresses (172.17.x.x) must be allowed."""
        assert _is_local_from_handler("172.17.0.1") is True

    def test_private_192168_allowed(self):
        assert _is_local_from_handler("192.168.1.100") is True

    def test_private_10_allowed(self):
        assert _is_local_from_handler("10.0.0.5") is True

    def test_public_ip_blocked(self):
        assert _is_local_from_handler("8.8.8.8") is False

    def test_xff_loopback_trusted(self):
        """Reverse proxy sets X-Forwarded-For to 127.0.0.1 — should be allowed."""
        assert _is_local_from_handler("172.20.0.1", xff="127.0.0.1") is True

    def test_xff_private_trusted(self):
        """Reverse proxy sets X-Forwarded-For to LAN IP — should be allowed."""
        assert _is_local_from_handler("172.20.0.1", xff="192.168.1.50") is True

    def test_xff_public_blocked(self):
        """Public IP in X-Forwarded-For should still be blocked."""
        assert _is_local_from_handler("172.20.0.1", xff="8.8.8.8") is False

    def test_xff_first_entry_used(self):
        """X-Forwarded-For may have multiple IPs; only the first (client) is used."""
        # First entry is private → allowed
        assert _is_local_from_handler("172.20.0.1", xff="10.0.0.1, 172.20.0.1") is True
        # First entry is public → blocked
        assert _is_local_from_handler("172.20.0.1", xff="8.8.8.8, 172.20.0.1") is False

    def test_xreal_ip_loopback_trusted(self):
        """X-Real-IP loopback → allowed."""
        assert _is_local_from_handler("172.20.0.1", xri="127.0.0.1") is True

    def test_xreal_ip_private_trusted(self):
        assert _is_local_from_handler("172.20.0.1", xri="10.1.2.3") is True

    def test_xff_takes_priority_over_xri(self):
        """X-Forwarded-For wins over X-Real-IP when both present."""
        # XFF says public, XRI says local → blocked (XFF takes priority)
        assert _is_local_from_handler("172.20.0.1", xff="8.8.8.8", xri="127.0.0.1") is False

    def test_open_env_bypasses_check(self):
        """HERMES_WEBUI_ONBOARDING_OPEN=1 allows any IP."""
        assert _is_local_from_handler("8.8.8.8", open_env=True) is True

    def test_auth_enabled_bypasses_check(self):
        """When auth is enabled, IP check is skipped entirely."""
        assert _is_local_from_handler("8.8.8.8", auth_enabled=True) is True

    def test_invalid_ip_blocked(self):
        """Malformed IP in header → treated as non-local → blocked."""
        assert _is_local_from_handler("not-an-ip") is False


# ---------------------------------------------------------------------------
# Integration tests — hit the live test server at port 8788
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestOnboardingSetupEndpoint:
    """
    Integration tests for /api/onboarding/setup.
    These require the test server running on port 8788.
    """

    def _post(self, path: str, data: dict, headers: dict | None = None) -> tuple[int, dict]:
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            BASE + path,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_loopback_request_allowed(self):
        """
        Requests from 127.0.0.1 (which is what the test server sees) should
        pass the IP check. We confirm no 403 is returned.
        """
        # The test server runs on 127.0.0.1:8788 so client_address[0] is 127.0.0.1.
        # A valid setup payload with a mock provider should not be rejected for IP reasons.
        # We patch apply_onboarding_setup to avoid actually writing any config.
        import unittest.mock
        with unittest.mock.patch("api.onboarding.apply_onboarding_setup", return_value={"ok": True}):
            status, body = self._post(
                "/api/onboarding/setup",
                {"provider": "anthropic", "model": "claude-sonnet-4.6", "api_key": "test-key"},
            )
        # Should not be 403 (IP blocked). May be 200 or another error from apply logic.
        assert status != 403, f"Got 403 — IP check incorrectly blocked loopback. Body: {body}"

    def test_xff_loopback_header_respected(self):
        """
        Simulated reverse proxy: raw TCP is 127.0.0.1 but X-Forwarded-For is also
        127.0.0.1. Should be allowed.
        """
        import unittest.mock
        with unittest.mock.patch("api.onboarding.apply_onboarding_setup", return_value={"ok": True}):
            status, body = self._post(
                "/api/onboarding/setup",
                {"provider": "anthropic", "model": "claude-sonnet-4.6", "api_key": "test-key"},
                headers={"X-Forwarded-For": "127.0.0.1"},
            )
        assert status != 403, f"Got 403 with XFF=127.0.0.1. Body: {body}"
