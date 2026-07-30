"""Unit tests for PR #4360: Extend credential pool quota status to non-Codex providers.

Tests cover:
- _local_pool_snapshot(provider) function with various pool states
- Integration with get_provider_quota for non-allowlist providers
- Preservation of allowlist and openrouter paths
"""
import sys
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from api.providers import _local_pool_snapshot, get_provider_quota


def _is_ambient_gh_cli_entry_real(source, label, key_source):
    markers = frozenset({"gh_cli", "gh auth token"})
    env_sources = frozenset({"env:github_token", "env:gh_token"})
    source_lower = source.strip().lower()
    return (
        source_lower in markers
        or source_lower in env_sources
        or label.strip().lower() == "gh auth token"
        or key_source.strip().lower() == "gh auth token"
    )


@contextmanager
def _fake_credential_pool(**kwargs):
    """Context manager that ensures agent.credential_pool and api.config exist and patches load_pool."""
    had_agent = "agent" in sys.modules
    had_cp = "agent.credential_pool" in sys.modules
    had_api = "api" in sys.modules
    had_api_config = "api.config" in sys.modules
    old_agent = sys.modules.get("agent")

    if not had_agent:
        _agent = types.ModuleType("agent")
        _agent.__path__ = []
        sys.modules["agent"] = _agent
    if not had_cp:
        _cp = types.ModuleType("agent.credential_pool")
        sys.modules["agent.credential_pool"] = _cp
        sys.modules["agent"].credential_pool = _cp

    if not had_api:
        _api = types.ModuleType("api")
        _api.__path__ = []
        sys.modules["api"] = _api
    if not had_api_config:
        _api_config = types.ModuleType("api.config")
        _api_config._is_ambient_gh_cli_entry = _is_ambient_gh_cli_entry_real
        sys.modules["api.config"] = _api_config
        sys.modules["api"].config = _api_config
    elif not hasattr(sys.modules["api.config"], "_is_ambient_gh_cli_entry"):
        sys.modules["api.config"]._is_ambient_gh_cli_entry = _is_ambient_gh_cli_entry_real

    cp = sys.modules["agent.credential_pool"]
    if not hasattr(cp, "load_pool"):
        cp.load_pool = None

    with patch("agent.credential_pool.load_pool", **kwargs) as m:
        yield m

    if not had_cp:
        sys.modules.pop("agent.credential_pool", None)
        if had_agent and old_agent is not None and hasattr(old_agent, "credential_pool"):
            delattr(old_agent, "credential_pool")
    if not had_agent:
        sys.modules.pop("agent", None)
    if not had_api_config:
        sys.modules.pop("api.config", None)
    if not had_api:
        sys.modules.pop("api", None)


