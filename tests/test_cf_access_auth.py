"""
Tests for Cloudflare Access JWT authentication in hermes-webui.
"""

import json
import os
import time
import threading
import urllib.parse
import importlib
import sys
import builtins
import http.cookies
from unittest.mock import patch, MagicMock

import pytest

from api.auth_cf_access import (
    _validate_team_domain,
    _canonical_team_domain,
    is_cf_access_configured,
    is_cf_access_ready,
    validate_cf_access_token,
    get_cf_access_identity,
    _get_public_keys,
    _safe_cached_keys,
    _CF_KEYS_CACHE,
    _CF_KEYS_FETCHED_AT,
    _CF_KEYS_INFLIGHT,
    _CF_KEYS_GENERATION,
    _CF_KEYS_MAX_STALE,
    _CF_LOCK,
)

_CF_MAPS = [("cache", _CF_KEYS_CACHE), ("ts", _CF_KEYS_FETCHED_AT),
            ("inflight", _CF_KEYS_INFLIGHT), ("gen", _CF_KEYS_GENERATION)]


# ── Cache domain guard ───────────────────────────────────────────────────────

class _CacheDomainGuard:
    """Per-domain snapshot/restore under _CF_LOCK with (present, value) semantics."""

    def _cf_snapshot(self, domains):
        snap = {}
        with _CF_LOCK:
            for d in domains:
                snap[d] = {}
                for name, m in _CF_MAPS:
                    snap[d][name] = (d in m, m.get(d))
                for _, m in _CF_MAPS:
                    m.pop(d, None)
        self._cf_snap = snap

    def _cf_restore(self):
        with _CF_LOCK:
            for d, s in self._cf_snap.items():
                for name, m in _CF_MAPS:
                    present, value = s[name]
                    if present:
                        m[d] = value
                    else:
                        m.pop(d, None)


# ── Team-domain validation ───────────────────────────────────────────────────

class TestTeamDomainValidation:
    def test_valid_domain(self):
        assert _validate_team_domain("https://myteam.cloudflareaccess.com") == "https://myteam.cloudflareaccess.com"

    def test_strips_trailing_slash(self):
        assert _validate_team_domain("https://myteam.cloudflareaccess.com/") == "https://myteam.cloudflareaccess.com"

    def test_rejects_http(self):
        assert _validate_team_domain("http://myteam.cloudflareaccess.com") is None

    def test_rejects_non_cloudflare_host(self):
        assert _validate_team_domain("https://evil.example.com") is None

    def test_rejects_empty(self):
        assert _validate_team_domain("") is None

    def test_rejects_credentials_in_url(self):
        assert _validate_team_domain("https://user:pass@myteam.cloudflareaccess.com") is None

    def test_rejects_query_string(self):
        assert _validate_team_domain("https://myteam.cloudflareaccess.com?x=1") is None

    def test_rejects_fragment(self):
        assert _validate_team_domain("https://myteam.cloudflareaccess.com#frag") is None

    def test_rejects_non_root_path(self):
        assert _validate_team_domain("https://myteam.cloudflareaccess.com/path") is None

    def test_rejects_non_default_port(self):
        assert _validate_team_domain("https://myteam.cloudflareaccess.com:8443") is None

    def test_rejects_malformed_port(self):
        assert _validate_team_domain("https://myteam.cloudflareaccess.com:abc") is None

    def test_rejects_out_of_range_port(self):
        assert _validate_team_domain("https://myteam.cloudflareaccess.com:99999") is None

    def test_rejects_empty_label(self):
        assert _validate_team_domain("https://.cloudflareaccess.com") is None

    def test_rejects_double_dot(self):
        assert _validate_team_domain("https://myteam..cloudflareaccess.com") is None

    def test_rejects_oversized_label(self):
        long_label = "a" * 64
        assert _validate_team_domain("https://" + long_label + ".cloudflareaccess.com") is None

    def test_accepts_max_length_label(self):
        label = "a" * 63
        url = "https://" + label + ".cloudflareaccess.com"
        assert _validate_team_domain(url) == url

    def test_rejects_underscore_in_label(self):
        assert _validate_team_domain("https://my_team.cloudflareaccess.com") is None

    def test_rejects_percent_encoding(self):
        assert _validate_team_domain("https://my%2ateam.cloudflareaccess.com") is None

    def test_rejects_subdomain_label(self):
        assert _validate_team_domain("https://sub.myteam.cloudflareaccess.com") is None

    def test_accepts_hyphenated_label(self):
        url = "https://my-team.cloudflareaccess.com"
        assert _validate_team_domain(url) == url

    def test_rejects_leading_hyphen(self):
        assert _validate_team_domain("https://-myteam.cloudflareaccess.com") is None

    def test_rejects_trailing_hyphen(self):
        assert _validate_team_domain("https://myteam-.cloudflareaccess.com") is None


