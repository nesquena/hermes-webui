"""Tests for #1105 — SSRF check allows user-configured custom_providers hostnames.

The SSRF check blocks requests to private IPs unless the hostname is in a
hardcoded allowlist. This fix extracts hostnames from custom_providers config
and adds them to the trusted set, so user-explicitly configured local endpoints
(ollama, llama.cpp, vLLM, TabbyAPI, etc.) are not blocked.
"""
import os
import pytest


# ---------- Source-code analysis tests ----------

def test_ssrf_trusted_hosts_variable_exists():
    """The _ssrf_trusted_hosts set must be built from custom_providers config."""
    with open("api/config.py") as f:
        src = f.read()
    assert "_ssrf_trusted_hosts" in src
    assert "_ssrf_trusted_hosts: set[str] = set()" in src


def test_ssrf_trusted_hosts_populated_from_custom_providers():
    """Trusted hosts are extracted by iterating custom_providers[].base_url."""
    with open("api/config.py") as f:
        src = f.read()
    # Must read custom_providers from cfg
    assert 'cfg.get("custom_providers"' in src
    # Must extract base_url from each entry
    assert '_cp.get("base_url")' in src
    # Must parse hostname with urlparse
    assert "_cp_parsed.hostname" in src
    # Must add to trusted set
    assert "_ssrf_trusted_hosts.add" in src


def test_ssrf_check_uses_trusted_hosts():
    """The SSRF check must consult _ssrf_trusted_hosts before blocking."""
    with open("api/config.py") as f:
        src = f.read()
    # The is_known_local check must include _ssrf_trusted_hosts
    assert "in _ssrf_trusted_hosts" in src


def test_ssrf_known_local_still_present():
    """Original hardcoded allowlist must still be present (no regression)."""
    with open("api/config.py") as f:
        src = f.read()
    for keyword in ("ollama", "localhost", "127.0.0.1", "lmstudio", "lm-studio"):
        assert keyword in src, f"Missing hardcoded allowlist entry: {keyword}"


def test_ssrf_block_still_present():
    """SSRF ValueError must still be raised for unknown private IPs."""
    with open("api/config.py") as f:
        src = f.read()
    assert 'SSRF: resolved hostname to private IP' in src


# ---------- Functional tests (mocked socket) ----------

def test_custom_provider_hostname_added_to_trusted():
    """A hostname from custom_providers base_url is added to trusted set."""
    import api.config as config
    import socket
    from unittest.mock import patch, MagicMock

    old_cfg = dict(config.cfg)
    try:
        config.cfg.update({
            "model": {"model": "my-model", "base_url": "http://my-llama-server:8080/v1"},
            "custom_providers": [
                {"name": "my-llama", "base_url": "http://my-llama-server:8080/v1", "model": "llama-3"}
            ],
            "providers": {},
        })
        config.invalidate_models_cache()

        # Mock socket.getaddrinfo to return a private IP for my-llama-server
        private_addr = ("192.168.1.100", None)
        mock_getaddrinfo = MagicMock(return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", private_addr)
        ])

        # Mock urllib to prevent actual HTTP call
        mock_urlopen = MagicMock()
        mock_urlopen.read.return_value = b'{"data": [{"id": "test-model"}]}'
        mock_urlopen.__enter__ = MagicMock(return_value=mock_urlopen)
        mock_urlopen.__exit__ = MagicMock(return_value=False)

        with patch("socket.getaddrinfo", mock_getaddrinfo), \
             patch("urllib.request.urlopen", mock_urlopen):
            # Should NOT raise ValueError (SSRF) because hostname is in trusted set
            result = config.get_available_models()

        # Verify models were returned (auto-detection succeeded)
        assert result is not None
        assert "groups" in result

    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config.invalidate_models_cache()


def test_unknown_private_ip_still_blocked():
    """A private IP from a hostname NOT in custom_providers is still blocked.

    The SSRF ValueError is caught by the broad `except Exception` around
    the custom endpoint fetch (line ~1571), so get_available_models() doesn't
    crash — but no models are auto-detected from that endpoint.
    """
    import api.config as config
    import socket
    from unittest.mock import patch, MagicMock

    old_cfg = dict(config.cfg)
    try:
        config.cfg.update({
            "model": {"model": "test", "base_url": "http://unknown-local-server:9999/v1"},
            "custom_providers": [
                {"name": "other", "base_url": "http://other-server:8080/v1", "model": "x"}
            ],
            "providers": {},
        })
        config.invalidate_models_cache()

        # Mock socket.getaddrinfo to return a private IP for unknown-local-server
        private_addr = ("10.0.0.50", None)
        mock_getaddrinfo = MagicMock(return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", private_addr)
        ])

        with patch("socket.getaddrinfo", mock_getaddrinfo):
            # Should NOT crash (ValueError is caught internally)
            result = config.get_available_models()

        # But no models should be auto-detected from the blocked endpoint
        assert result is not None
        assert "groups" in result
        # Verify no group with "unknown-local-server" models exists
        for group in result["groups"]:
            provider_name = group.get("provider", "")
            assert "unknown-local-server" not in provider_name

    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config.invalidate_models_cache()
