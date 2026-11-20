"""Regression tests for #9143 (items 2 and 3), follow-up to #9105 / #9118.

2. An OAuth plugin account that cannot be used any more (expired with no refresh
   token, or no token at all) must not make ``get_providers()`` report
   ``has_key=True`` just because the credential pool has a row for it: the card
   is decided from the Agent's status (``logged_in`` / ``needs_refresh``).
3. A signed-in OAuth plugin card reports ``key_source="oauth"`` and
   ``is_oauth=True`` (the Agent's plugin status carries no ``key_source``, and
   ``_provider_is_oauth()`` only knows the built-in set).

The pool rows follow the Agent's ``PooledCredential`` fields and the status
dicts follow ``get_plugin_oauth_auth_status()`` (hermes-agent
``hermes_cli/auth_plugin_providers.py``): ``accounts`` counts the rows that
carry a token, ``logged_in`` needs an unexpired one, ``needs_refresh`` needs an
expired one with a refresh token and a ``refresh_credential`` hook.
``agent.credential_pool`` is stubbed because the bundled Agent is not
installed in CI.
"""

from __future__ import annotations

import sys
import time
import types
from types import SimpleNamespace

import pytest

import api.config as config
from tests.test_issue9105_plugin_oauth_has_key import (
    ISSUE_9105_LOGGED_OUT_STATUS,
    _providers_for,
)


def _ms_from_now(delta_ms: int) -> int:
    return int(time.time() * 1000) + delta_ms


def _row(**overrides):
    row = {
        "auth_type": "oauth",
        "source": "manual:device_code",
        "label": "solstice account",
        "key_source": "",
        "access_token": "access-token-1",
        "refresh_token": "",
        "agent_key": "",
        "expires_at": None,
        "expires_at_ms": None,
        "last_status": None,
    }
    row.update(overrides)
    return SimpleNamespace(**row)


# ``get_plugin_oauth_auth_status()`` for an account the Agent cannot use: a token
# row exists (``accounts: 1``) but it is expired and nothing can renew it (no
# refresh token, or a refresh token but a plugin without ``refresh_credential``).
EXPIRED_NO_REFRESH_STATUS = {
    **ISSUE_9105_LOGGED_OUT_STATUS,
    "logged_in": False,
    "needs_refresh": False,
    "accounts": 1,
}
REFRESHABLE_STATUS = {**EXPIRED_NO_REFRESH_STATUS, "needs_refresh": True}
SIGNED_IN_STATUS = {
    **ISSUE_9105_LOGGED_OUT_STATUS,
    "logged_in": True,
    "needs_refresh": False,
    "accounts": 1,
    "hint": "",
}


@pytest.fixture
def pool_rows(monkeypatch):
    """Install a stub ``agent.credential_pool`` whose pool holds ``rows``."""
    rows: list = []
    fake_cp = types.ModuleType("agent.credential_pool")
    fake_cp.load_pool = lambda pid: SimpleNamespace(entries=lambda: list(rows) if pid == "solstice" else [])
    fake_agent = types.ModuleType("agent")
    fake_agent.__path__ = []
    fake_agent.credential_pool = fake_cp
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_cp)
    config._CREDENTIAL_POOL_CACHE.clear()
    yield rows
    config._CREDENTIAL_POOL_CACHE.clear()


def _card(monkeypatch, tmp_path, *, status, auth_type="oauth_external"):
    return _providers_for(monkeypatch, tmp_path, auth_type=auth_type, status=status)


# --- item 2: a pool row alone does not make the account usable ---------------


def test_expired_row_without_refresh_token_is_not_connected(monkeypatch, tmp_path, pool_rows):
    pool_rows.append(_row(expires_at_ms=_ms_from_now(-3_600_000)))
    card = _card(monkeypatch, tmp_path, status=EXPIRED_NO_REFRESH_STATUS)
    assert card is not None
    assert card["has_key"] is False
    assert card["key_source"] == "none"


def test_expired_row_with_refresh_token_but_no_refresh_hook_is_not_connected(monkeypatch, tmp_path, pool_rows):
    """Without a ``refresh_credential`` hook the Agent reports ``needs_refresh: False``."""
    pool_rows.append(_row(expires_at_ms=_ms_from_now(-3_600_000), refresh_token="refresh-token-1"))
    card = _card(monkeypatch, tmp_path, status=EXPIRED_NO_REFRESH_STATUS)
    assert card is not None
    assert card["has_key"] is False