class TestCanonicalTeamDomain:
    def test_canonical_returns_validated_url(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        assert _canonical_team_domain() == "https://myteam.cloudflareaccess.com"

    def test_canonical_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com/")
        assert _canonical_team_domain() == "https://myteam.cloudflareaccess.com"

    def test_canonical_returns_none_for_invalid(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://evil.example.com")
        assert _canonical_team_domain() is None

    def test_canonical_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", raising=False)
        assert _canonical_team_domain() is None


class TestConfiguredVsReady:
    def test_configured_when_team_domain_set(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_AUD", raising=False)
        assert is_cf_access_configured()
        assert not is_cf_access_ready()

    def test_configured_when_aud_only_set(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", raising=False)
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        assert is_cf_access_configured()
        assert not is_cf_access_ready()

    def test_not_configured_when_both_unset(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", raising=False)
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_AUD", raising=False)
        assert not is_cf_access_configured()

    def test_ready_when_all_set_with_pyjwt(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        with patch("api.auth_cf_access._is_pyjwt_available", return_value=True):
            assert is_cf_access_ready()

    def test_not_ready_when_pyjwt_unavailable(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        with patch("api.auth_cf_access._is_pyjwt_available", return_value=False):
            assert not is_cf_access_ready()

    def test_not_ready_when_domain_invalid(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://evil.example.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        with patch("api.auth_cf_access._is_pyjwt_available", return_value=True):
            assert not is_cf_access_ready()


class TestEmailAllowlist:
    def test_parses_comma_separated(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_EMAILS", "a@x.com,b@x.com")
        from api.auth_cf_access import _allowed_emails
        assert _allowed_emails() == {"a@x.com", "b@x.com"}

    def test_empty_returns_none(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_EMAILS", raising=False)
        from api.auth_cf_access import _allowed_emails
        assert _allowed_emails() is None

    def test_lowercases(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_EMAILS", "A@X.COM")
        from api.auth_cf_access import _allowed_emails
        assert _allowed_emails() == {"a@x.com"}


class TestTokenValidation:
    def test_rejects_empty_token(self):
        assert validate_cf_access_token("") is None

    def test_rejects_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", raising=False)
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_AUD", raising=False)
        assert validate_cf_access_token("some-token") is None

    def test_rejects_when_aud_missing(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_AUD", raising=False)
        assert validate_cf_access_token("some-token") is None


# ── JWKS cache lifecycle ─────────────────────────────────────────────────────

class TestJWKSCache(_CacheDomainGuard):
    _synthetic_domains = [
        "https://test.cloudflareaccess.com",
        "https://test2.cloudflareaccess.com",
        "https://test3.cloudflareaccess.com",
        "https://test4.cloudflareaccess.com",
        "https://test5.cloudflareaccess.com",
    ]

    def setup_method(self):
        self._cf_snapshot(self._synthetic_domains)

    def teardown_method(self):
        self._cf_restore()

    def test_stale_cache_discarded_after_max_age(self):
        domain = "https://test.cloudflareaccess.com"
        old_time = time.monotonic() - (_CF_KEYS_MAX_STALE + 1)
        _CF_KEYS_CACHE[domain] = {"kid1": "old-key"}
        _CF_KEYS_FETCHED_AT[domain] = old_time
        with patch("urllib.request.urlopen", side_effect=Exception("network error")):
            result = _get_public_keys(domain)
        assert result == {}
        assert domain not in _CF_KEYS_CACHE

    def test_empty_keyset_not_cached_as_fresh(self):
        domain = "https://test2.cloudflareaccess.com"
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"keys": []}).encode()
        mock_resp.__enter__ = lambda self: mock_resp
        mock_resp.__exit__ = lambda *a: None
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _get_public_keys(domain)
        assert result == {}
        assert domain not in _CF_KEYS_CACHE
        assert domain not in _CF_KEYS_FETCHED_AT

    def test_malformed_jwks_response_rejected(self):
        domain = "https://test3.cloudflareaccess.com"
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = lambda self: mock_resp
        mock_resp.__exit__ = lambda *a: None
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _get_public_keys(domain)
        assert result == {}
        assert domain not in _CF_KEYS_CACHE
        assert domain not in _CF_KEYS_FETCHED_AT

    def test_duplicate_kid_rejected(self):
        domain = "https://test4.cloudflareaccess.com"
        jwks = {
            "keys": [
                {"kid": "k1", "kty": "RSA", "n": "n1", "e": "AQAB"},
                {"kid": "k1", "kty": "RSA", "n": "n2", "e": "AQAB"},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(jwks).encode()
        mock_resp.__enter__ = lambda self: mock_resp
        mock_resp.__exit__ = lambda *a: None
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch("jwt.algorithms.RSAAlgorithm.from_jwk", return_value="fake-key"):
                result = _get_public_keys(domain)
        assert result == {}
        assert domain not in _CF_KEYS_CACHE
        assert domain not in _CF_KEYS_FETCHED_AT

    def test_all_or_nothing_one_bad_key_rejects_batch(self):
        domain = "https://test5.cloudflareaccess.com"
        jwks = {
            "keys": [
                {"kid": "k1", "kty": "RSA", "n": "n1", "e": "AQAB"},
                {"kid": "k2", "kty": "RSA", "n": "n2", "e": "AQAB"},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(jwks).encode()
        mock_resp.__enter__ = lambda self: mock_resp
        mock_resp.__exit__ = lambda *a: None
        call_count = [0]
        def selective_jwk(key_json):
            call_count[0] += 1
            if call_count[0] == 2:
                raise ValueError("bad key")
            return "good-key"
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch("jwt.algorithms.RSAAlgorithm.from_jwk", side_effect=selective_jwk):
                result = _get_public_keys(domain)
        assert result == {}
        assert domain not in _CF_KEYS_CACHE
        assert domain not in _CF_KEYS_FETCHED_AT


# ── Waiter branch tests ──────────────────────────────────────────────────────

class TestWaiterBranch(_CacheDomainGuard):
    _synthetic_domains = [
        "https://waiter-fresh.cloudflareaccess.com",
        "https://waiter-cross.cloudflareaccess.com",
        "https://waiter-exact.cloudflareaccess.com",
    ]

    def setup_method(self):
        self._cf_snapshot(self._synthetic_domains)

    def teardown_method(self):
        self._cf_restore()

    def test_waiter_returns_keys_when_still_fresh_after_wait(self):
        domain = "https://waiter-fresh.cloudflareaccess.com"
        fake_now = [100000.0]
        seeded_ts = fake_now[0] - (_CF_KEYS_MAX_STALE - 10)
        _CF_KEYS_CACHE[domain] = {"kid1": "cached-key"}
        _CF_KEYS_FETCHED_AT[domain] = seeded_ts
        _CF_KEYS_GENERATION[domain] = 1
        event = threading.Event()
        _CF_KEYS_INFLIGHT[domain] = event
        wait_called = [False]
        def mock_monotonic():
            return fake_now[0]
        def mock_wait(timeout=None):
            wait_called[0] = True
            fake_now[0] += 1
            event.set()
            return True
        with patch("api.auth_cf_access.time.monotonic", side_effect=mock_monotonic):
            with patch.object(event, "wait", mock_wait):
                result = _get_public_keys(domain)
        assert wait_called[0]
        assert result == {"kid1": "cached-key"}
        assert _CF_KEYS_CACHE[domain] == {"kid1": "cached-key"}
        assert _CF_KEYS_FETCHED_AT[domain] == seeded_ts

    def test_waiter_rejects_after_deadline_crossing(self):
        domain = "https://waiter-cross.cloudflareaccess.com"
        fake_now = [100000.0]
        _CF_KEYS_CACHE[domain] = {"kid1": "cached-key"}
        _CF_KEYS_FETCHED_AT[domain] = fake_now[0] - (_CF_KEYS_MAX_STALE - 5)
        _CF_KEYS_GENERATION[domain] = 1
        event = threading.Event()
        _CF_KEYS_INFLIGHT[domain] = event
        wait_called = [False]
        def mock_monotonic():
            return fake_now[0]
        def mock_wait(timeout=None):
            wait_called[0] = True
            fake_now[0] += 200
            event.set()
            return True
        with patch("api.auth_cf_access.time.monotonic", side_effect=mock_monotonic):
            with patch.object(event, "wait", mock_wait):
                result = _get_public_keys(domain)
        assert wait_called[0]
        assert result == {}
        assert domain not in _CF_KEYS_CACHE
        assert domain not in _CF_KEYS_FETCHED_AT

    def test_waiter_rejects_at_exact_deadline_crossing(self):
        domain = "https://waiter-exact.cloudflareaccess.com"
        fake_now = [100000.0]
        _CF_KEYS_CACHE[domain] = {"kid1": "cached-key"}
        _CF_KEYS_FETCHED_AT[domain] = fake_now[0] - (_CF_KEYS_MAX_STALE - 5)
        _CF_KEYS_GENERATION[domain] = 1
        event = threading.Event()
        _CF_KEYS_INFLIGHT[domain] = event
        wait_called = [False]
        def mock_monotonic():
            return fake_now[0]
        def mock_wait(timeout=None):
            wait_called[0] = True
            fake_now[0] += 5
            event.set()
            return True
        with patch("api.auth_cf_access.time.monotonic", side_effect=mock_monotonic):
            with patch.object(event, "wait", mock_wait):
                result = _get_public_keys(domain)
        assert wait_called[0]
        assert result == {}
        assert domain not in _CF_KEYS_CACHE
        assert domain not in _CF_KEYS_FETCHED_AT


# ── _safe_cached_keys ────────────────────────────────────────────────────────

class TestSafeCachedKeys(_CacheDomainGuard):
    _synthetic_domains = [
        "https://stale-test.cloudflareaccess.com",
        "https://fresh-test.cloudflareaccess.com",
    ]

    def setup_method(self):
        self._cf_snapshot(self._synthetic_domains)

    def teardown_method(self):
        self._cf_restore()

    def test_stale_cache_expired(self):
        domain = "https://stale-test.cloudflareaccess.com"
        _CF_KEYS_CACHE[domain] = {"kid1": "key1"}
        _CF_KEYS_FETCHED_AT[domain] = time.monotonic() - (_CF_KEYS_MAX_STALE + 100)
        result = _safe_cached_keys(domain)
        assert result == {}
        assert domain not in _CF_KEYS_CACHE

    def test_fresh_cache_returned(self):
        domain = "https://fresh-test.cloudflareaccess.com"
        _CF_KEYS_CACHE[domain] = {"kid1": "key1"}
        _CF_KEYS_FETCHED_AT[domain] = time.monotonic()
        result = _safe_cached_keys(domain)
        assert result == {"kid1": "key1"}


# ── Cache isolation sentinel ─────────────────────────────────────────────────

class TestCacheIsolationSentinel:
    """Prove unrelated entries survive real fixture setup/teardown lifecycle.
    teardown_method is in finally so it runs even if body assertions fail.
    """

    @pytest.mark.parametrize("fixture_cls,owned_domain", [
        (TestJWKSCache, "https://test.cloudflareaccess.com"),
        (TestWaiterBranch, "https://waiter-fresh.cloudflareaccess.com"),
        (TestSafeCachedKeys, "https://stale-test.cloudflareaccess.com"),
    ])
    def test_absent_owned_target_leaves_no_residue(self, fixture_cls, owned_domain):
        """Owned absent before setup -> body write after setup -> teardown
        must leave no residue in any of the four maps.
        """
        sentinel_domain = "https://sentinel-isolation.cloudflareaccess.com"
        saved_sentinel = {}
        saved_owned = {}
        with _CF_LOCK:
            for name, m in _CF_MAPS:
                saved_sentinel[name] = (sentinel_domain in m, m.get(sentinel_domain))
                saved_owned[name] = (owned_domain in m, m.get(owned_domain))

        try:
            sentinel_cache = {"sentinel": "key"}
            sentinel_event = threading.Event()
            with _CF_LOCK:
                _CF_KEYS_CACHE[sentinel_domain] = sentinel_cache
                _CF_KEYS_FETCHED_AT[sentinel_domain] = 99999.0
                _CF_KEYS_INFLIGHT[sentinel_domain] = sentinel_event
                _CF_KEYS_GENERATION[sentinel_domain] = 999

            instance = fixture_cls()
            instance.setup_method()
            try:
                # Assert owned absent from all 4 maps after setup
                with _CF_LOCK:
                    assert owned_domain not in _CF_KEYS_CACHE
                    assert owned_domain not in _CF_KEYS_FETCHED_AT
                    assert owned_domain not in _CF_KEYS_INFLIGHT
                    assert owned_domain not in _CF_KEYS_GENERATION

                # Body-time owned-target write
                body_cache = {"body": "write"}
                body_event = threading.Event()
                with _CF_LOCK:
                    _CF_KEYS_CACHE[owned_domain] = body_cache
                    _CF_KEYS_FETCHED_AT[owned_domain] = 12345.0
                    _CF_KEYS_INFLIGHT[owned_domain] = body_event
                    _CF_KEYS_GENERATION[owned_domain] = 42

                # Replace sentinel with fresh objects
                fresh_cache = {"sentinel": "fresh"}
                fresh_event = threading.Event()
                with _CF_LOCK:
                    _CF_KEYS_CACHE[sentinel_domain] = fresh_cache
                    _CF_KEYS_FETCHED_AT[sentinel_domain] = 88888.0
                    _CF_KEYS_INFLIGHT[sentinel_domain] = fresh_event
                    _CF_KEYS_GENERATION[sentinel_domain] = 777
            finally:
                instance.teardown_method()

            # Post-teardown: no owned residue in any map
            with _CF_LOCK:
                assert owned_domain not in _CF_KEYS_CACHE
                assert owned_domain not in _CF_KEYS_FETCHED_AT
                assert owned_domain not in _CF_KEYS_INFLIGHT
                assert owned_domain not in _CF_KEYS_GENERATION
                # Sentinel fresh objects survive with identity
                assert _CF_KEYS_CACHE[sentinel_domain] is fresh_cache
                assert _CF_KEYS_INFLIGHT[sentinel_domain] is fresh_event
                assert _CF_KEYS_GENERATION[sentinel_domain] == 777
        finally:
            with _CF_LOCK:
                for name, m in _CF_MAPS:
                    present, value = saved_sentinel[name]
                    if present:
                        m[sentinel_domain] = value
                    else:
                        m.pop(sentinel_domain, None)
                    present, value = saved_owned[name]
                    if present:
                        m[owned_domain] = value
                    else:
                        m.pop(owned_domain, None)

    @pytest.mark.parametrize("fixture_cls,owned_domain", [
        (TestJWKSCache, "https://test.cloudflareaccess.com"),
        (TestWaiterBranch, "https://waiter-fresh.cloudflareaccess.com"),
        (TestSafeCachedKeys, "https://stale-test.cloudflareaccess.com"),
    ])
    def test_preseeded_owned_target_restored_after_lifecycle(self, fixture_cls, owned_domain):
        """Owned pre-seeded -> setup snapshots+removes -> body write ->
        teardown restores exact original object identity.
        """
        sentinel_domain = "https://sentinel-isolation.cloudflareaccess.com"
        saved_sentinel = {}
        saved_owned = {}
        with _CF_LOCK:
            for name, m in _CF_MAPS:
                saved_sentinel[name] = (sentinel_domain in m, m.get(sentinel_domain))
                saved_owned[name] = (owned_domain in m, m.get(owned_domain))

        try:
            sentinel_cache = {"sentinel": "key"}
            sentinel_event = threading.Event()
            with _CF_LOCK:
                _CF_KEYS_CACHE[sentinel_domain] = sentinel_cache
                _CF_KEYS_FETCHED_AT[sentinel_domain] = 99999.0
                _CF_KEYS_INFLIGHT[sentinel_domain] = sentinel_event
                _CF_KEYS_GENERATION[sentinel_domain] = 999

            orig_cache = {"owned": "original"}
            orig_ts = 55555.0
            orig_event = threading.Event()
            orig_gen = 444
            with _CF_LOCK:
                _CF_KEYS_CACHE[owned_domain] = orig_cache
                _CF_KEYS_FETCHED_AT[owned_domain] = orig_ts
                _CF_KEYS_INFLIGHT[owned_domain] = orig_event
                _CF_KEYS_GENERATION[owned_domain] = orig_gen

            instance = fixture_cls()
            instance.setup_method()
            try:
                # Assert owned absent from all 4 maps after setup
                with _CF_LOCK:
                    assert owned_domain not in _CF_KEYS_CACHE
                    assert owned_domain not in _CF_KEYS_FETCHED_AT
                    assert owned_domain not in _CF_KEYS_INFLIGHT
                    assert owned_domain not in _CF_KEYS_GENERATION

                # Body-time owned-target write
                body_cache = {"body": "write"}
                body_event = threading.Event()
                with _CF_LOCK:
                    _CF_KEYS_CACHE[owned_domain] = body_cache
                    _CF_KEYS_FETCHED_AT[owned_domain] = 12345.0
                    _CF_KEYS_INFLIGHT[owned_domain] = body_event
                    _CF_KEYS_GENERATION[owned_domain] = 42
            finally:
                instance.teardown_method()

            # Post-teardown: exact original object identity restored
            with _CF_LOCK:
                assert _CF_KEYS_CACHE[owned_domain] is orig_cache
                assert _CF_KEYS_FETCHED_AT[owned_domain] is orig_ts
                assert _CF_KEYS_INFLIGHT[owned_domain] is orig_event
                assert _CF_KEYS_GENERATION[owned_domain] == orig_gen
                # Sentinel survived
                assert _CF_KEYS_INFLIGHT[sentinel_domain] is sentinel_event
        finally:
            with _CF_LOCK:
                for name, m in _CF_MAPS:
                    present, value = saved_sentinel[name]
                    if present:
                        m[sentinel_domain] = value
                    else:
                        m.pop(sentinel_domain, None)
                    present, value = saved_owned[name]
                    if present:
                        m[owned_domain] = value
                    else:
                        m.pop(owned_domain, None)

# ── Identity extraction ──────────────────────────────────────────────────────

class TestIdentityExtraction:
    def test_no_identity_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", raising=False)
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_AUD", raising=False)
        handler = MagicMock()
        handler.headers = {}
        assert get_cf_access_identity(handler) is None

    def test_no_identity_without_token(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        handler = MagicMock()
        handler.headers = {}
        with patch("api.auth_cf_access._get_public_keys", return_value={}):
            assert get_cf_access_identity(handler) is None

    def test_reads_header_token(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        handler = MagicMock()
        handler.headers = {"Cf-Access-Jwt-Assertion": "fake-token"}
        with patch("api.auth_cf_access.validate_cf_access_token", return_value={"email": "user@example.com"}):
            result = get_cf_access_identity(handler)
        assert result is not None
        assert result["username"] == "user@example.com"

    def test_reads_cookie_token_fallback(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        handler = type("H", (), {"headers": {"Cookie": "CF_Authorization=cookie-token"}})()
        with patch("api.auth_cf_access.validate_cf_access_token", return_value={"sub": "user123"}):
            result = get_cf_access_identity(handler)
        assert result is not None
        assert result["username"] == "user123"

    def test_extracts_sub_as_username(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        handler = MagicMock()
        handler.headers = {"Cf-Access-Jwt-Assertion": "fake-token"}
        with patch("api.auth_cf_access.validate_cf_access_token", return_value={"sub": "user123"}):
            result = get_cf_access_identity(handler)
        assert result is not None
        assert result["username"] == "user123"

    def test_rejects_non_string_principal(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        handler = MagicMock()
        handler.headers = {"Cf-Access-Jwt-Assertion": "fake-token"}
        with patch("api.auth_cf_access.validate_cf_access_token", return_value={"email": ["list", "not", "string"]}):
            result = get_cf_access_identity(handler)
        assert result is None

    def test_rejects_oversized_principal(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        handler = MagicMock()
        handler.headers = {"Cf-Access-Jwt-Assertion": "fake-token"}
        long_email = "a" * 300 + "@x.com"
        with patch("api.auth_cf_access.validate_cf_access_token", return_value={"email": long_email}):
            result = get_cf_access_identity(handler)
        assert result is None

    def test_rejects_empty_string_principal(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        handler = MagicMock()
        handler.headers = {"Cf-Access-Jwt-Assertion": "fake-token"}
        with patch("api.auth_cf_access.validate_cf_access_token", return_value={"email": "", "sub": ""}):
            result = get_cf_access_identity(handler)
        assert result is None


# ── Production-composed check_auth ────────────────────────────────────────────

class _ConcreteHandler:
    def __init__(self):
        self.headers = {}
        self._response_code = None
        self._sent_headers = []
        self._sent_body = None
        self.request = None
        self.wfile = type("MockWfile", (), {"write": lambda s, d: None, "flush": lambda s: None})()
    def send_response(self, code, message=None):
        self._response_code = code
    def send_header(self, key, value):
        self._sent_headers.append((key, value))
    def end_headers(self):
        pass


@pytest.fixture
def session_state():
    from api.auth import _sessions, _SESSIONS_LOCK, _SESSIONS_FILE
    with _SESSIONS_LOCK:
        saved_registry = dict(_sessions)
    saved_exists = _SESSIONS_FILE.exists()
    saved_bytes = _SESSIONS_FILE.read_bytes() if saved_exists else None
    yield
    with _SESSIONS_LOCK:
        _sessions.clear()
        _sessions.update(saved_registry)
    if saved_exists:
        _SESSIONS_FILE.write_bytes(saved_bytes)
    elif _SESSIONS_FILE.exists():
        _SESSIONS_FILE.unlink()


class TestCheckAuthPartialConfigs:
    def _make_handler(self):
        return _ConcreteHandler()

    def _snapshot_sessions(self):
        from api.auth import _sessions, _SESSIONS_LOCK, _SESSIONS_FILE
        with _SESSIONS_LOCK:
            registry = dict(_sessions)
        file_bytes = _SESSIONS_FILE.read_bytes() if _SESSIONS_FILE.exists() else None
        return (registry, file_bytes)

    def _assert_no_cookie_mutation(self, handler):
        assert not hasattr(handler, "_pending_set_cookies")
        assert not any(h[0].lower() == "set-cookie" for h in handler._sent_headers)

    def test_import_fallback_is_auth_enabled(self, monkeypatch):
        import api as api_pkg
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        real_import = builtins.__import__
        import_blocked = [False]
        def block_cf_access(name, *args, **kwargs):
            if name == "api.auth_cf_access":
                import_blocked[0] = True
                raise ImportError("forced failure for test")
            return real_import(name, *args, **kwargs)
        sys_had = "api.auth" in sys.modules
        sys_saved = sys.modules.get("api.auth")
        pkg_had = "auth" in api_pkg.__dict__
        pkg_saved = api_pkg.__dict__.get("auth")
        monkeypatch.setattr(builtins, "__import__", block_cf_access)
        sys.modules.pop("api.auth", None)
        try:
            fresh = importlib.import_module("api.auth")
            assert import_blocked[0]
            assert fresh.is_auth_enabled()
            assert fresh.get_cf_access_identity(None) is None
        finally:
            monkeypatch.setattr(builtins, "__import__", real_import)
            if sys_had:
                sys.modules["api.auth"] = sys_saved
            else:
                sys.modules.pop("api.auth", None)
            if pkg_had:
                api_pkg.auth = pkg_saved
            else:
                api_pkg.__dict__.pop("auth", None)

    def test_check_auth_team_only_config_rejects(self, monkeypatch, session_state):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_AUD", raising=False)
        before = self._snapshot_sessions()
        handler = self._make_handler()
        handler.headers = {"Cf-Access-Jwt-Assertion": "some-token"}
        parsed = urllib.parse.urlparse("/api/test")
        from api.auth import check_auth
        with patch("api.auth.get_cf_access_identity", return_value=None) as spy:
            result = check_auth(handler, parsed)
        assert result is False
        assert spy.called
        assert handler._response_code == 401
        self._assert_no_cookie_mutation(handler)
        assert self._snapshot_sessions() == before

    def test_check_auth_aud_only_config_rejects(self, monkeypatch, session_state):
        monkeypatch.delenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", raising=False)
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        before = self._snapshot_sessions()
        handler = self._make_handler()
        handler.headers = {"Cf-Access-Jwt-Assertion": "some-token"}
        parsed = urllib.parse.urlparse("/api/test")
        from api.auth import check_auth
        with patch("api.auth.get_cf_access_identity", return_value=None) as spy:
            result = check_auth(handler, parsed)
        assert result is False
        assert spy.called
        assert handler._response_code == 401
        self._assert_no_cookie_mutation(handler)
        assert self._snapshot_sessions() == before

    def test_check_auth_invalid_domain_rejects(self, monkeypatch, session_state):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://evil.example.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        before = self._snapshot_sessions()
        handler = self._make_handler()
        handler.headers = {"Cf-Access-Jwt-Assertion": "some-token"}
        parsed = urllib.parse.urlparse("/api/test")
        from api.auth import check_auth
        with patch("api.auth.get_cf_access_identity", return_value=None) as spy:
            result = check_auth(handler, parsed)
        assert result is False
        assert spy.called
        assert handler._response_code == 401
        self._assert_no_cookie_mutation(handler)
        assert self._snapshot_sessions() == before

    def test_check_auth_missing_identity_rejects(self, monkeypatch, session_state):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        before = self._snapshot_sessions()
        handler = self._make_handler()
        handler.headers = {"Cf-Access-Jwt-Assertion": "fake-token"}
        parsed = urllib.parse.urlparse("/api/test")
        from api.auth import check_auth
        with patch("api.auth.get_cf_access_identity", return_value=None) as spy:
            result = check_auth(handler, parsed)
        assert result is False
        assert spy.called
        assert handler._response_code == 401
        self._assert_no_cookie_mutation(handler)
        assert self._snapshot_sessions() == before

    def test_check_auth_valid_config_no_token_rejects(self, monkeypatch, session_state):
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        before = self._snapshot_sessions()
        handler = self._make_handler()
        handler.headers = {}
        parsed = urllib.parse.urlparse("/api/test")
        from api.auth import check_auth
        with patch("api.auth.get_cf_access_identity", return_value=None) as spy:
            result = check_auth(handler, parsed)
        assert result is False
        assert spy.called
        assert handler._response_code == 401
        self._assert_no_cookie_mutation(handler)
        assert self._snapshot_sessions() == before

    def test_check_auth_valid_identity_creates_session(self, monkeypatch, session_state):
        """Positive: valid CF identity creates durable session.
        Freezes time/TTL, seeds complete pre-existing record in both
        authorities, asserts exact unfiltered sent headers, all cookie
        attributes including Secure, complete record, and both complete
        maps equal pre-state plus exactly one new row.
        """
        from api.auth import (
            check_auth, _resolve_cookie_name, _sessions, _SESSIONS_LOCK,
            _SESSIONS_FILE, _save_sessions, invalidate_session,
        )
        from api.helpers import flush_pending_auth_cookies
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        monkeypatch.setenv("HERMES_WEBUI_SECURE", "0")  # deterministic Secure absence

        frozen_time = 5000000.0
        fake_ttl = 2592000

        with patch("api.auth.time.time", return_value=frozen_time):
            with patch("api.auth._resolve_session_ttl", return_value=fake_ttl):
                # Seed complete pre-existing session record in both authorities
                pre_token = "pre-existing-" + "a" * 48
                pre_record = {
                    "expiry": frozen_time + 3600,
                    "auth_type": "password",
                    "username": "existing@example.com",
                    "bound_profile": None,
                }
                with _SESSIONS_LOCK:
                    _sessions[pre_token] = pre_record
                _save_sessions(dict(_sessions))
                expected_pre = dict(_sessions)

                handler = self._make_handler()
                handler.headers = {"Cf-Access-Jwt-Assertion": "valid-token"}
                parsed = urllib.parse.urlparse("/api/test")

                # handler.request = None -> _is_secure_context returns False -> no Secure
                with patch("api.auth.get_cf_access_identity", return_value={"username": "test@example.com"}):
                    with patch("api.routes._raw_peer_is_trusted_proxy", return_value=True):
                        result = check_auth(handler, parsed)
                assert result is True

                # Capture exact cookie before flush
                assert len(handler._pending_set_cookies) == 1
                captured = handler._pending_set_cookies[0]
                flush_pending_auth_cookies(handler)
                assert handler._pending_set_cookies == []

                # Exact unfiltered sent-header list
                assert handler._sent_headers == [("Set-Cookie", captured)]

                # Parse cookie and assert ALL attributes
                c = http.cookies.SimpleCookie()
                c.load(captured)
                cookie_name = _resolve_cookie_name()
                assert cookie_name in c
                morsel = c[cookie_name]
                assert morsel["httponly"]
                assert morsel["samesite"] == "Lax"
                assert morsel["path"] == "/"
                assert morsel["max-age"] == str(fake_ttl)
                # Secure: handler.request is None -> deterministic absence
                assert not morsel.get("secure")

                # Extract raw token
                raw_token = morsel.value.rsplit(".", 1)[0]

                # Complete expected new record
                expected_record = {
                    "expiry": frozen_time + fake_ttl,
                    "auth_type": "trusted",
                    "username": "test@example.com",
                    "bound_profile": None,
                }
                assert _sessions[raw_token] == expected_record

                # Complete in-memory map = pre-state plus one row
                expected_mem = dict(expected_pre)
                expected_mem[raw_token] = expected_record
                assert _sessions == expected_mem

                # Complete persisted map = pre-state plus one row
                persisted = json.loads(_SESSIONS_FILE.read_bytes())
                expected_persisted = dict(expected_pre)
                expected_persisted[raw_token] = expected_record
                assert persisted == expected_persisted

                # Cleanup
                invalidate_session(morsel.value)
                # Remove pre-existing row directly (it was seeded as raw token, not signed cookie)
                with _SESSIONS_LOCK:
                    _sessions.pop(pre_token, None)


# ── Privacy ──────────────────────────────────────────────────────────────────

class TestPrivacyLog:
    def test_no_email_in_allowlist_rejection_log(self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "https://myteam.cloudflareaccess.com")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_AUD", "aud")
        monkeypatch.setenv("HERMES_WEBUI_CF_ACCESS_EMAILS", "allowed@example.com")
        with caplog.at_level(logging.WARNING):
            with patch("api.auth_cf_access._get_public_keys", return_value={"kid1": "key1"}):
                with patch("jwt.get_unverified_header", return_value={"kid": "kid1"}):
                    with patch("jwt.decode", return_value={"iss": "https://myteam.cloudflareaccess.com", "aud": "aud", "email": "rejected@example.com"}):
                        result = validate_cf_access_token("fake-token")
        assert result is None
        for record in caplog.records:
            assert "rejected@example.com" not in record.getMessage()
