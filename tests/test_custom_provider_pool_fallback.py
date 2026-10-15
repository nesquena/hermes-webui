"""Regression test: bare-slug custom providers must find their ``custom:`` pool.

Named custom providers defined in ``config.yaml``'s ``custom_providers[]``
resolve to a bare slug (``bothub``) via ``_named_custom_provider_slug_for_provider``,
but their credential-pool entries are keyed with the ``custom:`` prefix
(``custom:bothub``). ``_pool_entry_payloads`` loaded the pool under the bare
slug, missed every entry, and reported "no credentials" for a fully
configured provider.

The fix routes both pool-load call sites through
``_load_pool_with_custom_fallback``, which retries under ``custom:<slug>``
only when the bare pool is empty AND the pid is a known *named* custom slug.
Built-in provider ids (``openai``, ...) never trigger the fallback, so a
bare built-in provider with no credentials of its own cannot borrow a named
custom provider's keys under the same name.

Tests are WebUI-only: the real ``agent.credential_pool`` is injected through
``sys.modules`` (mirroring ``test_credential_pool_providers``), so the suite
does not require the Hermes Agent package. The injected fake exposes only
``entries()`` — not ``has_credentials()`` — to prove the tolerant
``_pool_has_credentials`` helper keeps working with minimal fakes.
"""

import sys
import types

import pytest


def _install_fake_credential_pool(monkeypatch, pool_data):
    """Inject a minimal ``agent.credential_pool`` with a data-driven load_pool.

    ``pool_data`` maps provider id -> list of entry dicts.
    """
    class _FakePool:
        def __init__(self, entries):
            self._entries = entries

        def entries(self):
            return list(self._entries)

    # NOTE: deliberately NOT implementing has_credentials() — mirrors the
    # minimal fakes in test_credential_pool_providers, which must keep
    # working after the fix.

    def _fake_load_pool(pid):
        return _FakePool(pool_data.get(pid, []))

    fake_agent = types.ModuleType("agent")
    fake_cp = types.ModuleType("agent.credential_pool")
    fake_cp.load_pool = _fake_load_pool
    fake_agent.credential_pool = fake_cp
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_cp)


@pytest.fixture()
def cfg_custom(monkeypatch):
    """Seed a named custom provider ``bothub`` in the live config."""
    import api.config as config

    saved_cfg = dict(config.cfg)
    saved_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["custom_providers"] = [
        {
            "name": "bothub",
            "api_key": "sk-bh...test",
            "base_url": "https://bothub.chat/v1",
            "display_name": "Bothub",
        },
    ]
    try:
        yield config
    finally:
        config.cfg.clear()
        config.cfg.update(saved_cfg)
        config._cfg_mtime = saved_mtime


def test_bare_slug_falls_back_to_custom_prefixed_pool(monkeypatch, cfg_custom):
    """A named custom slug with no bare pool must borrow its custom:<slug> pool."""
    import api.config as config

    _install_fake_credential_pool(monkeypatch, {
        "bothub": [],                                  # bare slug: empty
        "custom:bothub": [{"label": "k1", "runtime_api_key": "sk-x"}],
    })
    config._CREDENTIAL_POOL_CACHE.clear()

    payloads = config._pool_entry_payloads("bothub")

    assert len(payloads) == 1
    assert payloads[0].get("label") == "k1"
    assert payloads[0].get("runtime_api_key") == "sk-x"


def test_prefixed_pid_is_not_double_looked_up(monkeypatch, cfg_custom):
    """An already-prefixed pid must not trigger the fallback again."""
    import api.config as config

    looked_up = []
    data = {
        "custom:bothub": [{"label": "direct"}],
    }

    class _TrackingPool:
        def __init__(self, entries):
            self._entries = entries

        def entries(self):
            return list(self._entries)

    def _fake(pid):
        looked_up.append(pid)
        return _TrackingPool(data.get(pid, []))

    fake_agent = types.ModuleType("agent")
    fake_cp = types.ModuleType("agent.credential_pool")
    fake_cp.load_pool = _fake
    fake_agent.credential_pool = fake_cp
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_cp)
    config._CREDENTIAL_POOL_CACHE.clear()

    config._pool_entry_payloads("custom:bothub")

    assert looked_up == ["custom:bothub"]


def test_populated_bare_pool_short_circuits(monkeypatch, cfg_custom):
    """When the bare-slug pool already has entries, no fallback load happens."""
    import api.config as config

    looked_up = []
    data = {"bothub": [{"label": "direct", "runtime_api_key": "sk-d"}]}

    def _fake(pid):
        looked_up.append(pid)
        return _MinimalPool(data.get(pid, []))

    fake_agent = types.ModuleType("agent")
    fake_cp = types.ModuleType("agent.credential_pool")
    fake_cp.load_pool = _fake
    fake_agent.credential_pool = fake_cp
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_cp)
    config._CREDENTIAL_POOL_CACHE.clear()

    payloads = config._pool_entry_payloads("bothub")

    assert len(payloads) == 1 and payloads[0].get("label") == "direct"
    assert looked_up == ["bothub"]


def test_builtin_provider_does_not_borrow_custom_pool(monkeypatch, cfg_custom):
    """P1 guard: a bare built-in provider id (``openai``) must NOT fall back
    to ``custom:openai`` — otherwise it could borrow a named custom
    provider's keys under the same name."""
    import api.config as config

    looked_up = []
    data = {
        "openai": [],                                   # built-in: empty
        "custom:openai": [{"label": "custom-openai-key"}],
    }

    def _fake(pid):
        looked_up.append(pid)
        return _MinimalPool(data.get(pid, []))

    fake_agent = types.ModuleType("agent")
    fake_cp = types.ModuleType("agent.credential_pool")
    fake_cp.load_pool = _fake
    fake_agent.credential_pool = fake_cp
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_cp)
    config._CREDENTIAL_POOL_CACHE.clear()

    payloads = config._pool_entry_payloads("openai")

    # openai is not a named custom slug → no fallback, empty result, and the
    # custom: pool is never consulted.
    assert payloads == []
    assert "custom:openai" not in looked_up


def test_fallback_also_runs_on_expired_cache_refresh(monkeypatch, cfg_custom):
    """P1-2 guard: when the cached pool has expired (> 24 h), the refresh
    path must apply the same custom: fallback rather than caching the empty
    bare result."""
    import time
    import api.config as config

    _install_fake_credential_pool(monkeypatch, {
        "bothub": [],
        "custom:bothub": [{"label": "refreshed"}],
    })
    # Seed an expired cache entry for the bare slug so the refresh branch runs.
    config._CREDENTIAL_POOL_CACHE[
        (config._credential_pool_profile_tag(), "bothub")
    ] = (time.time() - 200_000.0, _MinimalPool([]))

    payloads = config._pool_entry_payloads("bothub")

    assert len(payloads) == 1
    assert payloads[0].get("label") == "refreshed"


class _MinimalPool:
    """Fake pool exposing only entries() — the shape the fix must tolerate."""

    def __init__(self, entries):
        self._entries = entries

    def entries(self):
        return list(self._entries)