class TestLocalPoolSnapshot(unittest.TestCase):
    """Tests for _local_pool_snapshot(provider) function."""

    def test_local_pool_snapshot_returns_none_for_empty_pool(self):
        """_local_pool_snapshot returns None when load_pool returns None."""
        with _fake_credential_pool( return_value=None):
            result = _local_pool_snapshot("some-provider")
            assert result is None

    def test_local_pool_snapshot_returns_none_for_pool_with_no_entries(self):
        """_local_pool_snapshot returns None when pool has no entries."""
        pool = MagicMock()
        pool.entries.return_value = []
        with _fake_credential_pool( return_value=pool):
            result = _local_pool_snapshot("some-provider")
            assert result is None

    def test_local_pool_snapshot_with_available_entries(self):
        """_local_pool_snapshot returns correct snapshot with available entries."""
        entry1 = SimpleNamespace(
            label="Cred 1",
            source="test",
            last_status="available",
            last_status_at=None,
            last_error_code=None,
            last_error_reset_at=None,
        )
        entry2 = SimpleNamespace(
            label="Cred 2",
            source="test",
            last_status="available",
            last_status_at=None,
            last_error_code=None,
            last_error_reset_at=None,
        )
        pool = MagicMock()
        pool.entries.return_value = [entry1, entry2]

        with _fake_credential_pool( return_value=pool):
            result = _local_pool_snapshot("google")
            assert result is not None
            assert result.provider == "google"
            assert result.source == "local_pool"
            assert result.available is True
            assert result.pool["total_credentials"] == 2
            assert result.pool["available_credentials"] == 2
            assert result.pool["exhausted_credentials"] == 0
            assert len(result.pool["credentials"]) == 2
            assert result.pool["credentials"][0]["status"] == "available"
            assert "2/2 credentials available" in result.details

    def test_local_pool_snapshot_with_exhausted_entries(self):
        """_local_pool_snapshot returns correct snapshot with all exhausted entries."""
        exhausted_until = datetime.now(timezone.utc) + timedelta(hours=1)
        entry1 = SimpleNamespace(
            label="Exhausted Cred",
            source="test",
            last_status="exhausted",
            last_status_at=exhausted_until - timedelta(hours=1),
            last_error_code="401",
            last_error_reset_at=exhausted_until,
        )
        pool = MagicMock()
        pool.entries.return_value = [entry1]

        with _fake_credential_pool( return_value=pool):
            result = _local_pool_snapshot("openai")
            assert result is not None
            assert result.available is False
            assert result.pool["total_credentials"] == 1
            assert result.pool["available_credentials"] == 0
            assert result.pool["exhausted_credentials"] == 1
            assert result.pool["credentials"][0]["status"] == "exhausted"
            assert "All pool credentials are unavailable" in result.unavailable_reason
            assert "1 exhausted" in result.details

    def test_local_pool_snapshot_with_mixed_entries(self):
        """_local_pool_snapshot returns correct counts with mixed available/exhausted."""
        entry_avail = SimpleNamespace(
            label="Available",
            source="test",
            last_status="available",
            last_status_at=None,
            last_error_code=None,
            last_error_reset_at=None,
        )
        exhausted_until = datetime.now(timezone.utc) + timedelta(hours=1)
        entry_exhausted = SimpleNamespace(
            label="Exhausted",
            source="test",
            last_status="exhausted",
            last_status_at=exhausted_until - timedelta(hours=1),
            last_error_code="429",
            last_error_reset_at=exhausted_until,
        )
        pool = MagicMock()
        pool.entries.return_value = [entry_avail, entry_exhausted]

        with _fake_credential_pool( return_value=pool):
            result = _local_pool_snapshot("vertex")
            assert result is not None
            assert result.available is True
            assert result.pool["total_credentials"] == 2
            assert result.pool["available_credentials"] == 1
            assert result.pool["exhausted_credentials"] == 1
            assert "1/2 credentials available" in result.details
            assert "1 exhausted" in result.details

    def test_local_pool_snapshot_with_dead_entry(self):
        """_local_pool_snapshot treats dead credentials as unavailable, not available."""
        entry = SimpleNamespace(
            label="Dead Cred",
            source="test",
            last_status="dead",
            last_status_at=None,
            last_error_code="401",
            last_error_reset_at=None,
        )
        pool = MagicMock()
        pool.entries.return_value = [entry]

        with _fake_credential_pool( return_value=pool):
            result = _local_pool_snapshot("xai-oauth")
            assert result is not None
            assert result.available is False
            assert result.pool["available_credentials"] == 0
            assert result.pool["dead_credentials"] == 1
            assert result.pool["credentials"][0]["status"] == "dead"
            assert "All pool credentials are unavailable" in result.unavailable_reason

    def test_local_pool_snapshot_handles_load_pool_exception(self):
        """_local_pool_snapshot returns None when load_pool raises exception."""
        with _fake_credential_pool( side_effect=RuntimeError("Pool error")):
            result = _local_pool_snapshot("custom-provider")
            assert result is None

    def test_local_pool_snapshot_snapshot_shape(self):
        """_local_pool_snapshot returns properly shaped SimpleNamespace."""
        entry = SimpleNamespace(
            label="Test",
            source="test",
            last_status="available",
            last_status_at=None,
            last_error_code=None,
            last_error_reset_at=None,
        )
        pool = MagicMock()
        pool.entries.return_value = [entry]

        with _fake_credential_pool( return_value=pool):
            result = _local_pool_snapshot("test-provider")
            assert hasattr(result, "provider")
            assert hasattr(result, "source")
            assert hasattr(result, "title")
            assert hasattr(result, "plan")
            assert hasattr(result, "windows")
            assert hasattr(result, "details")
            assert hasattr(result, "available")
            assert hasattr(result, "unavailable_reason")
            assert hasattr(result, "fetched_at")
            assert hasattr(result, "pool")
            assert result.title == "Credential pool"
            assert result.plan is None
            assert result.windows == ()
            assert isinstance(result.fetched_at, datetime)


    def test_local_pool_snapshot_filters_ambient_gh_cli_entries(self):
        """_local_pool_snapshot skips ambient gh-cli entries so providers like
        copilot don't show a phantom "1 credential available" (#4247 class)."""
        entry = SimpleNamespace(
            label="gh auth token",
            source="gh_cli",
            key_source="gh auth token",
            last_status="available",
            last_status_at=None,
            last_error_code=None,
            last_error_reset_at=None,
        )
        pool = MagicMock()
        pool.entries.return_value = [entry]

        with _fake_credential_pool(return_value=pool):
            result = _local_pool_snapshot("copilot")
            assert result is None

    def test_local_pool_snapshot_keeps_non_ambient_alongside_ambient(self):
        """Non-ambient entries are kept even when ambient ones are present."""
        ambient = SimpleNamespace(
            label="gh auth token",
            source="gh_cli",
            key_source="gh auth token",
            last_status="available",
            last_status_at=None,
            last_error_code=None,
            last_error_reset_at=None,
        )
        real = SimpleNamespace(
            label="My API Key",
            source="user",
            key_source="",
            last_status="available",
            last_status_at=None,
            last_error_code=None,
            last_error_reset_at=None,
        )
        pool = MagicMock()
        pool.entries.return_value = [ambient, real]

        with _fake_credential_pool(return_value=pool):
            result = _local_pool_snapshot("copilot")
            assert result is not None
            assert result.pool["total_credentials"] == 1
            assert result.pool["available_credentials"] == 1
            assert len(result.pool["credentials"]) == 1


