"""Tests for issue #6626: HTTP 402 pool-exhaustion TTL parity with the runtime.

WebUI's copies of ``_entry_exhausted_ttl_seconds()`` must stay tied to the
installed runtime contract (``agent.credential_pool._exhausted_ttl``): when the
runtime exposes the 2-minute 402 cooldown, WebUI uses it; on mixed-version
installs the one-hour fallback is preserved, so display/probe code never marks
an entry usable earlier than ``CredentialPool.select()`` will.
"""

import inspect
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


def test_402_ttl_matches_installed_runtime(providers):
    """Parity: WebUI eligibility must equal the installed runtime contract."""
    from agent.credential_pool import _exhausted_ttl as runtime_ttl

    assert providers._entry_exhausted_ttl_seconds(402) == runtime_ttl(402)
    # Persisted error codes are strings ("402") — same contract.
    assert providers._entry_exhausted_ttl_seconds("402") == runtime_ttl(402)


def test_402_boundary_not_one_hour(providers):
    """With a runtime that supports the 402 cooldown, WebUI is NOT 1h (and not 119s)."""
    from agent.credential_pool import _exhausted_ttl as runtime_ttl

    if runtime_ttl(402) == 3600:
        pytest.skip("installed runtime predates the 402 cooldown")
    ttl = providers._entry_exhausted_ttl_seconds("402")
    assert ttl == runtime_ttl(402)
    assert ttl == 120
    assert ttl != 119
    assert ttl != 3600


def test_401_and_default_unchanged(providers):
    assert providers._entry_exhausted_ttl_seconds(401) == 5 * 60
    assert providers._entry_exhausted_ttl_seconds("401") == 5 * 60
    assert providers._entry_exhausted_ttl_seconds(None) == 60 * 60
    assert providers._entry_exhausted_ttl_seconds("") == 60 * 60
    assert providers._entry_exhausted_ttl_seconds("403") == 60 * 60


def test_legacy_runtime_fallback_preserved(providers, monkeypatch):
    """Older installed runtime (no 402 support) -> WebUI keeps the 1h fallback."""
    import agent.credential_pool as cp

    monkeypatch.setattr(cp, "_exhausted_ttl", lambda error_code: 3600)
    assert providers._entry_exhausted_ttl_seconds("402") == 60 * 60
    assert providers._entry_exhausted_ttl_seconds(402) == 60 * 60


def test_pool_exhausted_until_uses_402_ttl(providers):
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


def test_both_helper_copies_carry_runtime_parity(providers):
    """The duplicated helper (drift hazard) has both copies on the parity path."""
    src = inspect.getsource(providers)
    assert src.count("def _entry_exhausted_ttl_seconds(error_code):") == 2
    assert src.count("_runtime_exhausted_ttl(int(code))") == 2
