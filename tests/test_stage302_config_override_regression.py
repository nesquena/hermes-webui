"""Regression tests for stage-302 in-release fix — config.cfg test override.

PR #1728 introduced path/mtime-aware reload in `get_config()`. The
new `cache_stale = current_mtime != _cfg_mtime or _cfg_path != config_path`
check correctly bypasses reload when in-memory overrides exist, but the
existing `_cfg_has_in_memory_overrides()` helper only inspected
`_cfg_cache`, missing the common test idiom:

    monkeypatch.setattr(config, "cfg", {...test override...})

Because `cfg = _cfg_cache` is an alias bound at import time, the rebinding
only changes the module attribute — `_cfg_cache` itself stays untouched.
The fingerprint check returned False, the reload fired, and tests that
assert against a forced provider/default lost their override silently.
v0.51.7 stage-302 caught this on `test_issue1426_openrouter_*` and
`test_issue1680_codex_*` failing in the full suite while passing
standalone.

Fix:
  1. `_cfg_has_in_memory_overrides()` now ALSO returns True when
     `cfg is not _cfg_cache` (module attr rebound).
  2. `get_config()` now returns `cfg` (the override) rather than
     `_cfg_cache` when they're not the same object.

These tests pin both prongs.
"""
from __future__ import annotations

import os

import api.config as config


def test_get_config_respects_module_attr_rebind(monkeypatch, tmp_path):
    """monkeypatch.setattr(config, 'cfg', X) must survive get_config()."""
    config.reload_config()
    test_override = {
        "model": {"provider": "openrouter", "default": "test/model-x"},
        "providers": {"openrouter": {"api_key": "***"}},
    }
    monkeypatch.setattr(config, "cfg", test_override, raising=False)

    result = config.get_config()
    # The override must survive — get_config() must not silently fall
    # through to _cfg_cache.
    assert result is test_override, (
        f"get_config() returned _cfg_cache instead of the override; "
        f"override has provider={test_override['model']['provider']}, "
        f"result has provider={result.get('model', {}).get('provider')}"
    )
    assert result["model"]["provider"] == "openrouter"
    assert result["model"]["default"] == "test/model-x"


def test_cfg_has_in_memory_overrides_detects_attr_rebind(monkeypatch):
    """The helper must report True when cfg is rebound away from _cfg_cache."""
    config.reload_config()
    # No override yet — fingerprint matches, attr is the alias.
    assert config._cfg_has_in_memory_overrides() is False

    # Rebind cfg.
    monkeypatch.setattr(config, "cfg", {"model": {"provider": "openrouter"}}, raising=False)
    assert config._cfg_has_in_memory_overrides() is True


def test_cfg_has_in_memory_overrides_detects_in_place_mutation(monkeypatch):
    """The helper must still detect the original in-place mutation case."""
    config.reload_config()
    assert config._cfg_has_in_memory_overrides() is False

    # Mutate _cfg_cache directly (NOT a rebind).
    config._cfg_cache["__test_key"] = "test_value"
    try:
        assert config._cfg_has_in_memory_overrides() is True
    finally:
        config._cfg_cache.pop("__test_key", None)


def test_get_config_does_not_reload_when_only_in_memory_override_same_path_mtime_only(
    monkeypatch, tmp_path
):
    """same_path cfg overrides must survive mtime-only staleness."""
    config_path = tmp_path / "config.yaml"
    base_mtime = 1_700_000_000.0
    config_path.write_text(
        "model:\n  provider: openrouter\n  default: test/model-x\n",
        encoding="utf-8",
    )
    os.utime(config_path, (base_mtime, base_mtime))
    # reload_config() mutates module globals that monkeypatch does not restore
    # (_cfg_cache/_cfg_mtime/_cfg_path/_cfg_fingerprint/cfg); snapshot + restore
    # them so this test can't leak the temp config.yaml path into later tests
    # (order-dependent flake — matches the cleanup in test_profile_switch_1200.py).
    # NOTE: _cfg_cache is mutated IN PLACE by reload_config() (clear+repopulate the
    # same dict object), so it must be snapshotted BY VALUE (deepcopy) and restored
    # via clear()+update() — a bare reference would restore the already-mutated dict.
    import copy as _copy
    _saved_cache = _copy.deepcopy(getattr(config, "_cfg_cache", {}) or {})
    _saved = {
        "_cfg_mtime": getattr(config, "_cfg_mtime", None),
        "_cfg_path": getattr(config, "_cfg_path", None),
        "_cfg_fingerprint": getattr(config, "_cfg_fingerprint", None),
        "cfg": getattr(config, "cfg", None),
    }
    try:
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        config.reload_config()

        test_override = {
            "model": {"provider": "openai", "default": "gpt-test"},
            "providers": {},
        }
        monkeypatch.setattr(config, "cfg", test_override, raising=False)

        os.utime(config_path, (base_mtime + 1.0, base_mtime + 1.0))

        # The same_path mtime-only reload would normally trigger reload, but the
        # override-detection should suppress it.
        result = config.get_config()
        assert result is test_override
        assert result["model"]["provider"] == "openai"
    finally:
        # Restore _cfg_cache contents in place (it's mutated in place by reload).
        try:
            if isinstance(getattr(config, "_cfg_cache", None), dict):
                config._cfg_cache.clear()
                config._cfg_cache.update(_saved_cache)
        except Exception:
            pass
        for _name, _val in _saved.items():
            try:
                setattr(config, _name, _val)
            except Exception:
                pass