class TestGetProviderQuotaLocalPool(unittest.TestCase):
    """Tests for get_provider_quota integration with local pool snapshots."""

    def test_get_provider_quota_uses_local_pool_for_non_allowlist_provider(self):
        """get_provider_quota returns available status for non-allowlist provider with pool."""
        entry = SimpleNamespace(
            label="Test Cred",
            source="test",
            last_status="available",
            last_status_at=None,
            last_error_code=None,
            last_error_reset_at=None,
        )
        pool = MagicMock()
        pool.entries.return_value = [entry]

        with _fake_credential_pool( return_value=pool):
            with patch("api.providers._active_provider_id", return_value="google"):
                result = get_provider_quota("google")
                assert result["ok"] is True
                assert result["provider"] == "google"
                assert result["status"] == "available"
                assert result["supported"] is True
                assert "account_limits" in result
                assert result["account_limits"]["source"] == "local_pool"
                assert result["account_limits"]["available"] is True

    def test_get_provider_quota_unavailable_when_all_pool_exhausted(self):
        """get_provider_quota returns unavailable when all pool credentials exhausted."""
        exhausted_until = datetime.now(timezone.utc) + timedelta(hours=1)
        entry = SimpleNamespace(
            label="Exhausted",
            source="test",
            last_status="exhausted",
            last_status_at=exhausted_until - timedelta(hours=1),
            last_error_code="401",
            last_error_reset_at=exhausted_until,
        )
        pool = MagicMock()
        pool.entries.return_value = [entry]

        with _fake_credential_pool( return_value=pool):
            with patch("api.providers._active_provider_id", return_value="openai"):
                result = get_provider_quota("openai")
                assert result["ok"] is False
                assert result["provider"] == "openai"
                assert result["status"] == "unavailable"
                assert result["supported"] is True
                assert "all credentials are unavailable" in result["message"]
                assert result["account_limits"]["available"] is False

    def test_get_provider_quota_falls_through_to_unsupported_when_no_pool(self):
        """get_provider_quota returns unsupported when no pool exists for provider."""
        with _fake_credential_pool( return_value=None):
            with patch("api.providers._active_provider_id", return_value="some-other-provider"):
                result = get_provider_quota("some-other-provider")
                assert result["ok"] is False
                assert result["status"] == "unsupported"
                assert result["supported"] is False

    def test_get_provider_quota_falls_through_when_pool_has_no_entries(self):
        """get_provider_quota returns unsupported when pool exists but has no entries."""
        pool = MagicMock()
        pool.entries.return_value = []
        with _fake_credential_pool( return_value=pool):
            with patch("api.providers._active_provider_id", return_value="custom-provider"):
                result = get_provider_quota("custom-provider")
                assert result["ok"] is False
                assert result["status"] == "unsupported"

    def test_allowlist_providers_bypass_local_pool(self):
        """get_provider_quota uses probe path for allowlist providers, not local pool."""
        with patch("api.providers._provider_account_usage_status") as mock_probe:
            with _fake_credential_pool(return_value=None) as mock_load_pool:
                mock_probe.return_value = {
                    "ok": True,
                    "provider": "openai-codex",
                    "status": "available",
                }
                get_provider_quota("openai-codex")
                mock_probe.assert_called_once()
                mock_load_pool.assert_not_called()

    def test_allowlist_anthropic_uses_probe_path(self):
        """get_provider_quota uses probe path for anthropic, not local pool."""
        with patch("api.providers._provider_account_usage_status") as mock_probe:
            with _fake_credential_pool(return_value=None) as mock_load_pool:
                mock_probe.return_value = {
                    "ok": True,
                    "provider": "anthropic",
                    "status": "available",
                }
                get_provider_quota("anthropic")
                mock_probe.assert_called_once()
                mock_load_pool.assert_not_called()

    def test_openrouter_uses_api_key_path_not_local_pool(self):
        """get_provider_quota uses openrouter API-key path, not local pool."""
        with _fake_credential_pool(return_value=None) as mock_load_pool:
            with patch("api.providers._get_provider_api_key", return_value=None):
                result = get_provider_quota("openrouter")
                assert result["status"] == "no_key"
                mock_load_pool.assert_not_called()
