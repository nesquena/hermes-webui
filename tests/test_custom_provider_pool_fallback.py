"""Regression test: bare-slug custom providers must find their ``custom:`` pool.

Custom providers defined in ``config.yaml``'s ``custom_providers[]`` resolve to
a bare slug (``bothub``) via ``_named_custom_provider_slug_for_provider``, but
their credential-pool entries are keyed with the ``custom:`` prefix
(``custom:bothub``). ``_pool_entry_payloads`` loaded the pool under the bare
slug, missed every entry, and a fully configured provider reported "no
credentials" on the models screen / provider status panels.
"""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def config_module(monkeypatch):
    """Import api.config with a disposable HERMES_HOME so no real state is read."""
    import importlib
    import sys

    monkeypatch.setenv("HERMES_HOME", "/tmp/webui-pool-fallback-home")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", "/tmp/webui-pool-fallback-state")
    monkeypatch.delenv("HERMES_WEBUI_PORT", raising=False)
    saved = {k: v for k, v in sys.modules.items() if k in {"api.config", "api"}}
    for k in list(saved):
        sys.modules.pop(k, None)
    mod = importlib.import_module("api.config")
    yield mod
    for k in list(sys.modules):
        if k in {"api.config", "api"}:
            sys.modules.pop(k, None)
    sys.modules.update(saved)


class _FakePool:
    def __init__(self, entries):
        self._entries = entries

    def has_credentials(self):
        return bool(self._entries)

    def entries(self):
        return list(self._entries)


def test_bare_slug_falls_back_to_custom_prefixed_pool(config_module, monkeypatch):
    from agent import credential_pool as cp

    loaded = []

    def fake_load_pool(provider):
        loaded.append(provider)
        if provider == "custom:bothub":
            return _FakePool([{"label": "k1"}])
        return _FakePool([])

    monkeypatch.setattr(cp, "load_pool", fake_load_pool)
    monkeypatch.setattr(
        config_module, "_credential_pool_profile_tag", lambda: ("",)
    )
    config_module._CREDENTIAL_POOL_CACHE.clear()

    payloads = config_module._pool_entry_payloads("bothub")

    assert len(payloads) == 1 and payloads[0].get("label") == "k1", (
        "bare-slug lookup must fall back to the custom:<slug> pool key"
    )
    assert "bothub" in loaded
    assert "custom:bothub" in loaded


def test_prefixed_pid_is_not_double_looked_up(config_module, monkeypatch):
    from agent import credential_pool as cp

    loaded = []

    def fake_load_pool(provider):
        loaded.append(provider)
        return _FakePool([])

    monkeypatch.setattr(cp, "load_pool", fake_load_pool)
    monkeypatch.setattr(
        config_module, "_credential_pool_profile_tag", lambda: ("",)
    )

    config_module._pool_entry_payloads("custom:bothub")

    assert loaded == ["custom:bothub"], (
        "an already-prefixed pid must not trigger the custom: fallback again"
    )


def test_populated_bare_pool_short_circuits(config_module, monkeypatch):
    """When the bare-slug pool already has entries, no fallback load happens."""
    from agent import credential_pool as cp

    loaded = []

    def fake_load_pool(provider):
        loaded.append(provider)
        return _FakePool([{"label": "direct"}])

    monkeypatch.setattr(cp, "load_pool", fake_load_pool)
    monkeypatch.setattr(
        config_module, "_credential_pool_profile_tag", lambda: ("",)
    )

    payloads = config_module._pool_entry_payloads("bothub")

    assert len(payloads) == 1 and payloads[0].get("label") == "direct"
    assert loaded == ["bothub"]