def test_row_without_any_token_is_not_connected(monkeypatch, tmp_path, pool_rows):
    """Rows without a token are not counted as accounts (``accounts: 0``)."""
    pool_rows.append(_row(access_token="", agent_key=""))
    card = _card(monkeypatch, tmp_path, status=ISSUE_9105_LOGGED_OUT_STATUS)
    assert card is not None
    assert card["has_key"] is False


def test_non_oauth_plugin_keeps_counting_any_pool_row(monkeypatch, tmp_path, pool_rows):
    """The status-only rule is OAuth-only: an API-key plugin keeps the old pool rule."""
    pool_rows.append(_row(access_token="", expires_at_ms=_ms_from_now(-3_600_000)))
    status = {"configured": True, "provider": "solstice", "logged_in": False}
    card = _card(monkeypatch, tmp_path, status=status, auth_type="api_key")
    assert card is not None
    assert card["has_key"] is True


def test_unusable_oauth_row_keeps_a_separately_configured_api_key(monkeypatch, tmp_path, pool_rows):
    (tmp_path / ".env").write_text("SOLSTICE_API_KEY=test-solstice-key-12345\n", encoding="utf-8")
    pool_rows.append(_row(expires_at_ms=_ms_from_now(-3_600_000)))
    card = _providers_for(
        monkeypatch,
        tmp_path,
        auth_type="oauth_external",
        status=EXPIRED_NO_REFRESH_STATUS,
        env_vars=("SOLSTICE_API_KEY",),
    )
    assert card is not None
    assert card["has_key"] is True
    assert card["key_source"] == "env_file"
    assert card["is_oauth"] is False


# --- item 3: signed-in OAuth plugin card reports OAuth ----------------------


@pytest.mark.parametrize("auth_type", ["oauth_external", "oauth_device_code"])
def test_signed_in_oauth_plugin_reports_oauth(monkeypatch, tmp_path, pool_rows, auth_type):
    pool_rows.append(_row(expires_at_ms=_ms_from_now(3_600_000)))
    card = _card(monkeypatch, tmp_path, status=SIGNED_IN_STATUS, auth_type=auth_type)
    assert card is not None
    assert card["has_key"] is True
    assert card["key_source"] == "oauth"
    assert card["is_oauth"] is True
    assert card["configurable"] is False


def test_signed_in_oauth_plugin_without_a_counted_oauth_row_keeps_the_current_card(monkeypatch, tmp_path, pool_rows):
    """WebUI's pool view is empty (stale cache), the Agent's status says signed in.

    The OAuth card needs a counted OAuth row; the status alone does not make one.
    """
    card = _card(monkeypatch, tmp_path, status=SIGNED_IN_STATUS)
    assert card is not None
    assert card["has_key"] is True
    assert card["key_source"] == "config_yaml"
    assert card["is_oauth"] is False


def test_expired_but_refreshable_oauth_plugin_reports_oauth(monkeypatch, tmp_path, pool_rows):
    # The Agent reports needs_refresh only for a plugin that ships ``refresh_credential``;
    # the stubbed profile has none, so state that the hook exists.
    monkeypatch.setattr("api.providers.plugin_model_provider_can_refresh", lambda pid: True)
    pool_rows.append(_row(expires_at_ms=_ms_from_now(-3_600_000), refresh_token="refresh-token-1"))
    card = _card(monkeypatch, tmp_path, status=REFRESHABLE_STATUS)
    assert card is not None
    assert card["has_key"] is True
    assert card["key_source"] == "oauth"
    assert card["is_oauth"] is True


def test_signed_in_non_oauth_plugin_card_is_unchanged(monkeypatch, tmp_path, pool_rows):
    status = {"configured": True, "provider": "solstice", "logged_in": True}
    card = _card(monkeypatch, tmp_path, status=status, auth_type="api_key")
    assert card is not None
    assert card["has_key"] is True
    assert card["key_source"] == "config_yaml"
    assert card["is_oauth"] is False
