"""Regression test: the credential-pool detection cache must be profile-scoped.

Guards the cross-profile leak Codex caught on #4247 — _CREDENTIAL_POOL_CACHE
was keyed by provider id alone, so a custom provider configured under profile A
made profile B (no pool entry) report has_key=True from a stale cache hit.
The cache key now includes the active profile's auth-store path.

Note: `_has_explicit_pool_credentials` does `from agent.credential_pool import
load_pool` at call time. The bundled `agent` package is importable locally but
NOT in CI's environment, so the test injects a stub `agent.credential_pool`
module into sys.modules rather than importing/monkeypatching the real one.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.config as config  # noqa: E402


class _FakeEntry:
    def __init__(self, source="config_yaml", label="custom:bothub", key_source="config_yaml"):
        self.source = source
        self.label = label
        self.key_source = key_source


class _FakePool:
    def __init__(self, entries):
        self._entries = entries

    def entries(self):
        return list(self._entries)


def test_has_explicit_pool_credentials_is_profile_scoped(monkeypatch):
    """A pool loaded under profile A must not satisfy a lookup under profile B."""
    config._CREDENTIAL_POOL_CACHE.clear()

    # Profile A's auth store has a custom:bothub pool entry; profile B has none.
    profile_pools = {
        "/tmp/profileA/auth.json": _FakePool([_FakeEntry()]),
        "/tmp/profileB/auth.json": _FakePool([]),
    }
    active = {"path": "/tmp/profileA/auth.json"}

    monkeypatch.setattr(config, "_get_auth_store_path", lambda: active["path"])
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda p: p)

    # Inject a stub `agent.credential_pool` so the in-function import resolves
    # here regardless of whether the real bundled agent is installed (it is not
    # on CI). Build a stub `agent` parent package too if needed.
    def _fake_load_pool(provider):
        return profile_pools[active["path"]]

    fake_cp = types.ModuleType("agent.credential_pool")
    fake_cp.load_pool = _fake_load_pool
    fake_agent = sys.modules.get("agent")
    created_agent = False
    if fake_agent is None:
        fake_agent = types.ModuleType("agent")
        fake_agent.__path__ = []  # mark as package
        monkeypatch.setitem(sys.modules, "agent", fake_agent)
        created_agent = True
    monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_cp)
    # If a real `agent` package exists, temporarily point its attribute too.
    if not created_agent:
        monkeypatch.setattr(fake_agent, "credential_pool", fake_cp, raising=False)

    # Under profile A: custom:bothub IS configured (real entry, not ambient gh_cli).
    active["path"] = "/tmp/profileA/auth.json"
    assert config._has_explicit_pool_credentials("custom:bothub") is True

    # Switch to profile B (no pool entry). It must NOT inherit A's cached pool.
    active["path"] = "/tmp/profileB/auth.json"
    assert config._has_explicit_pool_credentials("custom:bothub") is False

    # Back to A — still configured (its own cache key survives).
    active["path"] = "/tmp/profileA/auth.json"
    assert config._has_explicit_pool_credentials("custom:bothub") is True

    config._CREDENTIAL_POOL_CACHE.clear()
