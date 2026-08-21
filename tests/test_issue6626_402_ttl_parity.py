"""Tests for issue #6626: HTTP 402 pool-exhaustion TTL parity with the runtime.

WebUI's copies of ``_entry_exhausted_ttl_seconds()`` must stay tied to the
installed runtime contract (``agent.credential_pool._exhausted_ttl``): when the
runtime exposes the 2-minute 402 cooldown, WebUI uses it; on mixed-version
installs the one-hour fallback is preserved, so display/probe code never marks
an entry usable earlier than ``CredentialPool.select()`` will.

The WebUI test suite runs standalone (the Hermes Agent runtime is not
installed in CI), so parity tests simulate the runtime contract with a fake
``agent.credential_pool`` module instead of importing the real one.
"""

import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


def _entry(status="exhausted", error_code=None, status_at=None, reset_at=None):
    return SimpleNamespace(
        last_status=status,
        last_error_code=error_code,
        last_status_at=status_at,
        last_error_reset_at=reset_at,
    )


@pytest.fixture
def providers():
    import api.providers as p
    return p


@pytest.fixture
def fake_runtime(monkeypatch):
    """Install a fake ``agent.credential_pool`` module in ``sys.modules``.

    Mirrors the standalone CI environment where the real Hermes Agent runtime
    is not installed. Exposes ``_exhausted_ttl`` (default: 120s for 402, the
    contract when ``EXHAUSTED_TTL_402_SECONDS`` is present in the runtime).
    """

    def _install(ttl_402):
        agent_mod = types.ModuleType("agent")
        agent_mod.__path__ = []
        credential_pool_mod = types.ModuleType("agent.credential_pool")
        credential_pool_mod._exhausted_ttl = (
            lambda error_code: ttl_402 if error_code == 402 else 3600
        )
        monkeypatch.setitem(sys.modules, "agent", agent_mod)
        monkeypatch.setitem(sys.modules, "agent.credential_pool", credential_pool_mod)
        return credential_pool_mod

    return _install


def test_402_ttl_matches_installed_runtime(providers, fake_runtime):
    """Parity: WebUI eligibility must equal the installed runtime contract."""
    fake_runtime(ttl_402=120)

    from agent.credential_pool import _exhausted_ttl as runtime_ttl

    assert providers._entry_exhausted_ttl_seconds(402) == runtime_ttl(402)
    # Persisted error codes are strings ("402") — same contract.
    assert providers._entry_exhausted_ttl_seconds("402") == runtime_ttl(402)


def test_402_boundary_not_one_hour(providers, fake_runtime):
    """With a runtime that supports the 402 cooldown, WebUI is NOT 1h (and not 119s)."""
    fake_runtime(ttl_402=120)

    from agent.credential_pool import _exhausted_ttl as runtime_ttl

    ttl = providers._entry_exhausted_ttl_seconds("402")
    assert ttl == runtime_ttl(402)
    assert ttl == 120
    assert ttl != 119
    assert ttl != 3600


def test_host_helper_402_boundary_119_120(providers, fake_runtime):
    """Host helper: with runtime TTL=120, an exhausted 402 entry stays unavailable
    at 119s and becomes available at 120s (both int and persisted-string codes)."""
    fake_runtime(ttl_402=120)

    assert providers._entry_exhausted_ttl_seconds(402) == 120
    assert providers._entry_exhausted_ttl_seconds("402") == 120

    now = datetime.now(timezone.utc)
    entry_119 = _entry(error_code="402", status_at=(now - timedelta(seconds=119)).timestamp())
    entry_120 = _entry(error_code="402", status_at=(now - timedelta(seconds=120)).timestamp())
    # 119s after the failure the pool is still exhausted (unavailable)...
    assert providers._entry_is_pool_exhausted(entry_119) is True
    # ...and exactly at the 120s TTL boundary it becomes available again.
    assert providers._entry_is_pool_exhausted(entry_120) is False


def test_401_and_default_unchanged(providers):
    assert providers._entry_exhausted_ttl_seconds(401) == 5 * 60
    assert providers._entry_exhausted_ttl_seconds("401") == 5 * 60
    assert providers._entry_exhausted_ttl_seconds(None) == 60 * 60
    assert providers._entry_exhausted_ttl_seconds("") == 60 * 60
    assert providers._entry_exhausted_ttl_seconds("403") == 60 * 60


def test_legacy_runtime_fallback_preserved(providers, fake_runtime):
    """Older installed runtime (no 402 support) -> WebUI keeps the 1h fallback."""
    fake_runtime(ttl_402=3600)

    assert providers._entry_exhausted_ttl_seconds("402") == 60 * 60
    assert providers._entry_exhausted_ttl_seconds(402) == 60 * 60


def test_pool_exhausted_until_uses_402_ttl(providers, fake_runtime):
    fake_runtime(ttl_402=120)

    from agent.credential_pool import _exhausted_ttl as runtime_ttl

    status_at = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    entry = _entry(error_code="402", status_at=status_at.timestamp())
    exhausted_until = providers._entry_pool_exhausted_until(entry)
    assert exhausted_until is not None
    assert exhausted_until == status_at + timedelta(seconds=runtime_ttl(402))


def test_last_error_reset_at_precedence(providers):
    """A present last_error_reset_at wins over status_at + ttl."""
    status_at = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    reset_at = status_at + timedelta(minutes=30)
    entry = _entry(error_code="402", status_at=status_at.timestamp(), reset_at=reset_at.timestamp())
    assert providers._entry_pool_exhausted_until(entry) == reset_at


def test_embedded_subprocess_helper_402_boundary_119_120(monkeypatch):
    """The helper copy embedded in _ACCOUNT_USAGE_SUBPROCESS_CODE must behave
    identically: TTL 120 for int/string 402 and 119/120s eligibility boundary."""
    import api.providers as providers

    agent_mod = types.ModuleType("agent")
    agent_mod.__path__ = []
    account_usage_mod = types.ModuleType("agent.account_usage")
    credential_pool_mod = types.ModuleType("agent.credential_pool")

    def fake_fetch_account_usage(provider, *, base_url=None, api_key=None):
        return None

    class FakePool:
        def entries(self):
            return []

    credential_pool_mod._exhausted_ttl = lambda error_code: 120
    credential_pool_mod.load_pool = lambda provider: FakePool()
    account_usage_mod.fetch_account_usage = fake_fetch_account_usage
    monkeypatch.setitem(sys.modules, "agent", agent_mod)
    monkeypatch.setitem(sys.modules, "agent.account_usage", account_usage_mod)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", credential_pool_mod)
    monkeypatch.setattr(sys, "argv", ["quota-probe", "openai-codex", ""])

    namespace = {"__name__": "__main__"}
    exec(providers._ACCOUNT_USAGE_SUBPROCESS_CODE, namespace)

    embedded_ttl = namespace["_entry_exhausted_ttl_seconds"]
    embedded_is_pool_exhausted = namespace["_entry_is_pool_exhausted"]
    embedded_pool_exhausted_until = namespace["_entry_pool_exhausted_until"]

    assert embedded_ttl(402) == 120
    assert embedded_ttl("402") == 120

    now = datetime.now(timezone.utc)
    entry_119 = _entry(error_code="402", status_at=(now - timedelta(seconds=119)).timestamp())
    entry_120 = _entry(error_code="402", status_at=(now - timedelta(seconds=120)).timestamp())
    assert embedded_is_pool_exhausted(entry_119) is True
    assert embedded_is_pool_exhausted(entry_120) is False

    status_at = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    entry = _entry(error_code="402", status_at=status_at.timestamp())
    assert embedded_pool_exhausted_until(entry) == status_at + timedelta(seconds=120)
